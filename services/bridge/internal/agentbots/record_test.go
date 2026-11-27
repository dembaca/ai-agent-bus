package agentbots

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
)

// pubs records what the bridge publishes.
type pubs struct {
	mu   sync.Mutex
	msgs []pubMsg
}

type pubMsg struct {
	subject string
	body    map[string]any
}

func (p *pubs) Publish(subject string, data []byte) error {
	var body map[string]any
	if err := json.Unmarshal(data, &body); err != nil {
		return err
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.msgs = append(p.msgs, pubMsg{subject, body})
	return nil
}

func (p *pubs) last() (pubMsg, int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.msgs) == 0 {
		return pubMsg{}, 0
	}
	return p.msgs[len(p.msgs)-1], len(p.msgs)
}

// RT-6.44 step 3: after the bot is set up and its token minted the bridge
// publishes the record, exactly these keys, valid three minutes; the minute's
// round publishes it again for a bot that holds a client.
func TestRT644ThePublishedBotRecordNamesTheBotAndItsOwnerAccount(t *testing.T) {
	f := newFake(t)
	b := newBots(t, f, aliceAgent())
	p := &pubs{}
	b.NATS = p
	c := newClock(t0)
	b.Now = c.now
	if b.Bot(context.Background(), "agent-a", "home") == nil {
		t.Fatal("no bot")
	}
	m, n := p.last()
	if n != 1 || m.subject != "presence.bot.agent-a.v1" {
		t.Fatalf("published %d, last on %q", n, m.subject)
	}
	want := map[string]any{"v": float64(1), "agent": "agent-a", "bot": botID(f, "bb-agent-a"),
		"username": "bb-agent-a", "owner_account": "alice",
		"iat": float64(t0.Unix()), "exp": float64(t0.Add(3 * time.Minute).Unix())}
	if len(m.body) != len(want) {
		t.Fatalf("body %v, want %v", m.body, want)
	}
	for k, v := range want {
		if m.body[k] != v {
			t.Errorf("%s = %v, want %v", k, m.body[k], v)
		}
	}
	// a second message of the same agent sets nothing up: no new record
	b.Bot(context.Background(), "agent-a", "home")
	if _, n := p.last(); n != 1 {
		t.Fatalf("a second post published again: %d", n)
	}
	c.add(time.Minute)
	b.Announce()
	m, n = p.last()
	if n != 2 || m.body["bot"] != botID(f, "bb-agent-a") || m.body["iat"] != float64(t0.Add(time.Minute).Unix()) {
		t.Fatalf("the minute's round: %d, %v", n, m.body)
	}
}

// RT-6.44 step 8: the sweep publishes bot null and the minute's round stops
// for that name.
func TestRT644TheSweepPublishesNullAndStops(t *testing.T) {
	_, b, snap, c, _ := sweepWorld(t)
	p := &pubs{}
	b.NATS = p
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	c.at(25 * time.Hour)
	if n := pass(s, snap, c); n != 1 {
		t.Fatalf("swept %d", n)
	}
	m, n := p.last()
	if n != 1 || m.subject != "presence.bot.agent-a.v1" || m.body["bot"] != nil ||
		m.body["username"] != nil || m.body["owner_account"] != nil || m.body["agent"] != "agent-a" {
		t.Fatalf("after the sweep: %d, %v", n, m)
	}
	b.Announce()
	if _, n2 := p.last(); n2 != n {
		t.Fatalf("the round published for a swept bot")
	}
}

// RT-6.44 step 3: a bot an admin disabled gets null, not a link.
func TestRT644AnAdminDisabledBotGetsNull(t *testing.T) {
	f := newFake(t)
	f.addBot("off", "bb-agent-a", Marker+"agent-a")
	f.set("off", "owner_id", "alice")
	f.set("off", "delete_at", int64(1))
	b := newBots(t, f, aliceAgent())
	p := &pubs{}
	b.NATS = p
	if b.Bot(context.Background(), "agent-a", "home") != nil {
		t.Fatal("posted with a disabled bot")
	}
	m, n := p.last()
	if n != 1 || m.body["bot"] != nil {
		t.Fatalf("published %d, %v", n, m.body)
	}
}

// A bot that failed to be set up, or an agent without an owner record, is
// announced with nothing.
func TestRT644NoRecordWithoutABot(t *testing.T) {
	f := newFake(t)
	b := newBotsOf(t, f, aliceAgent(), owners{})
	p := &pubs{}
	b.NATS = p
	if b.Bot(context.Background(), "agent-a", "home") != nil {
		t.Fatal("a bot without an owner")
	}
	b.Announce()
	if _, n := p.last(); n != 0 {
		t.Fatalf("published %d", n)
	}
}

// RT-6.44 step 3: after a restart the find-only pass publishes the record of
// an idle agent whose bot exists, and creates nothing for one without a bot.
func TestRT644TheFindPassAnnouncesAnIdleAgentsBot(t *testing.T) {
	f := newFake(t)
	f.addBot("b1", "bb-agent-a", Marker+"agent-a")
	f.set("b1", "owner_id", "alice")
	snap := aliceAgent()
	snap.Observed("agent-b", "alice~x", "", presence.Online, time.Now())
	b := newBots(t, f, snap)
	p := &pubs{}
	b.NATS = p
	b.AnnounceFound(context.Background())
	m, n := p.last()
	if n != 1 || m.subject != "presence.bot.agent-a.v1" || m.body["bot"] != "b1" ||
		m.body["username"] != "bb-agent-a" || m.body["owner_account"] != "alice" {
		t.Fatalf("published %d, %v", n, m)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.created != 0 {
		t.Fatalf("the find pass created %d bots", f.created)
	}
}

// RT-6.44 step 3: the find-only pass looks up only present agents, those
// with a presence within FindWindow of the newest; a name gone quiet longer
// costs no admin read and gets no record.
func TestRT644TheFindPassSkipsAnAgentGoneQuiet(t *testing.T) {
	f := newFake(t)
	f.addBot("b1", "bb-agent-a", Marker+"agent-a")
	f.set("b1", "owner_id", "alice")
	now := time.Now()
	snap := presence.New()
	snap.Observed("agent-a", "alice~x", "", presence.Online, now.Add(-FindWindow-time.Minute))
	snap.Observed("agent-b", "alice~x", "", presence.Online, now)
	b := newBots(t, f, snap)
	p := &pubs{}
	b.NATS = p
	b.AnnounceFound(context.Background())
	if m, n := p.last(); n != 0 {
		t.Fatalf("published %d for a quiet agent: %v", n, m)
	}
}

// Owner binding on every path: a bot of another owner gets null from the find
// pass; a changed owner record gets null from Bot and from the round.
func TestRT644ABotOfAnotherOwnerGetsNull(t *testing.T) {
	f := newFake(t)
	f.addBot("b1", "bb-agent-a", Marker+"agent-a")
	f.set("b1", "owner_id", "bob")
	b := newBots(t, f, aliceAgent())
	p := &pubs{}
	b.NATS = p
	b.AnnounceFound(context.Background())
	if m, n := p.last(); n != 1 || m.body["bot"] != nil {
		t.Fatalf("find pass: %d, %v", n, m.body)
	}

	f2 := newFake(t)
	o := aliceRecord()
	b2 := newBotsOf(t, f2, aliceAgent(), o)
	p2 := &pubs{}
	b2.NATS = p2
	if b2.Bot(context.Background(), "agent-a", "home") == nil {
		t.Fatal("no bot")
	}
	o["alice.x"] = "bob"
	b2.Announce()
	if m, n := p2.last(); n != 2 || m.body["bot"] != nil {
		t.Fatalf("round after the owner changed: %d, %v", n, m.body)
	}
	b2.Bot(context.Background(), "agent-a", "home")
	if m, n := p2.last(); n != 3 || m.body["bot"] != nil {
		t.Fatalf("Bot after the owner changed: %d, %v", n, m.body)
	}
}
