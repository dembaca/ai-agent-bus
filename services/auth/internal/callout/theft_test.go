package callout

import (
	"errors"
	"sync"
	"testing"

	gojwt "github.com/golang-jwt/jwt/v5"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/theft"
)

// RT-6.34, #154 step 3: the issue is watched under the host key and the
// token's Keycloak session, from the client's network.
type stubWatch struct {
	mu      sync.Mutex
	verdict theft.Verdict
	ids     []string
	nets    []string
}

func (s *stubWatch) Observe(id, network string) theft.Verdict {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ids = append(s.ids, id)
	s.nets = append(s.nets, network)
	return s.verdict
}

func (s *stubWatch) seen() (ids, nets []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.ids...), append([]string(nil), s.nets...)
}

func TestTheSourceAddressIsItsNetwork(t *testing.T) {
	for in, want := range map[string]string{
		"203.0.113.7:51234":    "203.0.113.0/24",
		"203.0.113.9":          "203.0.113.0/24", // one network, two addresses
		"[2001:db8::1]:443":    "2001:db8::/64",
		"[2001:db8::2]":        "2001:db8::/64",
		" 198.51.100.9:1234  ": "198.51.100.0/24",
		"nats.example":         "nats.example", // no address: its own network
		"":                     "",
	} {
		if got := source(in); got != want {
			t.Errorf("source(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestBothTheHostKeyAndTheSessionAreWatched(t *testing.T) {
	w := &stubWatch{verdict: theft.Same}
	s := &Service{}
	s.SetWatcher(w)
	if err := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:51234"); err != nil {
		t.Fatalf("%v", err)
	}
	ids, nets := w.seen()
	if len(ids) != 2 || ids[0] != "nkey:UAHOST" || ids[1] != "sid:sess-1" {
		t.Fatalf("watched %v", ids)
	}
	if nets[0] != "203.0.113.0/24" || nets[1] != "203.0.113.0/24" {
		t.Fatalf("networks %v", nets)
	}
	// A token without a session id: the host key alone, no empty id.
	w.mu.Lock()
	w.ids, w.nets = nil, nil
	w.mu.Unlock()
	if err := s.watchTheft("UAHOST", "", "203.0.113.7:1"); err != nil {
		t.Fatalf("%v", err)
	}
	ids, _ = w.seen()
	if len(ids) != 1 || ids[0] != "nkey:UAHOST" {
		t.Fatalf("watched %v", ids)
	}
}

func TestTwoNetworksAtOnceRefuseTheIssue(t *testing.T) {
	s := &Service{}
	s.SetWatcher(&stubWatch{verdict: theft.Concurrent})
	err := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1")
	if !errors.Is(err, ErrConcurrentUse) {
		t.Fatalf("a concurrent use: %v, want ErrConcurrentUse", err)
	}
}

func TestAMoveDoesNotRefuse(t *testing.T) {
	s := &Service{}
	s.SetWatcher(&stubWatch{verdict: theft.Moved})
	if err := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1"); err != nil {
		t.Fatalf("a network change: %v, want no error", err)
	}
}

func TestWithoutAWatcherNothingIsRefused(t *testing.T) {
	s := &Service{}
	if err := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1"); err != nil {
		t.Fatalf("no watcher: %v", err)
	}
}

// RT-6.34, #154 step 3: an issue that fails before the token checks never
// reaches the guard — the watcher sees a good issue and nothing of a bad one,
// and the callout's own wiring is what is exercised.
func TestAnUnauthenticatedIssueNeverTouchesTheWatcher(t *testing.T) {
	l := newLab(t)
	w := &stubWatch{verdict: theft.Same}
	l.svc.SetWatcher(w)
	// A token the realm will not accept (another audience): the issue is refused
	// before the watch, so a refused request cannot be counted as a network use.
	bad := l.realm.Token("alice", func(c gojwt.MapClaims) { c["aud"] = "not-this-client" })
	if _, err := l.connectAt(l.url, "alice", bad, Hello{Home: "home"}, nil, 0); err == nil {
		t.Fatal("a token with another audience connected")
	}
	if ids, _ := w.seen(); len(ids) != 0 {
		t.Fatalf("the guard saw a refused issue: %v", ids)
	}
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatalf("the good issue: %v", err)
	}
	if ids, _ := w.seen(); len(ids) == 0 {
		t.Fatal("the guard saw nothing of a good issue")
	}
}
