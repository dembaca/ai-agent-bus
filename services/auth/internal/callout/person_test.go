package callout

// The console on the bus as its operator (RT-6.43, the addition of 2026-11-25
// to ADR-015, points 1 to 6): a person's credential for the console's own
// Keycloak client, its rights, its consumers, and every refusal.

import (
	"context"
	"encoding/base64"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
	natsjwt "github.com/nats-io/jwt/v2"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

const (
	consoleClient = "bus-console"
	operators     = "bus-operators"
)

// fakePersons is the directory's part: anchors by sub, visible channels.
type fakePersons struct {
	mu      sync.Mutex
	anchors map[string]string
	visible []string
}

func (f *fakePersons) Anchor(_ context.Context, sub string) (string, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	a, ok := f.anchors[sub]
	return a, ok, nil
}

func (f *fakePersons) Visible(context.Context, string) ([]string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return slices.Clone(f.visible), nil
}

func (f *fakePersons) see(chs ...string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.visible = chs
}

func personLab(t *testing.T) (*cl, *fakePersons) {
	t.Helper()
	l := newLabCfg(t, nil, 2800*time.Millisecond, func(c *Config) {
		c.Console = Console{ClientID: consoleClient, OperatorGroup: operators}.withDefaults()
	})
	c := &cl{lab: l}
	c.nc, c.js = l.admin()
	var err error
	if c.mgr, err = consumers.New(c.nc, consumers.Config{Bus: "bus", Presence: "presence", Log: slogTo(l.logs)}); err != nil {
		t.Fatal(err)
	}
	if err := c.mgr.EnsureStreams(ctx5(t)); err != nil {
		t.Fatal(err)
	}
	l.svc.SetConsumers(c.mgr)
	cfg := l.realm.Config()
	cfg.ClientID, cfg.ApproverGroup, cfg.OtherClient = consoleClient, "", testrealm.Client
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	pv, err := token.New(ctx, cfg, l.chat)
	if err != nil {
		t.Fatal(err)
	}
	fp := &fakePersons{anchors: map[string]string{"sub-ann": "ann.k"}, visible: []string{"home"}}
	l.svc.SetPersons(pv, fp)
	return c, fp
}

// consoleToken is an access token of the console's client for owner.
func (c *cl) consoleToken(owner string, edit func(jwt.MapClaims)) string {
	return c.realm.Token(owner, func(m jwt.MapClaims) {
		m["azp"], m["aud"], m["groups"] = consoleClient, []string{consoleClient}, []string{operators}
		if edit != nil {
			edit(m)
		}
	})
}

// operator connects as the console's server does for one login: its key, the
// bare access token.
func (c *cl) operator(tok string, key nkeys.KeyPair) (*agent, error) {
	c.t.Helper()
	pub, _ := key.PublicKey()
	a := &agent{name: "person-ann~k", errs: make(chan error, 16)}
	nc, err := nats.Connect(c.url, nats.Nkey(pub, key.Sign), nats.Token(tok),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { a.errs <- err }), nats.MaxReconnects(0))
	if err != nil {
		return nil, err
	}
	c.t.Cleanup(nc.Close)
	a.nc = nc
	return a, nil
}

func loginToken(key nkeys.KeyPair) string {
	pub, _ := key.PublicKey()
	return strings.ToLower(pub[1:11])
}

// The user JWT itself: the name from the anchor, 15 minutes, no publish,
// subscribe only on the login's deliver subjects.
func TestAnOperatorsCredentialReadsOnlyAndLivesFifteenMinutes(t *testing.T) {
	c, _ := personLab(t)
	srv, _ := nkeys.CreateServer()
	key, _ := nkeys.CreateUser()
	pub, _ := key.PublicKey()
	nonce := "nonce-console"
	sig, _ := key.Sign([]byte(nonce))
	req, err := c.svc.request(serverRequest(t, srv, func(q *natsjwt.AuthorizationRequestClaims) {
		q.ClientInformation.Nonce = nonce
		q.ConnectOptions.Nkey = pub
		q.ConnectOptions.SignedNonce = base64.RawURLEncoding.EncodeToString(sig)
		q.ConnectOptions.Token = c.consoleToken("ann", nil)
	}), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	userJWT, _, err := c.svc.authorize(context.Background(), req, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	uc, err := natsjwt.DecodeUserClaims(userJWT)
	if err != nil {
		t.Fatal(err)
	}
	login := strings.ToLower(pub[1:11])
	p := uc.Permissions
	if uc.Name != "person-ann~k" || uc.Expires-uc.IssuedAt != int64(Lifetime.Seconds()) || Lifetime != 15*time.Minute ||
		uc.Subject != req.UserNkey || uc.Audience != "BUS" {
		t.Fatalf("name %q, %d s, subject %s, audience %s", uc.Name, uc.Expires-uc.IssuedAt, uc.Subject, uc.Audience)
	}
	if len(p.Pub.Allow) != 0 || !slices.Equal(p.Pub.Deny, natsjwt.StringList{">"}) ||
		!slices.Equal(p.Sub.Allow, natsjwt.StringList{"deliver.person-ann~k." + login + ".>"}) || len(p.Sub.Deny) != 0 || p.Resp != nil {
		t.Fatalf("permissions %+v", p)
	}
}

// Through the real server: the login's two consumers deliver presence, ask
// records and STATS lines; every other right is refused.
func TestAnOperatorReadsThroughTheirOwnConsumersOnly(t *testing.T) {
	c, _ := personLab(t)
	key := newHost()
	a, err := c.operator(c.consoleToken("ann", nil), key)
	if err != nil {
		t.Fatal(err)
	}
	login := loginToken(key)
	pres, stats := consumers.PersonConsumerNames("person-ann~k", login)
	ci := c.info("PRESENCE", pres)
	if ci == nil {
		t.Fatal("no presence consumer")
	}
	want := []string{"presence.*.*", "presence.attest.*.v1", "presence.crown.home.*", "presence.member.home.*", "presence.ask.home.*"}
	if !slices.Equal(ci.Config.FilterSubjects, want) || ci.Config.InactiveThreshold != 15*time.Minute ||
		ci.Config.DeliverPolicy != nats.DeliverLastPerSubjectPolicy || ci.Config.AckPolicy != nats.AckNonePolicy ||
		ci.Config.DeliverSubject != "deliver.person-ann~k."+login+".presence" || ci.Config.Metadata["agent"] != "" {
		t.Fatalf("presence consumer %+v", ci.Config)
	}
	si := c.info("STATS", stats)
	if si == nil || si.Config.InactiveThreshold != 15*time.Minute || si.Config.DeliverPolicy != nats.DeliverByStartTimePolicy ||
		si.Config.OptStartTime == nil || time.Since(*si.Config.OptStartTime) < 7*24*time.Hour-time.Minute ||
		si.Config.DeliverSubject != "deliver.person-ann~k."+login+".stats" {
		t.Fatalf("stats consumer %+v", si)
	}

	got := c.listen(a, "deliver.person-ann~k."+login+".presence", "deliver.person-ann~k."+login+".stats")
	for _, s := range []string{"presence.alice.agent-x", "presence.ask.home.agent-x", "presence.ask.secret.agent-y", "stats.agent-x"} {
		if err := c.nc.Publish(s, []byte(`{"v":1}`)); err != nil {
			t.Fatal(err)
		}
	}
	seen := map[string]bool{}
	deadline := time.After(3 * time.Second)
	for len(seen) < 3 {
		select {
		case m := <-got:
			seen[m.Subject] = true
		case <-deadline:
			t.Fatalf("delivered %v", seen)
		}
	}
	if !seen["presence.alice.agent-x"] || !seen["presence.ask.home.agent-x"] || !seen["stats.agent-x"] || seen["presence.ask.secret.agent-y"] {
		t.Fatalf("delivered %v", seen)
	}

	// No publish anywhere: no presence, no post, no JetStream API (no other
	// consumer, no pull, no direct get, no delete), no inbox.
	for _, s := range []string{"presence.ann~k.person-ann~k", "bus.ch.home.person-ann~k", "stats.person-ann~k",
		"$JS.API.CONSUMER.CREATE.PRESENCE.other", "$JS.API.CONSUMER.MSG.NEXT.PRESENCE." + pres,
		"$JS.API.DIRECT.GET.PRESENCE", "$JS.API.CONSUMER.DELETE.PRESENCE." + pres, "$JS.API.STREAM.INFO.PRESENCE",
		"_INBOX.x", "deliver.person-ann~k." + login + ".presence"} {
		if err := a.nc.Publish(s, nil); err != nil {
			t.Fatal(err)
		}
		if !a.denied(t) {
			t.Errorf("publish %s allowed", s)
		}
	}
	for _, s := range []string{"deliver.agent-x.>", "deliver.person-ann~k.other.>", "presence.>", "_INBOX.>", "stats.>"} {
		if _, err := a.nc.Subscribe(s, func(*nats.Msg) {}); err != nil {
			t.Fatal(err)
		}
		if !a.denied(t) {
			t.Errorf("subscribe %s allowed", s)
		}
	}
}

// A renewal keeps both consumers; changed channels make the presence one
// anew; a second login of the same person has consumers of its own.
func TestRenewalsAndSecondLoginsOfAnOperator(t *testing.T) {
	c, fp := personLab(t)
	key := newHost()
	if _, err := c.operator(c.consoleToken("ann", nil), key); err != nil {
		t.Fatal(err)
	}
	pres, stats := consumers.PersonConsumerNames("person-ann~k", loginToken(key))
	before, beforeStats := c.info("PRESENCE", pres).Created, c.info("STATS", stats).Created
	if _, err := c.operator(c.consoleToken("ann", nil), key); err != nil {
		t.Fatal(err)
	}
	if c.info("PRESENCE", pres).Created != before || c.info("STATS", stats).Created != beforeStats {
		t.Fatal("a renewal made the consumers anew")
	}
	fp.see("home", "lobby")
	if _, err := c.operator(c.consoleToken("ann", nil), key); err != nil {
		t.Fatal(err)
	}
	ci := c.info("PRESENCE", pres)
	if ci.Created == before || !slices.Contains(ci.Config.FilterSubjects, "presence.ask.lobby.*") ||
		c.info("STATS", stats).Created != beforeStats {
		t.Fatalf("changed channels: %+v", ci.Config.FilterSubjects)
	}
	other := newHost()
	if _, err := c.operator(c.consoleToken("ann", nil), other); err != nil {
		t.Fatal(err)
	}
	p2, _ := consumers.PersonConsumerNames("person-ann~k", loginToken(other))
	if p2 == pres || c.info("PRESENCE", p2) == nil || c.info("PRESENCE", pres) == nil {
		t.Fatal("a second login shares or drops consumers")
	}
}

func TestOperatorsRefused(t *testing.T) {
	c, _ := personLab(t)
	for name, tc := range map[string]struct {
		tok, reason string
	}{
		"the bus's token, bare": {c.realm.Token("ann", nil), ErrRequest.Error()},
		"azp console, aud the bus's": {c.consoleToken("ann", func(m jwt.MapClaims) { m["aud"] = []string{testrealm.Client} }),
			token.ErrAudience.Error()},
		"aud naming both": {c.consoleToken("ann", func(m jwt.MapClaims) { m["aud"] = []string{consoleClient, testrealm.Client} }),
			token.ErrAudience.Error()},
		"not an operator": {c.consoleToken("ann", func(m jwt.MapClaims) { m["groups"] = []string{testrealm.Group} }), ErrNotOperator.Error()},
		"no group claim":  {c.consoleToken("ann", func(m jwt.MapClaims) { delete(m, "groups") }), ErrNotOperator.Error()},
		"unbound sub":     {c.consoleToken("bob", nil), ErrUnbound.Error()},
		"expired":         {c.consoleToken("ann", func(m jwt.MapClaims) { m["exp"] = time.Now().Add(-time.Hour).Unix() }), token.ErrExpired.Error()},
		"other issuer":    {c.consoleToken("ann", func(m jwt.MapClaims) { m["iss"] = "https://evil.example/realms/test" }), token.ErrIssuer.Error()},
		"in an agent hello": {func() string {
			raw, _ := jsonMarshal(Hello{Token: c.consoleToken("ann", nil), Home: "home"})
			return string(raw)
		}(), token.ErrAudience.Error()},
	} {
		seen := len(c.logs.String())
		if _, err := c.operator(tc.tok, newHost()); err == nil {
			t.Errorf("%s: connected", name)
		}
		if !strings.Contains(c.logs.String()[seen:], tc.reason) {
			t.Errorf("%s: no %q in the log", name, tc.reason)
		}
	}
	for name := range c.js.ConsumerNames("PRESENCE") {
		if strings.HasPrefix(name, "person-") {
			t.Fatalf("a person's consumer after refusals: %s", name)
		}
	}
}

func TestTheConsolesConfigurationIsChecked(t *testing.T) {
	base := Config{Account: "BUS"}.WithDefaults()
	if base.Console.PersonPrefix != DefaultPersonPrefix || base.check() != nil {
		t.Fatalf("defaults: %+v", base.Console)
	}
	for name, con := range map[string]Console{
		"no operators' group":     {ClientID: "c", PersonPrefix: "person-"},
		"prefix = name prefix":    {ClientID: "c", OperatorGroup: "g", PersonPrefix: "agent-"},
		"prefix starts the other": {ClientID: "c", OperatorGroup: "g", PersonPrefix: "a-"},
		"other starts the prefix": {ClientID: "c", OperatorGroup: "g", PersonPrefix: "agent-p-"},
		"not ending in -":         {ClientID: "c", OperatorGroup: "g", PersonPrefix: "brid"},
		"a dot":                   {ClientID: "c", OperatorGroup: "g", PersonPrefix: "p.x-"},
	} {
		c := base
		c.Console = con
		if name == "prefix starts the other" {
			c.NamePrefix = "a-b-"
		}
		if c.check() == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	c := base
	c.Console = Console{ClientID: "c", OperatorGroup: "g", PersonPrefix: "person-"}
	if err := c.check(); err != nil {
		t.Fatal(err)
	}
}

// The login service's JetStream user makes an operator's consumers with the
// rights the README documents (jsmin): PRESENCE's and STATS' consumer API.
func TestTheDocumentedMinimalRightsMakeAnOperatorsConsumers(t *testing.T) {
	l := newLabCfg(t, nil, 2800*time.Millisecond, nil)
	nc, err := nats.Connect(l.url, nats.UserInfo("jsmin", "pwd"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	mgr, err := consumers.New(nc, consumers.Config{Bus: "bus", Presence: "presence", Log: slogTo(l.logs)})
	if err != nil {
		t.Fatal(err)
	}
	if err := mgr.EnsureStreams(ctx5(t)); err != nil {
		t.Fatal(err)
	}
	for range 2 { // made, then kept
		if err := mgr.EnsurePerson(ctx5(t), "person-ann~k", "abcdefghij", []string{"home"}); err != nil {
			t.Fatalf("EnsurePerson: %v", err)
		}
	}
	if err := mgr.EnsurePerson(ctx5(t), "person-ann~k", "abcdefghij", []string{"home", "lobby"}); err != nil {
		t.Fatalf("EnsurePerson with other channels: %v", err)
	}
	for _, bad := range [][2]string{{"person.x", "abcdefghij"}, {"person-x", "ABCDEFGHIJ"}, {"person-x", "abc"}, {"person-*", "abcdefghij"}} {
		if mgr.EnsurePerson(ctx5(t), bad[0], bad[1], nil) == nil {
			t.Errorf("EnsurePerson(%q, %q) accepted", bad[0], bad[1])
		}
	}
}
