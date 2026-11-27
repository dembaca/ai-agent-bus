package requests

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// goodID is a request id of the one shape: 16 characters of [a-z0-9].
const goodID = "abcdef0123456789"

// answer is the body on deliver.<agent>.req.<request id>.
type answer struct {
	V       int    `json:"v"`
	Request string `json:"request"`
	Kind    string `json:"kind"`
	Error   string `json:"error"`
}

// start is an in-process nats-server and one client.
func start(t *testing.T) (*natsserver.Server, *nats.Conn) {
	t.Helper()
	srv, err := natsserver.NewServer(&natsserver.Options{Host: "127.0.0.1", Port: -1,
		NoLog: true, NoSigs: true})
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
	before := srv.NumSubscriptions()
	h := &Handler{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Prefix: prefix}
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
	ch := make(chan *nats.Msg, 8)
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
// with this phase's "not built" and the same request id.
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
		`{"v":1,"request":"`+goodID+`","kind":"history","error":"not built"}`; got != want {
		t.Fatalf("answer %s, want %s", got, want)
	}
	var a answer
	if err := json.Unmarshal(m.Data, &a); err != nil {
		t.Fatal(err)
	}
	if a.V != 1 || a.Request != goodID || a.Kind != "history" || a.Error != "not built" {
		t.Fatalf("answer %+v", a)
	}
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
