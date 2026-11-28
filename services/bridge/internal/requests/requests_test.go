package requests

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// goodID is a request id of the one shape: 16 characters of [a-z0-9].
const goodID = "abcdef0123456789"

// quiet is the handler's log in the tests, which assert answers, not lines.
var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

// answer is the body on deliver.<agent>.req.<request id>.
type answer struct {
	V       int             `json:"v"`
	Request string          `json:"request"`
	Kind    string          `json:"kind"`
	Part    int             `json:"part"`
	Last    bool            `json:"last"`
	Result  json.RawMessage `json:"result"`
	Error   string          `json:"error"`
}

// start is an in-process nats-server and one client. Its payload limit leaves
// room for a file request over the handler's own limit, which must be refused
// by the handler, not by the server.
func start(t *testing.T) (*natsserver.Server, *nats.Conn) {
	t.Helper()
	srv, err := natsserver.NewServer(&natsserver.Options{Host: "127.0.0.1", Port: -1,
		MaxPayload: 2 << 20, NoLog: true, NoSigs: true})
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	return srv, nc
}

// handler starts a Handler with prefix and waits until its subscription is on
// the server, so a request published after it is not lost.
func handler(t *testing.T, srv *natsserver.Server, nc *nats.Conn, prefix string) {
	t.Helper()
	startHandler(t, srv, nc, &Handler{Log: quiet, Prefix: prefix})
}

// startHandler runs one Handler and waits for its subscription; the test ends
// it, and Run must then return nil.
func startHandler(t *testing.T, srv *natsserver.Server, nc *nats.Conn, h *Handler) {
	t.Helper()
	before := srv.NumSubscriptions()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.Run(ctx, nc) }()
	t.Cleanup(func() {
		cancel()
		if err := <-done; err != nil {
			t.Errorf("Run: %v", err)
		}
	})
	deadline := time.Now().Add(2 * time.Second)
	for srv.NumSubscriptions() <= before {
		if time.Now().After(deadline) {
			t.Fatal("the handler did not subscribe")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// watch collects the answers on deliver.<agent>.req.<request id>.
func watch(t *testing.T, nc *nats.Conn) <-chan *nats.Msg {
	t.Helper()
	ch := make(chan *nats.Msg, 64)
	sub, err := nc.ChanSubscribe("deliver.*.req.>", ch)
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	return ch
}

func publish(t *testing.T, nc *nats.Conn, subject, body string) {
	t.Helper()
	if err := nc.Publish(subject, []byte(body)); err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
}

// body is a request body of the one shape.
func body(kind, id string) string {
	return fmt.Sprintf(`{"v":1,"request":%q,"kind":%q,"payload":{"anchor":"x"}}`, id, kind)
}

func want(t *testing.T, ch <-chan *nats.Msg, wait time.Duration) *nats.Msg {
	t.Helper()
	select {
	case m := <-ch:
		return m
	case <-time.After(wait):
		t.Fatalf("no answer within %s", wait)
		return nil
	}
}

func none(t *testing.T, ch <-chan *nats.Msg, wait time.Duration) {
	t.Helper()
	select {
	case m := <-ch:
		t.Fatalf("an answer was published on %s: %s", m.Subject, m.Data)
	case <-time.After(wait):
	}
}

// ADR-014 phase 5: a valid request is answered on deliver.<agent>.req.<id>
// with this phase's "not built", the same request id, and the one part.
func TestTheValidRequestGetsTheNotBuiltAnswer(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	handler(t, srv, nc, "req")

	publish(t, nc, "req.history.alice", body("history", goodID))
	m := want(t, ch, time.Second)
	if want := "deliver.alice.req." + goodID; m.Subject != want {
		t.Fatalf("answer on %s, want %s", m.Subject, want)
	}
	if got, want := string(m.Data),
		`{"v":1,"request":"`+goodID+`","kind":"history","part":1,"last":true,"error":"not built"}`; got != want {
		t.Fatalf("answer %s, want %s", got, want)
	}
	var a answer
	if err := json.Unmarshal(m.Data, &a); err != nil {
		t.Fatal(err)
	}
	if a.V != 1 || a.Request != goodID || a.Kind != "history" || a.Part != 1 || !a.Last || a.Error != "not built" {
		t.Fatalf("answer %+v", a)
	}
	if len(a.Result) != 0 {
		t.Fatalf("the scaffold answered with a result: %s", a.Result)
	}
}

// A piece's raw JSON is the answer's result, and the piece's own part and last
// are what the answer carries - never the request's.
func TestAPiecesResultIsTheAnswer(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	startHandler(t, srv, nc, &Handler{Log: quiet, Prefix: "req",
		Handlers: map[string]Piece{"history": func(_ context.Context, agent string, req Request) ([]Reply, error) {
			if agent != "alice" || req.Request != goodID || !strings.Contains(string(req.Payload), "anchor") {
				t.Errorf("the piece got %q, %+v", agent, req)
			}
			return []Reply{{Result: json.RawMessage(`{"messages":[{"id":"m1"}]}`)}}, nil
		}}})
	// not built is the scaffold's answer only
	publish(t, nc, "req.file.alice",
		fmt.Sprintf(`{"v":1,"request":%q,"kind":"file","part":2,"last":true,"payload":{"anchor":"x"}}`, goodID))
	m := want(t, ch, time.Second)
	var a answer
	if err := json.Unmarshal(m.Data, &a); err != nil {
		t.Fatal(err)
	}
	if a.Error != "not built" || a.Part != 1 || !a.Last {
		t.Fatalf("a kind without a piece: %+v", a)
	}

	// one part is part 1 and the last, whatever the request said
	publish(t, nc, "req.history.alice",
		fmt.Sprintf(`{"v":1,"request":%q,"kind":"history","part":2,"last":false,"payload":{"anchor":"x"}}`, goodID))
	m = want(t, ch, time.Second)
	a = answer{}
	if err := json.Unmarshal(m.Data, &a); err != nil {
		t.Fatal(err)
	}
	if a.Error != "" || string(a.Result) != `{"messages":[{"id":"m1"}]}` || a.Part != 1 || !a.Last {
		t.Fatalf("the piece's answer: %s", m.Data)
	}
}

// A piece answers in parts; the bridge numbers them 1..n and marks the last,
// so a caller that reads until `last` has the whole answer.
func TestTheBridgeNumbersAPiecesParts(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	startHandler(t, srv, nc, &Handler{Log: quiet, Prefix: "req",
		Handlers: map[string]Piece{"file": func(context.Context, string, Request) ([]Reply, error) {
			return []Reply{{Result: json.RawMessage(`{"part":1}`)},
				{Result: json.RawMessage(`{"part":2}`)}}, nil
		}}})
	publish(t, nc, "req.file.alice", body("file", goodID))
	for part := 1; part <= 2; part++ {
		m := want(t, ch, time.Second)
		var a answer
		if err := json.Unmarshal(m.Data, &a); err != nil {
			t.Fatal(err)
		}
		if a.Part != part || a.Last != (part == 2) ||
			string(a.Result) != fmt.Sprintf(`{"part":%d}`, part) {
			t.Fatalf("part %d: %s", part, m.Data)
		}
	}
	none(t, ch, 100*time.Millisecond)
}

// A piece's error is the answer's error, and there is no result beside it.
func TestAPiecesErrorIsTheAnswer(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	startHandler(t, srv, nc, &Handler{Log: quiet, Prefix: "req",
		Handlers: map[string]Piece{"history": func(context.Context, string, Request) ([]Reply, error) {
			return []Reply{{Result: json.RawMessage(`{"ignored":true}`)}}, errors.New("no anchor in the request")
		}}})

	publish(t, nc, "req.history.alice", body("history", goodID))
	m := want(t, ch, time.Second)
	var a answer
	if err := json.Unmarshal(m.Data, &a); err != nil {
		t.Fatal(err)
	}
	if a.Error != "no anchor in the request" || len(a.Result) != 0 || a.Part != 1 || !a.Last {
		t.Fatalf("the piece's error: %s", m.Data)
	}
}

// A body over the kind's limit is refused before it is decoded; a file part
// may carry more than a history read, but its own limit holds too.
func TestAnOversizedBodyIsRefused(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	handler(t, srv, nc, "req")

	big := func(kind string, size int) string {
		return fmt.Sprintf(`{"v":1,"request":%q,"kind":%q,"payload":%q}`,
			goodID, kind, strings.Repeat("x", size))
	}
	publish(t, nc, "req.history.alice", big("history", maxBody+1))
	publish(t, nc, "req.header.alice", big("header", maxBody+1))
	publish(t, nc, "req.file.alice", big("file", maxFileBody+1))
	none(t, ch, 500*time.Millisecond)

	// a body under the limit is no refusal: it is decoded and answered
	publish(t, nc, "req.history.alice", big("history", maxBody-200))
	m := want(t, ch, time.Second)
	if want := "deliver.alice.req." + goodID; m.Subject != want {
		t.Fatalf("answer on %s, want %s", m.Subject, want)
	}
}

// The per-agent rate is a token bucket: 20 requests of a burst are answered,
// the 21st gets a busy answer rather than silence, and the warning is throttled.
func TestTheTwentyFirstOfABurstIsThrottled(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	handler(t, srv, nc, "req")

	const n = 21
	for i := 1; i <= n; i++ {
		if err := nc.Publish("req.history.alice", []byte(body("history", fmt.Sprintf("%016d", i)))); err != nil {
			t.Fatal(err)
		}
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	busy := 0
	for range n {
		m := want(t, ch, 2*time.Second)
		var a answer
		if err := json.Unmarshal(m.Data, &a); err != nil {
			t.Fatal(err)
		}
		if strings.HasPrefix(a.Error, "busy") {
			busy++
			if a.Part != 1 || !a.Last {
				t.Fatalf("a busy answer: %s", m.Data)
			}
		}
	}
	if busy != 1 {
		t.Fatalf("%d busy answers of %d requests", busy, n)
	}
}

// A full workers' queue answers busy too: a piece that blocks nothing of the
// subscription must not drop an agent's request into silence.
func TestAFullQueueAnswersBusy(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	release := make(chan struct{})
	var once sync.Once
	free := func() { once.Do(func() { close(release) }) }
	startHandler(t, srv, nc, &Handler{Log: quiet, Prefix: "req", Rate: 1000, // the queue, not the rate
		Handlers: map[string]Piece{"history": func(context.Context, string, Request) ([]Reply, error) {
			<-release
			return []Reply{{Result: json.RawMessage(`{}`)}}, nil
		}}})
	t.Cleanup(free) // before the handler's own cleanup: the workers must be able to return

	total := workerCount + queueLen + 1
	for i := 1; i <= total; i++ {
		// One at a time: the subscription's own channel drops what it cannot
		// hold, and the queue under test is the workers' one.
		if err := nc.Publish("req.history.alice", []byte(body("history", fmt.Sprintf("%016d", i)))); err != nil {
			t.Fatal(err)
		}
		if err := nc.Flush(); err != nil {
			t.Fatal(err)
		}
		time.Sleep(5 * time.Millisecond)
	}
	// The workers are stuck in the piece and the queue is full: the last
	// request is answered busy without ever reaching a worker.
	busy := false
	for !busy {
		m := want(t, ch, 3*time.Second)
		var a answer
		if err := json.Unmarshal(m.Data, &a); err != nil {
			t.Fatal(err)
		}
		if strings.HasPrefix(a.Error, "busy") {
			busy = true
		}
	}
	free()
}

// The body's kind must be the subject's: a body that names another kind is no
// request in this subject and gets no answer.
func TestABodyKindThatDiffersIsRefused(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	handler(t, srv, nc, "req")

	publish(t, nc, "req.history.alice", body("file", goodID))
	none(t, ch, 300*time.Millisecond)
}

// A request id is 16 characters of [a-z0-9], nothing else.
func TestABadRequestIDIsRefused(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	handler(t, srv, nc, "req")

	for _, id := range []string{
		"abcdef012345678",  // 15
		"abcdef0123456-89", // 16, with a '-'
		"ABCDEF0123456789", // 16, upper case
	} {
		publish(t, nc, "req.history.alice", body("history", id))
	}
	none(t, ch, 300*time.Millisecond)
}

// The agents are named as everywhere: the last subject token, and a valid
// agent name.
func TestABadAgentIsRefused(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	handler(t, srv, nc, "req")

	for _, subject := range []string{
		"req.history.Alice",                      // upper case
		"req.history.-alice",                     // no name starts with '-'
		"req.history." + strings.Repeat("a", 65), // 65 characters
		"req.history.alice.extra",                // one token too many
		"req.notakind.alice",                     // no scaffold kind
		"bus.history.alice",                      // not the request prefix
	} {
		publish(t, nc, subject, body("history", goodID))
	}
	none(t, ch, 300*time.Millisecond)
}

// An unreadable body is logged and left unanswered, never a panic.
func TestAnUnreadableBodyIsRefused(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	handler(t, srv, nc, "req")

	for _, raw := range []string{
		`not json`,
		`{"v":2,"request":"` + goodID + `","kind":"history","payload":null}`,
		`{"v":1,"request":"` + goodID + `","kind":"history"`, // cut
	} {
		publish(t, nc, "req.history.alice", raw)
	}
	none(t, ch, 300*time.Millisecond)
}

// The answer's agent is the subject's last token: a body field that names
// another agent is ignored, and no answer goes to that other agent.
func TestTheAgentIsTheSubjectsLastToken(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	handler(t, srv, nc, "req")

	publish(t, nc, "req.header.alice",
		fmt.Sprintf(`{"v":1,"request":%q,"kind":"header","payload":null,"agent":"bob"}`, goodID))
	m := want(t, ch, time.Second)
	if want := "deliver.alice.req." + goodID; m.Subject != want {
		t.Fatalf("answer on %s, want %s", m.Subject, want)
	}
	if strings.HasPrefix(m.Subject, "deliver.bob.") {
		t.Fatalf("the body's agent was used: %s", m.Subject)
	}
	none(t, ch, 200*time.Millisecond)
}

// The handler answers on its own prefix only, the one it is given.
func TestTheHandlerAnswersItsOwnPrefix(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	handler(t, srv, nc, "rq")

	publish(t, nc, "req.history.alice", body("history", goodID))
	none(t, ch, 200*time.Millisecond)
	publish(t, nc, "rq.history.alice", body("history", goodID))
	m := want(t, ch, time.Second)
	if want := "deliver.alice.req." + goodID; m.Subject != want {
		t.Fatalf("answer on %s, want %s", m.Subject, want)
	}
}

// RT-6.13, plan B piece 10: "permission" is a scaffold kind; without a piece
// it gets the "not built" answer, with one its result is the answer.
func TestRT613PermissionIsAScaffoldKind(t *testing.T) {
	srv, nc := start(t)
	ch := watch(t, nc)
	startHandler(t, srv, nc, &Handler{Log: quiet, Prefix: "req",
		Handlers: map[string]Piece{"permission": func(_ context.Context, agent string, req Request) ([]Reply, error) {
			return []Reply{{Result: json.RawMessage(`{"v":1,"posted":true}`)}}, nil
		}}})
	publish(t, nc, "req.permission.alice", body("permission", goodID))
	m := want(t, ch, time.Second)
	var a answer
	if err := json.Unmarshal(m.Data, &a); err != nil {
		t.Fatal(err)
	}
	if a.Kind != "permission" || a.Error != "" || string(a.Result) != `{"v":1,"posted":true}` {
		t.Fatalf("the permission answer: %s", m.Data)
	}
	if !kindOK("permission") || kindOK("permissions") {
		t.Fatal("kindOK")
	}
}
