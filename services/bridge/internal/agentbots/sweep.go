package agentbots

import (
	"context"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
)

// The sweep's settings (RT-6.44 step 8, issue #151 step 2).
const (
	DefaultKeep   = 24 * time.Hour   // CB_BRIDGE_MM_BOT_KEEP's default
	MinKeep       = time.Hour        // the shortest keep time the bridge starts with
	SweepInterval = 10 * time.Minute // one pass each
	SweepCap      = 20               // bots disabled per pass at most
)

// Sweeper disables the bots the bridge made for agents that have shown no
// presence for longer than Keep, and revokes their tokens (RT-6.44 step 8).
// A bot is the bridge's only when its username is Prefix plus a name and its
// description is exactly Marker plus that name: never by the prefix alone,
// so the registrar's bots and anyone else's stay. An agent's last sign is
// the time of its latest presence (any status: a bye is its last sign too),
// never earlier than Start, so after a restart no bot is swept before one
// keep time has passed; nothing is stored but in memory.
//
// One goroutine runs it (Run); Pass is not safe for concurrent use.
type Sweeper struct {
	Bots  *Bots
	Keep  time.Duration // DefaultKeep when 0
	Start time.Time     // the bridge's start; every bot counts as seen then
	Cap   int           // SweepCap when 0

	seen map[string]time.Time
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

// lastSeen is name's last sign: its latest presence, or Start, whichever is
// later, and never earlier than what an earlier pass saw.
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
	bots, err := b.Admin.ListBots(ctx)
	if err != nil {
		b.Log.Warn("agentbots: the sweep cannot list the bots", "error", err)
		return 0
	}
	swept := 0
	for _, bot := range bots {
		name, ok := strings.CutPrefix(bot.Username, b.Prefix)
		if !ok || name == "" || bot.Description != Marker+name || bot.DeleteAt != 0 {
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

// sweep revokes the bot's tokens and disables it, under the name's lock, so
// no post of the agent sets the bot up meanwhile; the presence is read
// again under it.
func (s *Sweeper) sweep(ctx context.Context, name string, a core.Agent, keep time.Duration) bool {
	b := s.Bots
	e := b.entry(name)
	e.mu.Lock()
	defer e.mu.Unlock()
	idle := b.now().Sub(s.lastSeen(name))
	if idle <= keep {
		return false
	}
	if err := b.Admin.RevokeAll(ctx, a); err != nil {
		b.Log.Warn("agentbots: the sweep cannot revoke the bot's tokens", "bot", a.Username, "error", err)
		return false
	}
	if err := b.Admin.DisableBot(ctx, a.ID); err != nil {
		b.Log.Warn("agentbots: the sweep cannot disable the bot", "bot", a.Username, "error", err)
		return false
	}
	// no backoff: an agent that comes back gets its bot enabled at once
	e.agent, e.client, e.joined, e.failed = a, nil, map[string]bool{}, time.Time{}
	b.Log.Info("agentbots: swept", "bot", a.Username, "idle", idle.Round(time.Minute).String())
	return true
}
