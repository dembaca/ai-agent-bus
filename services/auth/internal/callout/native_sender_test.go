package callout

// The standing proof of the permission model: the sender of a bus message is
// unforgeable with native NATS means alone. The sender is the last token of
// the subject, an agent may publish only under its own name, reads only what
// push consumers (made by the login service, here the admin connection)
// deliver to deliver.<own name>.>, and has no JetStream, key-value, system or
// inbox subject at all. Streams do not acknowledge (no_ack) and every allow_*
// is off.
//
// The suite runs on every update of the NATS server. Checked with
// nats-server v2.15.3 (go.mod); an update of the server is allowed only after
// this suite is green (ADR-014).

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
)

// The stream configurations are the production ones (consumers package).
var (
	busStreamCfg      = consumers.BusStreamJSON("bus", 0)
	presenceStreamCfg = consumers.PresenceStreamJSON("presence", 0)
)

type tapped struct {
	tap string // which wiretap saw it: bus, presence, deliver
	m   *nats.Msg
}

type native struct {
	t        *testing.T
	l        *lab
	nc       *nats.Conn
	js       nats.JetStreamContext
	a, b     *agent
	aIn, bIn chan *nats.Msg
	mu       sync.Mutex
	seen     []tapped
	bobSent  map[string]bool
	rows     [][2]string
}

func sender(subject string) string { return subject[strings.LastIndex(subject, ".")+1:] }

func (n *native) stream(name, cfg string) {
	n.t.Helper()
	r, err := n.nc.Request("$JS.API.STREAM.CREATE."+name, []byte(cfg), 2*time.Second)
	if err != nil || strings.Contains(string(r.Data), `"error"`) {
		n.t.Fatalf("stream %s: %v %s", name, err, r.Data)
	}
}

// login is what the login service does for an agent: one push consumer per
// channel, one for its direct messages, one for presence: every agent's
// presence, but crown claims only of its own channels (ADR-014 point 16).
func (n *native) login(ag *agent, channels ...string) (delivers []string) {
	n.t.Helper()
	add := func(stream, suffix string, filters []string, dp nats.DeliverPolicy) {
		cfg := &nats.ConsumerConfig{
			Durable: ag.name + "_" + strings.ReplaceAll(suffix, ".", "_"), DeliverSubject: "deliver." + ag.name + "." + suffix,
			AckPolicy: nats.AckNonePolicy, DeliverPolicy: dp, InactiveThreshold: time.Hour,
		}
		if len(filters) == 1 {
			cfg.FilterSubject = filters[0]
		} else {
			cfg.FilterSubjects = filters
		}
		_, err := n.js.AddConsumer(stream, cfg)
		if err != nil {
			n.t.Fatalf("consumer %s: %v", suffix, err)
		}
		delivers = append(delivers, "deliver."+ag.name+"."+suffix)
	}
	presence := []string{"presence.*.*", "presence.attest.*.v1"}
	for _, ch := range channels {
		add("BUS", "ch."+ch, []string{"bus.ch." + ch + ".*"}, nats.DeliverNewPolicy)
		presence = append(presence, "presence.crown."+ch+".*", "presence.member."+ch+".*")
	}
	add("BUS", "dm", []string{"bus.dm." + ag.name + ".*"}, nats.DeliverNewPolicy)
	add("PRESENCE", "presence", presence, nats.DeliverLastPerSubjectPolicy)
	return delivers
}

// listen subscribes an agent to each of its deliver subjects by its literal
// name: a push consumer only delivers while a subscription on exactly its
// deliver subject exists, a wildcard above it does not count.
func (n *native) listen(ag *agent, delivers []string) chan *nats.Msg {
	n.t.Helper()
	ch := make(chan *nats.Msg, 8192)
	for _, d := range delivers {
		if _, err := ag.nc.ChanSubscribe(d, ch); err != nil {
			n.t.Fatal(err)
		}
	}
	if ag.denied(n.t) {
		n.t.Fatal("own deliver subject refused")
	}
	return ch
}

func newNative(t *testing.T, busCfg string) *native {
	t.Helper()
	n := &native{t: t, bobSent: map[string]bool{}}
	n.l = newLabCfg(t, nil, 0, nil)
	n.nc, n.js = n.l.admin()
	n.stream("BUS", busCfg)
	n.stream("PRESENCE", presenceStreamCfg)
	for _, tap := range []string{"bus", "presence", "deliver"} {
		tap := tap
		if _, err := n.nc.Subscribe(tap+".>", func(m *nats.Msg) {
			n.mu.Lock()
			n.seen = append(n.seen, tapped{tap, m})
			n.mu.Unlock()
		}); err != nil {
			t.Fatal(err)
		}
	}
	n.a, n.b = n.l.twoAgents()
	n.aIn = n.listen(n.a, n.login(n.a, "home"))
	n.bIn = n.listen(n.b, n.login(n.b, "home", "secret"))
	return n
}

func (n *native) last(stream string) uint64 {
	n.t.Helper()
	si, err := n.js.StreamInfo(stream)
	if err != nil {
		n.t.Fatal(err)
	}
	return si.State.LastSeq
}

func take(ch chan *nats.Msg, wait time.Duration) []*nats.Msg {
	var out []*nats.Msg
	for {
		select {
		case m := <-ch:
			out = append(out, m)
		case <-time.After(wait):
			return out
		}
	}
}

func (n *native) names(s string) string {
	return strings.ReplaceAll(strings.ReplaceAll(s, n.b.name, "<bob>"), n.a.name, "<alice>")
}

func (n *native) bobPosts(subject, data string) {
	n.t.Helper()
	n.bobSent[subject+"|"+data] = true
	if err := n.b.nc.Publish(subject, []byte(data)); err != nil {
		n.t.Fatal(err)
	}
	if n.b.denied(n.t) {
		n.t.Fatalf("bob's own post to %s refused", subject)
	}
}

// attack sends one message as alice and says what came of it.
func (n *native) attack(what, subject, reply string, hdr map[string]string, data string) (denied bool, stored int) {
	n.t.Helper()
	m := nats.NewMsg(subject)
	m.Reply = reply
	m.Data = []byte(data)
	for k, v := range hdr {
		m.Header.Set(k, v)
	}
	b0, p0 := n.last("BUS"), n.last("PRESENCE")
	if err := n.a.nc.PublishMsg(m); err != nil {
		n.t.Fatalf("%s: %v", what, err)
	}
	denied = n.a.denied(n.t)
	stored = int(n.last("BUS") - b0 + n.last("PRESENCE") - p0)
	got := take(n.bIn, 150*time.Millisecond)
	var subs []string
	for _, g := range got {
		subs = append(subs, n.names(g.Subject))
		if g.Header.Get("Status") == "" && sender(g.Subject) != n.a.name {
			n.t.Errorf("%s: bob was delivered %s (reply %q): %.80q", what, g.Subject, g.Reply, g.Data)
		}
	}
	res := ""
	if denied {
		res = "DENIED (permissions violation)"
	} else if stored == 0 {
		res = "dropped by the stream"
	} else {
		res = fmt.Sprintf("stored %d under alice's name", stored)
	}
	n.rows = append(n.rows, [2]string{n.names(what), fmt.Sprintf("%s; bob got %d %v", res, len(got), subs)})
	return denied, stored
}

// audit: nothing in the streams and nothing any wiretap saw carries a name
// its publisher did not have.
func (n *native) audit() {
	n.t.Helper()
	time.Sleep(300 * time.Millisecond)
	ok := func(where, subject string, data []byte) {
		s := sender(subject)
		if s == n.a.name || (s == n.b.name && n.bobSent[subject+"|"+string(data)]) {
			return
		}
		n.t.Errorf("FORGED %s: %s %.80q", where, subject, data)
	}
	for _, st := range []string{"BUS", "PRESENCE"} {
		si, _ := n.js.StreamInfo(st)
		cnt := 0
		for seq := si.State.FirstSeq; seq <= si.State.LastSeq && seq > 0; seq++ {
			m, err := n.js.GetMsg(st, seq)
			if errors.Is(err, nats.ErrMsgNotFound) && st == "PRESENCE" {
				continue // replaced: one message per subject
			}
			if err != nil {
				n.t.Errorf("audit: %s seq %d not read: %v", st, seq, err)
				continue
			}
			cnt++
			ok("in stream "+st, m.Subject, m.Data)
		}
		n.t.Logf("audit: stream %s holds %d messages, last seq %d", st, cnt, si.State.LastSeq)
	}
	n.mu.Lock()
	defer n.mu.Unlock()
	for _, s := range n.seen {
		if s.m.Header.Get("Status") != "" && s.tap == "deliver" {
			continue
		}
		ok("on the wire ("+s.tap+")", s.m.Subject, s.m.Data)
		if s.tap == "deliver" && !strings.HasPrefix(s.m.Reply, "$JS.ACK.") {
			n.t.Errorf("on a deliver subject without JetStream's reply: %s reply %q", s.m.Subject, s.m.Reply)
		}
	}
	n.t.Logf("audit: wiretaps saw %d messages", len(n.seen))
}

func (n *native) table() {
	for _, r := range n.rows {
		n.t.Logf("| %s | %s |", r[0], r[1])
	}
}

func (n *native) victims() []string {
	return []string{
		"bus.ch.home.agent-victim", "bus.ch.secret.agent-victim", "bus.ch.secret." + n.b.name, "bus.ch.home." + n.b.name,
		"bus.dm." + n.b.name + ".agent-victim", "bus.dm." + n.a.name + "." + n.b.name,
		"deliver." + n.b.name + ".ch.secret", "deliver." + n.b.name + ".ch.home", "deliver." + n.b.name + ".dm",
		n.b.presence(), "presence.crown.home." + n.b.name, "presence.crown.secret.agent-victim",
	}
}

func TestNativeConfigAsCreated(t *testing.T) {
	n := newNative(t, busStreamCfg)
	for _, st := range []string{"BUS", "PRESENCE"} {
		r, err := n.nc.Request("$JS.API.STREAM.INFO."+st, nil, 2*time.Second)
		if err != nil {
			t.Fatal(err)
		}
		var v struct {
			Config json.RawMessage `json:"config"`
		}
		if err := json.Unmarshal(r.Data, &v); err != nil {
			t.Fatal(err)
		}
		t.Logf("%s config: %s", st, v.Config)
		// What the server kept, not what was asked: a misspelt or ignored
		// switch would show here.
		var got map[string]any
		if err := json.Unmarshal(v.Config, &got); err != nil {
			t.Fatal(err)
		}
		want := map[string]any{"no_ack": true, "deny_delete": true, "deny_purge": true}
		for _, k := range []string{"allow_direct", "mirror_direct", "allow_rollup_hdrs", "allow_msg_ttl",
			"allow_msg_counter", "allow_atomic", "allow_msg_schedules", "allow_batched"} {
			want[k] = false
		}
		for k, w := range want {
			if g, present := got[k]; (present || w == true) && g != w {
				t.Errorf("%s: %s = %v, want %v", st, k, g, w)
			}
		}
		if mb, _ := got["max_bytes"].(float64); mb <= 0 {
			t.Errorf("%s: max_bytes = %v, want a limit", st, got["max_bytes"])
		}
		if ma, _ := got["max_age"].(float64); ma <= 0 {
			t.Errorf("%s: max_age = %v, want a limit", st, got["max_age"])
		}
		if mps, _ := got["max_msgs_per_subject"].(float64); st == "PRESENCE" && mps != 1 {
			t.Errorf("PRESENCE: max_msgs_per_subject = %v, want 1", got["max_msgs_per_subject"])
		}
	}
	for _, c := range []struct{ stream, name string }{{"BUS", n.b.name + "_ch_home"}, {"BUS", n.b.name + "_dm"}, {"PRESENCE", n.b.name + "_presence"}} {
		ci, err := n.js.ConsumerInfo(c.stream, c.name)
		if err != nil {
			t.Fatalf("consumer %s: %v", c.name, err)
		}
		cfg, _ := json.Marshal(ci.Config)
		t.Logf("consumer: %s", n.names(string(cfg)))
		if ci.Config.AckPolicy != nats.AckNonePolicy || ci.Config.FlowControl || !strings.HasPrefix(ci.Config.DeliverSubject, "deliver."+n.b.name+".") {
			t.Errorf("consumer %s: ack %v, flow control %v, deliver %s", c.name, ci.Config.AckPolicy, ci.Config.FlowControl, ci.Config.DeliverSubject)
		}
	}
}

// The presence consumer shows every agent's presence but crown claims only of
// the agent's own channels: a claim names its channel, and so would tell who
// is in a channel the agent is not in.
func TestNativePresenceShowsCrownsOnlyOfOwnChannels(t *testing.T) {
	n := newNative(t, busStreamCfg) // alice: home; bob: home and secret
	n.bobPosts(n.b.presence(), "bob online")
	// bob may not claim in a guest channel (RT-6.38): the claim in secret comes from the server's own side
	if err := n.nc.Publish("presence.crown.secret."+n.b.name, []byte("claim in secret")); err != nil {
		t.Fatal(err)
	}
	n.bobPosts("presence.crown.home."+n.b.name, "claim in home")
	seen := map[string]bool{}
	for _, g := range take(n.aIn, 500*time.Millisecond) {
		seen[g.Subject] = true
	}
	if !seen[n.b.presence()] || !seen["presence.crown.home."+n.b.name] {
		t.Errorf("alice misses bob's presence or his claim in home: %v", seen)
	}
	if seen["presence.crown.secret."+n.b.name] {
		t.Error("alice was delivered a crown claim of a channel she is not in")
	}
}

// A crown claim is allowed in the agent's home only (RT-6.38): bob, whose home is
// home and who is a guest in secret, is refused in secret and allowed in home, as
// the real server enforces the credential.
func TestNativeACrownClaimIsRefusedInAGuestChannel(t *testing.T) {
	n := newNative(t, busStreamCfg) // bob: home (his home) and secret (a guest)
	before := n.last("PRESENCE")
	if err := n.b.nc.Publish("presence.crown.secret."+n.b.name, []byte("claim in secret")); err != nil {
		t.Fatal(err)
	}
	if !n.b.denied(t) {
		t.Error("a crown claim in a guest channel was not refused")
	}
	if n.last("PRESENCE") != before {
		t.Error("a crown claim in a guest channel was stored")
	}
	n.bobPosts("presence.crown.home."+n.b.name, "claim in home")
	if n.last("PRESENCE") != before+1 {
		t.Errorf("the claim in the home was not stored: %d -> %d", before, n.last("PRESENCE"))
	}
}

// Own posts with a reply subject set to somebody else's subject: the stream
// stores the post under the publisher's name and answers nobody.
func TestNativeReplySubjectOnOwnPosts(t *testing.T) {
	n := newNative(t, busStreamCfg)
	own := []string{"bus.ch.home." + n.a.name, "bus.dm." + n.b.name + "." + n.a.name, n.a.presence(), "presence.crown.home." + n.a.name}
	for _, s := range own {
		for _, v := range n.victims() {
			denied, stored := n.attack("own "+s+" reply="+v, s, v, nil, "reply-attack")
			if denied || stored != 1 {
				t.Errorf("own post to %s with reply %s: denied=%v stored=%d", s, v, denied, stored)
			}
		}
	}
	n.audit()
	n.table()
}

// Every JetStream, key-value, system, inbox and foreign subject is refused,
// whatever the reply subject or the deliver_subject in the request says.
func TestNativeEveryServiceSubjectIsRefused(t *testing.T) {
	n := newNative(t, busStreamCfg)
	ac, bc := n.a.name+"_ch_home", n.b.name+"_ch_secret"
	body := `{"stream_name":"BUS","batch":1,"seq":1,"last_by_subj":"bus.ch.home.x","deliver_subject":"bus.ch.secret.agent-victim","config":{"deliver_subject":"bus.ch.secret.agent-victim","ack_policy":"none","durable_name":"x","filter_subject":"bus.>"}}`
	subjects := []string{
		"$JS.API.INFO", "$JS.API.STREAM.INFO.BUS", "$JS.API.STREAM.NAMES", "$JS.API.STREAM.LIST", "$JS.API.STREAM.CREATE.X",
		"$JS.API.STREAM.UPDATE.BUS", "$JS.API.STREAM.DELETE.BUS", "$JS.API.STREAM.PURGE.BUS", "$JS.API.STREAM.PURGE.PRESENCE",
		"$JS.API.STREAM.MSG.GET.BUS", "$JS.API.STREAM.MSG.DELETE.PRESENCE", "$JS.API.STREAM.SNAPSHOT.BUS", "$JS.API.STREAM.RESTORE.BUS",
		"$JS.API.DIRECT.GET.BUS", "$JS.API.DIRECT.GET.BUS.bus.ch.home." + n.a.name, "$JS.API.DIRECT.GET.PRESENCE.presence." + n.a.name,
		"$JS.API.CONSUMER.CREATE.BUS", "$JS.API.CONSUMER.CREATE.BUS.x", "$JS.API.CONSUMER.CREATE.BUS.x.bus.ch.secret.*",
		"$JS.API.CONSUMER.DURABLE.CREATE.BUS.x", "$JS.API.CONSUMER.INFO.BUS." + ac, "$JS.API.CONSUMER.MSG.NEXT.BUS." + ac,
		"$JS.API.CONSUMER.MSG.NEXT.BUS." + bc, "$JS.API.CONSUMER.DELETE.BUS." + bc, "$JS.API.CONSUMER.NAMES.BUS", "$JS.API.CONSUMER.LIST.BUS",
		"$JS.API.CONSUMER.PAUSE.BUS." + bc, "$JS.API.CONSUMER.UNPIN.BUS." + bc, "$JS.API.ACCOUNT.PURGE.BUS",
		"$JS.ACK.BUS." + ac + ".1.1.1.1.0", "$JS.ACK.BUS." + bc + ".1.1.1.1.0", "$JS.FC.BUS." + ac + ".x", "$JS.SNAPSHOT.ACK.BUS.x",
		"$JS.EVENT.ADVISORY.x", "$JSC.x", "$NRG.x", "$KV.presence." + n.a.name, "$KV.presence.x", "$O.x.C.y",
		"$SYS.REQ.SERVER.PING", "$SYS.REQ.USER.INFO", "$SYS.REQ.USER.AUTH", "$SYS.REQ.ACCOUNT.PING.CONNZ", "$SYS.x",
		"_INBOX.x", "_INBOX.x.y", "_R_.x", "$GR.x", "$GNR.x",
		"deliver." + n.b.name + ".ch.secret", "deliver." + n.b.name + ".dm", "deliver." + n.a.name + ".ch.home", "deliver.x",
		"bus.ch.secret." + n.a.name, "bus.ch.home." + n.b.name, "bus.ch.home.agent-victim", "bus.ch.home", "bus.ch.home." + n.a.name + ".x",
		"bus.dm." + n.b.name + "." + n.b.name, "bus.dm." + n.b.name, "bus.dm.a.b." + n.a.name,
		n.b.presence(), "presence.crown.home." + n.b.name, "presence.crown.secret." + n.a.name, "presence.x." + n.a.name,
		// the login service's attestations (RT-6.41): another's, and one's own
		"presence.attest." + n.b.name + ".v1", "presence.attest." + n.a.name + ".v1", "presence.attest.x.v1",
	}
	vs := n.victims()
	for i, s := range subjects {
		denied, stored := n.attack("publish "+s, s, vs[i%len(vs)], nil, body)
		if !denied || stored != 0 {
			t.Errorf("publish %s: denied=%v stored=%d", s, denied, stored)
		}
	}
	for _, s := range []string{"deliver." + n.b.name + ".>", "deliver." + n.b.name + ".ch.home", "deliver.>", "deliver.*.>", "deliver.*.ch.home", ">", "*.>",
		"_INBOX.>", "_INBOX.*", "bus.>", "bus.ch.home.*", "bus.ch.home." + n.a.name, "bus.dm." + n.a.name + ".*", "presence.>", n.a.presence(),
		"$JS.>", "$JS.API.>", "$JS.EVENT.ADVISORY.>", "$KV.>", "$SYS.>", "$SYS.ACCOUNT.*.CONNECT"} {
		if _, err := n.a.nc.SubscribeSync(s); err != nil {
			t.Fatal(err)
		}
		d := n.a.denied(t)
		n.rows = append(n.rows, [2]string{"subscribe " + n.names(s), fmt.Sprintf("denied=%v", d)})
		if !d {
			t.Errorf("subscribe %s: not denied", s)
		}
	}
	n.audit()
	n.table()
}

// Headers JetStream gives a meaning to, on alice's own posts, each with a
// reply subject pointing at a victim.
func TestNativeHeadersOnOwnPosts(t *testing.T) {
	n := newNative(t, busStreamCfg)
	n.bobPosts("bus.ch.secret."+n.b.name, "bob's secret post")
	n.bobPosts("bus.ch.home."+n.b.name, "bob's home post")
	n.bobPosts(n.b.presence(), "bob online")
	n.bobPosts("presence.crown.home."+n.b.name, "bob's claim")
	take(n.bIn, 300*time.Millisecond)
	busBefore, _ := n.js.StreamInfo("BUS")
	own, vs := "bus.ch.home."+n.a.name, n.victims()
	soon := time.Now().Add(1500 * time.Millisecond).UTC().Format(time.RFC3339)
	type h = map[string]string
	cases := []struct {
		what   string
		hdr    h
		stored int // -1: either
	}{
		{"Nats-Rollup: all", h{"Nats-Rollup": "all"}, 0},
		{"Nats-Rollup: sub", h{"Nats-Rollup": "sub"}, 0},
		{"Nats-Msg-Id", h{"Nats-Msg-Id": "id-1"}, 1},
		{"Nats-Msg-Id again (duplicate)", h{"Nats-Msg-Id": "id-1"}, -1},
		{"Nats-Expected-Stream: OTHER", h{"Nats-Expected-Stream": "OTHER"}, 0},
		{"Nats-Expected-Last-Sequence: 0", h{"Nats-Expected-Last-Sequence": "0"}, 0},
		{"Nats-Expected-Last-Subject-Sequence: 999", h{"Nats-Expected-Last-Subject-Sequence": "999"}, 0},
		{"Nats-Expected-Last-Subject-Sequence-Subject: bob's", h{"Nats-Expected-Last-Subject-Sequence": "1", "Nats-Expected-Last-Subject-Sequence-Subject": "bus.ch.secret." + n.b.name}, -1},
		{"Nats-Expected-Last-Msg-Id: nope", h{"Nats-Expected-Last-Msg-Id": "nope"}, 0},
		{"Nats-TTL: 1s", h{"Nats-TTL": "1s"}, 0},
		{"Nats-TTL: never", h{"Nats-TTL": "never"}, -1},
		{"Nats-Marker-Reason: Purge", h{"Nats-Marker-Reason": "Purge"}, -1},
		{"Nats-Marker-Reason: MaxAge + Nats-TTL", h{"Nats-Marker-Reason": "MaxAge", "Nats-TTL": "1s"}, 0},
		{"Nats-Schedule @at + Target = victim", h{"Nats-Schedule": "@at " + soon, "Nats-Schedule-Target": "bus.ch.secret.agent-victim"}, 0},
		{"Nats-Schedule @every + Target = bob", h{"Nats-Schedule": "@every 1s", "Nats-Schedule-Target": "bus.ch.secret." + n.b.name, "Nats-Schedule-TTL": "5s"}, 0},
		{"Nats-Scheduler / Nats-Schedule-Next (server's own marks)", h{"Nats-Scheduler": "bus.ch.secret." + n.b.name, "Nats-Schedule-Next": "purge"}, -1},
		{"Nats-Batch-Id/-Sequence", h{"Nats-Batch-Id": "b1", "Nats-Batch-Sequence": "1"}, 0},
		{"Nats-Batch-Id/-Sequence/-Commit", h{"Nats-Batch-Id": "b1", "Nats-Batch-Sequence": "2", "Nats-Batch-Commit": "1"}, 0},
		{"Nats-Incr: 1", h{"Nats-Incr": "1"}, 0},
		{"Nats-Counter-Sources", h{"Nats-Counter-Sources": `{"x":{"y":"1"}}`}, -1},
		{"Nats-Stream-Source (as if sourced)", h{"Nats-Stream-Source": "X 1 > bus.ch.secret.agent-victim"}, -1},
		{"Nats-Required-Api-Level: 99", h{"Nats-Required-Api-Level": "99"}, -1},
		{"Nats-Request-Info (forged client info)", h{"Nats-Request-Info": `{"acc":"BUS","user":"agent-victim"}`}, 1},
		{"Nats-Subject/-Stream/-Sequence/-Time-Stamp (direct-get look)", h{"Nats-Subject": "bus.ch.secret.agent-victim", "Nats-Stream": "BUS", "Nats-Sequence": "1", "Nats-Time-Stamp": "2020-01-01T00:00:00Z", "Nats-Last-Sequence": "0"}, 1},
		{"Nats-Trace-Dest = victim subject", h{"Nats-Trace-Dest": "bus.ch.secret.agent-victim"}, 0},
		{"Nats-Trace-Dest = bob's deliver subject", h{"Nats-Trace-Dest": "deliver." + n.b.name + ".ch.secret"}, 0},
		{"Nats-Trace-Dest = victim dm, Nats-Trace-Only", h{"Nats-Trace-Dest": "bus.dm." + n.b.name + ".agent-victim", "Nats-Trace-Only": "true"}, 0},
		{"Nats-Trace-Dest = _INBOX.x", h{"Nats-Trace-Dest": "_INBOX.x"}, 0},
		{"Nats-Trace-Dest = own subject (trace event under own name)", h{"Nats-Trace-Dest": own}, -1},
		{"Nats-Trace-Dest = own subject, Nats-Trace-Only", h{"Nats-Trace-Dest": own, "Nats-Trace-Only": "true"}, -1},
		{"traceparent sampled (account has no msg_trace)", h{"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}, 1},
		{"Nats-Trace-Hop / Nats-Trace-Origin-Account", h{"Nats-Trace-Hop": "1", "Nats-Trace-Origin-Account": "BUS"}, 1},
	}
	for i, c := range cases {
		denied, stored := n.attack("header "+c.what, own, vs[i%len(vs)], c.hdr, "hdr-attack "+c.what)
		if strings.Contains(c.what, "Trace-Dest =") && !strings.Contains(c.what, "own subject") {
			if !denied || stored != 0 {
				t.Errorf("%s: denied=%v stored=%d, want a permissions violation", c.what, denied, stored)
			}
			continue
		}
		if denied || (c.stored >= 0 && stored != c.stored) {
			t.Errorf("%s: denied=%v stored=%d, want stored=%d", c.what, denied, stored, c.stored)
		}
	}
	// The same on alice's presence subject: no rollup, bob's entries stay.
	for _, r := range []string{"all", "sub"} {
		if denied, stored := n.attack("presence: Nats-Rollup: "+r, n.a.presence(), vs[0], h{"Nats-Rollup": r}, "x"); denied || stored != 0 {
			t.Errorf("presence rollup %s: denied=%v stored=%d", r, denied, stored)
		}
	}
	time.Sleep(3 * time.Second) // a schedule would have fired by now
	for _, s := range []string{n.b.presence(), "presence.crown.home." + n.b.name, "bus.ch.secret." + n.b.name, "bus.ch.home." + n.b.name} {
		st := "BUS"
		if strings.HasPrefix(s, "presence.") {
			st = "PRESENCE"
		}
		if m, err := n.js.GetLastMsg(st, s); err != nil || sender(m.Subject) != n.b.name {
			t.Errorf("bob's %s is gone: %v", s, err)
		}
	}
	if si, _ := n.js.StreamInfo("BUS"); si.State.FirstSeq != busBefore.State.FirstSeq {
		t.Errorf("BUS lost messages: first seq %d -> %d", busBefore.State.FirstSeq, si.State.FirstSeq)
	}
	if got := take(n.bIn, 200*time.Millisecond); len(got) != 0 {
		t.Errorf("late deliveries to bob: %d, first %s", len(got), got[0].Subject)
	}
	n.audit()
	n.table()
}

// What a reader sees: the stream's subject (the sender is its last token),
// JetStream's own reply subject, and the publisher's headers untouched.
func TestNativeWhatADeliveredMessageLooksLike(t *testing.T) {
	n := newNative(t, busStreamCfg)
	m := nats.NewMsg("bus.ch.home." + n.a.name)
	m.Reply = "bus.ch.secret.agent-victim"
	m.Data = []byte("hello")
	for k, v := range map[string]string{"Nats-Subject": "bus.ch.secret.agent-victim", "Sender": "agent-victim", "Nats-Stream": "OTHER", "Nats-Sequence": "7"} {
		m.Header.Set(k, v)
	}
	if err := n.a.nc.PublishMsg(m); err != nil {
		t.Fatal(err)
	}
	var got *nats.Msg
	select {
	case got = <-n.bIn:
	case <-time.After(2 * time.Second):
		t.Fatal("nothing delivered")
	}
	t.Logf("delivered: Subject=%s Reply=%s Header=%v Data=%q", n.names(got.Subject), n.names(got.Reply), got.Header, got.Data)
	if got.Subject != "bus.ch.home."+n.a.name {
		t.Errorf("subject %s", got.Subject)
	}
	want := "$JS.ACK.BUS." + n.b.name + "_ch_home."
	if !strings.HasPrefix(got.Reply, want) {
		t.Errorf("reply %s, want prefix %s", got.Reply, want)
	}
	if md, err := got.Metadata(); err != nil {
		t.Logf("Metadata(): %v (parse the reply subject by hand)", err)
	} else {
		t.Logf("Metadata(): stream=%s consumer=%s stream seq=%d consumer seq=%d server time=%s", md.Stream, n.names(md.Consumer), md.Sequence.Stream, md.Sequence.Consumer, md.Timestamp.UTC().Format(time.RFC3339Nano))
	}
	// A direct message, and one sent to a literal wildcard recipient.
	for _, s := range []string{"bus.dm." + n.b.name + "." + n.a.name, "bus.dm.*." + n.a.name, "bus.dm.>." + n.a.name} {
		err := n.a.nc.Publish(s, []byte("dm"))
		d := err != nil || n.a.denied(t)
		var subs []string
		for _, g := range take(n.bIn, 300*time.Millisecond) {
			subs = append(subs, n.names(g.Subject))
			if sender(g.Subject) != n.a.name {
				t.Errorf("dm under another name: %s", g.Subject)
			}
		}
		t.Logf("dm publish %s: refused=%v (%v), bob got %v", n.names(s), d, err, subs)
	}
	// Alice has no consumer on the secret channel and reads nothing of it.
	n.bobPosts("bus.ch.secret."+n.b.name, "secret")
	n.bobPosts("bus.ch.home."+n.b.name, "open")
	var subs []string
	for _, g := range take(n.aIn, 400*time.Millisecond) {
		subs = append(subs, n.names(g.Subject))
		if strings.Contains(g.Subject, ".secret.") {
			t.Errorf("alice read %s", g.Subject)
		}
	}
	t.Logf("alice was delivered %v", subs)
	n.audit()
}

// Nats-Msg-Id is deduplicated per stream, not per subject: a claimed id
// swallows another agent's post with the same id inside the window.
func TestNativeMsgIdsAreStreamWide(t *testing.T) {
	long := strings.Replace(busStreamCfg, `"duplicate_window":100000000`, `"duplicate_window":120000000000`, 1)
	for _, c := range [][2]string{{"window 2m (default)", long}, {"window 100ms (minimum)", busStreamCfg}} {
		n := newNative(t, c[1])
		pub := func(ag *agent, id string) int {
			m := nats.NewMsg("bus.ch.home." + ag.name)
			if id != "" {
				m.Header.Set("Nats-Msg-Id", id)
			}
			b0 := n.last("BUS")
			_ = ag.nc.PublishMsg(m)
			_ = ag.nc.Flush()
			time.Sleep(30 * time.Millisecond)
			return int(n.last("BUS") - b0)
		}
		a1 := pub(n.a, "guess-1")
		b1 := pub(n.b, "guess-1")
		b2 := pub(n.b, "")
		time.Sleep(400 * time.Millisecond)
		b3 := pub(n.b, "guess-1")
		t.Logf("%s: alice claims id: stored %d; bob same id at once: stored %d; bob without id: stored %d; bob same id 450 ms later: stored %d", c[0], a1, b1, b2, b3)
		if a1 != 1 || b1 != 0 || b2 != 1 {
			t.Errorf("%s: unexpected: %d %d %d", c[0], a1, b1, b2)
		}
	}
}

// Presence and the crown on a plain stream with one message per subject.
func TestNativePresenceAndCrown(t *testing.T) {
	n := newNative(t, busStreamCfg)
	n.bobPosts(n.b.presence(), "bob online")
	if err := n.a.nc.Publish("presence.crown.home."+n.a.name, []byte("claim")); err != nil {
		t.Fatal(err)
	}
	if n.a.denied(t) {
		t.Fatal("alice's own claim refused")
	}
	n.bobPosts("presence.crown.home."+n.b.name, "claim")
	for i := 0; i < 50; i++ {
		_ = n.a.nc.Publish(n.a.presence(), []byte(fmt.Sprintf("beat %d", i)))
	}
	_ = n.a.nc.Flush()
	// The holder is the oldest claim by the stream's own sequence; the time
	// of each entry is the server's, both from JetStream's reply subject.
	holder := func(sub chan *nats.Msg) (string, map[string]uint64) {
		seqs := map[string]uint64{}
		for _, g := range take(sub, 400*time.Millisecond) {
			md, err := g.Metadata()
			if err != nil {
				t.Fatalf("%s: %v", g.Subject, err)
			}
			if strings.HasPrefix(g.Subject, "presence.crown.home.") && md.Sequence.Stream > seqs[sender(g.Subject)] {
				seqs[sender(g.Subject)] = md.Sequence.Stream
			}
		}
		who, low := "", uint64(0)
		for s, q := range seqs {
			if low == 0 || q < low {
				who, low = s, q
			}
		}
		return who, seqs
	}
	who, seqs := holder(n.bIn)
	t.Logf("claims by stream sequence: alice %d, bob %d; holder is alice: %v", seqs[n.a.name], seqs[n.b.name], who == n.a.name)
	if who != n.a.name {
		t.Errorf("holder %s, want alice (first claim)", who)
	}
	si, _ := n.js.StreamInfo("PRESENCE")
	t.Logf("PRESENCE after 50 beats: %d messages", si.State.Msgs)
	if si.State.Msgs != 4 {
		t.Errorf("PRESENCE holds %d messages, want 4 (one per subject)", si.State.Msgs)
	}
	// Alice cannot write or remove bob's entries, by subject, rollup or API.
	for _, s := range []string{n.b.presence(), "presence.crown.home." + n.b.name, "presence.crown.secret." + n.a.name,
		"presence.attest." + n.b.name + ".v1", "presence.attest." + n.a.name + ".v1",
		"$JS.API.STREAM.PURGE.PRESENCE", "$JS.API.STREAM.MSG.DELETE.PRESENCE", "$KV.presence." + n.b.name} {
		if denied, stored := n.attack("presence: publish "+s, s, n.b.presence(), nil, `{"seq":1,"filter":"presence.>"}`); !denied || stored != 0 {
			t.Errorf("%s: denied=%v stored=%d", s, denied, stored)
		}
	}
	for _, r := range []string{"all", "sub"} {
		if denied, stored := n.attack("presence: own claim with Nats-Rollup: "+r, "presence.crown.home."+n.a.name, n.b.presence(), map[string]string{"Nats-Rollup": r}, "x"); denied || stored != 0 {
			t.Errorf("rollup %s: denied=%v stored=%d", r, denied, stored)
		}
	}
	for _, s := range []string{n.b.presence(), "presence.crown.home." + n.b.name} {
		m, err := n.js.GetLastMsg("PRESENCE", s)
		if err != nil || !n.bobSent[s+"|"+string(m.Data)] {
			t.Errorf("bob's %s changed or gone: %v", s, err)
		}
	}
	// Even the admin cannot purge or delete (deny_purge, deny_delete).
	t.Logf("admin purge: %v; admin delete: %v", n.js.PurgeStream("PRESENCE"), n.js.DeleteMsg("PRESENCE", n.last("PRESENCE")))
	// A claim made again goes to the back of the line.
	take(n.bIn, 200*time.Millisecond)
	_ = n.a.nc.Publish("presence.crown.home."+n.a.name, []byte("claim again"))
	_ = n.a.nc.Flush()
	_, again := holder(n.bIn)
	if again[n.a.name] <= seqs[n.b.name] {
		t.Errorf("alice's new claim %d is not behind bob's %d", again[n.a.name], seqs[n.b.name])
	}
	t.Logf("alice claims again: her sequence %d is now behind bob's %d", again[n.a.name], seqs[n.b.name])
	n.audit()
	n.table()
}

// Controls: the same attacks succeed once a stream setting is left at the
// wrong value, so the audit above would have caught them.
func TestNativeControlsWhatTheSettingsClose(t *testing.T) {
	t.Run("a stream that acknowledges", func(t *testing.T) {
		n := newNative(t, strings.Replace(busStreamCfg, `"no_ack":true`, `"no_ack":false`, 1))
		_ = n.a.nc.PublishRequest("bus.ch.home."+n.a.name, "bus.ch.secret.agent-victim", []byte("x"))
		forged := false
		for _, g := range take(n.bIn, 500*time.Millisecond) {
			t.Logf("bob was delivered: Subject=%s Reply=%s Data=%.90q", n.names(g.Subject), n.names(g.Reply), g.Data)
			forged = forged || g.Subject == "bus.ch.secret.agent-victim"
		}
		if !forged {
			t.Error("no forged delivery: the control did not reproduce the hole")
		}
	})
	t.Run("schedules allowed", func(t *testing.T) {
		cfg := strings.NewReplacer(`"allow_msg_schedules":false`, `"allow_msg_schedules":true`, `"allow_msg_ttl":false`, `"allow_msg_ttl":true`).Replace(busStreamCfg)
		n := newNative(t, cfg)
		m := nats.NewMsg("bus.ch.home." + n.a.name)
		m.Header.Set("Nats-Schedule", "@at "+time.Now().Add(1500*time.Millisecond).UTC().Format(time.RFC3339))
		m.Header.Set("Nats-Schedule-Target", "bus.ch.secret.agent-victim")
		m.Data = []byte("scheduled forgery")
		_ = n.a.nc.PublishMsg(m)
		forged := false
		for _, g := range take(n.bIn, 4*time.Second) {
			t.Logf("bob was delivered: Subject=%s Header=%v Data=%.90q", n.names(g.Subject), g.Header, g.Data)
			forged = forged || g.Subject == "bus.ch.secret.agent-victim"
		}
		if !forged {
			t.Error("no scheduled forgery: the control did not reproduce the hole")
		}
	})
	t.Run("rollups allowed", func(t *testing.T) {
		cfg := strings.NewReplacer(`"allow_rollup_hdrs":false`, `"allow_rollup_hdrs":true`, `"deny_purge":true`, `"deny_purge":false`).Replace(busStreamCfg)
		n := newNative(t, cfg)
		n.bobPosts("bus.ch.secret."+n.b.name, "bob's secret post")
		m := nats.NewMsg("bus.ch.home." + n.a.name)
		m.Header.Set("Nats-Rollup", "all")
		_ = n.a.nc.PublishMsg(m)
		_ = n.a.nc.Flush()
		time.Sleep(200 * time.Millisecond)
		_, err := n.js.GetLastMsg("BUS", "bus.ch.secret."+n.b.name)
		t.Logf("with allow_rollup_hdrs bob's post after alice's rollup: %v", err)
		if err == nil {
			t.Error("the rollup did not remove bob's post: the control did not reproduce")
		}
	})
}

// A push consumer delivers only while somebody subscribes to exactly its
// deliver subject; a wildcard subscription above it leaves it waiting (the
// messages are kept, not lost).
func TestNativeAConsumerWaitsForItsLiteralSubject(t *testing.T) {
	l := newLabCfg(t, nil, 0, nil)
	n := &native{t: t, l: l, bobSent: map[string]bool{}}
	n.nc, n.js = l.admin()
	n.stream("BUS", busStreamCfg)
	n.stream("PRESENCE", presenceStreamCfg)
	n.a, n.b = l.twoAgents()
	n.login(n.b, "home")
	ch := make(chan *nats.Msg, 16)
	if _, err := n.b.nc.ChanSubscribe("deliver."+n.b.name+".>", ch); err != nil {
		t.Fatal(err)
	}
	if n.b.denied(t) {
		t.Fatal("refused")
	}
	n.bobPosts("bus.ch.home."+n.b.name, "one")
	ci, _ := n.js.ConsumerInfo("BUS", n.b.name+"_ch_home")
	t.Logf("wildcard subscription only: delivered %d, pending %d, push_bound %v", len(take(ch, 400*time.Millisecond)), ci.NumPending, ci.PushBound)
	if ci.PushBound {
		t.Error("a wildcard now binds a push consumer: the literal subscriptions are no longer needed")
	}
	got := take(n.listen(n.b, []string{"deliver." + n.b.name + ".ch.home"}), 500*time.Millisecond)
	ci, _ = n.js.ConsumerInfo("BUS", n.b.name+"_ch_home")
	t.Logf("literal subscription: delivered %d, pending %d, push_bound %v", len(got), ci.NumPending, ci.PushBound)
	if len(got) != 1 {
		t.Errorf("delivered %d, want the one waiting message", len(got))
	}
}

// The owner is pinned in the presence subject by the credential, like the
// sender: only the own owner token and the own name publish, and a reader
// takes the owner from the subject.
func TestNativePresenceCarriesTheOwnerPinnedByTheCredential(t *testing.T) {
	n := newNative(t, busStreamCfg)
	if n.a.owner == n.b.owner {
		t.Fatal("the test needs two owners")
	}
	n.bobPosts(n.b.presence(), "bob online")
	take(n.aIn, 300*time.Millisecond)
	take(n.bIn, 300*time.Millisecond)
	for _, s := range []string{
		"presence." + ownerToken(n.b.owner) + "." + n.a.name, // another owner's token
		"presence.-." + n.a.name,
		"presence." + n.a.name, // the old form
	} {
		if denied, stored := n.attack("presence "+s, s, "", nil, "x"); !denied || stored != 0 {
			t.Errorf("publish %s: denied=%v stored=%d, want denied", s, denied, stored)
		}
	}
	want := "presence." + ownerToken(n.a.owner) + "." + n.a.name
	if want != n.a.presence() {
		t.Fatalf("presence() = %s", n.a.presence())
	}
	if err := n.a.nc.Publish(want, []byte("alice online")); err != nil {
		t.Fatal(err)
	}
	if n.a.denied(t) {
		t.Fatalf("publish %s denied", want)
	}
	seen := map[string]bool{}
	for _, g := range take(n.bIn, 500*time.Millisecond) {
		seen[g.Subject] = true
	}
	if !seen[want] {
		t.Errorf("bob's presence consumer misses %s: %v", want, seen)
	}
}

// An agent publishes its membership on its own member subject in its channels
// only: not in a channel it was not granted, not under another name (RT-6.37).
func TestNativeMemberSubjectIsOwnAndOnlyInGrantedChannels(t *testing.T) {
	n := newNative(t, busStreamCfg) // alice: home; bob: home and secret
	denied, stored := n.attack("own member subject", "presence.member.home."+n.a.name, n.b.presence(), nil, "member")
	if denied || stored != 1 {
		t.Errorf("own member subject in home: denied=%v stored=%d", denied, stored)
	}
	for _, s := range []string{"presence.member.secret." + n.a.name, "presence.member.home." + n.b.name,
		"presence.member.secret." + n.b.name, "presence.member.home.agent-victim", "presence.member.home", "presence.member.home." + n.a.name + ".x"} {
		denied, stored := n.attack("member "+s, s, n.b.presence(), nil, "member")
		if !denied || stored != 0 {
			t.Errorf("publish %s: denied=%v stored=%d", s, denied, stored)
		}
	}
	// Membership is shown only for the channels a reader is in.
	n.bobPosts("presence.member.secret."+n.b.name, "bob in secret")
	n.bobPosts("presence.member.home."+n.b.name, "bob in home")
	seen := map[string]bool{}
	for _, g := range take(n.aIn, 500*time.Millisecond) {
		seen[g.Subject] = true
	}
	if !seen["presence.member.home."+n.b.name] || seen["presence.member.secret."+n.b.name] {
		t.Errorf("alice's view of bob's memberships: %v", seen)
	}
}

// The console's asks and STATS lines (RT-6.43), as the real server enforces the
// credential: bob may publish his one ask record in his home and his STATS line;
// not in his guest channel, and alice may publish neither under bob's name nor
// on a further subject.
func TestNativeAskRecordAndStatsOnlyUnderOwnName(t *testing.T) {
	n := newNative(t, busStreamCfg) // alice: home; bob: home and secret (a guest)
	n.stream(consumers.StatsStream, consumers.StatsStreamJSON("stats", 0))
	before, stats := n.last("PRESENCE"), n.last(consumers.StatsStream)
	n.bobPosts("presence.ask.home."+n.b.name, `{"v":1,"asks":[],"more":0}`)
	n.bobPosts("stats."+n.b.name, `{"v":"0.101.0"}`)
	if n.last("PRESENCE") != before+1 || n.last(consumers.StatsStream) != stats+1 {
		t.Fatalf("bob's own ask record or STATS line was not stored: %d -> %d, %d -> %d",
			before, n.last("PRESENCE"), stats, n.last(consumers.StatsStream))
	}
	refused := func(a *agent, subject string) {
		t.Helper()
		if err := a.nc.Publish(subject, []byte("x")); err != nil {
			t.Fatal(err)
		}
		if !a.denied(t) {
			t.Errorf("publish %s by %s: not refused", subject, a.name)
		}
	}
	refused(n.b, "presence.ask.secret."+n.b.name) // a guest channel is not the home
	refused(n.a, "presence.ask.home."+n.b.name)   // another agent's record
	refused(n.a, "presence.ask.home."+n.a.name+".1")
	refused(n.a, "presence.ask.secret."+n.a.name)
	refused(n.a, "stats."+n.b.name)
	refused(n.a, "stats."+n.a.name+".x")
	refused(n.a, "stats.x")
	if n.last("PRESENCE") != before+1 || n.last(consumers.StatsStream) != stats+1 {
		t.Error("a refused ask record or STATS line was stored")
	}
	if _, err := n.a.nc.SubscribeSync("stats.>"); err != nil {
		t.Fatal(err)
	}
	if !n.a.denied(t) {
		t.Error("an agent may read the STATS lines")
	}
}
