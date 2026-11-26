package callout

import (
	"errors"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/theft"
)

// RT-6.34, #154 step 3: the issue is watched under the host key and the
// token's Keycloak session, from the client's network.
type stubWatch struct {
	verdict theft.Verdict
	ids     []string
	nets    []string
}

func (s *stubWatch) Observe(id, network string) theft.Verdict {
	s.ids = append(s.ids, id)
	s.nets = append(s.nets, network)
	return s.verdict
}

func TestTheSourceAddressIsItsNetwork(t *testing.T) {
	for in, want := range map[string]string{
		"203.0.113.7:51234":    "203.0.113.7",
		"[2001:db8::1]:443":    "2001:db8::1",
		"203.0.113.7":          "203.0.113.7",
		"[2001:db8::1]":        "2001:db8::1",
		" 198.51.100.9:1234  ": "198.51.100.9",
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
	if len(w.ids) != 2 || w.ids[0] != "nkey:UAHOST" || w.ids[1] != "sid:sess-1" {
		t.Fatalf("watched %v", w.ids)
	}
	if w.nets[0] != "203.0.113.7" || w.nets[1] != "203.0.113.7" {
		t.Fatalf("networks %v", w.nets)
	}
	// A token without a session id: the host key alone, no empty id.
	w.ids, w.nets = nil, nil
	if err := s.watchTheft("UAHOST", "", "203.0.113.7:1"); err != nil {
		t.Fatalf("%v", err)
	}
	if len(w.ids) != 1 || w.ids[0] != "nkey:UAHOST" {
		t.Fatalf("watched %v", w.ids)
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
