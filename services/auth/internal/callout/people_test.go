package callout

// RT-6.42 (ADR-016 step 2): the login service's part of people's posts on
// NATS: each agent reads a channel's people through a consumer of its own, the
// presence consumer carries the account records and the bridge's signal, only
// this service may write the account record, and an older token never replaces
// a newer one's.

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	gojwt "github.com/golang-jwt/jwt/v5"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

func filtersOfInfo(ci *nats.ConsumerInfo) []string {
	return append(append([]string{}, ci.Config.FilterSubjects...), ci.Config.FilterSubject)
}

// RT-6.42 step 8: one people consumer per granted channel, beside the
// channel's own, filtered on the people's subjects only.
func TestEachGrantedChannelGetsAPeopleConsumer(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	bob, err := c.login("bob", newHost(), Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	for _, ch := range []string{"home", "secret"} {
		ci := c.info("BUS", bob.name+"_hu_"+ch)
		if ci == nil {
			t.Fatalf("no people consumer for %s", ch)
		}
		cfg := ci.Config
		if cfg.FilterSubject != "bus.hu."+ch+".*" || len(cfg.FilterSubjects) != 0 ||
			cfg.DeliverSubject != "deliver."+bob.name+".hu."+ch || cfg.AckPolicy != nats.AckNonePolicy ||
			cfg.DeliverPolicy != nats.DeliverNewPolicy || cfg.FlowControl || cfg.Heartbeat != 0 ||
			cfg.Metadata["agent"] != bob.name || cfg.Metadata["owner"] != "bob" {
			t.Errorf("%s: %+v", ch, cfg)
		}
		// the channel's own consumer does not read the people
		if f := c.info("BUS", bob.name+"_ch_"+ch).Config.FilterSubject; f != "bus.ch."+ch+".*" {
			t.Errorf("channel consumer filter %s", f)
		}
	}
	if n := len(c.consumersOf(bob)); n != 6 {
		t.Errorf("%d consumers: %v", n, c.consumersOf(bob))
	}
	// The presence consumer adds the account records and, per granted channel,
	// the bridge's signal; nothing for a channel that is not granted.
	f := strings.Join(filtersOfInfo(c.info("PRESENCE", bob.name+"_presence")), " ")
	for _, want := range []string{"presence.account.mattermost.*.v1", "presence.bridge.home.v1", "presence.bridge.secret.v1"} {
		if !strings.Contains(f, want) {
			t.Errorf("presence filters lack %s: %s", want, f)
		}
	}
	if strings.Contains(f, "presence.bridge.*") {
		t.Errorf("presence reads the signal of every channel: %s", f)
	}
}

func TestAPeopleConsumerDeliversOnlyItsChannelsPeople(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	bob, err := c.login("bob", newHost(), Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	in := c.listen(bob, "deliver."+bob.name+".hu.home", "deliver."+bob.name+".ch.home")
	c.publish("bus.hu.home.alice", "a person")
	c.publish("bus.ch.home.agent-other", "an agent")
	c.publish("bus.hu.other.alice", "a person elsewhere")
	got := map[string]string{}
	for _, m := range take(in, time.Second) {
		got[m.Subject] = string(m.Data) // a push consumer keeps the stored message's subject
	}
	if len(got) != 2 || got["bus.hu.home.alice"] != "a person" || got["bus.ch.home.agent-other"] != "an agent" {
		t.Fatalf("got %v", got)
	}
}

func TestTheDroppedChannelsPeopleConsumerGoesToo(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	host := newHost()
	bob, err := c.login("bob", host, Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	c.chat.Channels["bob"] = []string{"home"} // removed from secret
	if _, err := c.login("bob", host, Hello{Home: "home", Guests: []string{"secret"}}); err != nil {
		t.Fatal(err)
	}
	if c.info("BUS", bob.name+"_hu_secret") != nil {
		t.Error("the people consumer of the dropped channel is still there")
	}
	if c.info("BUS", bob.name+"_hu_home") == nil {
		t.Error("the people consumer of the kept channel is gone")
	}
	f := strings.Join(filtersOfInfo(c.info("PRESENCE", bob.name+"_presence")), " ")
	if strings.Contains(f, "bridge.secret") {
		t.Errorf("presence still reads the bridge's signal of secret: %s", f)
	}
}

func TestAByeRemovesThePeopleConsumersToo(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	if err := c.mgr.WatchByes(ctx); err != nil {
		t.Fatal(err)
	}
	bob, err := c.login("bob", newHost(), Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := bob.nc.Publish(bob.presence(), []byte(`{"status":"bye"}`)); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "bob's consumers removed by his bye", func() bool { return len(c.consumersOf(bob)) == 0 })
}

// RT-6.42 step 8: a person is vouched for by the bridge's publish right alone.
func TestNoAgentMayPublishPeopleAccountRecordsOrTheBridgesSignal(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	bob, err := c.login("bob", newHost(), Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	for _, subj := range []string{
		"bus.hu.home.bob", "bus.hu.home." + bob.name, "bus.hu.home.-", "bus.hu.secret.alice",
		"presence.account.mattermost." + testrealm.ChatID("bob") + ".v1",
		"presence.bridge.home.v1", "presence.bridge.secret.v1",
	} {
		if err := bob.nc.Publish(subj, []byte("{}")); err != nil || !bob.denied(t) {
			t.Errorf("publish on %s was not refused (%v)", subj, err)
		}
	}
}

func accountLab(t *testing.T) (*lab, *fakeAttester, *directory.Store) {
	t.Helper()
	l := newLab(t)
	nc, _ := l.admin()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	dir, err := directory.Open(ctx5(t), js, "", false)
	if err != nil {
		t.Fatal(err)
	}
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	l.svc.SetAnchors(directory.Anchors{Store: dir})
	return l, f, dir
}

func (f *fakeAttester) records() (out []AccountRecord) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, p := range f.got {
		if strings.Contains(p.subject, ".account.") {
			var r AccountRecord
			if err := json.Unmarshal(p.body, &r); err == nil {
				out = append(out, r)
			}
		}
	}
	return out
}

// RT-6.42 step 3: a login whose chat account is proven publishes the
// account's record: its anchor, the approver flag of the token, the token's
// issue time and the credential's expiry.
func TestALoginWritesTheAccountRecord(t *testing.T) {
	l, f, _ := accountLab(t)
	before := time.Now().Unix()
	iat := time.Now().Add(-time.Minute).Unix()
	if _, err := l.connect("alice", l.realm.Token("alice", func(c gojwt.MapClaims) { c["iat"] = iat }), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	var subject string
	f.mu.Lock()
	for _, p := range f.got {
		if strings.Contains(p.subject, ".account.") {
			subject = p.subject
		}
	}
	f.mu.Unlock()
	id := testrealm.ChatID("alice")
	if subject != "presence.account.mattermost."+id+".v1" {
		t.Fatalf("subject %q", subject)
	}
	rs := f.records()
	if len(rs) != 1 {
		t.Fatalf("%d records", len(rs))
	}
	r := rs[0]
	if r.V != 1 || r.Account != id || r.Anchor != "alice" || r.Name != "alice" || !r.Approver || r.TokenIat != iat ||
		r.Iat < before || r.Exp-r.Iat != int64(Lifetime/time.Second) {
		t.Errorf("record %+v", r)
	}
}

func TestTheRecordSaysNoApproverWhenTheTokenHasNoGroup(t *testing.T) {
	l, f, _ := accountLab(t)
	if _, err := l.connect("alice", l.realm.Token("alice", func(c gojwt.MapClaims) { delete(c, "groups") }), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	if rs := f.records(); len(rs) != 1 || rs[0].Approver {
		t.Errorf("records %+v", rs)
	}
}

// RT-6.42 step 3: an older but still valid token never replaces the newer
// one's record, so it cannot flip the flag back after a removal from the group.
func TestAnOlderTokenNeverReplacesTheRecord(t *testing.T) {
	l, f, dir := accountLab(t)
	now := time.Now().Unix()
	login := func(iat int64, groups bool) {
		t.Helper()
		tok := l.realm.Token("alice", func(c gojwt.MapClaims) {
			c["iat"] = iat
			if !groups {
				delete(c, "groups")
			}
		})
		if _, err := l.connect("alice", tok, Hello{Home: "home"}, nil); err != nil {
			t.Fatal(err)
		}
	}
	login(now-60, true)
	login(now-10, false) // removed from the group since
	login(now-60, true)  // the old token, still valid, replayed
	rs := f.records()
	if len(rs) != 2 || !rs[0].Approver || rs[1].Approver || rs[1].TokenIat != now-10 {
		t.Fatalf("records %+v", rs)
	}
	// The same token again renews the record.
	login(now-10, false)
	if rs := f.records(); len(rs) != 3 || rs[2].Approver {
		t.Fatalf("records %+v", rs)
	}
	// The guard is the directory's: it keeps the newest iat per identity.
	if ok, err := dir.NewestToken(ctx5(t), "sub-alice", now-20); err != nil || ok {
		t.Errorf("an older iat passes the guard: %v %v", ok, err)
	}
}

// Without the directory there is no guard, and so no record: an account record
// could otherwise be replaced by an older token's.
func TestWithoutAGuardNoRecordIsWritten(t *testing.T) {
	l := newLab(t)
	f := &fakeAttester{}
	l.svc.SetAttester(f)
	l.svc.SetAnchors(fixedAnchor("alice"))
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	if rs := f.records(); len(rs) != 0 {
		t.Errorf("records %+v", rs)
	}
}

func TestAFailedRecordRefusesTheLoginLikeAFailedAttestation(t *testing.T) {
	l, f, _ := accountLab(t)
	f.failOn = ".account."
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err == nil {
		t.Fatal("the login passed without its account record")
	}
}

// A chat account that logs in otherwise than over OpenID Connect is not proven
// to be the identity's: no record.
func TestAPasswordAccountGetsNoRecord(t *testing.T) {
	l, f, _ := accountLab(t)
	l.chat.Users["pw"] = tokenUserPassword("pw")
	l.chat.Channels["pw"] = []string{"home"}
	if _, err := l.connect("pw", l.realm.Token("pw", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
	if rs := f.records(); len(rs) != 0 {
		t.Errorf("records %+v", rs)
	}
}

func tokenUserPassword(name string) token.User {
	return token.User{Username: name, ID: testrealm.ChatID(name), AuthKnown: true}
}
