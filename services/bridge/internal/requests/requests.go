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
	"sync/atomic"
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

// subscribeRetry is how long Run waits before it tries a refused <Prefix>.>
// again: the rights are fixed by hand in the server's configuration, so a
// minute is soon enough, and one Error line a minute says requests stay
// unanswered meanwhile.
const subscribeRetry = time.Minute

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

// Reply is what a piece answers for one request: its result as raw JSON and
// the part it is. A piece sets Part and Last itself - a file download or a long
// history read sends parts 1..n and marks the last one - and the bridge never
// fills them from the request. Part below 1 counts as 1.
type Reply struct {
	Result json.RawMessage
	Part   int
	Last   bool
}

// Piece answers one validated request of one kind. It returns the answer's
// result and part, or the error that answer carries instead; the agent is the
// subject's last token, never a body field. It is the Handler.Handlers entry's
// type, named for the doc.
type Piece = func(ctx context.Context, agent string, req Request) (Reply, error)

// Handler answers requests on <Prefix>.>: it validates each message strictly,
// hands it to the piece for its kind, and publishes the answer. A kind
// without an entry in Handlers gets this phase's "not built" answer, so a
// piece is one entry and nothing else changes. Prefix is one subject token,
// the request subjects' prefix (CB_BRIDGE_REQ_PREFIX, default req). Rate is
// the requests a second one agent may make, and the burst twice that; zero
// uses defaultRate. Failure names why the handler is not answering while the
// server refuses <Prefix>.> or an answer subject: the bridge runs on either
// way, so a deployment before the rights leaves requests unanswered, never the
// bridge down.
type Handler struct {
	Log      *slog.Logger
	Prefix   string
	Handlers map[string]Piece
	Rate     int

	// failure is why the handler is not answering, nil while it is: a refused
	// subscribe and a refused answer subject set it, a (re)subscribe the
	// server takes clears it. A pointer, so Failure reads it while Run works.
	failure atomic.Pointer[string]

	// retry is the wait before a refused <Prefix>.> is tried again; zero uses
	// subscribeRetry. The tests shorten it.
	retry time.Duration
}

// Failure names why the handler is not answering - the server refused
// <Prefix>.> or an answer subject - and is false while it answers. The bridge
// reads it for /healthz and runs on either way.
func (h *Handler) Failure() (string, bool) {
	if p := h.failure.Load(); p != nil {
		return *p, true
	}
	return "", false
}

// setFailure records why the handler is not answering.
func (h *Handler) setFailure(err error) {
	s := err.Error()
	h.failure.Store(&s)
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
	h    *Handler
	log  *slog.Logger
	nc   *nats.Conn
	rate int
	jobs chan job

	mu     sync.Mutex
	tokens map[string]*bucket
	warned map[string]time.Time
}

// Run subscribes <Prefix>.> and answers until ctx ends. A message that does
// not validate is logged and left unanswered; the caller's request then fails.
// A refused subscription, or a refused answer subject, is no error: the bridge
// keeps carrying people's posts and requests stay unanswered until the rights
// are granted, with Failure naming what is missing (/healthz) and one Error
// line a minute while the subscription is refused. Only an empty Prefix is
// fatal.
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
	sub := h.subscribe(ctx, nc, subject, ch, log)
	if sub == nil { // ctx ended while the server refused the subscription
		return nil
	}
	defer func() { _ = sub.Unsubscribe() }()
	rate := h.Rate
	if rate <= 0 {
		rate = defaultRate
	}
	r := &runner{
		h: h, log: log, nc: nc, rate: rate,
		jobs:   make(chan job, queueLen),
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
		case m := <-ch:
			if m == nil { // a closed channel is no message
				continue
			}
			r.accept(m)
		}
	}
}

// subscribe makes the <Prefix>.> subscription, and while the server refuses it
// sets Failure, writes the Error line and waits subscribeRetry before it tries
// again: a missing right leaves requests unanswered, never the bridge down. It
// returns nil when ctx ends first.
func (h *Handler) subscribe(ctx context.Context, nc *nats.Conn, subject string,
	ch chan *nats.Msg, log *slog.Logger) *nats.Subscription {
	retry := h.retry
	if retry <= 0 {
		retry = subscribeRetry
	}
	for {
		sub, err := attempt(nc, subject, ch)
		if err == nil {
			h.failure.Store(nil) // the server took the (re)subscribe
			return sub
		}
		h.setFailure(err)
		log.Error("requests: the request subject is refused; the bridge keeps running, requests stay unanswered",
			"subject", clip(subject), "error", clip(err.Error()), "retry", retry.String())
		t := time.NewTimer(retry)
		select {
		case <-ctx.Done():
			t.Stop()
			return nil
		case <-t.C:
		}
	}
}

// attempt makes one <Prefix>.> subscription. ChanSubscribe's refusal arrives
// asynchronously: the server's -ERR is read before the flush's PONG, so a
// refused subscription is LastError by now. A bridge without this check would
// run deaf.
func attempt(nc *nats.Conn, subject string, ch chan *nats.Msg) (*nats.Subscription, error) {
	sub, err := nc.ChanSubscribe(subject, ch)
	if err != nil {
		return nil, fmt.Errorf("subscribe %s: %w", subject, err)
	}
	if err := nc.Flush(); err != nil {
		_ = sub.Unsubscribe()
		return nil, fmt.Errorf("subscribe %s: %w", subject, err)
	}
	if err := nc.LastError(); err != nil && errors.Is(err, nats.ErrPermissionViolation) {
		// A refusal we cannot attribute is taken as ours: the subscribe is
		// the one this connection just made.
		if _, refused, ok := violation(err); !ok || refused == subject {
			_ = sub.Unsubscribe()
			return nil, fmt.Errorf("subscribe %s: %w", subject, err)
		}
	}
	return sub, nil
}

// accept validates one message and queues it. Only the validation and the
// queue run on the receive loop; a handler and its publish run on a worker.
// The rate is checked on the subject alone, before the body is decoded, so a
// flooding agent costs no decode; the refusal it gets still names its request.
func (r *runner) accept(m *nats.Msg) {
	agent, kind, err := r.h.subject(m.Subject)
	if err != nil {
		if r.throttle("refused:" + lastToken(m.Subject)) {
			r.log.Warn("requests: refused", "subject", clip(m.Subject), "error", clip(err.Error()))
		}
		return
	}
	if !r.allow(agent, time.Now()) {
		if r.throttle("rate:" + agent) {
			r.log.Warn("requests: throttled, the agent asks too fast", "agent", clip(agent), "rate", r.rate)
		}
		r.busy(m, agent, kind)
		return
	}
	req, err := r.h.body(m, kind, agent)
	if err != nil {
		if r.throttle("refused:" + lastToken(m.Subject)) {
			r.log.Warn("requests: refused", "subject", clip(m.Subject), "error", clip(err.Error()),
				"body", clip(string(m.Data)))
		}
		return
	}
	select {
	case r.jobs <- job{agent: agent, req: req}:
	default:
		if r.throttle("queue:" + agent) {
			r.log.Warn("requests: busy, the workers' queue is full", "agent", clip(agent))
		}
		r.sendAnswer(agent, Answer{V: 1, Request: req.Request, Kind: kind, Part: 1, Last: true,
			Error: "busy: the bridge's queue is full, ask again"})
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
		reply, err := piece(ctx, j.agent, j.req)
		if err != nil {
			a.Error = clipN(err.Error(), answerErrorRunes)
		} else {
			a.Result = reply.Result
			a.Part, a.Last = reply.Part, reply.Last
			if a.Part < 1 {
				a.Part = 1
			}
		}
	}
	r.sendAnswer(j.agent, a)
}

// sendAnswer publishes one answer for agent and flush-checks it: the publish is
// buffered, so only the flush's round trip says whether the server took it, and
// an answer that cannot go out must not look answered. A permanent refusal of
// the answer subject sets Failure, so /healthz names a bridge that cannot
// answer, and the run goes on: the rest of the bridge must not go down with it.
// Any other async error is only debug, since the connection's last error may be
// another subject's.
func (r *runner) sendAnswer(agent string, a Answer) {
	body, err := json.Marshal(a)
	if err != nil {
		r.log.Warn("requests: answer", "agent", clip(agent), "error", clip(err.Error()))
		return
	}
	subject := "deliver." + agent + ".req." + a.Request
	if err := r.nc.Publish(subject, body); err != nil {
		if r.throttle("answer:" + subject) {
			r.log.Warn("requests: answer", "subject", clip(subject), "error", clip(err.Error()))
		}
		return
	}
	if err := r.nc.Flush(); err != nil {
		if r.throttle("answer:" + subject) {
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
		r.h.setFailure(fmt.Errorf("answer on %s: %w", subject, err))
		if r.throttle("answer:" + subject) {
			r.log.Error("requests: the answer subject is refused; the bridge keeps running, the agent gets no answer",
				"subject", clip(subject), "error", clip(err.Error()))
		}
		return
	}
	r.log.Debug("requests: answer", "subject", clip(subject), "error", clip(err.Error()))
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

// subject checks the subject alone, before any body is read: exactly the
// prefix, the kind and the agent. The agent is the subject's last token, never
// a body field.
func (h *Handler) subject(subject string) (agent, kind string, err error) {
	tokens := strings.Split(subject, ".")
	if len(tokens) != 3 || tokens[0] != h.Prefix {
		return "", "", fmt.Errorf("subject is no %s.<kind>.<agent>", h.Prefix)
	}
	kind, agent = tokens[1], tokens[2]
	if !kindOK(kind) {
		return "", "", fmt.Errorf("kind %q is not history, file or header", kind)
	}
	if !agentRE.MatchString(agent) {
		return "", "", fmt.Errorf("agent %q is no agent name", agent)
	}
	return agent, kind, nil
}

// body checks the rest of one message, once the rate let it through: a body
// within the kind's limit, of v 1, whose kind is the subject's and whose
// request id has the one shape.
func (h *Handler) body(m *nats.Msg, kind, agent string) (Request, error) {
	var req Request
	limit := maxBody
	if kind == "file" {
		limit = maxFileBody
	}
	if len(m.Data) > limit {
		return req, fmt.Errorf("body is %d bytes, more than the %d a %s request may carry",
			len(m.Data), limit, kind)
	}
	if err := json.Unmarshal(m.Data, &req); err != nil {
		return req, fmt.Errorf("body: %w", err)
	}
	switch {
	case req.V != 1:
		return req, fmt.Errorf("body v %d is not 1", req.V)
	case !requestRE.MatchString(req.Request):
		return req, fmt.Errorf("request %q is no 16 characters of a-z0-9", req.Request)
	case req.Kind != kind:
		return req, fmt.Errorf("body kind %q is not the subject's %q", req.Kind, kind)
	}
	return req, nil
}

// busy answers a request the rate refused. The request id is in the body, so
// the envelope is decoded for it - but only up to maxBody, so a flood pays the
// rate check and at most a small decode; a larger body is only logged.
func (r *runner) busy(m *nats.Msg, agent, kind string) {
	if len(m.Data) > maxBody {
		return
	}
	var envelope struct {
		Request string `json:"request"`
	}
	if err := json.Unmarshal(m.Data, &envelope); err != nil || !requestRE.MatchString(envelope.Request) {
		return
	}
	r.sendAnswer(agent, Answer{V: 1, Request: envelope.Request, Kind: kind, Part: 1, Last: true,
		Error: "busy: too many requests, ask again"})
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
