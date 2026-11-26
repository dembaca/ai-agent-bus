package agentbots

import (
	"bytes"
	"context"
	"fmt"
	"log/slog"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
)

// clock is the sweep tests' fake clock, safe for concurrent use.
type clock struct{ ns atomic.Int64 }

func newClock(t time.Time) *clock {
	c := &clock{}
	c.set(t)
	return c
}

func (c *clock) now() time.Time      { return time.Unix(0, c.ns.Load()).UTC() }
func (c *clock) set(t time.Time)     { c.ns.Store(t.UnixNano()) }
func (c *clock) add(d time.Duration) { c.ns.Add(int64(d)) }
func (c *clock) at(d time.Duration)  { c.set(t0.Add(d)) }

var t0 = time.Date(2026, 11, 26, 8, 0, 0, 0, time.UTC)

// pulse is another agent's presence now: the feed is alive.
func pulse(snap *presence.Snapshot, c *clock) {
	snap.Observed("zz-other", "bob", "", presence.Online, c.now())
}

// pass is one sweep pass with the presence feed alive.
func pass(s *Sweeper, snap *presence.Snapshot, c *clock) int {
	pulse(snap, c)
	return s.Pass(context.Background())
}

// sweepWorld is one bridge whose agent-a has its bot bb-agent-a, made and
// minted at t0 (its last message), its last presence at t0; the bridge
// started at t0.
func sweepWorld(t *testing.T) (*fakeMM, *Bots, *presence.Snapshot, *clock, string) {
	t.Helper()
	f := newFake(t)
	snap := presence.New()
	snap.Observed("agent-a", "alice~x", "", presence.Online, t0)
	c := newClock(t0)
	b := newBots(t, f, snap)
	b.Now = c.now
	if b.Bot(context.Background(), "agent-a", "home") == nil {
		t.Fatal("no bot for agent-a")
	}
	return f, b, snap, c, botID(f, "bb-agent-a")
}

// emptyWorld is a bridge without agents, its clock at t0+at.
func emptyWorld(t *testing.T, f *fakeMM, at time.Duration) (*Bots, *presence.Snapshot, *clock) {
	t.Helper()
	snap := presence.New()
	b := newBots(t, f, snap)
	c := newClock(t0.Add(at))
	b.Now = c.now
	return b, snap, c
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

func (f *fakeMM) description(id string) any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.bots[id]["description"]
}

// set changes one field of a bot, as an admin would.
func (f *fakeMM) set(id, key string, v any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.bots[id][key] = v
	if key == "delete_at" {
		f.users[id]["delete_at"] = v
	}
}

func (f *fakeMM) listings() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.listed
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

// logs is a log sink the tests count lines in.
type logs struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (l *logs) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.buf.Write(p)
}

func (l *logs) count(s string) int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return strings.Count(l.buf.String(), s)
}

func (l *logs) logger() *slog.Logger { return slog.New(slog.NewTextHandler(l, nil)) }

// RT-6.44 step 8 (a): a bot the bridge made, whose agent showed no sign for
// longer than the keep time, is marked swept, disabled and all its tokens
// revoked.
func TestRT644SweepDisablesAnIdleBridgeBot(t *testing.T) {
	f, b, snap, c, id := sweepWorld(t)
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	c.at(24 * time.Hour)
	if n := pass(s, snap, c); n != 0 {
		t.Fatalf("swept %d at exactly the keep time", n)
	}
	c.at(25 * time.Hour)
	if n := pass(s, snap, c); n != 1 {
		t.Fatalf("swept %d, want 1", n)
	}
	if !f.disabled(id) || f.activeTokens(id) != 0 || f.description(id) != Marker+"agent-a"+SweptSuffix {
		t.Fatalf("disabled %v, active tokens %d, description %v", f.disabled(id), f.activeTokens(id), f.description(id))
	}
	if e := b.entry("agent-a"); e.client != nil || len(e.joined) != 0 || !e.failed.IsZero() {
		t.Fatalf("the entry is kept: client %v joined %v", e.client, e.joined)
	}
	if n := pass(s, snap, c); n != 0 {
		t.Fatalf("a disabled bot swept again: %d", n)
	}
}

// RT-6.44 step 8 (b): an agent seen within the keep time keeps its bot.
func TestRT644SweepKeepsABotSeenWithinTheKeepTime(t *testing.T) {
	f, b, snap, c, id := sweepWorld(t)
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	snap.Observed("agent-a", "alice~x", "", presence.Bye, t0.Add(20*time.Hour))
	c.at(43 * time.Hour)
	if n := pass(s, snap, c); n != 0 || f.disabled(id) || f.activeTokens(id) != 1 {
		t.Fatalf("swept %d, disabled %v, tokens %d", n, f.disabled(id), f.activeTokens(id))
	}
	c.at(45 * time.Hour)
	if n := pass(s, snap, c); n != 1 {
		t.Fatalf("swept %d after the bye's keep time", n)
	}
}

// RT-6.44 step 8: an agent's message is a sign too: one that posts while its
// own presence is old keeps its bot.
func TestRT644APostingAgentIsNotSwept(t *testing.T) {
	f, b, snap, c, id := sweepWorld(t)
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	c.at(20 * time.Hour)
	if b.Bot(context.Background(), "agent-a", "home") == nil {
		t.Fatal("no bot")
	}
	c.at(43 * time.Hour)
	if n := pass(s, snap, c); n != 0 || f.disabled(id) {
		t.Fatalf("a posting agent swept: %d", n)
	}
	c.at(45 * time.Hour)
	if n := pass(s, snap, c); n != 1 {
		t.Fatalf("swept %d a keep time after the last message", n)
	}
}

// RT-6.44 step 8: while the presence feed is stale (no presence at all, or
// the newest older than StaleFeed) no pass sweeps or even lists the bots,
// and the skip is logged once per stale episode.
func TestRT644AStalePresenceFeedSweepsNothing(t *testing.T) {
	f, b, snap, c, id := sweepWorld(t)
	var l logs
	b.Log = l.logger()
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	for _, at := range []time.Duration{25 * time.Hour, 26 * time.Hour, 27 * time.Hour} {
		c.at(at)
		if n := s.Pass(context.Background()); n != 0 || f.disabled(id) {
			t.Fatalf("swept %d with a stale feed", n)
		}
	}
	if n := l.count("presence feed is stale"); n != 1 {
		t.Fatalf("the skip logged %d times, want once", n)
	}
	if n := f.listings(); n != 0 {
		t.Fatalf("the bots listed %d times with a stale feed", n)
	}
	// the feed comes back, then stalls again: a second episode, a second line
	if n := pass(s, snap, c); n != 1 {
		t.Fatalf("swept %d once the feed is back", n)
	}
	c.add(StaleFeed + time.Second)
	s.Pass(context.Background())
	if n := l.count("presence feed is stale"); n != 2 {
		t.Fatalf("%d lines for two episodes, want 2", n)
	}
	// an empty snapshot is stale too
	f2 := newFake(t)
	f2.addBot("b1", "bb-agent-1", Marker+"agent-1")
	b2, _, _ := emptyWorld(t, f2, 2*time.Hour)
	if n := (&Sweeper{Bots: b2, Keep: time.Hour, Start: t0}).Pass(context.Background()); n != 0 || f2.disabled("b1") {
		t.Fatalf("swept %d without any presence", n)
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
	b, snap, c := emptyWorld(t, f, 1000*time.Hour)
	s := &Sweeper{Bots: b, Keep: time.Hour, Start: t0}
	if n := pass(s, snap, c); n != 0 {
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
	f, b, snap, c, id := sweepWorld(t)
	restart := t0.Add(100 * time.Hour)
	b.by = nil // a new process: no message seen yet
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: restart}
	for _, after := range []time.Duration{time.Minute, time.Hour, 24 * time.Hour} {
		c.set(restart.Add(after))
		if n := pass(s, snap, c); n != 0 || f.disabled(id) {
			t.Fatalf("swept %d %s after the restart", n, after)
		}
	}
	c.set(restart.Add(24*time.Hour + 10*time.Minute))
	if n := pass(s, snap, c); n != 1 {
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
	b, snap, c := emptyWorld(t, f, 2*time.Hour)
	s := &Sweeper{Bots: b, Keep: time.Hour, Start: t0, Cap: 2}
	for i, want := range []int{2, 2, 1, 0} {
		if n := pass(s, snap, c); n != want {
			t.Fatalf("pass %d swept %d, want %d", i, n, want)
		}
	}
	if len(f.toggled) != 5 {
		t.Fatalf("toggled %v", f.toggled)
	}
}

// RT-6.44 step 3 and step 8 (f): a swept agent that comes back gets its bot
// enabled again, its plain mark back and a fresh token at its next message,
// without a backoff; no second bot is made, and the next pass keeps it.
func TestRT644ASweptAgentGetsItsBotBack(t *testing.T) {
	f, b, snap, c, id := sweepWorld(t)
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	c.at(25 * time.Hour)
	if pass(s, snap, c) != 1 {
		t.Fatal("not swept")
	}
	c.add(time.Minute)
	snap.Observed("agent-a", "alice~x", "", presence.Online, c.now())
	if b.Bot(context.Background(), "agent-a", "home") == nil {
		t.Fatal("the bridge bot posts for the agent that came back")
	}
	if f.disabled(id) || f.activeTokens(id) != 1 || f.created != 1 || f.description(id) != Marker+"agent-a" {
		t.Fatalf("disabled %v, active tokens %d, created %d, description %v",
			f.disabled(id), f.activeTokens(id), f.created, f.description(id))
	}
	if !slices.Equal(f.toggled, []string{"disable " + id, "enable " + id}) {
		t.Fatalf("toggled %v", f.toggled)
	}
	c.add(time.Hour)
	if n := pass(s, snap, c); n != 0 {
		t.Fatalf("the returned agent's bot swept again: %d", n)
	}
}

// RT-6.44 step 3: a bot an admin disabled (the plain mark) stays off and the
// agent posts as the bridge bot, logged once; a disabled bot with the swept
// mark but another owner, or with another agent's swept mark, is not
// enabled either.
func TestRT644OnlyTheSweepsOwnBotsAreEnabledAgain(t *testing.T) {
	for _, tc := range []struct {
		name        string
		description string
		owner       string
	}{
		{"an admin's", Marker + "agent-a", "alice"},
		{"another owner", Marker + "agent-a" + SweptSuffix, "bob"},
		{"another's mark", Marker + "agent-b" + SweptSuffix, "alice"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFake(t)
			f.addBot("off", "bb-agent-a", tc.description)
			f.set("off", "owner_id", tc.owner)
			f.set("off", "delete_at", int64(1))
			var l logs
			b := newBots(t, f, aliceAgent())
			b.Log = l.logger()
			c := newClock(t0)
			b.Now = c.now
			for range 2 {
				if b.Bot(context.Background(), "agent-a", "home") != nil {
					t.Fatal("posted with a disabled bot")
				}
				c.add(10 * time.Minute) // past the backoff
			}
			if len(f.toggled) != 0 || !f.disabled("off") || f.description("off") != tc.description || f.created != 0 {
				t.Fatalf("toggled %v, description %v, created %d", f.toggled, f.description("off"), f.created)
			}
			if tc.name == "an admin's" && l.count("bot disabled by an admin") != 1 {
				t.Fatalf("the admin's disable logged %d times, want once", l.count("bot disabled by an admin"))
			}
		})
	}
}

// RT-6.44 step 8: a failure on one bot is logged and the next bot is tried;
// a bot whose swept mark cannot be set is not disabled; a failed disable
// restores the plain mark; a failed listing sweeps nothing.
func TestRT644SweepGoesOnAfterAFailure(t *testing.T) {
	f := newFake(t)
	f.addBot("b1", "bb-agent-1", Marker+"agent-1")
	f.addBot("b2", "bb-agent-2", Marker+"agent-2")
	f.addBot("b3", "bb-agent-3", Marker+"agent-3")
	b, snap, c := emptyWorld(t, f, 2*time.Hour)
	f.failDisable, f.failPatch = "b1", "b3"
	s := &Sweeper{Bots: b, Keep: time.Hour, Start: t0}
	if n := pass(s, snap, c); n != 1 || !f.disabled("b2") {
		t.Fatalf("swept %d, b2 disabled %v", n, f.disabled("b2"))
	}
	if f.disabled("b1") || f.description("b1") != Marker+"agent-1" {
		t.Fatalf("b1: disabled %v, description %v", f.disabled("b1"), f.description("b1"))
	}
	if f.disabled("b3") || f.description("b3") != Marker+"agent-3" {
		t.Fatalf("b3 disabled without its swept mark")
	}
	f.srv.Close()
	if n := pass(s, snap, c); n != 0 {
		t.Fatalf("swept %d without Mattermost", n)
	}
}

// RT-6.44 step 8: under the name's lock the bot is read again; one an admin
// changed since the listing (its mark gone) is not swept.
func TestRT644SweepRereadsTheBotUnderTheLock(t *testing.T) {
	f := newFake(t)
	f.addBot("b1", "bb-agent-1", Marker+"agent-1")
	b, snap, c := emptyWorld(t, f, 2*time.Hour)
	s := &Sweeper{Bots: b, Keep: time.Hour, Start: t0}
	e := b.entry("agent-1")
	e.mu.Lock()
	done := make(chan int)
	go func() { done <- pass(s, snap, c) }()
	// the pass listed the bot and waits for the lock; the admin changes it
	for f.listings() == 0 {
		time.Sleep(time.Millisecond)
	}
	f.set("b1", "description", "kept by hand")
	e.mu.Unlock()
	if n := <-done; n != 0 || f.disabled("b1") || f.activeTokens("b1") != 1 {
		t.Fatalf("swept %d a bot whose mark is gone", n)
	}
}

// RT-6.44 step 8: passes and the agent's messages for the same name at once
// (run with -race): each sweep and each set-up is whole, and at the end the
// bot is off exactly when the bridge holds no client for it.
func TestRT644SweepAndBotAtOnce(t *testing.T) {
	f, b, snap, c, id := sweepWorld(t)
	s := &Sweeper{Bots: b, Keep: 24 * time.Hour, Start: t0}
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for range 30 {
			pass(s, snap, c)
		}
	}()
	go func() {
		defer wg.Done()
		for range 30 {
			c.add(25 * time.Hour)
			snap.Observed("agent-a", "alice~x", "", presence.Online, c.now().Add(-25*time.Hour))
			b.Bot(context.Background(), "agent-a", "home")
		}
	}()
	wg.Wait()
	e := b.entry("agent-a")
	e.mu.Lock()
	defer e.mu.Unlock()
	if f.disabled(id) != (e.client == nil) {
		t.Fatalf("disabled %v, client %v", f.disabled(id), e.client != nil)
	}
	if e.client != nil && f.activeTokens(id) != 1 {
		t.Fatalf("%d active tokens for one client", f.activeTokens(id))
	}
}
