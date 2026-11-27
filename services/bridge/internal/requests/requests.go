// Package requests is the bridge's side of the request scaffold (ADR-014
// phase 5): an agent asks for what is not a channel message (a history read,
// file parts, the header line) on <req>.<kind>.<agent>, and the bridge answers
// on deliver.<agent>.req.<request id>. Nothing is kept: a lost message fails
// the request, and this phase's answer says only that the piece is not built.
// A piece plugs in as one entry of Handler.Handlers.
package requests

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

// kindOK says whether kind is one of the scaffold's: the history read, file
// parts and the header line (ADR-014 phase 5).
func kindOK(kind string) bool {
	return kind == "history" || kind == "file" || kind == "header"
}

// agentRE is an agent name as the bus writes it: the subject's last token.
var agentRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// requestRE is a request id: 16 characters of [a-z0-9].
var requestRE = regexp.MustCompile(`^[a-z0-9]{16}$`)

// The request envelope's own limits, checked before the body is decoded. A
// history read and a header line carry an anchor, never the messages
// themselves; only a file part carries bytes, and then one part of a file.
const (
	maxBody     = 64 << 10 // history and header
	maxFileBody = 1 << 20  // file
)

// The receive loop hands validated requests to a small pool, so a handler
// reading history cannot stall the subscription: a full queue drops the
// request with the throttled warning, never grows the bridge's memory.
const (
	queueLen    = 64
	workerCount = 4
)

// The per-agent rate: a request a second (Handler.Rate, defaultRate when
// unset), with a burst of twice that. An agent asking faster is dropped with
// the throttled warning; its request fails by timeout, never silently.
const (
	defaultRate = 10
	burstFactor = 2
)

// warnEvery is the least time between two log lines about one agent: a flood
// writes a line every 10 s, not one per message.
const warnEvery = 10 * time.Second

// clipRunes is the longest a logged value (subject, error, body excerpt) is
// written: a body from an agent or an error from a piece must not fill the
// log. The ellipsis comes on top.
const clipRunes = 80

// answerErrorRunes bounds the error text an answer carries; a piece's error
// message is its own, but one answer is no place for a megabyte.
const answerErrorRunes = 256

// Request is the body an agent publishes on <req>.<kind>.<agent>. Part and
// Last let a piece answer in parts (file parts, a long history read).
type Request struct {
	V       int             `json:"v"`
	Request string          `json:"request"`
	Kind    string          `json:"kind"`
	Part    int             `json:"part"`
	Last    bool            `json:"last"`
	Payload json.RawMessage `json:"payload"`
}

// Answer is the body the bridge publishes on deliver.<agent>.req.<request id>.
// Result carries a piece's JSON; Error says why there is none.
type Answer struct {
	V       int             `json:"v"`
	Request string          `json:"request"`
	Kind    string          `json:"kind"`
	Part    int             `json:"part"`
	Last    bool            `json:"last"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   string          `json:"error,omitempty"`
}

// notBuilt is this phase's answer: the scaffold answers, the pieces do not.
const notBuilt = "not built"

// Piece answers one validated request of one kind. It returns the answer's
// result as raw JSON, or the error that answer carries instead; the agent is
// the subject's last token, never a body field. It is the Handler.Handlers
// entry's type, named for the doc.
type Piece = func(ctx context.Context, agent string, req Request) (json.RawMessage, error)

// Handler answers requests on <Prefix>.>: it validates each message strictly,
// hands it to the piece for its kind, and publishes the answer. A kind
// without an entry in Handlers gets this phase's "not built" answer, so a
// piece is one entry and nothing else changes. Prefix is one subject token,
// the request subjects' prefix (CB_BRIDGE_REQ_PREFIX, default req). Rate is
// the requests a second one agent may make, and the burst twice that; zero
// uses defaultRate.
type Handler struct {
	Log      *slog.Logger
	Prefix   string
	Handlers map[string]Piece
	Rate     int
}

// bucket is one agent's token bucket: tokens at last, refilled at rate.
type bucket struct {
	tokens float64
	last   time.Time
}

// job is one validated request on its way to a worker.
type job struct {
	agent string
	req   Request
}

// runner is a Handler's state for one Run: the connection, the workers' queue
// and what the rate limit and the warnings have seen.
type runner struct {
	h      *Handler
	log    *slog.Logger
	nc     *nats.Conn
	rate   int
	jobs   chan job
	failed chan error

	mu     sync.Mutex
	tokens map[string]*bucket
	warned map[string]time.Time
}

// Run subscribes <Prefix>.> and answers until ctx ends. A message that does
// not validate is logged and left unanswered; the caller's request then fails.
// A refused subscription, or an answer the server refuses, is returned as an
// error: the scaffold's side of the bridge must not fail silently.
func (h *Handler) Run(ctx context.Context, nc *nats.Conn) error {
	if h.Prefix == "" {
		return errors.New("requests: no prefix")
	}
	log := h.Log
	if log == nil {
		log = slog.Default()
	}
	subject := h.Prefix + ".>"
	ch := make(chan *nats.Msg, queueLen)
	sub, err := nc.ChanSubscribe(subject, ch)
	if err != nil {
		return fmt.Errorf("requests: subscribe %s: %w", subject, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	// ChanSubscribe's refusal arrives asynchronously: the server's -ERR is
	// read before the flush's PONG, so a refused subscription is LastError by
	// now. A bridge without this check would run deaf.
	if err := nc.Flush(); err != nil {
		return fmt.Errorf("requests: subscribe %s: %w", subject, err)
	}
	if err := nc.LastError(); err != nil && errors.Is(err, nats.ErrPermissionViolation) {
		// A refusal we cannot attribute is taken as ours: the subscribe is
		// the one this connection just made.
		if _, refused, ok := violation(err); !ok || refused == subject {
			return fmt.Errorf("requests: subscribe %s: %w", subject, err)
		}
	}
	rate := h.Rate
	if rate <= 0 {
		rate = defaultRate
	}
	r := &runner{
		h: h, log: log, nc: nc, rate: rate,
		jobs:   make(chan job, queueLen),
		failed: make(chan error, 1),
		tokens: map[string]*bucket{},
		warned: map[string]time.Time{},
	}
	// The workers take the slow part off the receive loop; the run's end
	// stops them before the connection goes away.
	wctx, stop := context.WithCancel(ctx)
	var wg sync.WaitGroup
	for range workerCount {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r.work(wctx)
		}()
	}
	defer func() {
		stop()
		close(r.jobs)
		wg.Wait()
	}()
	for {
		select {
		case <-ctx.Done():
			return nil
		case err := <-r.failed:
			return err
		case m := <-ch:
			if m == nil { // a closed channel is no message
				continue
			}
			r.accept(m)
		}
	}
}

// accept validates one message and queues it. Only the validation and the
// queue run on the receive loop; a handler and its publish run on a worker.
func (r *runner) accept(m *nats.Msg) {
	agent, _, req, err := r.h.decode(m)
	if err != nil {
		if r.throttle("refused:" + lastToken(m.Subject)) {
			r.log.Warn("requests: refused", "subject", clip(m.Subject), "error", clip(err.Error()),
				"body", clip(string(m.Data)))
		}
		return
	}
	if !r.allow(agent, time.Now()) {
		if r.throttle("rate:" + agent) {
			r.log.Warn("requests: throttled, the agent asks too fast", "agent", clip(agent), "rate", r.rate)
		}
		return
	}
	select {
	case r.jobs <- job{agent: agent, req: req}:
	default:
		if r.throttle("queue:" + agent) {
			r.log.Warn("requests: dropped, the workers' queue is full", "agent", clip(agent))
		}
	}
}

// work answers queued requests until the queue is closed or the run ends.
func (r *runner) work(ctx context.Context) {
	for j := range r.jobs {
		if ctx.Err() != nil { // the run ended: a late answer is no answer
			return
		}
		r.answer(ctx, j)
	}
}

// answer runs the piece for one request and publishes its answer. A request
// that cannot be answered is logged and left unanswered: the agent's request
// then fails, never a wrong answer.
func (r *runner) answer(ctx context.Context, j job) {
	a := Answer{V: 1, Request: j.req.Request, Kind: j.req.Kind, Part: j.req.Part, Last: j.req.Last}
	switch piece := r.h.Handlers[j.req.Kind]; {
	case piece == nil:
		// The scaffold: one answer, so part 1 is the last.
		a.Part, a.Last = 1, true
		a.Error = notBuilt
	default:
		result, err := piece(ctx, j.agent, j.req)
		if err != nil {
			a.Error = clipN(err.Error(), answerErrorRunes)
		} else {
			a.Result = result
		}
	}
	body, err := json.Marshal(a)
	if err != nil {
		r.log.Warn("requests: answer", "agent", clip(j.agent), "error", clip(err.Error()))
		return
	}
	subject := "deliver." + j.agent + ".req." + j.req.Request
	if err := r.nc.Publish(subject, body); err != nil {
		if r.throttle("answer:" + j.agent) {
			r.log.Warn("requests: answer", "subject", clip(subject), "error", clip(err.Error()))
		}
		return
	}
	// The publish is buffered: only the flush's round trip says whether the
	// server took it, and an answer that cannot go out must not look answered.
	if err := r.nc.Flush(); err != nil {
		if r.throttle("answer:" + j.agent) {
			r.log.Warn("requests: answer", "subject", clip(subject), "error", clip(err.Error()))
		}
		return
	}
	err = r.nc.LastError()
	if err == nil {
		return
	}
	action, refused, ok := violation(err)
	if ok && action == "publish" && refused == subject {
		// Permanent: the bridge's rights cannot answer this agent. The loop
		// goes on, but /healthz must see it, so Run returns.
		r.log.Error("requests: the answer subject is refused", "subject", clip(subject), "error", clip(err.Error()))
		r.fail(fmt.Errorf("requests: answer on %s: %w", subject, err))
		return
	}
	// The connection's last async error is not necessarily this publish's:
	// the same subject was refused before, or another subscription was.
	r.log.Debug("requests: answer", "subject", clip(subject), "error", clip(err.Error()))
}

// fail reports a failure that must end Run and reach /healthz; only the first
// one matters, and a worker never blocks on it.
func (r *runner) fail(err error) {
	select {
	case r.failed <- err:
	default:
	}
}

// allow takes one token from the agent's bucket: burstFactor*rate requests at
// once, refilled at rate a second. A dropped request costs no token.
func (r *runner) allow(agent string, now time.Time) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	burst := float64(r.rate * burstFactor)
	b := r.tokens[agent]
	if b == nil {
		if len(r.tokens) >= 1024 { // an agent list, not a log: drop the idle ones
			r.pruneLocked(now)
		}
		b = &bucket{tokens: burst, last: now}
		r.tokens[agent] = b
	}
	b.tokens += now.Sub(b.last).Seconds() * float64(r.rate)
	if b.tokens > burst {
		b.tokens = burst
	}
	b.last = now
	if b.tokens < 1 {
		return false
	}
	b.tokens--
	return true
}

// pruneLocked drops the buckets and the warnings that have refilled or
// expired, so neither map grows with every agent the bridge ever saw.
func (r *runner) pruneLocked(now time.Time) {
	for agent, b := range r.tokens {
		if now.Sub(b.last) > time.Minute {
			delete(r.tokens, agent)
		}
	}
	for key, at := range r.warned {
		if now.Sub(at) > warnEvery {
			delete(r.warned, key)
		}
	}
}

// throttle reports whether a warning about key may be written now, and
// records it: one line per key per warnEvery. The caller builds the line only
// when it says yes, so a flood allocates nothing and the log keeps its bounds.
func (r *runner) throttle(key string) bool {
	now := time.Now()
	r.mu.Lock()
	defer r.mu.Unlock()
	if last, ok := r.warned[key]; ok && now.Sub(last) < warnEvery {
		return false
	}
	if len(r.warned) >= 1024 { // an agent list, not a log: drop the expired ones
		r.pruneLocked(now)
	}
	r.warned[key] = now
	return true
}

// decode checks the subject and the body: exactly the prefix, the kind and
// the agent; a body within the kind's limit, of v 1, whose kind is the
// subject's and whose request id has the one shape. The agent is the
// subject's last token, never a body field.
func (h *Handler) decode(m *nats.Msg) (agent, kind string, req Request, err error) {
	tokens := strings.Split(m.Subject, ".")
	if len(tokens) != 3 || tokens[0] != h.Prefix {
		return "", "", req, fmt.Errorf("subject is no %s.<kind>.<agent>", h.Prefix)
	}
	kind, agent = tokens[1], tokens[2]
	if !kindOK(kind) {
		return "", "", req, fmt.Errorf("kind %q is not history, file or header", kind)
	}
	limit := maxBody
	if kind == "file" {
		limit = maxFileBody
	}
	if len(m.Data) > limit {
		return "", "", req, fmt.Errorf("body is %d bytes, more than the %d a %s request may carry",
			len(m.Data), limit, kind)
	}
	if !agentRE.MatchString(agent) {
		return "", "", req, fmt.Errorf("agent %q is no agent name", agent)
	}
	if err := json.Unmarshal(m.Data, &req); err != nil {
		return "", "", req, fmt.Errorf("body: %w", err)
	}
	switch {
	case req.V != 1:
		return "", "", req, fmt.Errorf("body v %d is not 1", req.V)
	case !requestRE.MatchString(req.Request):
		return "", "", req, fmt.Errorf("request %q is no 16 characters of a-z0-9", req.Request)
	case req.Kind != kind:
		return "", "", req, fmt.Errorf("body kind %q is not the subject's %q", req.Kind, kind)
	}
	return agent, kind, req, nil
}

// violationRE is the server's -ERR for a refused subject. nats.go keeps the
// text in LastError; only a subscription's subject comes as a field.
var violationRE = regexp.MustCompile(`Permissions Violation for (Publish|Subscription) to "([^"]+)"`)

// violation splits a refused subject's error into the action ("publish" or
// "subscription", lower case) and the subject. ok is false for any other
// error.
func violation(err error) (action, subject string, ok bool) {
	if !errors.Is(err, nats.ErrPermissionViolation) {
		return "", "", false
	}
	m := violationRE.FindStringSubmatch(err.Error())
	if len(m) < 3 {
		return "", "", false
	}
	return strings.ToLower(m[1]), m[2], true
}

// lastToken is a subject's last token, the key a refusal is throttled by.
func lastToken(subject string) string {
	if i := strings.LastIndex(subject, "."); i >= 0 {
		return subject[i+1:]
	}
	return subject
}

// clip shortens a logged value to clipRunes runes plus an ellipsis.
func clip(s string) string { return clipN(s, clipRunes) }

// clipN shortens s to n runes plus an ellipsis.
func clipN(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n]) + "..."
}
