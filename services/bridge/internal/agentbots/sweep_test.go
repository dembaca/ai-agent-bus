package agentbots

import (
	"context"
	"fmt"
	"slices"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
)

// clock is the sweep tests' fake clock.
type clock struct{ t time.Time }

func (c *clock) now() time.Time { return c.t }

var t0 = time.Date(2026, 11, 26, 8, 0, 0, 0, time.UTC)

// sweepWorld is one bridge whose agent-a has its bot bb-agent-a, made and
// minted at t0, its last presence at t0; the bridge started at t0.
func sweepWorld(t *testing.T) (*fakeMM, *Bots, *presence.Snapshot, *clock, string) {
	t.Helper()
	f := newFake(t)
	snap := presence.New()
	snap.Observed("agent-a", "alice~x", "", presence.Online, t0)
	c := &clock{t: t0}
	b := newBots(t, f, snap)
	b.Now = c.now
	if b.Bot(context.Background(), "agent-a", "home") == nil {
		t.Fatal("no bot for agent-a")
	}
	return f, b, snap, c, botID(f, "bb-agent-a")
}

func botID(f *fakeMM, username string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	for id, b := range f.bots {
		if b["username"] == username {
			return id
		}
	}
	return ""
}

// addBot puts a bot with one active token into the fake, as someone made it.
func (f *fakeMM) addBot(id, username, description string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.users[id] = map[string]any{"id": id, "username": username, "is_bot": true, "roles": "system_user"}
	f.bots[id] = map[string]any{"user_id": id, "username": username, "description": description, "owner_id": "bob"}
	tok := map[string]any{"id": "tid-" + id, "token": "tok-" + id, "description": "theirs", "is_active": true}
	f.tokens[id] = append(f.tokens[id], tok)
	f.byToken["tok-"+id] = id
}

func (f *fakeMM) disabled(id string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.bots[id]["delete_at"] != nil
}

func (f *fakeMM) activeTokens(id string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, t := range f.tokens[id] {
		if t["is_active"] == true {
			n++
		}
	}
	return n
}

// RT-6.44 step 8 (a): a bot the bridge made, whose agent showed no presence
// for longer than the keep time, is disabled and all its tokens revoked.
func TestRT644SweepDisablesAnIdleBridgeBot(t *testing.T) {
	f, b, _, c, id := sweepWorld(t)
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	c.t = t0.Add(24 * time.Hour)
	if n := s.Pass(context.Background()); n != 0 {
		t.Fatalf("swept %d at exactly the keep time", n)
	}
	c.t = t0.Add(25 * time.Hour)
	if n := s.Pass(context.Background()); n != 1 {
		t.Fatalf("swept %d, want 1", n)
	}
	if !f.disabled(id) || f.activeTokens(id) != 0 {
		t.Fatalf("disabled %v, active tokens %d", f.disabled(id), f.activeTokens(id))
	}
	if e := b.entry("agent-a"); e.client != nil || len(e.joined) != 0 || !e.failed.IsZero() {
		t.Fatalf("the entry is kept: %+v", e)
	}
	if n := s.Pass(context.Background()); n != 0 {
		t.Fatalf("a disabled bot swept again: %d", n)
	}
}

// RT-6.44 step 8 (b): an agent seen within the keep time keeps its bot.
func TestRT644SweepKeepsABotSeenWithinTheKeepTime(t *testing.T) {
	f, b, snap, c, id := sweepWorld(t)
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	snap.Observed("agent-a", "alice~x", "", presence.Bye, t0.Add(20*time.Hour))
	c.t = t0.Add(43 * time.Hour)
	if n := s.Pass(context.Background()); n != 0 || f.disabled(id) || f.activeTokens(id) != 1 {
		t.Fatalf("swept %d, disabled %v, tokens %d", n, f.disabled(id), f.activeTokens(id))
	}
	c.t = t0.Add(45 * time.Hour)
	if n := s.Pass(context.Background()); n != 1 {
		t.Fatalf("swept %d after the bye's keep time", n)
	}
}

// RT-6.44 step 8 (c): a bot with the prefix but without the bridge's mark for
// its name (the registrar's, another's), or with the mark under another
// prefix, is never touched, however long its name was idle.
func TestRT644SweepNeverTouchesABotWithoutTheMark(t *testing.T) {
	f := newFake(t)
	f.addBot("reg", "bb-agent-r", "registrar bot")
	f.addBot("other", "bb-agent-o", Marker+"agent-x")
	f.addBot("noprefix", "agent-n", Marker+"agent-n")
	f.addBot("bare", "bb-", Marker)
	b := newBots(t, f, presence.New())
	c := &clock{t: t0.Add(1000 * time.Hour)}
	b.Now = c.now
	s := &Sweeper{Bots: b, Keep: time.Hour, Start: t0}
	if n := s.Pass(context.Background()); n != 0 {
		t.Fatalf("swept %d", n)
	}
	for _, id := range []string{"reg", "other", "noprefix", "bare"} {
		if f.disabled(id) || f.activeTokens(id) != 1 {
			t.Errorf("%s touched", id)
		}
	}
	if len(f.revoked) != 0 || len(f.toggled) != 0 || f.listed != 1 {
		t.Fatalf("revoked %v, toggled %v, listed %d", f.revoked, f.toggled, f.listed)
	}
}

// RT-6.44 step 8 (d): after a restart every bot counts as seen at the start,
// so nothing is swept before one keep time, whatever the presence says.
func TestRT644SweepWaitsOneKeepTimeAfterARestart(t *testing.T) {
	f, b, _, c, id := sweepWorld(t)
	restart := t0.Add(100 * time.Hour)
	c.t = restart
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: restart}
	for _, after := range []time.Duration{time.Minute, time.Hour, 24 * time.Hour} {
		c.t = restart.Add(after)
		if n := s.Pass(context.Background()); n != 0 || f.disabled(id) {
			t.Fatalf("swept %d %s after the restart", n, after)
		}
	}
	c.t = restart.Add(24*time.Hour + 10*time.Minute)
	if n := s.Pass(context.Background()); n != 1 {
		t.Fatalf("swept %d after one keep time", n)
	}
}

// RT-6.44 step 8 (e): one pass disables at most Cap bots; the rest goes at
// the next pass.
func TestRT644SweepStopsAtTheCap(t *testing.T) {
	f := newFake(t)
	for i := range 5 {
		f.addBot(fmt.Sprintf("b%d", i), fmt.Sprintf("bb-agent-%d", i), Marker+fmt.Sprintf("agent-%d", i))
	}
	b := newBots(t, f, presence.New())
	c := &clock{t: t0.Add(2 * time.Hour)}
	b.Now = c.now
	s := &Sweeper{Bots: b, Keep: time.Hour, Start: t0, Cap: 2}
	for i, want := range []int{2, 2, 1, 0} {
		if n := s.Pass(context.Background()); n != want {
			t.Fatalf("pass %d swept %d, want %d", i, n, want)
		}
	}
	if len(f.toggled) != 5 {
		t.Fatalf("toggled %v", f.toggled)
	}
}

// RT-6.44 step 8 (f): a swept agent that comes back gets its bot enabled
// again and a fresh token, at its next message, without a backoff; no
// second bot is made.
func TestRT644ASweptAgentGetsItsBotBack(t *testing.T) {
	f, b, snap, c, id := sweepWorld(t)
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	c.t = t0.Add(25 * time.Hour)
	if s.Pass(context.Background()) != 1 {
		t.Fatal("not swept")
	}
	snap.Observed("agent-a", "alice~x", "", presence.Online, c.t.Add(time.Minute))
	c.t = c.t.Add(time.Minute)
	sink := b.Bot(context.Background(), "agent-a", "home")
	if sink == nil {
		t.Fatal("the bridge bot posts for the agent that came back")
	}
	if f.disabled(id) || f.activeTokens(id) != 1 || f.created != 1 {
		t.Fatalf("disabled %v, active tokens %d, created %d", f.disabled(id), f.activeTokens(id), f.created)
	}
	if !slices.Equal(f.toggled, []string{"disable " + id, "enable " + id}) {
		t.Fatalf("toggled %v", f.toggled)
	}
	// seen now: the next pass keeps it
	c.t = c.t.Add(time.Hour)
	if n := s.Pass(context.Background()); n != 0 {
		t.Fatalf("the returned agent's bot swept again: %d", n)
	}
}

// RT-6.44 step 8: a failure on one bot is logged and the next bot is tried;
// a failed listing sweeps nothing.
func TestRT644SweepGoesOnAfterAFailure(t *testing.T) {
	f := newFake(t)
	f.addBot("b1", "bb-agent-1", Marker+"agent-1")
	f.addBot("b2", "bb-agent-2", Marker+"agent-2")
	b := newBots(t, f, presence.New())
	c := &clock{t: t0.Add(2 * time.Hour)}
	b.Now = c.now
	f.failDisable = "b1"
	s := &Sweeper{Bots: b, Keep: time.Hour, Start: t0}
	if n := s.Pass(context.Background()); n != 1 || !f.disabled("b2") {
		t.Fatalf("swept %d, b2 disabled %v", n, f.disabled("b2"))
	}
	f.srv.Close()
	if n := s.Pass(context.Background()); n != 0 {
		t.Fatalf("swept %d without Mattermost", n)
	}
}
