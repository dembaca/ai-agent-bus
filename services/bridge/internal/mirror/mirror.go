// Package mirror turns NATS bus messages of a home into Mattermost posts with
// the bridge's own bot, one way (RT-6.36, ADR-016 point 4): the NATS side
// stays the source of truth, the mirror is for people.
//
// Every mirrored post carries the NATS id in its props, so an agent's MCP
// server drops it on read instead of reading it twice. A message already in
// the id map is skipped, so a restart after a crash neither repeats nor loses
// a post (best effort: a crash between the Mattermost post and the map write
// can repeat that one post, never drop one).
package mirror

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/message"
)

// Post is what the bridge remembers about a mirrored message.
type Post struct {
	ID     string `json:"id"`             // the Mattermost post id
	RootID string `json:"root,omitempty"` // the Mattermost root, when it answers one
}

// Sink writes to Mattermost with the bridge's bot.
type Sink interface {
	// Create posts `text` in channelID, as a reply to rootID when that is set,
	// with props; it returns the new post's id.
	Create(ctx context.Context, channelID, text, rootID string, props map[string]any) (string, error)
	// React adds emoji to a post, Unreact removes the bridge's own reaction.
	React(ctx context.Context, postID, emoji string) error
	Unreact(ctx context.Context, postID, emoji string) error
}

// Store is the id map and the position, in the bridge's own KV bucket.
type Store interface {
	// Position is the last stream sequence this bridge has handled.
	Position(ctx context.Context) (uint64, bool, error)
	// Post finds the mirror of a message, by channel and stream sequence.
	Post(ctx context.Context, channel string, seq uint64) (Post, bool, error)
	// Save remembers a mirror and moves the position; both in one call so a
	// crash cannot remember a post without moving on or move on without it.
	Save(ctx context.Context, channel string, seq uint64, p Post) error
}

// Links resolves an agent's name to the Mattermost username of its bot, when
// the link is proven (bot and name name each other, the name fresh, the
// owner matches), like RT-6.37 step 4. Unknown or unproven: "".
type Links interface {
	ShownAs(ctx context.Context, agent string) (string, error)
}

// LinksNone resolves nothing: the attribution names the NATS name (the phase
// before the presence consumer is wired in).
type LinksNone struct{}

// ShownAs implements Links.
func (LinksNone) ShownAs(context.Context, string) (string, error) { return "", nil }

// Mirror mirrors messages of one home.
type Mirror struct {
	Home   string // the home's channel name, for the NATS subject and the id
	Prefix string // the props prefix, the same as the home's MM_PROPS_PREFIX
	Log    *slog.Logger
	Sink   Sink
	Store  Store
	Links  Links
}

// ChannelID resolves a home's channel name to Mattermost's id, once.
type ChannelID func(ctx context.Context, name string) (string, error)

// Handle mirrors one message; sender is the subject's last token, seq the
// stream sequence. It is idempotent: a message already saved is skipped.
func (m *Mirror) Handle(ctx context.Context, channelID func(context.Context) (string, error),
	sender string, seq uint64, msg message.Msg) error {
	if seen, done, err := m.Store.Post(ctx, m.Home, seq); err != nil {
		return err
	} else if done {
		// Already mirrored (a replay after a crash): only move the position on.
		return m.Store.Save(ctx, m.Home, seq, seen)
	}
	cid, err := channelID(ctx)
	if err != nil {
		return err
	}
	shown, err := m.Links.ShownAs(ctx, sender)
	if err != nil {
		return err
	}
	if shown == "" {
		shown = sender
	}
	id := fmt.Sprintf("n1:%s:%d", m.Home, seq)
	props := map[string]any{}
	for k, v := range msg.Props {
		// The bridge's own semantics only: an a2a kind or crown from a NATS
		// sender must never make the mirror a task, an ask or a crown.
		if strings.HasPrefix(k, m.Prefix+"a2a") {
			continue
		}
		props[k] = v
	}
	props[m.Prefix+"nats_id"] = id
	props[m.Prefix+"shown_as"] = shown
	p := Post{}
	switch msg.Kind {
	case message.Post:
		root := ""
		if msg.Root != nil {
			if r, ok, err := m.Store.Post(ctx, m.Home, *msg.Root); err != nil {
				return err
			} else if ok {
				root = r.ID
			} else {
				props[m.Prefix+"nats_root"] = fmt.Sprintf("n1:%s:%d", m.Home, *msg.Root)
			}
		}
		pid, err := m.Sink.Create(ctx, cid, Attribution(shown, msg.Text), root, props)
		if err != nil {
			return err
		}
		p = Post{ID: pid, RootID: root}
	case message.Reaction, message.Unreaction:
		target, ok, err := m.Store.Post(ctx, m.Home, *msg.Ref)
		if err != nil {
			return err
		}
		if !ok {
			m.Log.Info("bridge: a reaction to an unknown post is skipped",
				"home", m.Home, "ref", *msg.Ref, "emoji", msg.Emoji)
			return m.Store.Save(ctx, m.Home, seq, Post{})
		}
		if msg.Kind == message.Reaction {
			err = m.Sink.React(ctx, target.ID, msg.Emoji)
		} else {
			err = m.Sink.Unreact(ctx, target.ID, msg.Emoji)
		}
		if err != nil {
			return err
		}
		// A reaction makes no post of its own; keep the target so the map
		// still answers for the sequence, and move on.
		p = target
	default:
		return errors.New("bridge: unknown message kind")
	}
	return m.Store.Save(ctx, m.Home, seq, p)
}

// Attribution puts the sender above the text, as the bridge posts with one
// bot for every agent (the pilot's decision): a line of its own, so Markdown
// blocks and tables below it stay intact.
func Attribution(shown, text string) string {
	return "**" + shown + "**\n\n" + text
}
