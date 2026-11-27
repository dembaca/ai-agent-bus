package agentbots

import (
	"bytes"
	"context"
	"encoding/json"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
)

// The bot record (RT-6.44 step 3, ADR-014 phase 5 piece 2): the bridge says
// on `<presence>.bot.<agent>.v1` which Mattermost bot it made for an agent,
// so a session finds the link of a bot to the agent's NATS name from NATS,
// not from the bot's status. Only the bridge's user may publish it.
const (
	// RecordEvery is how often a bot that holds a client is announced again.
	RecordEvery = time.Minute
	// RecordValid is how long a record counts.
	RecordValid = 3 * time.Minute
	// FindWindow is how recent a presence must be for the find-only pass to
	// look the agent's bot up: a session's presence beats every 30 s, so a
	// name gone quiet longer is no present agent and costs no admin read.
	FindWindow = 5 * time.Minute
	// RecordPrefix is the presence subject prefix of the record.
	RecordPrefix = "presence.bot."
)

// Publisher puts a record on NATS; it returns an error when the server did
// not take it. inbound.Confirmed is one.
type Publisher interface {
	Publish(subject string, data []byte) error
}

// record is the body: bot, username and owner_account are null once the bot
// is gone (swept or disabled by an admin).
type record struct {
	V            int     `json:"v"`
	Agent        string  `json:"agent"`
	Bot          *string `json:"bot"`
	Username     *string `json:"username"`
	OwnerAccount *string `json:"owner_account"`
	Iat          int64   `json:"iat"`
	Exp          int64   `json:"exp"`
}

func (b *Bots) body(name string, a *core.Agent, owner string) []byte {
	now := b.now()
	r := record{V: 1, Agent: name, Iat: now.Unix(), Exp: now.Add(RecordValid).Unix()}
	if a != nil {
		r.Bot, r.Username, r.OwnerAccount = &a.ID, &a.Username, &owner
	}
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(r)
	return bytes.TrimRight(buf.Bytes(), "\n")
}

// announce publishes name's record: its bot a, owned by the account owner;
// a nil a says there is none. A failure is logged and never stops a post.
func (b *Bots) announce(name string, a *core.Agent, owner string) {
	if b.NATS == nil {
		return
	}
	b.pub.Lock()
	defer b.pub.Unlock()
	if err := b.NATS.Publish(RecordPrefix+name+".v1", b.body(name, a, owner)); err != nil {
		b.Log.Warn("agentbots: the bot record was not published", "agent", name, "error", err)
	}
}

// Announce publishes the record of every name that holds a client now, as
// the bridge's signal does every minute; one that is being set up is left
// for the next round.
func (b *Bots) Announce() {
	b.mu.Lock()
	names := make([]string, 0, len(b.by))
	entries := make([]*entry, 0, len(b.by))
	for n, e := range b.by {
		names, entries = append(names, n), append(entries, e)
	}
	b.mu.Unlock()
	for i, e := range entries {
		if !e.mu.TryLock() {
			continue
		}
		if e.client != nil {
			if owner := b.owner(names[i]); owner != e.owner {
				// the owner's account record changed since the bot was
				// checked: no link until Bot checks it again
				b.announce(names[i], nil, "")
			} else {
				a := e.agent
				b.announce(names[i], &a, owner)
			}
		}
		e.mu.Unlock()
	}
}

// AnnounceFound is the find-only pass for the agents that are present (a
// presence within FindWindow of the newest) but hold no client (after a restart, or while idle): when the bridge's bot of
// the name exists, is active and is owned by the account that owns the
// agent's anchor, its record is published. It creates no bot, mints no token
// and joins no channel; a bot of another owner gets a null record, one that
// is not found or not the bridge's is left alone (the sweep's disabled ones
// among them).
func (b *Bots) AnnounceFound(ctx context.Context) {
	newest := b.Presence.Newest()
	for _, name := range b.Presence.Names() {
		if ctx.Err() != nil {
			return
		}
		info, ok := b.Presence.Get(name)
		if !ok || !presence.Fresh(info, newest, FindWindow) {
			continue
		}
		e := b.entry(name)
		if !e.mu.TryLock() {
			continue
		}
		if e.client == nil {
			b.announceFound(ctx, name)
		}
		e.mu.Unlock()
	}
}

func (b *Bots) announceFound(ctx context.Context, name string) {
	owner := b.owner(name)
	if owner == "" {
		return
	}
	a, err := b.Admin.FindAgent(ctx, b.Prefix+name)
	if err != nil || a == nil {
		return
	}
	bot, err := b.Admin.BotOf(ctx, a.ID)
	if err != nil || bot == nil || bot.Description != Marker+name {
		return
	}
	if bot.OwnerID != owner {
		b.announce(name, nil, "")
		return
	}
	b.announce(name, a, owner)
}

// RunRecords announces at once and then every interval until ctx ends.
func (b *Bots) RunRecords(ctx context.Context, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		b.Announce()
		b.AnnounceFound(ctx)
		select {
		case <-ctx.Done():
			return
		case <-t.C:
		}
	}
}
