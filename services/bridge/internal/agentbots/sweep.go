package agentbots

import (
	"context"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/config"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
)

// The sweep's settings (RT-6.44 step 8, issue #151 step 2); the keep time's
// default and minimum are config's, the one source.
const (
	DefaultKeep   = config.DefaultBotKeep // CB_BRIDGE_MM_BOT_KEEP's default
	MinKeep       = config.MinBotKeep     // the shortest keep time the bridge starts with
	SweepInterval = 10 * time.Minute      // one pass each
	SweepCap      = 20                    // bots disabled per pass at most
	// StaleFeed is how old the newest presence of all may be before a pass
	// is skipped: agents send theirs well within 90 s, so an older one means
	// the presence feed stalled, not that every agent left.
	StaleFeed = 5 * time.Minute
)

// Sweeper disables the bots the bridge made for agents that have shown no
// sign for longer than Keep, and revokes their tokens (RT-6.44 step 8). A
// bot is the bridge's only when its username is Prefix plus a name and its
// description is exactly Marker plus that name: never by the prefix alone,
// so the registrar's bots and anyone else's stay. Before it disables a bot
// it appends SweptSuffix to that mark, so only the sweep's bots are enabled
// again (setUp). An agent's last sign is the latest of its presence (any
// status: a bye is its last sign too), its latest message (Bot) and Start,
// so after a restart no bot is swept before one keep time has passed;
// nothing is stored but in memory. While the presence feed is stale
// (StaleFeed) no pass sweeps.
//
// One goroutine runs it (Run); Pass is not safe for concurrent use with
// itself, but is with Bots.Bot.
type Sweeper struct {
	Bots  *Bots
	Keep  time.Duration // DefaultKeep when 0
	Start time.Time     // the bridge's start; every bot counts as seen then
	Cap   int           // SweepCap when 0

	seen  map[string]time.Time
	stale bool // the feed is stale and that was logged
}

// Run makes a pass every interval until ctx ends; the first after one
// interval.
func (s *Sweeper) Run(ctx context.Context, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.Pass(ctx)
		}
	}
}

// lastSeen is name's last sign: its latest presence, its latest message or
// Start, whichever is latest, and never earlier than what an earlier pass
// saw.
func (s *Sweeper) lastSeen(name string) time.Time {
	if s.seen == nil {
		s.seen = map[string]time.Time{}
	}
	t := s.Start
	if prev := s.seen[name]; prev.After(t) {
		t = prev
	}
	if info, ok := s.Bots.Presence.Get(name); ok && info.At.After(t) {
		t = info.At
	}
	if n := s.Bots.entry(name).posted.Load(); n != 0 {
		if p := time.Unix(0, n); p.After(t) {
			t = p
		}
	}
	s.seen[name] = t
	return t
}

// Pass lists the enabled bots and sweeps the bridge's whose agent is idle
// longer than the keep time, at most Cap of them; it returns how many it
// swept. A failure is logged and the next bot is tried; nothing stops the
// bridge.
func (s *Sweeper) Pass(ctx context.Context) int {
	b := s.Bots
	keep, limit := s.Keep, s.Cap
	if keep <= 0 {
		keep = DefaultKeep
	}
	if limit <= 0 {
		limit = SweepCap
	}
	if newest := b.Presence.Newest(); newest.IsZero() || b.now().Sub(newest) > StaleFeed {
		if !s.stale {
			s.stale = true
			b.Log.Warn("agentbots: the presence feed is stale; the sweep waits for it", "newest", newest)
		}
		return 0
	}
	if s.stale {
		s.stale = false
		b.Log.Info("agentbots: the presence feed is back; the sweep goes on")
	}
	bots, err := b.Admin.ListBots(ctx)
	if err != nil {
		b.Log.Warn("agentbots: the sweep cannot list the bots", "error", err)
		return 0
	}
	swept := 0
	for _, bot := range bots {
		name, ok := strings.CutPrefix(bot.Username, b.Prefix)
		if !ok || name == "" || !marked(bot.Description, name) || bot.DeleteAt != 0 {
			continue // not the bridge's bot for an agent, or disabled already
		}
		if b.now().Sub(s.lastSeen(name)) <= keep {
			continue
		}
		if swept == limit {
			b.Log.Info("agentbots: the sweep's cap is reached; the rest waits for the next pass", "cap", limit)
			break
		}
		if s.sweep(ctx, name, core.Agent{ID: bot.UserID, Username: bot.Username}, keep) {
			swept++
		}
	}
	return swept
}

// sweep revokes the bot's tokens, marks it swept and disables it, under the
// name's lock, so no message of the agent sets the bot up meanwhile; the
// last sign and the bot itself are read again under it.
func (s *Sweeper) sweep(ctx context.Context, name string, a core.Agent, keep time.Duration) bool {
	b := s.Bots
	e := b.entry(name)
	e.mu.Lock()
	defer e.mu.Unlock()
	idle := b.now().Sub(s.lastSeen(name))
	if idle <= keep {
		return false
	}
	mark := Marker + name
	cur, err := b.Admin.BotOfAny(ctx, a.ID)
	if err != nil {
		b.Log.Warn("agentbots: the sweep cannot read the bot", "bot", a.Username, "error", err)
		return false
	}
	if cur == nil || cur.Username != b.Prefix+name || !marked(cur.Description, name) || cur.DeleteAt != 0 {
		return false // changed since the listing: not the bridge's enabled bot for name any more
	}
	if err := b.Admin.RevokeAll(ctx, a); err != nil {
		b.Log.Warn("agentbots: the sweep cannot revoke the bot's tokens", "bot", a.Username, "error", err)
		return false
	}
	// the tokens are gone: so is the client, whatever follows
	e.agent, e.client, e.joined = a, nil, map[string]bool{}
	// an enabled bot with the swept mark already is one whose earlier sweep
	// was cut off between two calls: it keeps the mark
	if cur.Description == mark {
		if err := b.Admin.UpdateBotDescription(ctx, a.ID, mark+SweptSuffix); err != nil {
			b.Log.Warn("agentbots: the sweep cannot mark the bot swept", "bot", a.Username, "error", err)
			return false
		}
	}
	if err := b.Admin.DisableBot(ctx, a.ID); err != nil {
		b.Log.Warn("agentbots: the sweep cannot disable the bot", "bot", a.Username, "error", err)
		if err := b.Admin.UpdateBotDescription(ctx, a.ID, mark); err != nil {
			// setUp restores it at the agent's next message
			b.Log.Warn("agentbots: the sweep cannot restore the bot's mark", "bot", a.Username, "error", err)
		}
		return false
	}
	// no backoff: an agent that comes back gets its bot enabled at once
	e.failed = time.Time{}
	b.Log.Info("agentbots: swept", "bot", a.Username, "idle", idle.Round(time.Minute).String())
	return true
}

// marked says whether an enabled bot's description is the bridge's mark for
// name: the plain one, or the swept one an interrupted sweep left.
func marked(description, name string) bool {
	return description == Marker+name || description == Marker+name+SweptSuffix
}
