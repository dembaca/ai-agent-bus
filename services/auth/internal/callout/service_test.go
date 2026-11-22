package callout

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	gojwt "github.com/golang-jwt/jwt/v5"
	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
)

// lab is a real nats-server with auth_callout, the callout service
// connected to it as its auth user, a test realm and a chat.
type lab struct {
	t     *testing.T
	url   string
	realm *testrealm.Realm
	chat  *testrealm.Chat
	logs  *syncBuffer
	brake string
}

type syncBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.b.String()
}

func newLab(t *testing.T) *lab { return newLabWith(t, nil, 0) }

// newLabWith runs every test with JetStream on in the bus's account, as in
// the lab; wrap, if set, wraps the chat's channel answers (delays, failures),
// and timeout, if set, is the service's request timeout.
func newLabWith(t *testing.T, wrap func(Channels) Channels, timeout time.Duration) *lab {
	t.Helper()
	issuer, _ := nkeys.CreateAccount()
	issuerPub, _ := issuer.PublicKey()
	xkey, _ := nkeys.CreateCurveKeys()
	xkeyPub, _ := xkey.PublicKey()
	// The service's own user is an nkey user, as in the lab: with an nkey
	// user configured the server sends every client a nonce to sign.
	svcUser, _ := nkeys.CreateUser()
	svcPub, _ := svcUser.PublicKey()
	conf := filepath.Join(t.TempDir(), "nats.conf")
	err := os.WriteFile(conf, []byte(`
listen: "127.0.0.1:-1"
jetstream { store_dir: "`+t.TempDir()+`" }
accounts {
  AUTH { users: [ { nkey: "`+svcPub+`" } ] }
  BUS { jetstream: enabled, users: [ { user: admin, password: pwd } ] }
  OTHER { users: [ { user: other, password: pwd } ] }
}
authorization {
  timeout: 3s
  auth_callout {
    issuer: "`+issuerPub+`"
    account: AUTH
    auth_users: [ "`+svcPub+`", other, admin ]
    xkey: "`+xkeyPub+`"
  }
}
`), 0o600)
	if err != nil {
		t.Fatal(err)
	}
	opts, err := server.ProcessConfigFile(conf)
	if err != nil {
		t.Fatal(err)
	}
	opts.NoLog, opts.NoSigs = true, true
	srv, err := server.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)

	l := &lab{t: t, url: srv.ClientURL(), realm: testrealm.New(t), chat: testrealm.NewChat("alice", "gus"),
		logs: &syncBuffer{}, brake: filepath.Join(t.TempDir(), "brake")}
	g := l.chat.Users["gus"]
	g.Guest = true
	l.chat.Users["gus"] = g
	l.chat.Channels["alice"] = []string{"home"}
	if err := os.WriteFile(l.brake, []byte("# nobody stopped\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	var ch Channels = l.chat
	if wrap != nil {
		ch = wrap(ch)
	}
	cfg := Config{Account: "BUS", Lobby: "lobby", Timeout: timeout}.WithDefaults()
	svc, err := New(cfg, issuer, xkey, l.realm.Verifier(l.chat), ch, NewBrake(l.brake, nil),
		slog.New(slog.NewJSONHandler(l.logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	nc, err := nats.Connect(l.url, nats.Nkey(svcPub, svcUser.Sign))
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { _ = svc.Serve(ctx, nc); close(done) }()
	t.Cleanup(func() { cancel(); <-done; nc.Close() })
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	return l
}

// agent is a host's connection: its own nkey signs the nonce, the hello
// carries the token and the wished channels.
type agent struct {
	nc   *nats.Conn
	name string
	errs chan error
}

func (l *lab) connect(owner, tok string, hello Hello, signer nkeys.KeyPair) (*agent, error) {
	l.t.Helper()
	host, _ := nkeys.CreateUser()
	pub, _ := host.PublicKey()
	if signer == nil {
		signer = host
	}
	hello.Token = tok
	raw, _ := jsonMarshal(hello)
	a := &agent{name: DefaultNamePrefix + name(owner, pub), errs: make(chan error, 16)}
	nc, err := nats.Connect(l.url,
		nats.Nkey(pub, func(nonce []byte) ([]byte, error) { return signer.Sign(nonce) }),
		nats.Token(string(raw)),
		nats.CustomInboxPrefix("_INBOX."+pub),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { a.errs <- err }),
		nats.MaxReconnects(0))
	if err != nil {
		return nil, err
	}
	l.t.Cleanup(nc.Close)
	a.nc = nc
	return a, nil
}

// denied says whether the server refused the last operation.
func (a *agent) denied(t *testing.T) bool {
	t.Helper()
	if err := a.nc.Flush(); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-a.errs:
		return strings.Contains(err.Error(), "Permissions Violation")
	case <-time.After(300 * time.Millisecond):
		return false
	}
}

func TestAnAgentGetsItsNameAndOnlyItsChannels(t *testing.T) {
	l := newLab(t)
	tok := l.realm.Token("alice", nil)
	a, err := l.connect("alice", tok, Hello{Home: "home", Guests: []string{"elsewhere"}, Lobby: true}, nil)
	if err != nil {
		t.Fatal(err)
	}
	// Granted: home (a member) and the lobby; refused: the guest channel
	// alice is no member of, and every subject of another sender.
	for subj, want := range map[string]bool{
		"bus.ch.home.*": false, "bus.ch.lobby.*": false, "bus.dm." + a.name + ".*": false,
		"bus.ch.elsewhere.*": true, "bus.dm.agent-other.*": true, "bus.>": true,
	} {
		if _, err := a.nc.SubscribeSync(subj); err != nil {
			t.Fatal(err)
		}
		if got := a.denied(t); got != want {
			t.Errorf("subscribe %s: denied %v, want %v", subj, got, want)
		}
	}
	for subj, want := range map[string]bool{
		"bus.ch.home." + a.name: false, "bus.dm.agent-other." + a.name: false,
		"bus.ch.home.agent-other": true, "bus.ch.elsewhere." + a.name: true, "bus.dm.agent-other.agent-other": true,
	} {
		if err := a.nc.Publish(subj, []byte("hi")); err != nil {
			t.Fatal(err)
		}
		if got := a.denied(t); got != want {
			t.Errorf("publish %s: denied %v, want %v", subj, got, want)
		}
	}
	logs := l.logs.String()
	if !strings.Contains(logs, `"event":"issued"`) || !strings.Contains(logs, `"agent":"`+a.name+`"`) ||
		!strings.Contains(logs, `"reason":"not a member"`) {
		t.Fatalf("audit log:\n%s", logs)
	}
	if strings.Contains(logs, tok[:40]) {
		t.Fatal("the token is in the log")
	}
}

func TestAPostReachesTheChannelUnderItsSendersName(t *testing.T) {
	l := newLab(t)
	l.chat.Channels["bob"] = []string{"home"}
	l.chat.Users["bob"] = l.chat.Users["alice"]
	u := l.chat.Users["bob"]
	u.Username, u.AuthData = "bob", "sub-bob"
	l.chat.Users["bob"] = u
	a, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	b, err := l.connect("bob", l.realm.Token("bob", nil), Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatal(err)
	}
	sub, _ := b.nc.SubscribeSync("bus.ch.home.*")
	_ = b.nc.Flush()
	if err := a.nc.Publish("bus.ch.home."+a.name, []byte("hello")); err != nil {
		t.Fatal(err)
	}
	m, err := sub.NextMsg(2 * time.Second)
	if err != nil || m.Subject != "bus.ch.home."+a.name {
		t.Fatalf("%v, %v", m, err)
	}
	// Another tenant's account shares no subject with the bus's.
	other, err := nats.Connect(l.url, nats.UserInfo("other", "pwd"))
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	_ = other.Publish("bus.ch.home."+a.name, []byte("from another tenant"))
	_ = other.Flush()
	if m, err := sub.NextMsg(300 * time.Millisecond); err == nil {
		t.Fatalf("crossed accounts: %s", m.Data)
	}
}

func TestRefusedConnections(t *testing.T) {
	l := newLab(t)
	stranger, _ := nkeys.CreateUser()
	cases := []struct {
		name, owner, tok string
		signer           nkeys.KeyPair
		reason           string
	}{
		{"expired token", "alice", l.realm.Token("alice", func(c gojwt.MapClaims) {
			c["exp"] = time.Now().Add(-time.Hour).Unix()
		}), nil, "token expired"},
		{"nonce signed by another nkey", "alice", l.realm.Token("alice", nil), stranger, ErrNoNkey.Error()},
		{"a guest", "gus", l.realm.Token("gus", nil), nil, "guest"},
		{"not a hello", "alice", "", nil, ErrRequest.Error()},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := l.connect(tc.owner, tc.tok, Hello{Home: "home"}, tc.signer)
			if err == nil || !strings.Contains(err.Error(), "Authorization Violation") {
				t.Fatalf("connected or wrong error: %v", err)
			}
			if !strings.Contains(l.logs.String(), tc.reason) {
				t.Fatalf("no refusal %q in the log:\n%s", tc.reason, l.logs.String())
			}
		})
	}
}

func TestTheEmergencyBrake(t *testing.T) {
	l := newLab(t)
	set := func(text string) {
		t.Helper()
		// A later modification time, as an edit gives, even within one tick.
		if err := os.WriteFile(l.brake, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
		later := time.Now().Add(time.Duration(len(text)) * time.Second)
		_ = os.Chtimes(l.brake, later, later)
	}
	try := func() error {
		_, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil)
		return err
	}
	set("alice  # stolen laptop\n")
	if try() == nil {
		t.Fatal("a stopped owner connected")
	}
	set("bob\n")
	if err := try(); err != nil {
		t.Fatalf("another owner stopped: %v", err)
	}
	set("*\n")
	if try() == nil {
		t.Fatal("connected while everyone is stopped")
	}
	set("ALICE\n") // names compare in lower case
	if try() == nil {
		t.Fatal("a stopped owner connected, named in upper case")
	}
	set("") // empty: nobody held, so the next refusal is the missing file's
	if err := try(); err != nil {
		t.Fatalf("an empty brake stopped: %v", err)
	}
	_ = os.Remove(l.brake)
	if try() == nil {
		t.Fatal("connected while the brake file is missing")
	}
}

func jsonMarshal(v any) ([]byte, error) { return json.Marshal(v) }
