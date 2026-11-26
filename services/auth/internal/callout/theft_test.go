package callout

import (
	"errors"
	"sync"
	"testing"

	gojwt "github.com/golang-jwt/jwt/v5"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/theft"
)

// RT-6.34, #154 step 3: the issue is watched under the host key (reported) and
// the token's Keycloak session (refused when it alternates), from the client's
// network.
type stubWatch struct {
	mu       sync.Mutex
	host     theft.Verdict
	session  theft.Verdict
	hostIDs  []string
	sessIDs  []string
	networks []string
}

func (s *stubWatch) HostKey(nkey, network string) theft.Verdict {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.hostIDs = append(s.hostIDs, nkey)
	s.networks = append(s.networks, network)
	return s.host
}

func (s *stubWatch) Session(sid, network string) theft.Verdict {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sessIDs = append(s.sessIDs, sid)
	s.networks = append(s.networks, network)
	return s.session
}

func (s *stubWatch) seen() (host, session []string, networks []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.hostIDs...), append([]string(nil), s.sessIDs...),
		append([]string(nil), s.networks...)
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
	w := &stubWatch{host: theft.Same, session: theft.Same}
	s := &Service{}
	s.SetWatcher(w)
	if err := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:51234"); err != nil {
		t.Fatalf("%v", err)
	}
	hosts, sids, nets := w.seen()
	if len(hosts) != 1 || hosts[0] != "UAHOST" {
		t.Fatalf("host keys watched %v", hosts)
	}
	if len(sids) != 1 || sids[0] != "sess-1" {
		t.Fatalf("sessions watched %v", sids)
	}
	if nets[0] != "203.0.113.0/24" || nets[1] != "203.0.113.0/24" {
		t.Fatalf("networks %v", nets)
	}
	// A token without a session id: nothing to compare, no empty id.
	w = &stubWatch{}
	s.SetWatcher(w)
	if err := s.watchTheft("UAHOST", "", "203.0.113.7:1"); err != nil {
		t.Fatalf("%v", err)
	}
	hosts, sids, _ = w.seen()
	if len(hosts) != 1 || len(sids) != 0 {
		t.Fatalf("watched hosts %v, sessions %v", hosts, sids)
	}
}

func TestAnAlternatingSessionRefusesTheIssue(t *testing.T) {
	s := &Service{}
	s.SetWatcher(&stubWatch{host: theft.Moved, session: theft.Copied})
	err := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1")
	if !errors.Is(err, ErrCopiedUse) {
		t.Fatalf("a copied session: %v, want ErrCopiedUse", err)
	}
}

func TestAMoveDoesNotRefuse(t *testing.T) {
	s := &Service{}
	s.SetWatcher(&stubWatch{host: theft.Moved, session: theft.Moved})
	if err := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1"); err != nil {
		t.Fatalf("a network change: %v, want no error", err)
	}
}

// A copied host key is reported, never refused: the owner rotates it, and a
// refusal would lock the owner out. A new session on that key is not refused
// either.
func TestACopiedHostKeyDoesNotRefuse(t *testing.T) {
	s := &Service{}
	s.SetWatcher(&stubWatch{host: theft.Copied, session: theft.Same})
	if err := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1"); err != nil {
		t.Fatalf("a copied host key: %v, want no error", err)
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
	w := &stubWatch{}
	l.svc.SetWatcher(w)
	// A token the realm will not accept (another audience): the issue is refused
	// before the watch, so a refused request cannot be counted as a network use.
	bad := l.realm.Token("alice", func(c gojwt.MapClaims) { c["aud"] = "not-this-client" })
	if _, err := l.connectAt(l.url, "alice", bad, Hello{Home: "home"}, nil, 0); err == nil {
		t.Fatal("a token with another audience connected")
	}
	if hosts, sids, _ := w.seen(); len(hosts) != 0 || len(sids) != 0 {
		t.Fatalf("the guard saw a refused issue: %v %v", hosts, sids)
	}
	withSID := l.realm.Token("alice", func(c gojwt.MapClaims) { c["sid"] = "sess-lab" })
	if _, err := l.connect("alice", withSID, Hello{Home: "home"}, nil); err != nil {
		t.Fatalf("the good issue: %v", err)
	}
	hosts, sids, _ := w.seen()
	if len(hosts) == 0 {
		t.Fatal("the guard saw no host key of a good issue")
	}
	if len(sids) != 1 || sids[0] != "sess-lab" {
		t.Fatalf("the sessions the guard saw: %v, want sess-lab", sids)
	}
}
