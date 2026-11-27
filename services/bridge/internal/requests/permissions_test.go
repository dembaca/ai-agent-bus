package requests

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// counts counts the Error lines a Handler writes. A refused subscribe is
// retried with one line each time; the test's own retry is far shorter than a
// minute, so more than one line shows the retry.
type counts struct {
	mu sync.Mutex
	n  int
}

func (c *counts) Enabled(context.Context, slog.Level) bool { return true }

func (c *counts) Handle(_ context.Context, r slog.Record) error {
	if r.Level >= slog.LevelError {
		c.mu.Lock()
		c.n++
		c.mu.Unlock()
	}
	return nil
}

func (c *counts) WithAttrs([]slog.Attr) slog.Handler { return c }
func (c *counts) WithGroup(string) slog.Handler      { return c }

func (c *counts) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.n
}

// The bridge's NATS user answers an agent's request with exactly the rights
// the example configuration documents, and no other agent reads the answer;
// an agent's own rights, as perms.go grants them, reach neither another
// agent's answers nor the bridge's answer subject. This runs the handler and
// the rights against a real server.
func TestTheDocumentedRightsAnswerOneAgentAndNoOther(t *testing.T) {
	publish, subscribe := documentedBridgeRights(t, "home")
	// A password user stands in for the bridge's nkey: the permissions are
	// what is under test. The agents' entries are perms.go's rights, kept
	// minimal: publish req.*.<name>, subscribe deliver.<name>.>
	srv, err := startUsers(t, fmt.Sprintf(`
port: -1
host: 127.0.0.1
authorization {
  users: [
    { user: bridge, password: pw, permissions: {
      publish: [ %s ]
      subscribe: [ %s ]
    } }
    { user: agent-a, password: pw, permissions: {
      publish: [ "req.*.agent-a" ]
      subscribe: [ "deliver.agent-a.>" ]
    } }
    { user: agent-b, password: pw, permissions: {
      publish: [ "req.*.agent-b" ]
      subscribe: [ "deliver.agent-b.>" ]
    } }
  ]
}
`, quoted(publish), quoted(subscribe)))
	if err != nil {
		t.Fatal(err)
	}

	bridge := connectAs(t, srv, "bridge", nats.CustomInboxPrefix("_INBOX_bridge"))
	a := connectAs(t, srv, "agent-a")
	b := connectAs(t, srv, "agent-b")

	// the agents' own answers' subjects
	ach, bch := make(chan *nats.Msg, 4), make(chan *nats.Msg, 4)
	asub, err := a.ChanSubscribe("deliver.agent-a.>", ach)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = asub.Unsubscribe() })
	bsub, err := b.ChanSubscribe("deliver.agent-b.>", bch)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = bsub.Unsubscribe() })
	if err := a.Flush(); err != nil {
		t.Fatal(err)
	}
	if err := b.Flush(); err != nil {
		t.Fatal(err)
	}

	before := srv.NumSubscriptions()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- (&Handler{Log: quiet, Prefix: "req"}).Run(ctx, bridge) }()
	t.Cleanup(func() {
		cancel()
		if err := <-done; err != nil {
			t.Errorf("Run: %v", err)
		}
	})
	waitSubs(t, srv, before)

	// agent-a's request is answered, on the subject the bridge may publish
	if err := a.Publish("req.history.agent-a", []byte(body("history", goodID))); err != nil {
		t.Fatal(err)
	}
	if err := a.Flush(); err != nil {
		t.Fatal(err)
	}
	select {
	case m := <-ach:
		if want := "deliver.agent-a.req." + goodID; m.Subject != want {
			t.Fatalf("answer on %s, want %s", m.Subject, want)
		}
		var ans answer
		if err := json.Unmarshal(m.Data, &ans); err != nil {
			t.Fatal(err)
		}
		if ans.Request != goodID || ans.Error != "not built" {
			t.Fatalf("answer %s", m.Data)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the documented rights carry no answer")
	}

	// and no other agent reads it
	select {
	case m := <-bch:
		t.Fatalf("agent-b read agent-a's answer on %s: %s", m.Subject, m.Data)
	case <-time.After(300 * time.Millisecond):
	}

	// an agent may not subscribe to another agent's answers, nor to every
	// agent's: its own deliver.<name>.> is the only reading right it holds.
	for _, subject := range []string{"deliver.agent-a.>", "deliver.*.req.>"} {
		nb := connectAs(t, srv, "agent-b")
		sub, err := nb.ChanSubscribe(subject, make(chan *nats.Msg, 1))
		if err != nil {
			t.Fatal(err)
		}
		if err := nb.Flush(); err != nil {
			t.Fatal(err)
		}
		if err := nb.LastError(); err == nil {
			t.Fatalf("agent-b subscribed %s", subject)
		} else if !strings.Contains(err.Error(), `Permissions Violation for Subscription to "`+subject+`"`) {
			t.Fatalf("agent-b's %s: %v", subject, err)
		}
		_ = sub.Unsubscribe()
	}
}

// A bridge whose subscribe right is missing keeps running: the failure is
// named for /healthz, the subscribe is retried with one Error line for it, and
// only ctx ends the run - a missing right leaves requests unanswered, never the
// bridge down.
func TestARefusedSubscribeKeepsTheRunAndNamesTheFailure(t *testing.T) {
	srv, err := startUsers(t, `
port: -1
host: 127.0.0.1
authorization {
  users: [
    { user: bridge, password: pw, permissions: {
      subscribe: [ "deliver.bridge.>" ]
      publish: [ "not.deliver.>" ]
    } }
  ]
}
`)
	if err != nil {
		t.Fatal(err)
	}
	bridge := connectAs(t, srv, "bridge")
	var lines counts
	h := &Handler{Log: slog.New(&lines), Prefix: "req", retry: 20 * time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.Run(ctx, bridge) }()

	// The failure names the refused subject ...
	deadline := time.Now().Add(2 * time.Second)
	for {
		msg, bad := h.Failure()
		if bad {
			if !strings.Contains(msg, "req.>") {
				t.Fatalf("Failure: %q, want the refused subscription", msg)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("a refused subscription is not named")
		}
		time.Sleep(5 * time.Millisecond)
	}
	// ... and the run retries it instead of stopping: the test's own wait is far
	// shorter than a minute, so more than one line means a retry.
	for lines.count() < 2 {
		if time.Now().After(deadline) {
			t.Fatalf("Run did not retry the refused subscription (%d Error lines)", lines.count())
		}
		time.Sleep(5 * time.Millisecond)
	}
	select {
	case err := <-done:
		t.Fatalf("Run stopped at a refused subscription: %v", err)
	default:
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not end with its ctx")
	}
}

// The failure is only the current state: once the server takes the subscribe,
// Failure is false again. The handler is the same one that failed before.
func TestAGrantedSubscribeClearsTheFailure(t *testing.T) {
	refused, err := startUsers(t, `
port: -1
host: 127.0.0.1
authorization {
  users: [
    { user: bridge, password: pw, permissions: {
      subscribe: [ "deliver.bridge.>" ]
      publish: [ "not.deliver.>" ]
    } }
  ]
}
`)
	if err != nil {
		t.Fatal(err)
	}
	nc := connectAs(t, refused, "bridge")
	h := &Handler{Log: quiet, Prefix: "req", retry: 20 * time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.Run(ctx, nc) }()
	deadline := time.Now().Add(2 * time.Second)
	for {
		if _, bad := h.Failure(); bad {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("a refused subscription is not named")
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Run: %v", err)
	}

	// The same handler against a server that grants the right: its subscribe is
	// taken, and the failure goes.
	granted, err := startUsers(t, `
port: -1
host: 127.0.0.1
authorization {
  users: [
    { user: bridge, password: pw, permissions: {
      subscribe: [ "req.>" ]
      publish: [ "deliver.*.req.*" ]
    } }
  ]
}
`)
	if err != nil {
		t.Fatal(err)
	}
	nc = connectAs(t, granted, "bridge")
	before := granted.NumSubscriptions()
	ctx, cancel = context.WithCancel(context.Background())
	go func() { done <- h.Run(ctx, nc) }()
	waitSubs(t, granted, before)
	deadline = time.Now().Add(2 * time.Second)
	for {
		if _, bad := h.Failure(); !bad {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("a taken subscription left the failure set")
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Run: %v", err)
	}
}

// A refused answer subject is no reason to stop either: the failure is named
// for /healthz, and the run answers what it can.
func TestARefusedAnswerSetsTheFailureAndTheRunGoesOn(t *testing.T) {
	srv, err := startUsers(t, `
port: -1
host: 127.0.0.1
authorization {
  users: [
    { user: bridge, password: pw, permissions: {
      subscribe: [ "req.>" ]
      publish: [ "not.deliver.>" ]
    } }
    { user: agent-a, password: pw, permissions: {
      publish: [ "req.*.agent-a" ]
    } }
  ]
}
`)
	if err != nil {
		t.Fatal(err)
	}
	bridge := connectAs(t, srv, "bridge", nats.CustomInboxPrefix("_INBOX_bridge"))
	a := connectAs(t, srv, "agent-a")

	before := srv.NumSubscriptions()
	h := &Handler{Log: quiet, Prefix: "req"}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.Run(ctx, bridge) }()
	waitSubs(t, srv, before)

	if err := a.Publish("req.history.agent-a", []byte(body("history", goodID))); err != nil {
		t.Fatal(err)
	}
	if err := a.Flush(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		msg, bad := h.Failure()
		if bad {
			if !strings.Contains(msg, "deliver.agent-a.req."+goodID) {
				t.Fatalf("Failure: %q, want the refused answer subject", msg)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the refused answer subject is not named")
		}
		time.Sleep(5 * time.Millisecond)
	}
	select {
	case err := <-done:
		t.Fatalf("Run stopped at a refused answer subject: %v", err)
	default:
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not end with its ctx")
	}
}

// startUsers is an in-process nats-server whose users are the given
// configuration's.
func startUsers(t *testing.T, conf string) (*natsserver.Server, error) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "nats.conf")
	if err := os.WriteFile(path, []byte(conf), 0o600); err != nil {
		t.Fatal(err)
	}
	opts, err := natsserver.ProcessConfigFile(path)
	if err != nil {
		return nil, err
	}
	opts.NoLog, opts.NoSigs = true, true
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		return nil, err
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	return srv, nil
}

// connectAs is one client of a user of that server. A refused subject is
// what some tests are about: the error stays in LastError, not on stderr.
func connectAs(t *testing.T, srv *natsserver.Server, user string, opts ...nats.Option) *nats.Conn {
	t.Helper()
	opts = append([]nats.Option{nats.UserInfo(user, "pw"),
		nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {})}, opts...)
	nc, err := nats.Connect(srv.ClientURL(), opts...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	return nc
}

// waitSubs waits until the server holds more subscriptions than before.
func waitSubs(t *testing.T, srv *natsserver.Server, before uint32) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for srv.NumSubscriptions() <= before {
		if time.Now().After(deadline) {
			t.Fatal("the handler did not subscribe")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// documentedBridgeRights reads the bridge user's publish and subscribe lists
// from the example server configuration. It mirrors the copy in
// services/bridge/internal/reader/permissions_test.go; that copy also pins
// how narrow the lists stay, and the login service's example-conf test pins
// them entry by entry. This one only reads them.
func documentedBridgeRights(t *testing.T, home string) (publish, subscribe []string) {
	t.Helper()
	raw, err := os.ReadFile("../../../auth/nats-server.example.conf")
	if err != nil {
		t.Fatalf("the example configuration: %v", err)
	}
	text := string(raw)
	i := strings.Index(text, `nkey: "<BRIDGE_USER_PUBLIC_NKEY>"`)
	if i < 0 {
		t.Fatal("the example configuration has no bridge user")
	}
	block := text[i:]
	if end := strings.Index(block, "\n      }"); end > 0 {
		block = block[:end]
	}
	publish, subscribe = listIn(block, "publish:"), listIn(block, "subscribe:")
	if len(publish) == 0 || len(subscribe) == 0 {
		t.Fatalf("the bridge user's lists: %v / %v", publish, subscribe)
	}
	for _, list := range [][]string{publish, subscribe} {
		for j := range list {
			list[j] = strings.ReplaceAll(list[j], "<HOME>", home)
		}
	}
	return publish, subscribe
}

// listIn returns the quoted entries of the first [ ... ] after field.
func listIn(block, field string) []string {
	i := strings.Index(block, field)
	if i < 0 {
		return nil
	}
	rest := block[i+len(field):]
	end := strings.Index(rest, "]")
	if end < 0 {
		return nil
	}
	rest = rest[:end]
	var out []string
	for {
		a := strings.Index(rest, `"`)
		if a < 0 {
			return out
		}
		rest = rest[a+1:]
		b := strings.Index(rest, `"`)
		if b < 0 {
			return out
		}
		out = append(out, rest[:b])
		rest = rest[b+1:]
	}
}

// quoted renders a list as one config line of quoted entries.
func quoted(list []string) string {
	parts := make([]string, len(list))
	for i, v := range list {
		parts[i] = `"` + v + `"`
	}
	return strings.Join(parts, ", ")
}
