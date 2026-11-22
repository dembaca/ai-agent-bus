package callout

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

func (l *lab) admin() (*nats.Conn, nats.JetStreamContext) {
	l.t.Helper()
	nc, err := nats.Connect(l.url, nats.UserInfo("admin", "pwd"))
	if err != nil {
		l.t.Fatal(err)
	}
	l.t.Cleanup(nc.Close)
	js, err := nc.JetStream()
	if err != nil {
		l.t.Fatal(err)
	}
	return nc, js
}

func (l *lab) twoAgents() (alice, bob *agent) {
	l.t.Helper()
	l.chat.Users["bob"] = token.User{Username: "bob", AuthKnown: true, AuthService: token.OpenIDService, AuthData: "sub-bob"}
	l.chat.Channels["alice"] = []string{"home"}
	l.chat.Channels["bob"] = []string{"home", "secret"}
	a, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil)
	if err != nil {
		l.t.Fatal(err)
	}
	b, err := l.connect("bob", l.realm.Token("bob", nil), Hello{Home: "home", Guests: []string{"secret"}}, nil)
	if err != nil {
		l.t.Fatal(err)
	}
	return a, b
}

// silent says nothing arrives on sub within a short while.
func silent(t *testing.T, sub *nats.Subscription) {
	t.Helper()
	if m, err := sub.NextMsg(300 * time.Millisecond); err == nil {
		t.Fatalf("arrived on %s: %q (reply %q)", m.Subject, m.Data, m.Reply)
	}
}

// The review of PR #8 made JetStream and the key-value store answer onto
// another agent's subject (NATS checks only a publish's subject, never its
// reply). With no JetStream or key-value subject in the credential, every
// such call is refused and nothing reaches the victim's subjects.
func TestNoServiceCanBeMadeToAnswerUnderAnotherName(t *testing.T) {
	l := newLab(t)
	_, js := l.admin()
	if _, err := js.AddStream(&nats.StreamConfig{Name: "BUS", Subjects: []string{"bus.>"}, NoAck: true}); err != nil {
		t.Fatal(err)
	}
	kv, err := js.CreateKeyValue(&nats.KeyValueConfig{Bucket: "presence"})
	if err != nil {
		t.Fatal(err)
	}
	_, _ = kv.Put("crown-home", []byte("lease"))
	a, b := l.twoAgents()
	if _, err := js.AddConsumer("BUS", &nats.ConsumerConfig{Durable: a.name, AckPolicy: nats.AckExplicitPolicy,
		FilterSubject: "bus.ch.home.*"}); err != nil {
		t.Fatal(err)
	}
	// Agents cannot read the bus subjects themselves any more: the victims'
	// subjects are watched on the admin connection.
	adm, _ := l.admin()
	home, _ := adm.SubscribeSync("bus.ch.home.*")
	secret, _ := adm.SubscribeSync("bus.ch.secret.*")
	dm, _ := adm.SubscribeSync("bus.dm." + b.name + ".*")
	_ = adm.Flush()

	victims := []string{"bus.ch.home.agent-victim", "bus.ch.secret.agent-victim", "bus.dm." + b.name + ".agent-victim"}
	calls := []string{
		"$KV.presence." + a.name,
		"$JS.API.DIRECT.GET.KV_presence.$KV.presence." + a.name,
		"$JS.API.CONSUMER.MSG.NEXT.BUS." + a.name,
		"$JS.API.CONSUMER.INFO.BUS." + a.name,
		"$JS.API.STREAM.INFO.BUS",
		"$JS.ACK.BUS." + a.name + ".1.1.1.1.1",
		"$SYS.REQ.SERVER.PING",
	}
	for _, call := range calls {
		for _, reply := range victims {
			if err := a.nc.PublishRequest(call, reply, []byte(`{"batch":1}`)); err != nil {
				t.Fatal(err)
			}
			if !a.denied(t) {
				t.Errorf("publish %s: not denied", call)
			}
		}
	}
	// A rollup on its own key would have emptied the bucket.
	m := nats.NewMsg("$KV.presence." + a.name)
	m.Header.Set("Nats-Rollup", "all")
	_ = a.nc.PublishMsg(m)
	if !a.denied(t) {
		t.Error("rollup not denied")
	}
	if _, err := kv.Get("crown-home"); err != nil {
		t.Fatalf("the crown lease is gone: %v", err)
	}
	// Its own post with a reply set: a stream without acks answers nobody.
	if err := a.nc.PublishRequest("bus.ch.home."+a.name, victims[1], []byte("own post")); err != nil {
		t.Fatal(err)
	}
	if m, err := home.NextMsg(2 * time.Second); err != nil || m.Subject != "bus.ch.home."+a.name {
		t.Fatalf("own post: %v, %v", m, err)
	}
	silent(t, home)
	silent(t, secret)
	silent(t, dm)
}

// A stream that acknowledges posts answers onto the post's reply subject,
// with the stream's own rights: that is why the bus's streams must be
// created with no_ack (README).
func TestAStreamThatAcksWouldCarryAForgedReply(t *testing.T) {
	l := newLab(t)
	_, js := l.admin()
	if _, err := js.AddStream(&nats.StreamConfig{Name: "BUS", Subjects: []string{"bus.>"}}); err != nil {
		t.Fatal(err)
	}
	a, _ := l.twoAgents()
	adm, _ := l.admin()
	secret, _ := adm.SubscribeSync("bus.ch.secret.*")
	_ = adm.Flush()
	if err := a.nc.PublishRequest("bus.ch.home."+a.name, "bus.ch.secret.agent-victim", []byte("x")); err != nil {
		t.Fatal(err)
	}
	if _, err := secret.NextMsg(2 * time.Second); err != nil {
		t.Fatalf("no ack arrived (if NATS stopped acking onto replies, relax the README): %v", err)
	}
}

func TestMoreRefusedSubjects(t *testing.T) {
	l := newLab(t)
	a, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Guests: []string{"lobby"}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	other, _ := nkeys.CreateUser()
	otherPub, _ := other.PublicKey()
	for subj, want := range map[string]bool{
		// The bus subjects and every inbox are closed to an agent: it reads
		// only its own deliver subjects.
		"bus.ch.lobby.*": true, "bus.dm." + a.name + ".*": true, "_INBOX." + otherPub + ".x": true, "_INBOX.>": true,
		"$KV.presence.x": true, "$SYS.>": true, "$JS.API.>": true, "$JS.API.CONSUMER.CREATE.BUS": true,
		"presence.>": true, "deliver.agent-other.x": true, "deliver.>": true,
		"deliver." + a.name + ".ch.lobby": false, "deliver." + a.name + ".>": false,
	} {
		_, _ = a.nc.SubscribeSync(subj)
		if got := a.denied(t); got != want {
			t.Errorf("subscribe %s: denied %v, want %v", subj, got, want)
		}
	}
}

func TestAHostWithoutAnNkeyIsRefused(t *testing.T) {
	l := newLab(t)
	raw, _ := jsonMarshal(Hello{Token: l.realm.Token("alice", nil), Home: "home"})
	_, err := nats.Connect(l.url, nats.Token(string(raw)), nats.MaxReconnects(0))
	if err == nil || !strings.Contains(err.Error(), "Authorization Violation") {
		t.Fatalf("connected or wrong error: %v", err)
	}
	if !strings.Contains(l.logs.String(), ErrNoNkey.Error()) {
		t.Fatalf("log:\n%s", l.logs.String())
	}
}

type slowChannels struct {
	Channels
	d time.Duration
}

func (s slowChannels) Memberships(ctx context.Context, u string, chs []string) (map[string]bool, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(s.d):
	}
	return s.Channels.Memberships(ctx, u, chs)
}

type failingChannels struct{ Channels }

func (failingChannels) Memberships(context.Context, string, []string) (map[string]bool, error) {
	return nil, errors.New("HTTP 500")
}

func TestLoginsAreAnsweredAtOnce(t *testing.T) {
	l := newLabWith(t, func(c Channels) Channels { return slowChannels{c, 600 * time.Millisecond} }, 0)
	var wg sync.WaitGroup
	errs := make(chan error, 6)
	for range 6 {
		wg.Go(func() {
			_, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil)
			errs <- err
		})
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("a login fell into the timeout: %v", err)
		}
	}
}

func TestALookupThatFailsOrRunsOutRefusesTheWholeLogin(t *testing.T) {
	for name, wrap := range map[string]func(Channels) Channels{
		"fails":    func(c Channels) Channels { return failingChannels{c} },
		"runs out": func(c Channels) Channels { return slowChannels{c, 2 * time.Second} },
	} {
		t.Run(name, func(t *testing.T) {
			l := newLabWith(t, wrap, 500*time.Millisecond)
			start := time.Now()
			_, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Lobby: true}, nil)
			if err == nil {
				t.Fatal("a partial credential was issued")
			}
			if time.Since(start) > 2*time.Second {
				t.Fatalf("took %s", time.Since(start))
			}
			if !strings.Contains(l.logs.String(), "membership") {
				t.Fatalf("log:\n%s", l.logs.String())
			}
		})
	}
}

func TestCheckToken(t *testing.T) {
	for _, ok := range []string{"home", "a-b_c", "x1"} {
		if checkToken(ok) != nil {
			t.Errorf("%q refused", ok)
		}
	}
	for _, bad := range []string{"", "*", ">", "a.b", "Home", "a\nb", "a b", "-x", strings.Repeat("a", 65)} {
		if checkToken(bad) == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestNamesAre128Bits(t *testing.T) {
	n := name("alice", "UABC")
	if len(n) != 26 || checkToken(n) != nil || n == name("bob", "UABC") || n != name("alice", "UABC") {
		t.Fatalf("name %q", n)
	}
}

// unit is a service without a server, for the request and response details.
func unit(t *testing.T, xkey nkeys.KeyPair) (*Service, nkeys.KeyPair, *testrealm.Realm) {
	t.Helper()
	issuer, _ := nkeys.CreateAccount()
	r := testrealm.New(t)
	chat := testrealm.NewChat("alice")
	chat.Channels["alice"] = []string{"home"}
	s, err := New(Config{Account: "BUS", Lobby: "lobby"}.WithDefaults(), issuer, xkey, r.Verifier(chat), chat, nil,
		slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	return s, issuer, r
}

func serverRequest(t *testing.T, srv nkeys.KeyPair, edit func(*jwt.AuthorizationRequestClaims)) []byte {
	t.Helper()
	user, _ := nkeys.CreateUser()
	userPub, _ := user.PublicKey()
	srvPub, _ := srv.PublicKey()
	req := jwt.NewAuthorizationRequestClaims(srvPub)
	req.Audience = requestAudience
	req.Server.ID = srvPub
	req.UserNkey = userPub
	req.Expires = time.Now().Add(2 * time.Second).Unix()
	if edit != nil {
		edit(req)
	}
	out, err := req.Encode(srv)
	if err != nil {
		t.Fatal(err)
	}
	return []byte(out)
}

func TestOnlyAServersOwnFreshRequestIsAnswered(t *testing.T) {
	s, _, _ := unit(t, nil)
	srv, _ := nkeys.CreateServer()
	other, _ := nkeys.CreateServer()
	otherPub, _ := other.PublicKey()
	if _, err := s.request(serverRequest(t, srv, nil), time.Now()); err != nil {
		t.Fatalf("a fresh request: %v", err)
	}
	for name, data := range map[string][]byte{
		"for another server": serverRequest(t, srv, func(r *jwt.AuthorizationRequestClaims) { r.Server.ID = otherPub }),
		"another audience":   serverRequest(t, srv, func(r *jwt.AuthorizationRequestClaims) { r.Audience = "x" }),
		"without expiry":     serverRequest(t, srv, func(r *jwt.AuthorizationRequestClaims) { r.Expires = 0 }),
		"expired":            serverRequest(t, srv, func(r *jwt.AuthorizationRequestClaims) { r.Expires = time.Now().Add(-time.Second).Unix() }),
	} {
		if _, err := s.request(data, time.Now()); err == nil {
			t.Errorf("%s: answered", name)
		}
	}
}

func TestAPlainRequestIsRefusedWhenAnXkeyIsSet(t *testing.T) {
	xkey, _ := nkeys.CreateCurveKeys()
	s, _, _ := unit(t, xkey)
	if _, _, err := s.open(&nats.Msg{Data: []byte("plain"), Header: nats.Header{}}); err == nil ||
		!strings.Contains(err.Error(), "plain request") {
		t.Fatalf("a plain request: %v", err)
	}
	plain, _, _ := unit(t, nil)
	if _, _, err := plain.open(&nats.Msg{Data: []byte("x"), Header: nats.Header{"Nats-Server-Xkey": {"X"}}}); err == nil {
		t.Fatal("an encrypted request was read without an xkey")
	}
}

func TestTheUserJWT(t *testing.T) {
	s, issuer, realm := unit(t, nil)
	issuerPub, _ := issuer.PublicKey()
	srv, _ := nkeys.CreateServer()
	host, _ := nkeys.CreateUser()
	hostPub, _ := host.PublicKey()
	nonce := "nonce-123"
	sig, _ := host.Sign([]byte(nonce))
	req, err := s.request(serverRequest(t, srv, func(q *jwt.AuthorizationRequestClaims) {
		q.ClientInformation.Nonce = nonce
		q.ConnectOptions.Nkey = hostPub
		q.ConnectOptions.SignedNonce = base64.RawURLEncoding.EncodeToString(sig)
	}), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	raw, _ := jsonMarshal(Hello{Token: realm.Token("alice", nil), Home: "home", Guests: []string{"lobby"}})
	req.ConnectOptions.Token = string(raw)
	userJWT, _, err := s.authorize(context.Background(), req, now)
	if err != nil {
		t.Fatal(err)
	}
	uc, err := jwt.DecodeUserClaims(userJWT)
	if err != nil {
		t.Fatal(err)
	}
	if uc.Issuer != issuerPub || uc.Subject != req.UserNkey || uc.Audience != "BUS" {
		t.Fatalf("issuer %s subject %s audience %s", uc.Issuer, uc.Subject, uc.Audience)
	}
	if uc.Expires != now.Add(Lifetime).Unix() || uc.IssuedAt != now.Unix() {
		t.Fatalf("expires %d, issued %d", uc.Expires, uc.IssuedAt)
	}
	if uc.Limits.Subs != maxSubs || uc.Limits.Payload != maxPayload ||
		!uc.AllowedConnectionTypes.Contains(jwt.ConnectionTypeStandard) || len(uc.AllowedConnectionTypes) != 1 {
		t.Fatalf("limits %+v, connection types %v", uc.Limits, uc.AllowedConnectionTypes)
	}
	if !uc.Pub.Allow.Contains("bus.ch.lobby."+uc.Name) || !uc.Pub.Allow.Contains("bus.ch.home."+uc.Name) {
		t.Fatalf("channels %v", uc.Pub.Allow)
	}
}

type recordingChannels struct {
	Channels
	mu    sync.Mutex
	asked []string
}

func (r *recordingChannels) Memberships(ctx context.Context, u string, chs []string) (map[string]bool, error) {
	r.mu.Lock()
	r.asked = append(r.asked, chs...)
	r.mu.Unlock()
	return r.Channels.Memberships(ctx, u, chs)
}

func TestOnlyValidChannelNamesReachTheChat(t *testing.T) {
	rec := &recordingChannels{}
	l := newLabWith(t, func(c Channels) Channels { rec.Channels = c; return rec }, 0)
	guests := []string{"a.b", "*", ">", "Home", "home", "lobby"}
	for i := range MaxGuests + 2 {
		guests = append(guests, fmt.Sprintf("g%d", i))
	}
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home", Guests: guests}, nil); err != nil {
		t.Fatal(err)
	}
	rec.mu.Lock()
	defer rec.mu.Unlock()
	for _, ch := range rec.asked {
		if checkToken(ch) != nil || ch == "lobby" {
			t.Errorf("asked the chat about %q", ch)
		}
	}
	if len(rec.asked) > MaxGuests+1 {
		t.Errorf("asked about %d channels, at most %d", len(rec.asked), MaxGuests+1)
	}
	if !strings.Contains(l.logs.String(), "more than 16 guest channels") {
		t.Errorf("log:\n%s", l.logs.String())
	}
}

func TestTheServersExpiryCapsTheDeadline(t *testing.T) {
	now := time.Unix(1000, 0)
	if d := deadline(now, 1500*time.Millisecond, 1001); !d.Equal(time.Unix(1001, 0).Add(-answerMargin)) {
		t.Fatalf("capped by exp: %s", d.Sub(now))
	}
	if d := deadline(now, 1500*time.Millisecond, 1010); !d.Equal(now.Add(1500 * time.Millisecond)) {
		t.Fatalf("the timeout: %s", d.Sub(now))
	}
}

func TestTheBrakeComparesOwnersInLowerCase(t *testing.T) {
	path := filepath.Join(t.TempDir(), "brake")
	if err := os.WriteFile(path, []byte("Alice\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	var warned []string
	b := NewBrake(path, func(line string) { warned = append(warned, line) })
	for _, owner := range []string{"alice", "ALICE", "Alice"} {
		if b.Check(owner) != ErrStopped {
			t.Errorf("%s not stopped", owner)
		}
	}
	if b.Check("bob") != nil {
		t.Error("bob stopped")
	}
	if err := os.WriteFile(path, []byte("not a name!\nbob\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	later := time.Now().Add(time.Minute)
	_ = os.Chtimes(path, later, later)
	if b.Check("bob") != ErrStopped || len(warned) != 1 {
		t.Fatalf("bob: %v, warned %q", b.Check("bob"), warned)
	}
}
