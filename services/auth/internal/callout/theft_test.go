package callout

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

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
	if res := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:51234"); res.host != theft.Same || res.session != theft.Same {
		t.Fatalf("%+v", res)
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
	s.watchTheft("UAHOST", "", "203.0.113.7:1")
	hosts, sids, _ = w.seen()
	if len(hosts) != 1 || len(sids) != 0 {
		t.Fatalf("watched hosts %v, sessions %v", hosts, sids)
	}
}

func TestACopiedSessionIsRefused(t *testing.T) {
	s := &Service{}
	s.SetWatcher(&stubWatch{host: theft.Moved, session: theft.Copied})
	if res := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1"); res.session != theft.Copied {
		t.Fatalf("a copied session: %+v, want the session copied", res)
	}
}

func TestAMoveDoesNotRefuse(t *testing.T) {
	s := &Service{}
	s.SetWatcher(&stubWatch{host: theft.Moved, session: theft.Moved})
	if res := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1"); res.session == theft.Copied {
		t.Fatalf("a network change: %+v, want no refusal", res)
	}
}

// A copied host key is reported, never refused: the owner rotates it, and a
// refusal would lock the owner out. A new session on that key is not refused
// either.
func TestACopiedHostKeyDoesNotRefuse(t *testing.T) {
	s := &Service{}
	s.SetWatcher(&stubWatch{host: theft.Copied, session: theft.Same})
	if res := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1"); res.host != theft.Copied || res.session == theft.Copied {
		t.Fatalf("a copied host key: %+v, want it reported only", res)
	}
}

func TestWithoutAWatcherNothingIsRefused(t *testing.T) {
	s := &Service{}
	if res := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1"); res.session == theft.Copied || res.host == theft.Copied {
		t.Fatalf("no watcher: %+v", res)
	}
}

type stubNoticer struct {
	mu       sync.Mutex
	subjects []string
	bodies   [][]byte
}

func (n *stubNoticer) Notice(_ context.Context, subject string, body []byte) error {
	n.mu.Lock()
	defer n.mu.Unlock()
	n.subjects = append(n.subjects, subject)
	n.bodies = append(n.bodies, body)
	return nil
}

// RT-6.34, #154 step 3: a move and a copy are published for the owner on a
// subject only the login service may publish, with no token and no address
// beyond the prefix; a same verdict publishes nothing.
func TestANoticeGoesToTheOwner(t *testing.T) {
	n := &stubNoticer{}
	s := &Service{cfg: Config{}.WithDefaults()}
	s.SetWatcher(&stubWatch{host: theft.Moved, session: theft.Copied})
	s.SetNoticer(n)
	now := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	w := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:51234")
	s.publishNotices(context.Background(), w, "bob.example", "agent-abc", "UAHOST", "sess-1", now)

	if len(n.subjects) != 2 {
		t.Fatalf("published %d notices, want 2 (the host key's move, the session's copy)", len(n.subjects))
	}
	if n.subjects[0] != "presence.notice.bob~example.moved.v1" || n.subjects[1] != "presence.notice.bob~example.copied.v1" {
		t.Fatalf("subjects %v", n.subjects)
	}
	var host, session Notice
	if err := json.Unmarshal(n.bodies[0], &host); err != nil {
		t.Fatalf("%v", err)
	}
	if err := json.Unmarshal(n.bodies[1], &session); err != nil {
		t.Fatalf("%v", err)
	}
	if host.Kind != "host_key" || host.Verdict != "moved" || host.SID != "UAHOST" {
		t.Fatalf("host key notice %+v", host)
	}
	if session.Kind != "session" || session.Verdict != "copied" || session.SID != "sess-1" ||
		session.Network != "203.0.113.0/24" || session.Agent != "agent-abc" {
		t.Fatalf("session notice %+v", session)
	}
	if session.V != 1 || session.Exp <= session.Iat || session.Exp-session.Iat != int64(NoticeLifetime/time.Second) {
		t.Fatalf("session notice lifetime %+v", session)
	}

	// A same verdict publishes nothing.
	n2 := &stubNoticer{}
	s.SetNoticer(n2)
	s.SetWatcher(&stubWatch{host: theft.Same, session: theft.Same})
	w = s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1")
	s.publishNotices(context.Background(), w, "bob", "agent-abc", "UAHOST", "sess-1", now)
	if len(n2.subjects) != 0 {
		t.Fatalf("a same verdict published %v", n2.subjects)
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

// RT-6.34: a refused session's retries publish nothing new within the block.
func TestARetryDoesNotTellAgain(t *testing.T) {
	n := &stubNoticer{}
	s := &Service{cfg: Config{}.WithDefaults()}
	s.SetWatcher(&stubWatch{host: theft.Same, session: theft.Copied})
	s.SetNoticer(n)
	now := time.Date(2026, 11, 26, 9, 0, 0, 0, time.UTC)
	for i := 0; i < 5; i++ {
		w := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1")
		s.publishNotices(context.Background(), w, "bob", "agent-abc", "UAHOST", "sess-1", now.Add(time.Duration(i)*time.Minute))
	}
	if len(n.subjects) != 1 {
		t.Fatalf("five retries published %d notices, want 1", len(n.subjects))
	}
	// After the cooldown a new copy tells again.
	w := s.watchTheft("UAHOST", "sess-1", "203.0.113.7:1")
	s.publishNotices(context.Background(), w, "bob", "agent-abc", "UAHOST", "sess-1", now.Add(NoticeCooldown))
	if len(n.subjects) != 2 {
		t.Fatalf("after the cooldown: %d notices, want 2", len(n.subjects))
	}
}
