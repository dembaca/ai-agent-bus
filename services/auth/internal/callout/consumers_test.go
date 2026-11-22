package callout

// Consumer management in the login service (ADR-014, point 16, RT-6.35): the streams
// with their safe settings, the consumers a login makes, keeps and removes,
// history from a sequence within 14 days, sign-off, the emergency brake, and
// the whole way from a login through the real callout to a delivered post.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// cl is a lab with the consumer manager wired into the service, on the
// tenant account's admin connection (the one holder of JetStream API rights).
type cl struct {
	*lab
	nc  *nats.Conn
	js  nats.JetStreamContext
	mgr *consumers.Manager
}

func newConsumerLab(t *testing.T, edit func(*consumers.Config)) *cl {
	t.Helper()
	l := newLabCfg(t, nil, 2800*time.Millisecond, nil) // the disk makes JetStream calls slow under -race
	c := &cl{lab: l}
	c.nc, c.js = l.admin()
	cfg := consumers.Config{Bus: "bus", Presence: "presence", Log: slogTo(l.logs)}
	if edit != nil {
		edit(&cfg)
	}
	var err error
	if c.mgr, err = consumers.New(c.nc, cfg); err != nil {
		t.Fatal(err)
	}
	if err := c.mgr.EnsureStreams(ctx5(t)); err != nil {
		t.Fatal(err)
	}
	l.svc.SetConsumers(c.mgr)
	return c
}

func ctx5(t *testing.T) context.Context {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	return ctx
}

// login connects a host (the nkey is the agent's identity, so a renewal
// is a second login with the same key).
func (c *cl) login(owner string, host nkeys.KeyPair, hello Hello) (*agent, error) {
	c.t.Helper()
	pub, _ := host.PublicKey()
	hello.Token = c.realm.Token(owner, nil)
	raw, _ := json.Marshal(hello)
	a := &agent{name: DefaultNamePrefix + name(owner, pub), errs: make(chan error, 16)}
	nc, err := nats.Connect(c.url, nats.Nkey(pub, host.Sign), nats.Token(string(raw)),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { a.errs <- err }), nats.MaxReconnects(0))
	if err != nil {
		return nil, err
	}
	c.t.Cleanup(nc.Close)
	a.nc = nc
	return a, nil
}

func (c *cl) setup() {
	c.chat.Users["bob"] = token.User{Username: "bob", AuthKnown: true, AuthService: token.OpenIDService, AuthData: "sub-bob"}
	c.chat.Channels["bob"] = []string{"home", "secret"}
}

func (c *cl) info(stream, consumer string) *nats.ConsumerInfo {
	ci, err := c.js.ConsumerInfo(stream, consumer)
	if err != nil {
		return nil
	}
	return ci
}

func (c *cl) consumersOf(a *agent) (names []string) {
	for _, st := range []string{"BUS", "PRESENCE"} {
		for n := range c.js.ConsumerNames(st) {
			if strings.HasPrefix(n, a.name+"_") {
				names = append(names, st+"/"+n)
			}
		}
	}
	return names
}

// publish stores a message and returns its stream sequence.
func (c *cl) publish(subject, data string) uint64 {
	c.t.Helper()
	before := c.last("BUS")
	if err := c.nc.Publish(subject, []byte(data)); err != nil {
		c.t.Fatal(err)
	}
	_ = c.nc.Flush()
	deadline := time.Now().Add(2 * time.Second)
	for c.last("BUS") == before && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
	return c.last("BUS")
}

func (c *cl) last(stream string) uint64 {
	si, err := c.js.StreamInfo(stream)
	if err != nil {
		c.t.Fatal(err)
	}
	return si.State.LastSeq
}

func (c *cl) listen(a *agent, subjects ...string) chan *nats.Msg {
	c.t.Helper()
	ch := make(chan *nats.Msg, 256)
	for _, s := range subjects {
		if _, err := a.nc.ChanSubscribe(s, ch); err != nil {
			c.t.Fatal(err)
		}
	}
	if a.denied(c.t) {
		c.t.Fatal("own deliver subject refused")
	}
	return ch
}

func slogTo(w *syncBuffer) *slog.Logger { return slog.New(slog.NewJSONHandler(w, nil)) }

func newHost() nkeys.KeyPair { k, _ := nkeys.CreateUser(); return k }

func waitFor(t *testing.T, what string, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out: %s", what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func rawStream(t *testing.T, nc *nats.Conn, name, cfg string) {
	t.Helper()
	r, err := nc.Request("$JS.API.STREAM.CREATE."+name, []byte(cfg), 2*time.Second)
	if err != nil || strings.Contains(string(r.Data), `"error"`) {
		t.Fatalf("stream %s: %v %s", name, err, r.Data)
	}
}

func TestEnsureStreamsCreatesTheSafeConfig(t *testing.T) {
	l := newLabCfg(t, nil, 0, nil)
	nc, _ := l.admin()
	m, err := consumers.New(nc, consumers.Config{Bus: "bus", Presence: "presence"})
	if err != nil {
		t.Fatal(err)
	}
	for range 2 { // the second run finds them and accepts them
		if err := m.EnsureStreams(ctx5(t)); err != nil {
			t.Fatal(err)
		}
	}
	for _, st := range []string{"BUS", "PRESENCE"} {
		r, err := nc.Request("$JS.API.STREAM.INFO."+st, nil, 2*time.Second)
		if err != nil {
			t.Fatal(err)
		}
		var v struct {
			Config map[string]any `json:"config"`
		}
		if err := json.Unmarshal(r.Data, &v); err != nil {
			t.Fatal(err)
		}
		g := v.Config
		for _, k := range []string{"no_ack", "deny_delete", "deny_purge"} {
			if g[k] != true {
				t.Errorf("%s: %s = %v", st, k, g[k])
			}
		}
		for k, val := range g {
			if strings.HasPrefix(k, "allow_") && val != false {
				t.Errorf("%s: %s = %v", st, k, val)
			}
		}
		wantAge, wantBytes := float64(14*24*time.Hour), float64(1<<30)
		if st == "PRESENCE" {
			wantAge, wantBytes = float64(24*time.Hour), float64(16<<20)
			if g["max_msgs_per_subject"] != float64(1) {
				t.Errorf("PRESENCE max_msgs_per_subject = %v", g["max_msgs_per_subject"])
			}
		}
		if g["max_age"] != wantAge || g["max_bytes"] != wantBytes || g["discard"] != "old" ||
			g["duplicate_window"] != float64(100*time.Millisecond) || g["max_msg_size"] == nil {
			t.Errorf("%s: %v", st, g)
		}
	}
}

func TestEnsureStreamsSizesAreConfigurable(t *testing.T) {
	l := newLabCfg(t, nil, 0, nil)
	nc, js := l.admin()
	m, _ := consumers.New(nc, consumers.Config{Bus: "bus", Presence: "presence", BusMaxBytes: 5 << 20, PresenceMaxBytes: 1 << 20})
	if err := m.EnsureStreams(ctx5(t)); err != nil {
		t.Fatal(err)
	}
	b, _ := js.StreamInfo("BUS")
	p, _ := js.StreamInfo("PRESENCE")
	if b.Config.MaxBytes != 5<<20 || p.Config.MaxBytes != 1<<20 {
		t.Fatalf("%d %d", b.Config.MaxBytes, p.Config.MaxBytes)
	}
}

func TestEnsureStreamsRefusesAnUnsafeStream(t *testing.T) {
	for name, c := range map[string]struct{ stream, cfg string }{
		"BUS acknowledges":           {"BUS", strings.Replace(busStreamCfg, `"no_ack":true`, `"no_ack":false`, 1)},
		"BUS allows schedules":       {"BUS", strings.Replace(busStreamCfg, `"allow_msg_schedules":false`, `"allow_msg_schedules":true`, 1)},
		"BUS allows rollups":         {"BUS", strings.NewReplacer(`"allow_rollup_hdrs":false`, `"allow_rollup_hdrs":true`, `"deny_purge":true`, `"deny_purge":false`).Replace(busStreamCfg)},
		"BUS allows deletes":         {"BUS", strings.Replace(busStreamCfg, `"deny_delete":true`, `"deny_delete":false`, 1)},
		"BUS allows purges":          {"BUS", strings.Replace(busStreamCfg, `"deny_purge":true`, `"deny_purge":false`, 1)},
		"BUS takes deliver subjects": {"BUS", strings.Replace(busStreamCfg, `"subjects":["bus.>"]`, `"subjects":["bus.>","deliver.>"]`, 1)},
		"BUS without a size limit":   {"BUS", strings.Replace(busStreamCfg, `"max_bytes":1073741824`, `"max_bytes":-1`, 1)},
		"PRESENCE keeps many":        {"PRESENCE", strings.Replace(presenceStreamCfg, `"max_msgs_per_subject":1`, `"max_msgs_per_subject":5`, 1)},
		"PRESENCE keeps all":         {"PRESENCE", strings.Replace(presenceStreamCfg, `"max_msgs_per_subject":1,`, ``, 1)},
		"PRESENCE acknowledges":      {"PRESENCE", strings.Replace(presenceStreamCfg, `"no_ack":true`, `"no_ack":false`, 1)},
	} {
		t.Run(name, func(t *testing.T) {
			l := newLabCfg(t, nil, 0, nil)
			nc, _ := l.admin()
			rawStream(t, nc, c.stream, c.cfg)
			m, _ := consumers.New(nc, consumers.Config{Bus: "bus", Presence: "presence"})
			err := m.EnsureStreams(ctx5(t))
			if !errors.Is(err, consumers.ErrUnsafeStream) {
				t.Fatalf("EnsureStreams: %v", err)
			}
			t.Log(err)
		})
	}
}

func TestALoginMakesTheConsumers(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	bob, err := c.login("bob", newHost(), Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	type want struct {
		stream, name, deliver string
		filters               []string
	}
	for _, w := range []want{
		{"BUS", "_ch_home", "ch.home", []string{"bus.ch.home.*"}},
		{"BUS", "_ch_secret", "ch.secret", []string{"bus.ch.secret.*"}},
		{"BUS", "_dm", "dm", []string{"bus.dm." + bob.name + ".*"}},
		{"PRESENCE", "_presence", "presence", []string{"presence.*", "presence.crown.home.*", "presence.crown.secret.*"}},
	} {
		ci := c.info(w.stream, bob.name+w.name)
		if ci == nil {
			t.Fatalf("no consumer %s", w.name)
		}
		cfg := ci.Config
		got := append([]string{}, cfg.FilterSubjects...)
		if cfg.FilterSubject != "" {
			got = append(got, cfg.FilterSubject)
		}
		if cfg.DeliverSubject != "deliver."+bob.name+"."+w.deliver || strings.Join(got, ",") != strings.Join(w.filters, ",") ||
			cfg.AckPolicy != nats.AckNonePolicy || cfg.FlowControl || cfg.Heartbeat != 0 ||
			cfg.InactiveThreshold != time.Hour || cfg.Metadata["owner"] != "bob" || cfg.Metadata["agent"] != bob.name {
			t.Errorf("%s: %+v", w.name, cfg)
		}
		if w.stream == "PRESENCE" && cfg.DeliverPolicy != nats.DeliverLastPerSubjectPolicy {
			t.Errorf("presence delivers %v", cfg.DeliverPolicy)
		}
		if w.stream == "BUS" && cfg.DeliverPolicy != nats.DeliverNewPolicy {
			t.Errorf("%s delivers %v", w.name, cfg.DeliverPolicy)
		}
	}
	if n := len(c.consumersOf(bob)); n != 4 {
		t.Errorf("%d consumers: %v", n, c.consumersOf(bob))
	}
}

func TestARenewalKeepsTheConsumersAndTheirPosition(t *testing.T) {
	c := newConsumerLab(t, nil)
	host := newHost()
	a, err := c.login("alice", host, Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	in := c.listen(a, "deliver."+a.name+".ch.home")
	c.publish("bus.ch.home.agent-other", "one")
	if got := take(in, time.Second); len(got) != 1 {
		t.Fatalf("delivered %d", len(got))
	}
	before := c.info("BUS", a.name+"_ch_home")
	if before.Delivered.Stream == 0 {
		t.Fatal("no position to keep")
	}
	if _, err := c.login("alice", host, Hello{Home: "home"}); err != nil { // the renewal
		t.Fatal(err)
	}
	after := c.info("BUS", a.name+"_ch_home")
	if after == nil || !after.Created.Equal(before.Created) || after.Delivered.Stream != before.Delivered.Stream || after.Delivered.Consumer != before.Delivered.Consumer {
		t.Fatalf("not kept: %+v -> %+v", before, after)
	}
	c.publish("bus.ch.home.agent-other", "two")
	got := take(in, time.Second)
	if len(got) != 1 || string(got[0].Data) != "two" {
		t.Fatalf("after renewal: %d messages", len(got))
	}
}

func TestFewerChannelsRemoveTheDroppedConsumerAndNarrowPresence(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	host := newHost()
	bob, err := c.login("bob", host, Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	if c.info("BUS", bob.name+"_ch_secret") == nil {
		t.Fatal("no secret consumer")
	}
	c.chat.Channels["bob"] = []string{"home"} // removed from secret
	bob2, err := c.login("bob", host, Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	if c.info("BUS", bob.name+"_ch_secret") != nil {
		t.Error("the consumer of the dropped channel is still there")
	}
	if c.info("BUS", bob.name+"_ch_home") == nil {
		t.Error("the consumer of the kept channel is gone")
	}
	pc := c.info("PRESENCE", bob.name+"_presence")
	if f := append(append([]string{}, pc.Config.FilterSubjects...), pc.Config.FilterSubject); strings.Contains(strings.Join(f, " "), "crown.secret") {
		t.Errorf("presence still shows crowns of secret: %v", f)
	}
	// And bob no longer gets the channel's messages.
	in := c.listen(bob2, "deliver."+bob.name+".ch.home", "deliver."+bob.name+".ch.secret")
	c.publish("bus.ch.secret.agent-other", "for the secret channel")
	c.publish("bus.ch.home.agent-other", "for home")
	got := take(in, time.Second)
	if len(got) != 1 || string(got[0].Data) != "for home" {
		t.Fatalf("got %d messages: %v", len(got), got)
	}
}

func TestSinceStartsAtTheSequenceOfAGrantedChannel(t *testing.T) {
	c := newConsumerLab(t, nil)
	seq1 := c.publish("bus.ch.home.agent-other", "m1")
	seq2 := c.publish("bus.ch.home.agent-other", "m2")
	c.publish("bus.ch.home.agent-other", "m3")
	// since is exclusive: the last sequence the agent has seen.
	a, err := c.login("alice", newHost(), Hello{Home: "home", Since: map[string]uint64{"home": seq1}})
	if err != nil {
		t.Fatal(err)
	}
	ci := c.info("BUS", a.name+"_ch_home")
	if ci.Config.DeliverPolicy != nats.DeliverByStartSequencePolicy || ci.Config.OptStartSeq != seq2 {
		t.Fatalf("%+v", ci.Config)
	}
	got := take(c.listen(a, "deliver."+a.name+".ch.home"), time.Second)
	if len(got) != 2 || string(got[0].Data) != "m2" || string(got[1].Data) != "m3" {
		t.Fatalf("history delivered: %d messages (m1, the one seen, must not be among them)", len(got))
	}
	// A renewal with a since again restarts the consumer after that sequence.
}

func TestSinceRestartsTheDirectMessageConsumer(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	host := newHost()
	a, err := c.login("alice", host, Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	dm := "bus.dm." + a.name + ".agent-other"
	seq1 := c.publish(dm, "d1")
	seq2 := c.publish(dm, "d2")
	c.publish(dm, "d3")
	// since["@dm"] is exclusive too: the consumer restarts after seq1.
	a, err = c.login("alice", host, Hello{Home: "home", Since: map[string]uint64{"@dm": seq1}})
	if err != nil {
		t.Fatal(err)
	}
	ci := c.info("BUS", a.name+"_dm")
	if ci.Config.DeliverPolicy != nats.DeliverByStartSequencePolicy || ci.Config.OptStartSeq != seq2 {
		t.Fatalf("%+v", ci.Config)
	}
	got := take(c.listen(a, "deliver."+a.name+".dm"), time.Second)
	if len(got) != 2 || string(got[0].Data) != "d2" || string(got[1].Data) != "d3" {
		t.Fatalf("history delivered: %d messages", len(got))
	}
	if c.info("BUS", a.name+"_ch_@dm") != nil {
		t.Error("a channel consumer for @dm")
	}
	// The key can never be a channel: the callout refuses "@" in a channel name.
	for _, ch := range []string{"@dm", "a@b", "dm@"} {
		if checkToken(ch) == nil {
			t.Errorf("checkToken accepts %q", ch)
		}
	}
}

// since["@dm"] restarts only the agent's own direct-message consumer, which
// holds only messages to that agent: another agent's direct messages never
// come with it, and the other agent's consumer keeps its place.
func TestSinceForDirectMessagesOnlyTouchesTheOwn(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	ha, hb := newHost(), newHost()
	a, err := c.login("alice", ha, Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	b, err := c.login("bob", hb, Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	bobBefore := c.info("BUS", b.name+"_dm")
	c.publish("bus.dm."+b.name+".agent-other", "for bob")
	c.publish("bus.dm."+a.name+".agent-other", "for alice")
	if a, err = c.login("alice", ha, Hello{Home: "home", Since: map[string]uint64{"@dm": 1}}); err != nil {
		t.Fatal(err)
	}
	got := take(c.listen(a, "deliver."+a.name+".dm"), time.Second)
	if len(got) != 1 || string(got[0].Data) != "for alice" {
		t.Fatalf("alice's history: %d messages", len(got))
	}
	bobAfter := c.info("BUS", b.name+"_dm")
	if bobAfter == nil || !bobAfter.Created.Equal(bobBefore.Created) || bobAfter.Config.DeliverPolicy != bobBefore.Config.DeliverPolicy {
		t.Error("alice's since moved bob's direct-message consumer")
	}
}

func TestSinceIsIgnoredForAChannelNotInTheCredential(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	c.publish("bus.ch.secret.agent-other", "secret history")
	// alice is no member of secret: asked for as a guest, refused; since too.
	a, err := c.login("alice", newHost(), Hello{Home: "home", Guests: []string{"secret"},
		Since: map[string]uint64{"secret": 1, "home": 0, "elsewhere": 1}})
	if err != nil {
		t.Fatal(err)
	}
	if c.info("BUS", a.name+"_ch_secret") != nil || c.info("BUS", a.name+"_ch_elsewhere") != nil {
		t.Error("a consumer exists for a channel outside the credential")
	}
	if ci := c.info("BUS", a.name+"_ch_home"); ci.Config.DeliverPolicy != nats.DeliverNewPolicy {
		t.Errorf("home: %v", ci.Config.DeliverPolicy)
	}
	got := take(c.listen(a, "deliver."+a.name+".ch.home"), 500*time.Millisecond)
	if len(got) != 0 {
		t.Errorf("%d messages of history", len(got))
	}
	if !strings.Contains(c.logs.String(), "since ignored") {
		t.Error("the ignored since is not logged")
	}
}

func TestSinceNeverReachesBackFurtherThan14Days(t *testing.T) {
	now := time.Now()
	clock := now
	c := newConsumerLab(t, func(cfg *consumers.Config) { cfg.Now = func() time.Time { return clock } })
	first := c.publish("bus.ch.home.agent-other", "old enough")
	c.publish("bus.ch.home.agent-other", "second")
	reconcile := func(agent string, since uint64) *nats.ConsumerInfo {
		t.Helper()
		if err := c.mgr.Reconcile(ctx5(t), agent, "alice", []string{"home"}, map[string]uint64{"home": since}); err != nil {
			t.Fatal(err)
		}
		return c.info("BUS", agent+"_ch_home")
	}
	// 13 days later the messages are young enough: by sequence.
	clock = now.Add(13 * 24 * time.Hour)
	ci := reconcile("agent-young", first)
	if ci.Config.DeliverPolicy != nats.DeliverByStartSequencePolicy || ci.Config.OptStartSeq != first+1 {
		t.Errorf("13 days: %+v", ci.Config)
	}
	// 15 days later they are too old: by time, at now minus 14 days.
	clock = now.Add(15 * 24 * time.Hour)
	ci = reconcile("agent-old", first)
	cutoff := clock.Add(-14 * 24 * time.Hour)
	if ci.Config.DeliverPolicy != nats.DeliverByStartTimePolicy || ci.Config.OptStartTime == nil ||
		ci.Config.OptStartTime.Sub(cutoff).Abs() > time.Second {
		t.Errorf("15 days: %+v", ci.Config)
	}
	// Just past the limit, within the server's expiry lag: still by sequence,
	// nothing replayed twice.
	clock = now.Add(14*24*time.Hour + 30*time.Second)
	ci = reconcile("agent-just-old", first)
	if ci.Config.DeliverPolicy != nats.DeliverByStartSequencePolicy || ci.Config.OptStartSeq != first+1 {
		t.Errorf("14 days and 30 s: %+v", ci.Config)
	}
	// A sequence that would overflow is past the last: new messages only.
	ci = reconcile("agent-overflow", ^uint64(0))
	if ci.Config.DeliverPolicy != nats.DeliverNewPolicy {
		t.Errorf("since 2^64-1: %+v", ci.Config)
	}
	// The last seen being the last one: nothing to replay, from new messages.
	ci = reconcile("agent-caught-up", first+1)
	if ci.Config.DeliverPolicy != nats.DeliverNewPolicy {
		t.Errorf("last seen is the last: %+v", ci.Config)
	}
	// A sequence beyond the stream's last is no error.
	ci = reconcile("agent-ahead", 1000)
	if ci.Config.DeliverPolicy != nats.DeliverNewPolicy {
		t.Errorf("beyond the last: %+v", ci.Config)
	}
}

func TestAByeOnTheOwnPresenceSubjectRemovesTheOwnConsumers(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	if err := c.mgr.WatchByes(ctx); err != nil {
		t.Fatal(err)
	}
	alice, err := c.login("alice", newHost(), Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	bob, err := c.login("bob", newHost(), Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	say := func(a *agent, subject, body string) {
		t.Helper()
		if err := a.nc.Publish(subject, []byte(body)); err != nil {
			t.Fatal(err)
		}
		if a.denied(t) {
			t.Fatalf("%s refused", subject)
		}
	}
	say(alice, "presence."+alice.name, `{"status":"online"}`)
	time.Sleep(300 * time.Millisecond)
	if len(c.consumersOf(alice)) != 3 {
		t.Fatal("a heartbeat removed consumers")
	}
	say(bob, "presence."+bob.name, `{"status":"bye"}`)
	waitFor(t, "bob's consumers removed", func() bool { return len(c.consumersOf(bob)) == 0 })
	if n := len(c.consumersOf(alice)); n != 3 {
		t.Errorf("bob's bye touched alice's consumers: %d left", n)
	}
	// Alice cannot say bye as bob: the subject is hers alone.
	if err := alice.nc.Publish("presence."+bob.name, []byte(`{"status":"bye"}`)); err != nil {
		t.Fatal(err)
	}
	if !alice.denied(t) {
		t.Error("alice could publish on bob's presence subject")
	}
	say(alice, "presence."+alice.name, `{"status":"bye"}`)
	waitFor(t, "alice's consumers removed", func() bool { return len(c.consumersOf(alice)) == 0 })
}

// A bye handled late, after the same agent logged in again, leaves the new
// login's consumers alone: only consumers made before the bye go.
func TestALateByeLeavesANewLoginsConsumers(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	host := newHost()
	alice, err := c.login("alice", host, Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	bye := time.Now()
	time.Sleep(50 * time.Millisecond)
	if err := c.mgr.Remove(context.Background(), alice.name); err != nil {
		t.Fatal(err)
	}
	if _, err := c.login("alice", host, Hello{Home: "home"}); err != nil {
		t.Fatal(err)
	}
	if n := len(c.consumersOf(alice)); n != 3 {
		t.Fatalf("after the new login: %d consumers", n)
	}
	if err := c.mgr.RemoveBefore(context.Background(), alice.name, bye); err != nil {
		t.Fatal(err)
	}
	if n := len(c.consumersOf(alice)); n != 3 {
		t.Errorf("the late bye removed the new login's consumers: %d left", n)
	}
}

func TestTheBrakeRemovesTheStoppedOwnersConsumers(t *testing.T) {
	c := newConsumerLab(t, func(cfg *consumers.Config) { cfg.BrakeInterval = 50 * time.Millisecond })
	c.setup()
	alice, err := c.login("alice", newHost(), Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	bob, err := c.login("bob", newHost(), Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	c.mgr.WatchBrake(ctx, NewBrake(c.brake, nil))
	set := func(text string, shift time.Duration) {
		t.Helper()
		if err := os.WriteFile(c.brake, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
		later := time.Now().Add(shift)
		_ = os.Chtimes(c.brake, later, later)
	}
	time.Sleep(200 * time.Millisecond)
	if len(c.consumersOf(alice)) != 3 || len(c.consumersOf(bob)) != 3 {
		t.Fatal("consumers removed without the brake")
	}
	set("alice\n", 10*time.Second)
	waitFor(t, "alice's consumers removed", func() bool { return len(c.consumersOf(alice)) == 0 })
	if len(c.consumersOf(bob)) != 3 {
		t.Errorf("bob's consumers touched: %v", c.consumersOf(bob))
	}
	set("*\n", 20*time.Second)
	waitFor(t, "everyone's consumers removed", func() bool { return len(c.consumersOf(bob)) == 0 })
}

func TestRemoveOwner(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	alice, _ := c.login("alice", newHost(), Hello{Home: "home"})
	bob, _ := c.login("bob", newHost(), Hello{Home: "home"})
	if err := c.mgr.RemoveOwner(ctx5(t), "Alice"); err != nil {
		t.Fatal(err)
	}
	if len(c.consumersOf(alice)) != 0 || len(c.consumersOf(bob)) != 3 {
		t.Errorf("alice %v, bob %v", c.consumersOf(alice), c.consumersOf(bob))
	}
}

type failingConsumers struct{}

func (failingConsumers) Reconcile(context.Context, string, string, []string, map[string]uint64) error {
	return errors.New("jetstream is down")
}

func TestAConsumerFailureRefusesTheLogin(t *testing.T) {
	l := newLab(t)
	l.svc.SetConsumers(failingConsumers{})
	tok := l.realm.Token("alice", nil)
	if _, err := l.connect("alice", tok, Hello{Home: "home"}, nil); err == nil {
		t.Fatal("logged in without consumers")
	}
	logs := l.logs.String()
	if !strings.Contains(logs, "refused") || !strings.Contains(logs, "jetstream is down") {
		t.Errorf("no reason logged: %s", logs)
	}
	if strings.Contains(logs, tok) {
		t.Error("the token is in the log")
	}
	l.svc.SetConsumers(nil) // without the hook: as before
	if _, err := l.connect("alice", l.realm.Token("alice", nil), Hello{Home: "home"}, nil); err != nil {
		t.Fatal(err)
	}
}

// The whole way: a login through the real callout makes the consumers, the
// agent subscribes literally to its deliver subjects and reads another
// agent's post, a direct message and presence.
func TestEndToEndAnAgentReadsAPostOfAnother(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	alice, err := c.login("alice", newHost(), Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	bob, err := c.login("bob", newHost(), Hello{Home: "home", Guests: []string{"secret"}})
	if err != nil {
		t.Fatal(err)
	}
	in := c.listen(bob, "deliver."+bob.name+".ch.home", "deliver."+bob.name+".ch.secret",
		"deliver."+bob.name+".dm", "deliver."+bob.name+".presence")
	for subject, body := range map[string]string{
		"bus.ch.home." + alice.name:             "hello home",
		"bus.dm." + bob.name + "." + alice.name: "hello bob",
		"presence." + alice.name:                `{"status":"online"}`,
	} {
		if err := alice.nc.Publish(subject, []byte(body)); err != nil {
			t.Fatal(err)
		}
	}
	if alice.denied(t) {
		t.Fatal("alice's own posts refused")
	}
	seen := map[string]bool{}
	for _, m := range take(in, time.Second) {
		if sender(m.Subject) == alice.name {
			seen[string(m.Data)] = true
		}
	}
	for _, w := range []string{"hello home", "hello bob", `{"status":"online"}`} {
		if !seen[w] {
			t.Errorf("bob did not receive %q (got %v)", w, seen)
		}
	}
}

// The bye is stamped by the server and the watcher handles it late, after the
// same agent logged in again: the consumers of the new login stay. A watcher
// that took the local clock would remove them.
func TestAByeHandledAfterAReloginLeavesTheConsumers(t *testing.T) {
	block, reached := make(chan struct{}), make(chan string, 4)
	var once sync.Once
	release := func() { once.Do(func() { close(block) }) }
	t.Cleanup(release)
	c := newConsumerLab(t, func(cfg *consumers.Config) {
		cfg.BeforeBye = func(agent string) { reached <- agent; <-block }
	})
	c.setup()
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	if err := c.mgr.WatchByes(ctx); err != nil {
		t.Fatal(err)
	}
	host := newHost()
	alice, err := c.login("alice", host, Hello{Home: "home"})
	if err != nil {
		t.Fatal(err)
	}
	if err := alice.nc.Publish("presence."+alice.name, []byte(`{"status":"bye"}`)); err != nil {
		t.Fatal(err)
	}
	select {
	case <-reached:
	case <-time.After(3 * time.Second):
		t.Fatal("the watcher did not see the bye")
	}
	time.Sleep(50 * time.Millisecond)
	if _, err := c.login("alice", host, Hello{Home: "home"}); err != nil { // the handler still waits
		t.Fatal(err)
	}
	release()
	waitFor(t, "the late bye is handled", func() bool { return strings.Contains(c.logs.String(), "logged in again since") })
	if n := len(c.consumersOf(alice)); n != 3 {
		t.Errorf("the late bye removed the new login's consumers: %d left", n)
	}
}

// A guest channel replaced by another, the number of filters unchanged: the
// presence consumer follows, the old channel's consumer goes, the new one is made.
func TestSwappingAGuestChannelChangesThePresenceFilters(t *testing.T) {
	c := newConsumerLab(t, nil)
	c.setup()
	c.chat.Channels["bob"] = []string{"home", "ga", "gb"}
	host := newHost()
	bob, err := c.login("bob", host, Hello{Home: "home", Guests: []string{"ga"}})
	if err != nil {
		t.Fatal(err)
	}
	filters := func() string {
		pc := c.info("PRESENCE", bob.name+"_presence")
		return strings.Join(append(append([]string{}, pc.Config.FilterSubjects...), pc.Config.FilterSubject), " ")
	}
	if f := filters(); !strings.Contains(f, "crown.ga.") || strings.Contains(f, "crown.gb.") {
		t.Fatalf("before: %s", f)
	}
	if _, err := c.login("bob", host, Hello{Home: "home", Guests: []string{"gb"}}); err != nil {
		t.Fatal(err)
	}
	if f := filters(); strings.Contains(f, "crown.ga.") || !strings.Contains(f, "crown.gb.") {
		t.Errorf("after: %s", f)
	}
	if c.info("BUS", bob.name+"_ch_ga") != nil {
		t.Error("the consumer of the replaced guest channel is still there")
	}
	if c.info("BUS", bob.name+"_ch_gb") == nil {
		t.Error("no consumer for the new guest channel")
	}
	if ch := c.info("PRESENCE", bob.name+"_presence").Config.Metadata["channels"]; ch != "gb,home" {
		t.Errorf("channels recorded: %q", ch)
	}
}

// Agent names where one is the other's prefix plus "_": a bye or a reconcile
// of one never touches the other's consumers, found by metadata not by name.
func TestAgentsWhoseNamesShareAPrefixDoNotShareConsumers(t *testing.T) {
	c := newConsumerLab(t, nil)
	ctx := ctx5(t)
	for _, a := range []string{"x", "x_y"} {
		if err := c.mgr.Reconcile(ctx, a, "alice", []string{"home"}, nil); err != nil {
			t.Fatal(err)
		}
	}
	count := func(prefix string) (n int) {
		for _, st := range []string{"BUS", "PRESENCE"} {
			for name := range c.js.ConsumerNames(st) {
				if strings.HasPrefix(name, prefix) {
					n++
				}
			}
		}
		return n
	}
	// x's reconcile with no channels must not remove x_y's.
	if err := c.mgr.Reconcile(ctx, "x", "alice", nil, nil); err != nil {
		t.Fatal(err)
	}
	if c.info("BUS", "x_y_ch_home") == nil || c.info("BUS", "x_ch_home") != nil {
		t.Fatal("reconcile of x touched x_y or kept its own dropped channel")
	}
	if err := c.mgr.Remove(ctx, "x"); err != nil {
		t.Fatal(err)
	}
	if c.info("BUS", "x_y_ch_home") == nil || c.info("BUS", "x_y_dm") == nil || c.info("PRESENCE", "x_y_presence") == nil {
		t.Errorf("removing x removed x_y's consumers (%d left)", count("x_y_"))
	}
	if count("x_") != 3 {
		t.Errorf("x left %d consumers beside x_y's three", count("x_")-3)
	}
	// The fallback, with no presence consumer to read, goes by metadata too:
	// a consumer of x_y's name that is not x_y's own is not deleted.
	if _, err := c.js.AddConsumer("BUS", &nats.ConsumerConfig{Durable: "x_foreign", DeliverSubject: "deliver.foreign",
		FilterSubject: "bus.ch.home.*", AckPolicy: nats.AckNonePolicy, Metadata: map[string]string{"agent": "other"}}); err != nil {
		t.Fatal(err)
	}
	if err := c.mgr.Remove(ctx, "x"); err != nil {
		t.Fatal(err)
	}
	if c.info("BUS", "x_foreign") == nil {
		t.Error("the fallback removed a consumer by its name prefix")
	}
}

// A bye costs a handful of calls, not a listing: with many other agents'
// consumers present only the leaver's go, and CONSUMER.LIST is never called
// while its presence consumer tells the names.
func TestAByeWithManyOtherConsumersRemovesOnlyTheLeaversAndListsNothing(t *testing.T) {
	c := newConsumerLab(t, nil)
	ctx := ctx5(t)
	for i := range 30 {
		if err := c.mgr.Reconcile(ctx, fmt.Sprintf("other%d", i), "alice", []string{"home"}, nil); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.mgr.Reconcile(ctx, "leaver", "alice", []string{"home", "a", "b"}, nil); err != nil {
		t.Fatal(err)
	}
	var lists, names atomic.Int32
	for subj, n := range map[string]*atomic.Int32{"$JS.API.CONSUMER.LIST.>": &lists, "$JS.API.CONSUMER.NAMES.>": &names} {
		sub, err := c.nc.Subscribe(subj, func(*nats.Msg) { n.Add(1) })
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { _ = sub.Unsubscribe() })
	}
	_ = c.nc.Flush()
	if err := c.mgr.Reconcile(ctx, "leaver", "alice", []string{"home", "a"}, nil); err != nil { // b dropped
		t.Fatal(err)
	}
	if c.info("BUS", "leaver_ch_b") != nil {
		t.Error("the dropped channel's consumer is still there")
	}
	if err := c.mgr.Remove(ctx, "leaver"); err != nil {
		t.Fatal(err)
	}
	_ = c.nc.Flush()
	time.Sleep(100 * time.Millisecond)
	if lists.Load() != 0 || names.Load() != 0 {
		t.Errorf("reconcile and remove listed consumers: %d LIST, %d NAMES", lists.Load(), names.Load())
	}
	left := 0
	for _, st := range []string{"BUS", "PRESENCE"} {
		for n := range c.js.ConsumerNames(st) {
			if strings.HasPrefix(n, "leaver_") {
				t.Errorf("%s left", n)
			}
			if strings.HasPrefix(n, "other") {
				left++
			}
		}
	}
	if left != 30*3 {
		t.Errorf("%d consumers of the others left, want %d", left, 90)
	}
}

// The login service's own JetStream user needs no more than the rights the
// README and RT-6.35 name: everything the manager does works with them.
func TestTheDocumentedMinimalRightsSuffice(t *testing.T) {
	l := newLabCfg(t, nil, 2800*time.Millisecond, nil)
	denied := make(chan error, 16)
	nc, err := nats.Connect(l.url, nats.UserInfo("jsmin", "pwd"),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { denied <- err }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	mgr, err := consumers.New(nc, consumers.Config{Bus: "bus", Presence: "presence", Log: slogTo(l.logs)})
	if err != nil {
		t.Fatal(err)
	}
	if err := mgr.EnsureStreams(ctx5(t)); err != nil {
		t.Fatalf("EnsureStreams: %v", err)
	}
	if err := mgr.EnsureStreams(ctx5(t)); err != nil {
		t.Fatalf("EnsureStreams on existing streams: %v", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	if err := mgr.WatchByes(ctx); err != nil {
		t.Fatalf("WatchByes: %v", err)
	}
	l.svc.SetConsumers(mgr)
	c := &cl{lab: l, mgr: mgr}
	c.nc, c.js = l.admin()
	a, err := c.login("alice", newHost(), Hello{Home: "home"})
	if err != nil {
		t.Fatalf("login: %v", err)
	}
	if n := len(c.consumersOf(a)); n != 3 {
		t.Fatalf("%d consumers", n)
	}
	// History needs no more rights: a login with since reads only the
	// stream's info, never a message.
	c.publish("bus.ch.home.agent-other", "m1")
	c.publish("bus.ch.home.agent-other", "m2")
	h, err := c.login("alice", newHost(), Hello{Home: "home", Since: map[string]uint64{"home": 1}})
	if err != nil {
		t.Fatalf("login with since: %v", err)
	}
	if ci := c.info("BUS", h.name+"_ch_home"); ci == nil || ci.Config.OptStartSeq != 2 {
		t.Fatalf("login with since: %+v", ci)
	}
	if err := mgr.SweepBrake(ctx5(t), NewBrake("", nil)); err != nil {
		t.Fatalf("SweepBrake: %v", err)
	}
	if err := a.nc.Publish("presence."+a.name, []byte(`{"status":"bye"}`)); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "alice's consumers removed by her bye", func() bool { return len(c.consumersOf(a)) == 0 })
	select {
	case err := <-denied:
		t.Errorf("the minimal user was refused: %v", err)
	default:
	}
}

// The channels are recorded together, old and new, before any consumer is
// made, and cut to the new ones only at the end: a reconcile that fails in
// between leaves a channel consumer the record still names, so the next
// reconcile removes it (a consumer missing from the record would keep a
// withdrawn read right for ever).
func TestAFailedRecordLeavesNoUnrecordedConsumer(t *testing.T) {
	var fail atomic.Bool
	c := newConsumerLab(t, func(cfg *consumers.Config) {
		cfg.BeforeRecord = func(string) error {
			if fail.Load() {
				return errors.New("the record could not be written")
			}
			return nil
		}
	})
	agent, owner := "agent-rec", "alice"
	if err := c.mgr.Reconcile(ctx5(t), agent, owner, []string{"home"}, nil); err != nil {
		t.Fatal(err)
	}
	fail.Store(true)
	if err := c.mgr.Reconcile(ctx5(t), agent, owner, []string{"home", "gb"}, nil); err == nil {
		t.Fatal("the failing record did not fail the reconcile")
	}
	if c.info("BUS", agent+"_ch_gb") == nil {
		t.Fatal("the new channel's consumer was not made before the record failed")
	}
	fail.Store(false)
	if err := c.mgr.Reconcile(ctx5(t), agent, owner, []string{"home"}, nil); err != nil {
		t.Fatal(err)
	}
	if c.info("BUS", agent+"_ch_gb") != nil {
		t.Error("the consumer of the withdrawn channel survived the next reconcile")
	}
}

// Removing an old channel's consumer fails: the record still names that
// channel (old and new are recorded first), so the next reconcile removes it.
func TestAFailedRemovalIsRetriedByTheNextReconcile(t *testing.T) {
	var fail atomic.Bool
	c := newConsumerLab(t, func(cfg *consumers.Config) {
		cfg.BeforeRemove = func(name string) error {
			if fail.Load() && strings.HasSuffix(name, "_ch_ga") {
				return errors.New("the removal failed")
			}
			return nil
		}
	})
	agent, owner := "agent-rm", "alice"
	if err := c.mgr.Reconcile(ctx5(t), agent, owner, []string{"home", "ga"}, nil); err != nil {
		t.Fatal(err)
	}
	fail.Store(true)
	if err := c.mgr.Reconcile(ctx5(t), agent, owner, []string{"home"}, nil); err == nil {
		t.Fatal("the failing removal did not fail the reconcile")
	}
	if c.info("BUS", agent+"_ch_ga") == nil {
		t.Fatal("the consumer is gone although its removal failed")
	}
	fail.Store(false)
	if err := c.mgr.Reconcile(ctx5(t), agent, owner, []string{"home"}, nil); err != nil {
		t.Fatal(err)
	}
	if c.info("BUS", agent+"_ch_ga") != nil {
		t.Error("the consumer of the withdrawn channel survived the next reconcile")
	}
}
