// Package status shows each agent's status under its own bot in Mattermost
// (ADR-014 phase 5, plan B piece 9; RT-6.6 step 1, RT-6.38 step 3, RT-6.44
// step 9). In a home where piece 9 is on, the agent's session writes no
// status, presence or crown to Mattermost; its presence states the status it
// would have shown (`shown`, presence.Shown). The bridge sets the agent's
// bridge bot's custom status and Mattermost presence from that presence, and
// the crown from the claims: the crown shows only on the claims' holder whose
// own session says it holds the crown. The way back is the switch off: the bridge
// clears what it set, and the session shows the status under its registrar
// bot again.
package status

import (
	"context"
	"log/slog"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
)

// Piece is plan B's piece for the status (ADR-014 phase 5).
const Piece = 9

// DefaultWindow is how old a presence may be and still count, the MCP
// server's PRESENCE_FRESH_SEC.
const DefaultWindow = 90 * time.Second

// Interval is how often Run compares what it shows with what it should.
const Interval = 5 * time.Second

// MaxUntil is the longest a custom status the bridge sets lasts: the
// backstop should the bridge stop; it clears a gone agent's status itself.
const MaxUntil = time.Hour

// CrownEmoji is the crown holder's custom status emoji, as the MCP server
// writes it for a home not over NATS.
const CrownEmoji = "crown"

// coordinatorHead starts a crown holder's status text (RT-6.6 step 2).
const coordinatorHead = "coordinator · "

// crownMark is the crown as a character, which only the crown's emoji and a
// holder's posts carry: a shown text never does.
const crownMark = "\U0001F451"

// Bot is what the bridge sets of an agent's own bot (mm.Client).
type Bot interface {
	SetStatus(ctx context.Context, status string) error
	SetCustomStatus(ctx context.Context, emoji, text string, expires time.Time) error
	ClearCustomStatus(ctx context.Context) error
}

// Bots gives an agent's own bot, joined to home, or nil while it has none
// (agentbots.Bots). The bridge never sets a status with another token.
type Bots interface {
	Bot(ctx context.Context, name, home string) Bot
}

// Switches are plan B's switches per channel (presence.Switches).
type Switches interface {
	On(piece int, channel string) bool
}

// view is what one agent's bot shows: its presence and custom status (no
// emoji: none).
type view struct {
	home     string
	presence string
	emoji    string
	text     string
	until    time.Time
}

func (v view) cleared() bool { return v.presence == "offline" && v.emoji == "" }

// Mirror sets the agents' bots' statuses. Pass is not safe for concurrent
// use: Run calls it from one goroutine.
type Mirror struct {
	Homes    []string // the homes the bridge serves, in order
	Presence presence.PresenceOf
	Shown    *presence.ShownStore
	Claims   *presence.Claims
	Switches Switches
	Bots     Bots
	// Member says whether an agent is a member of a channel; nil checks no
	// membership (the claims alone), until the bridge reads memberships.
	Member func(channel, name string) bool
	Window time.Duration // DefaultWindow when 0
	Log    *slog.Logger

	applied map[string]view // name -> what its bot shows by this process
	failed  map[string]bool // name -> its last write failed (logged once)
}

func (m *Mirror) window() time.Duration {
	if m.Window > 0 {
		return m.Window
	}
	return DefaultWindow
}

// Run compares and sets every interval until ctx ends.
func (m *Mirror) Run(ctx context.Context, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		m.Pass(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}

// Pass sets each agent's bot to what it should show now and returns how many
// bots it wrote. Nothing before the claims were read. An agent is shown in a
// home of the bridge where piece 9 is on, has a claim there and states a
// status in its presence; with its presence gone stale it shows offline and
// no status, and a bot this process set that is no longer shown (the switch
// off, no status stated, the claim gone) is cleared.
func (m *Mirror) Pass(ctx context.Context) int {
	if m.Claims == nil || !m.Claims.Read() {
		return 0
	}
	if m.applied == nil {
		m.applied, m.failed = map[string]view{}, map[string]bool{}
	}
	now := m.Claims.Now()
	want := map[string]view{}
	for _, home := range m.Homes {
		if !m.Switches.On(Piece, home) {
			continue
		}
		holder := m.Claims.Holder(home, m.Presence, m.window(), m.Member)
		for _, name := range m.Claims.Claimants(home) {
			if _, seen := want[name]; seen {
				continue // one status per bot: the first home that shows it
			}
			if m.Member != nil && !m.Member(home, name) {
				continue
			}
			shown := m.Shown.Get(name)
			info, ok := m.Presence.Get(name)
			if shown == nil || !ok {
				continue
			}
			want[name] = m.compose(home, shown, info, holder == name && shown.Crown, now)
		}
	}
	writes := 0
	for name, v := range want {
		if old, ok := m.applied[name]; ok && old == v {
			continue
		}
		if m.apply(ctx, name, v) {
			writes++
		}
	}
	for name, old := range m.applied {
		if _, ok := want[name]; ok {
			continue
		}
		if old.cleared() {
			delete(m.applied, name)
			continue
		}
		if m.apply(ctx, name, view{home: old.home, presence: "offline"}) {
			writes++
			delete(m.applied, name)
		}
	}
	return writes
}

// compose is what name's bot should show: offline and no status with its
// presence stale; else online (away when the presence says so) and the
// stated status until its end, at most MaxUntil, with the crown when the
// claims make it the holder and its session says it holds the crown.
func (m *Mirror) compose(home string, shown *presence.Shown, info presence.Info, crowned bool,
	now time.Time) view {
	if !presence.Fresh(info, now, m.window()) {
		return view{home: home, presence: "offline"}
	}
	v := view{home: home, presence: "online"}
	if info.Status == presence.Away {
		v.presence = "away"
	}
	until := shown.Until
	if limit := now.Add(MaxUntil); until.After(limit) {
		until = limit
	}
	if !until.After(now) {
		return v // the status ran out: none until the next presence states one
	}
	text := Plain(shown.Text)
	if text == "" {
		return v
	}
	v.emoji, v.text, v.until = shown.Emoji, text, until.Truncate(time.Second)
	if crowned {
		v.emoji, v.text = CrownEmoji, WithHead(coordinatorHead, text)
	}
	return v
}

// apply writes v to name's bot: the presence, then the custom status, or its
// removal. False when the bot is not to be had or Mattermost failed it: the
// next pass tries again.
func (m *Mirror) apply(ctx context.Context, name string, v view) bool {
	bot := m.Bots.Bot(ctx, name, v.home)
	if bot == nil {
		m.fail(name, nil)
		return false
	}
	err := bot.SetStatus(ctx, v.presence)
	if err == nil && v.emoji != "" {
		err = bot.SetCustomStatus(ctx, v.emoji, v.text, v.until)
	} else if err == nil {
		err = bot.ClearCustomStatus(ctx)
	}
	if err != nil {
		m.fail(name, err)
		return false
	}
	if m.failed[name] {
		m.Log.Info("status: the agent's bot shows its status again", "agent", name)
		delete(m.failed, name)
	}
	m.applied[name] = v
	return true
}

func (m *Mirror) fail(name string, err error) {
	if m.failed[name] {
		return
	}
	m.failed[name] = true
	if err == nil {
		m.Log.Info("status: the agent has no bot of its own now; its status is not shown", "agent", name)
		return
	}
	m.Log.Warn("status: the agent's bot did not take its status; tried again", "agent", name, "error", err)
}

// Plain is a stated text without a crown it may not claim: no crown
// character, no `coordinator` head (the bridge sets both by the claims).
func Plain(text string) string {
	text = strings.ReplaceAll(text, crownMark, "")
	for {
		t := strings.TrimSpace(text)
		switch {
		case t == "coordinator" || t == "coordinator ·":
			return ""
		case strings.HasPrefix(t, coordinatorHead):
			text = t[len(coordinatorHead):]
			continue
		}
		return presence.CleanText(t, presence.ShownTextMax)
	}
}

// WithHead is head before text within Mattermost's limit: past it the text
// before its last segment is cut, never the last one (the home, ` · ~<home>`).
func WithHead(head, text string) string {
	max := presence.ShownTextMax
	if len([]rune(head))+len([]rune(text)) <= max {
		return head + text
	}
	body, tail := text, ""
	if i := strings.LastIndex(text, " · "); i >= 0 {
		body, tail = text[:i], text[i:]
	}
	room := max - len([]rune(head)) - len([]rune(tail))
	if room <= 0 {
		return string([]rune(head + text)[:max])
	}
	if r := []rune(body); len(r) > room {
		body = strings.TrimRight(string(r[:room]), " ·")
	}
	return head + body + tail
}
