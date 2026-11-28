// Package mirror turns NATS bus messages of a home into Mattermost posts with
// the bridge's own bot, or each agent's own bot where that is on (RT-6.44),
// one way (RT-6.36, ADR-016 point 4): the NATS side stays the source of
// truth, the mirror is for people.
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
	// OwnReaction says whether the sender's own bot holds its reaction
	// (RT-6.44); SetOwnReaction records or clears that, beside the counts.
	OwnReaction(ctx context.Context, home string, seq uint64, emoji, sender string) (bool, error)
	SetOwnReaction(ctx context.Context, home string, seq uint64, emoji, sender string, on bool) error
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

// Bots gives an agent its own Mattermost bot to post and react with
// (RT-6.44, internal/agentbots); nil means the one bridge bot.
type Bots interface {
	Bot(ctx context.Context, name, home string) Sink
	// Unreact removes name's own bot's reaction from a post whatever state
	// the bot is in, nil when it is gone already; an error is retried.
	Unreact(ctx context.Context, name, post, emoji string) error
	// Forget drops a bot's client after Mattermost failed it, so the next
	// use finds the bot and mints its token again.
	Forget(name string)
}

// Mirror mirrors messages of one home.
type Mirror struct {
	Home   string // the home's channel name, for the NATS subject and the id
	Prefix string // the props prefix, the same as the home's MM_PROPS_PREFIX
	Log    *slog.Logger
	Sink   Sink
	Store  Store
	Links  Links
	Bots   Bots // optional: the agents' own bots; nil posts everything with Sink
	// Plan B piece 6 (RT-6.11, ask.go): the switches per channel and the lookup
	// of a person's username; without them asks are mirrored like any post.
	Switches  Switches
	People    People
	Directory Directory
	people    personCache
	mentions  mentionLimit
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
	keep, cards, asks := "", []any(nil), msg.Kind == message.Post && m.asksOn()
	in := msg.Props
	if asks {
		// Plan B piece 6: the ask's card and its one mention; its own keys never pass.
		if keep, cards, err = m.ask(ctx, sender, msg); err != nil {
			return err
		}
		in = m.withoutAsk(msg.Props)
	}
	props := m.senderProps(in)
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
		if cards != nil {
			props["attachments"] = cards
		}
		var pid string
		if own := m.own(ctx, sender); own != nil {
			// The agent's own bot shows who wrote it: no attribution line.
			pid, err = own.Create(ctx, cid, Cut(DefuseMentionsExcept(unmark(msg.Text), keep)), root, props)
			m.failed(sender, err)
		} else {
			pid, err = m.Sink.Create(ctx, cid, attribution(shown, msg.Text, keep), root, props)
		}
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
		if msg.Kind == message.Reaction {
			err = m.react(ctx, sender, *msg.Ref, target.ID, msg.Emoji)
		} else {
			err = m.unreact(ctx, sender, *msg.Ref, target.ID, msg.Emoji)
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
	return m.save(ctx, seq, p)
}

// own is the sender's own bot, nil for the bridge bot (RT-6.44).
func (m *Mirror) own(ctx context.Context, sender string) Sink {
	if m.Bots == nil {
		return nil
	}
	return m.Bots.Bot(ctx, sender, m.Home)
}

// failed drops the sender's bot client when Mattermost refused its token
// (401, 403): the bot is found and minted again after the backoff. A rate
// limit, a 5xx or a timeout keeps the client; the reader retries the message
// under the same bot.
func (m *Mirror) failed(sender string, err error) {
	if Revoked(err) {
		m.Bots.Forget(sender)
	}
}

// Revoked says Mattermost refused the token itself (401, 403), told by the
// status an error carries (mm.StatusError).
func Revoked(err error) bool {
	var st interface{ HTTPStatus() int }
	if !errors.As(err, &st) {
		return false
	}
	s := st.HTTPStatus()
	return s == 401 || s == 403
}

// react adds the sender's reaction: under its own bot, recorded so the
// removal goes to that bot whatever state it is in then; else under the
// bridge bot, which reacts once for all senders without one.
func (m *Mirror) react(ctx context.Context, sender string, ref uint64, post, emoji string) error {
	if own := m.own(ctx, sender); own != nil {
		err := own.React(ctx, post, emoji)
		m.failed(sender, err)
		if err != nil {
			return err
		}
		return m.Store.SetOwnReaction(ctx, m.Home, ref, emoji, sender, true)
	}
	n, err := m.Store.Reaction(ctx, m.Home, ref, emoji, sender, true)
	if err != nil {
		return err
	}
	if n == 1 {
		if err := m.Sink.React(ctx, post, emoji); err != nil {
			// The set moved with the reaction; a refused one must not be
			// counted, so take it back before returning.
			if _, undo := m.Store.Reaction(ctx, m.Home, ref, emoji, sender, false); undo != nil {
				m.Log.Error("bridge: reaction count out of step", "post", post, "emoji", emoji)
			}
			return err
		}
	}
	return nil
}

// unreact removes the sender's reaction wherever it went: the own bot's, by
// its record, through Bots.Unreact (its client, or the admin bot as the bot,
// so a bot in its backoff loses no removal), and the sender's place in the
// bridge bot's count, in case it reacted before its bot was there; the
// bridge bot's reaction goes when that count reaches 0. A reaction or post
// already gone (404) counts as removed; anything else is retried, each step
// idempotent.
func (m *Mirror) unreact(ctx context.Context, sender string, ref uint64, post, emoji string) error {
	had, err := m.Store.OwnReaction(ctx, m.Home, ref, emoji, sender)
	if err != nil {
		return err
	}
	if had {
		if m.Bots == nil {
			m.Log.Warn("bridge: an own bot's reaction stays: agent bots are off", "post", post, "emoji", emoji)
		} else if err := m.Bots.Unreact(ctx, sender, post, emoji); err != nil && !refused(err) {
			return err
		}
		if err := m.Store.SetOwnReaction(ctx, m.Home, ref, emoji, sender, false); err != nil {
			return err
		}
	}
	n, err := m.Store.Reaction(ctx, m.Home, ref, emoji, sender, false)
	if err != nil {
		return err
	}
	if n == 0 {
		if err := m.Sink.Unreact(ctx, post, emoji); err != nil && !refused(err) {
			if _, undo := m.Store.Reaction(ctx, m.Home, ref, emoji, sender, true); undo != nil {
				m.Log.Error("bridge: reaction count out of step", "post", post, "emoji", emoji)
			}
			return err
		}
	}
	return nil
}

func refused(err error) bool {
	var r *Refused
	return errors.As(err, &r)
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
	return attribution(shown, text, "")
}

// attribution is Attribution keeping the mention of keep (an ask's, ask.go).
func attribution(shown, text, keep string) string {
	// The sender line is ours and marked; the text cannot move it. Mentions in
	// the text would notify people through the bridge's bot, so every @ gets a
	// zero-width space after it. Cut after defusing, so the limit holds for what
	// is posted.
	return SenderMark + "**" + shown + "**\n\n" + Cut(DefuseMentionsExcept(unmark(text), keep))
}

// unmark replaces SenderMark in a mirrored text, under the bridge bot and an
// agent's own bot alike, so no text imitates a sender line.
func unmark(text string) string { return strings.ReplaceAll(text, SenderMark, "▏") }

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
