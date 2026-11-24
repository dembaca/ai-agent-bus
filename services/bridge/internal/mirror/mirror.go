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
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/message"
)

// MaxProps bounds the props a sender may carry through: the bus's own cap
// (a2a.user_props) is 2 000 characters of key and value.
const MaxProps = 2000

// MaxPost bounds a mirrored post's text: Mattermost refuses one above 16 383
// characters, so the mirror cuts to 16 000 runes and marks the cut. A bus post
// above that is one the agents read over NATS in full; the mirror is for
// people and says it was cut.
const MaxPost = 16000

// Post is what the bridge remembers about a mirrored message.
type Post struct {
	ID     string `json:"id"`             // the Mattermost post id
	RootID string `json:"root,omitempty"` // the Mattermost root, when it answers one
}

// Refused is Mattermost refusing a message for good (4xx, but not 429: a rate
// limit is transient). The reader logs it and moves on: one unmirrorable post
// must not stop a home.
type Refused struct {
	Status int
	Err    error
}

func (r *Refused) Error() string {
	return fmt.Sprintf("mattermost refused it (%d): %v", r.Status, r.Err)
}
func (r *Refused) Unwrap() error { return r.Err }

// Sink writes to Mattermost with the bridge's bot.
type Sink interface {
	// Create posts `text` in channelID, as a reply to rootID when that is set,
	// with props; it returns the new post's id.
	Create(ctx context.Context, channelID, text, rootID string, props map[string]any) (string, error)
	// React adds emoji to a post, Unreact removes the bridge's own reaction.
	React(ctx context.Context, postID, emoji string) error
	Unreact(ctx context.Context, postID, emoji string) error
}

// Store is the id map, the position and the reaction counts, in the bridge's
// own KV bucket.
type Store interface {
	Position(ctx context.Context, home string) (uint64, bool, error)
	Post(ctx context.Context, home string, seq uint64) (Post, bool, error)
	// Save remembers a mirror and moves the home's position.
	Save(ctx context.Context, home string, seq uint64, p Post) error
	// Reaction adds or removes one sender's reaction of a home's message and
	// emoji, and returns how many senders reacted; the bridge's bot reacts on
	// 1 and unreacts on 0.
	Reaction(ctx context.Context, home string, seq uint64, emoji, sender string, add bool) (int, error)
}

// Links resolves an agent's name to the Mattermost username of its bot, when
// the link is proven, like RT-6.37 step 4. Unknown or unproven: "".
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

// Handle mirrors one message; sender is the subject's last token, seq the
// stream sequence. It is idempotent: a message already saved is skipped.
func (m *Mirror) Handle(ctx context.Context, channelID func(context.Context) (string, error),
	sender string, seq uint64, msg message.Msg) error {
	if seen, done, err := m.Store.Post(ctx, m.Home, seq); err != nil {
		return err
	} else if done {
		// Already mirrored (a replay after a crash): only move the position on.
		return m.save(ctx, seq, seen)
	}
	cid, err := channelID(ctx)
	if err != nil {
		return err
	}
	shown, err := m.Links.ShownAs(ctx, sender)
	if err != nil {
		// Display only: a roster or presence hiccup must not stop a post.
		m.Log.Info("bridge: shown_as not resolved", "agent", sender, "error", err)
		shown = ""
	}
	if shown == "" {
		shown = sender
	}
	id := fmt.Sprintf("n1:%s:%d", m.Home, seq)
	props := m.senderProps(msg.Props)
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
			return m.save(ctx, seq, Post{})
		}
		add := msg.Kind == message.Reaction
		n, err := m.Store.Reaction(ctx, m.Home, *msg.Ref, msg.Emoji, sender, add)
		if err != nil {
			return err
		}
		switch {
		case add && n == 1:
			err = m.Sink.React(ctx, target.ID, msg.Emoji)
		case !add && n == 0:
			err = m.Sink.Unreact(ctx, target.ID, msg.Emoji)
		}
		if err != nil {
			// The set moved with the reaction; a refused one must not be
			// counted, so take it back before returning.
			if _, undo := m.Store.Reaction(ctx, m.Home, *msg.Ref, msg.Emoji, sender, !add); undo != nil {
				m.Log.Error("bridge: reaction count out of step", "post", target.ID, "emoji", msg.Emoji)
			}
			return err
		}
		// A reaction makes no post of its own; keep the target so the map
		// still answers for the sequence, and move on.
		p = target
	default:
		return errors.New("bridge: unknown message kind")
	}
	return m.save(ctx, seq, p)
}

// save writes the map and the position although the reader may be stopping:
// a post that Mattermost accepted must be remembered, or a restart repeats it.
func (m *Mirror) save(ctx context.Context, seq uint64, p Post) error {
	sctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	return m.Store.Save(sctx, m.Home, seq, p)
}

// senderProps keeps what a sender may carry through: keys with the bus props
// prefix and scalar values, without the keys the bridge itself sets (a forged
// system, nats_id, shown_as or nats_root must not reach Mattermost). Nothing
// else from Mattermost's own prop namespace passes, so no from_webhook, no
// override_username, no attachments. Too many bytes: the sender's props go.
func (m *Mirror) senderProps(in map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range in {
		if !strings.HasPrefix(k, m.Prefix) {
			continue
		}
		switch v.(type) {
		case string, bool, json.Number, float64, int, int64:
		default:
			continue
		}
		switch strings.TrimPrefix(k, m.Prefix) {
		case "system", "nats_id", "shown_as", "nats_root":
			continue
		}
		out[k] = v
	}
	if raw, err := json.Marshal(out); err != nil || len(raw) > MaxProps {
		return map[string]any{}
	}
	return out
}

// Attribution puts the sender above the text, as the bridge posts with one
// bot for every agent (the pilot's decision): a line of its own, so Markdown
// blocks and tables below it stay intact.
// SenderMark starts the sender line. The bridge strips it from every text, so
// a post cannot imitate the line: its own "**agent-other**" lacks the mark.
const SenderMark = "▍"

func Attribution(shown, text string) string {
	// The sender line is ours and marked; the text cannot move it. Mentions in
	// the text would notify people through the bridge's bot, so every @ gets a
	// zero-width space after it. Cut after defusing, so the limit holds for what
	// is posted.
	body := strings.ReplaceAll(text, SenderMark, "▏")
	return SenderMark + "**" + shown + "**\n\n" + Cut(DefuseMentions(body))
}

// DefuseMentions makes @channel, @all and @name in a mirrored text plain text.
func DefuseMentions(text string) string {
	if !strings.Contains(text, "@") {
		return text
	}
	var b strings.Builder
	b.Grow(len(text) + 8)
	for _, r := range text {
		b.WriteRune(r)
		if r == '@' {
			b.WriteRune('\u200b')
		}
	}
	return b.String()
}

// Cut shortens text to MaxPost runes, with a mark when it did.
func Cut(text string) string {
	if utf8.RuneCountInString(text) <= MaxPost {
		return text
	}
	runes := []rune(text)
	return string(runes[:MaxPost]) + " …"
}
