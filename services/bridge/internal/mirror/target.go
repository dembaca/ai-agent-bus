package mirror

// Plan B piece 7 (RT-6.42 step 13, ADR-014 phase 5): an agent's reply in a
// person's Mattermost thread, or its reaction on a Mattermost post, that it
// sent over NATS naming the post by its Mattermost id (`mm_root`, `mm_ref`).
// With the piece switched on for the home, the bridge reads the post with
// its own bot's token and writes the reply or reaction there only when the
// post is in the home's channel, not deleted, an ordinary post, no webhook's,
// and a person's or the sending agent's own linked bot's (its ask from
// before the switch). Anything else is logged and nothing is written; with
// the piece off, nothing that names a Mattermost post is written at all.

import (
	"context"
	"strconv"
	"strings"
)

// RepliesPiece is plan B's piece number of replies and reactions to a
// person's post.
const RepliesPiece = 7

// Target is a Mattermost post a message names by its id, with what the
// check needs of it and of its author.
type Target struct {
	ID, ChannelID, RootID, Type string
	DeleteAt                    int64
	Webhook                     bool   // from_webhook or override_username in its props
	AuthorName                  string // the author's username
	AuthorBot                   bool   // Mattermost's is_bot of the author
}

// ThreadRoot is the root a reply to the post goes under: Mattermost threads
// have one level, so a reply to a reply joins its thread.
func (t Target) ThreadRoot() string {
	if t.RootID != "" {
		return t.RootID
	}
	return t.ID
}

// Targets reads a Mattermost post by id with the bridge bot's token
// (mm.Client.Target). An error that carries an HTTP status below 500 (and not
// 429) is a refusal; any other is retried.
type Targets interface {
	Target(ctx context.Context, postID string) (Target, error)
}

// SeqRef names a message by its stream sequence for the reaction counts.
func SeqRef(seq uint64) string { return strconv.FormatUint(seq, 10) }

// MMRef names a Mattermost post by its id for the reaction counts; it never
// looks like a sequence.
func MMRef(post string) string { return "m" + post }

// repliesOn says whether piece 7 is on for this home.
func (m *Mirror) repliesOn() bool {
	return m.Switches != nil && m.Switches.On(RepliesPiece, m.Home)
}

// target checks the Mattermost post `id` a message of sender names: ok only
// when it may be written to (above). linked is the Mattermost username of
// the sender's proven bot (Links.ShownAs), "" when none is proven. An error
// is a read that may pass (a 5xx, a 429, the network): the reader tries the
// message again; every other refusal is logged and skips the message.
func (m *Mirror) target(ctx context.Context, channelID, sender, linked, id string) (Target, bool, error) {
	refuse := func(why string, args ...any) (Target, bool, error) {
		m.Log.Warn("bridge: a reply or reaction naming a Mattermost post is not written",
			append([]any{"home", m.Home, "agent", sender, "post", id, "why", why}, args...)...)
		return Target{}, false, nil
	}
	if !m.repliesOn() {
		return refuse("plan B piece 7 is off")
	}
	if m.Targets == nil {
		return refuse("no post lookup")
	}
	t, err := m.Targets.Target(ctx, id)
	if err != nil {
		if transient(err) {
			return Target{}, false, err
		}
		return refuse("not readable", "error", err)
	}
	switch {
	case t.ID != id:
		return refuse("another post came back")
	case t.ChannelID != channelID:
		return refuse("a post of another channel")
	case t.DeleteAt != 0:
		return refuse("deleted")
	case t.Type != "":
		return refuse("not an ordinary post")
	case t.Webhook:
		return refuse("a webhook's post")
	case t.AuthorBot && (linked == "" || !strings.EqualFold(t.AuthorName, linked)):
		return refuse("a bot's post, not the sender's own")
	}
	return t, true, nil
}
