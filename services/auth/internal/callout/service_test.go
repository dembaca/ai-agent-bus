package callout

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
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
	wsURL string
	realm *testrealm.Realm
	chat  *testrealm.Chat
	logs  *syncBuffer
	brake string
	svc   *Service
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
	return newLabCfg(t, wrap, timeout, nil)
}

// newLabCfg is newLabWith with a last word on the service's configuration.
func newLabCfg(t *testing.T, wrap func(Channels) Channels, timeout time.Duration, edit func(*Config)) *lab {
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
websocket { listen: "127.0.0.1:-1", no_tls: true }
jetstream { store_dir: "`+t.TempDir()+`" }
accounts {
  # The callout's user with the rights of nats-server.example.conf.
  AUTH { users: [ { nkey: "`+svcPub+`", permissions: {
    publish: { deny: ">" }, subscribe: [ "$SYS.REQ.USER.AUTH" ], allow_responses: true } } ] }
  BUS { jetstream: enabled, users: [ { user: admin, password: pwd },
    # The login service's own JetStream user with the minimal rights that
    # services/auth/README.md and RT-6.35 document.
    { user: jsmin, password: pwd, permissions: {
      publish: [ "$JS.API.STREAM.CREATE.BUS", "$JS.API.STREAM.INFO.BUS", "$JS.API.CONSUMER.*.BUS", "$JS.API.CONSUMER.*.BUS.>",
        "$JS.API.STREAM.CREATE.PRESENCE", "$JS.API.STREAM.INFO.PRESENCE", "$JS.API.CONSUMER.*.PRESENCE", "$JS.API.CONSUMER.*.PRESENCE.>", "$JS.API.STREAM.CREATE.STATS", "$JS.API.STREAM.INFO.STATS", "$JS.API.CONSUMER.*.STATS", "$JS.API.CONSUMER.*.STATS.>", "presence.person.*.v1", "$JS.API.STREAM.MSG.GET.PRESENCE", "presence.attest.*.v1", "presence.account.mattermost.*.v1",
        "$JS.API.INFO", "$JS.API.STREAM.CREATE.KV_BUS_DIRECTORY", "$JS.API.STREAM.INFO.KV_BUS_DIRECTORY", "$JS.API.DIRECT.GET.KV_BUS_DIRECTORY.>", "$KV.BUS_DIRECTORY.>",
        "$JS.API.CONSUMER.CREATE.KV_BUS_DIRECTORY.>", "$JS.API.CONSUMER.DELETE.KV_BUS_DIRECTORY.*" ]
      subscribe: [ "_INBOX.>" ] } },
    # The same without the right to publish attestations: a refused publish
    # must refuse the login (RT-6.41).
    { user: jsnoatt, password: pwd, permissions: {
      publish: [ "$JS.API.STREAM.INFO.PRESENCE", "$JS.API.STREAM.MSG.GET.PRESENCE" ]
      subscribe: [ "_INBOX.>" ] } } ] }
  OTHER { users: [ { user: other, password: pwd } ] }
}
authorization {
  timeout: 3s
  auth_callout {
    issuer: "`+issuerPub+`"
    account: AUTH
    auth_users: [ "`+svcPub+`", other, admin, jsmin, jsnoatt ]
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

	l := &lab{t: t, url: srv.ClientURL(), wsURL: srv.WebsocketURL(), realm: testrealm.New(t), chat: testrealm.NewChat("alice", "gus"),
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
	if edit != nil {
		edit(&cfg)
	}
	svc, err := New(cfg, issuer, xkey, l.realm.Verifier(l.chat), ch, NewBrake(l.brake, nil),
		slog.New(slog.NewJSONHandler(l.logs, nil)))
	if err != nil {
		t.Fatal(err)
	}
	l.svc = svc
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
	// owner is the owner the credential was made for; its token is in the
	// agent's presence subject.
	owner string
	errs  chan error
}

// presence is the subject the agent may publish its presence on.
func (a *agent) presence() string { return "presence." + ownerToken(a.owner) + "." + a.name }

func (l *lab) connect(owner, tok string, hello Hello, signer nkeys.KeyPair) (*agent, error) {
	return l.connectTimeout(owner, tok, hello, signer, 0)
}

// connectTimeout is connect with the client's own timeout, for a test that
// deliberately makes the server slow: nats.go's DefaultTimeout (2s) would
// otherwise end the login before the callout's deadline decides.
func (l *lab) connectTimeout(owner, tok string, hello Hello, signer nkeys.KeyPair,
	timeout time.Duration) (*agent, error) {
	l.t.Helper()
	return l.connectAt(l.url, owner, tok, hello, signer, timeout)
}

// connectAt is connectTimeout on another listener of the same server (a websocket one).
func (l *lab) connectAt(url, owner, tok string, hello Hello, signer nkeys.KeyPair,
	timeout time.Duration) (*agent, error) {
	l.t.Helper()
	host, _ := nkeys.CreateUser()
	pub, _ := host.PublicKey()
	if signer == nil {
		signer = host
	}
	hello.Token = tok
	raw, _ := jsonMarshal(hello)
	a := &agent{name: DefaultNamePrefix + name(owner, pub), owner: owner, errs: make(chan error, 16)}
	opts := []nats.Option{
		nats.Nkey(pub, func(nonce []byte) ([]byte, error) { return signer.Sign(nonce) }),
		nats.Token(string(raw)),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { a.errs <- err }),
		nats.MaxReconnects(0),
	}
	if timeout > 0 {
		opts = append(opts, nats.Timeout(timeout))
	}
	nc, err := nats.Connect(url, opts...)
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

// The credentials the callout issues allow standard connections only
// (AllowedConnectionTypes = STANDARD): a host cannot reach a websocket listener
// with one, so a home over WebSocket waits for that (RT-6.36, #51, #85).
func TestACredentialDoesNotAllowAWebsocketConnection(t *testing.T) {
	l := newLab(t)
	if l.wsURL == "" {
		t.Fatal("the lab's server has no websocket listener")
	}
	tok := l.realm.Token("alice", nil)
	_, err := l.connectAt(l.wsURL, "alice", tok, Hello{Home: "home"}, nil, 0)
	if err == nil {
		t.Fatal("a STANDARD credential reached a websocket listener")
	}
	// The listener type is the refusal, not a broken credential (RT-6.36, #85): assert the
	// authorization error, and that the same identity connects on the standard listener of
	// the same rig. A listener failing for another reason would fail here now.
	if !errors.Is(err, nats.ErrAuthorization) {
		t.Fatalf("the websocket listener refused with %v, want nats.ErrAuthorization", err)
	}
	a, err := l.connect("alice", tok, Hello{Home: "home"}, nil)
	if err != nil {
		t.Fatalf("the same identity on the standard listener: %v", err)
	}
	// Its own grant holds there: a publish on the agent's channel subject is not denied.
	if err := a.nc.Publish("bus.ch.home."+a.name, []byte(`{"v":1}`)); err != nil {
		t.Fatal(err)
	}
	if a.denied(t) {
		t.Fatal("the standard listener denied the credential's own publish")
	}
}

func TestAnAgentGetsItsNameAndOnlyItsChannels(t *testing.T) {
	l := newLab(t)
	tok := l.realm.Token("alice", nil)
	a, err := l.connect("alice", tok, Hello{Home: "home", Guests: []string{"elsewhere"}, Lobby: true}, nil)
	if err != nil {
		t.Fatal(err)
	}
	// Subscribing: only the own deliver subjects (reads go through push
	// consumers the login service creates); no bus subject, no inbox.
	for subj, want := range map[string]bool{
		"deliver." + a.name + ".ch.home": false, "deliver." + a.name + ".>": false,
		"deliver.agent-other.ch.home": true, "deliver.>": true,
		"bus.ch.home.*": true, "bus.ch.lobby.*": true, "bus.dm." + a.name + ".*": true,
		"bus.ch.elsewhere.*": true, "bus.dm.agent-other.*": true, "bus.>": true,
		"_INBOX.somehost.>": true, "presence.>": true,
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
		a.presence(): false, "presence.crown.home." + a.name: false, "presence.crown.lobby." + a.name: true, // a crown claim only in the home (RT-6.38)
		"presence." + ownerToken("alice") + ".agent-other": true, "presence.agent-other": true, "presence.-." + a.name: true, "presence.bob." + a.name: true, "presence.crown.home.agent-other": true, "presence.crown.elsewhere." + a.name: true,
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
	// Bob reads through a push consumer the admin connection creates (what
	// the login service does), subscribed by its literal deliver subject.
	_, js := l.admin()
	if _, err := js.AddStream(&nats.StreamConfig{Name: "BUS", Subjects: []string{"bus.>"}, NoAck: true}); err != nil {
		t.Fatal(err)
	}
	if _, err := js.AddConsumer("BUS", &nats.ConsumerConfig{Durable: b.name + "_ch_home", DeliverSubject: "deliver." + b.name + ".ch.home",
		FilterSubject: "bus.ch.home.*", AckPolicy: nats.AckNonePolicy, DeliverPolicy: nats.DeliverNewPolicy}); err != nil {
		t.Fatal(err)
	}
	sub, err := b.nc.SubscribeSync("deliver." + b.name + ".ch.home")
	if err != nil {
		t.Fatal(err)
	}
	_ = b.nc.Flush()
	if b.denied(t) {
		t.Fatal("bob's own deliver subject refused")
	}
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
