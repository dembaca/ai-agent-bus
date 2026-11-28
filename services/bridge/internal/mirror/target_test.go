package mirror

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/message"
)

// RT-6.42 step 12, plan B piece 7: an agent's reply in a person's Mattermost
// thread and its reaction on a Mattermost post, named by the post's id, are
// written only after the bridge read the post and found it in the home.

const (
	personPost = "qqqqqqqqqqqqqqqqqqqqqqqqqq"
	personNote = "rrrrrrrrrrrrrrrrrrrrrrrrrr" // a person's reply in that thread
	oldAsk     = "oooooooooooooooooooooooooo" // agent-a's ask from before the switch
)

type fakeTargets struct {
	posts map[string]Target
	read  []string
	err   error
}

func (f *fakeTargets) Target(_ context.Context, id string) (Target, error) {
	f.read = append(f.read, id)
	if f.err != nil {
		return Target{}, f.err
	}
	t, ok := f.posts[id]
	if !ok {
		return Target{}, statusErr(404)
	}
	return t, nil
}

type linksOf map[string]string

func (l linksOf) ShownAs(_ context.Context, agent string) (string, error) { return l[agent], nil }

func targetMirror(t *testing.T, on bool) (*Mirror, *fakeSink, *fakeStore, *fakeTargets, *bytes.Buffer) {
	t.Helper()
	m, sink, st := newMirror(t)
	var logs bytes.Buffer
	m.Log = slog.New(slog.NewTextHandler(&logs, nil))
	m.Prefix = "x_"
	m.Switches = fakeSwitches{RepliesPiece: on}
	m.Links = linksOf{"agent-a": "agent-a-bot"}
	tg := &fakeTargets{posts: map[string]Target{
		personPost: {ID: personPost, ChannelID: "chan", AuthorName: "dembaca"},
		personNote: {ID: personNote, ChannelID: "chan", RootID: personPost, AuthorName: "dembaca"},
		oldAsk:     {ID: oldAsk, ChannelID: "chan", AuthorName: "agent-a-bot", AuthorBot: true},
	}}
	m.Targets = tg
	return m, sink, st, tg, &logs
}

func reply(root, text string) message.Msg {
	return message.Msg{Kind: message.Post, Text: text, MMRoot: root, Props: map[string]any{}}
}

func react(kind message.Kind, post string) message.Msg {
	return message.Msg{Kind: kind, Emoji: "eyes", MMRef: post, Props: map[string]any{}}
}

func TestWithPiece7OnAReplyGoesIntoThePersonsThreadWithEveryMentionPlain(t *testing.T) {
	m, sink, st, _, _ := targetMirror(t, true)
	ctx := context.Background()
	if err := m.Handle(ctx, channel, "agent-a", 5, reply(personPost, "on it @dembaca @channel")); err != nil {
		t.Fatal(err)
	}
	// a reply to a person's reply joins the thread's root
	if err := m.Handle(ctx, channel, "agent-a", 6, reply(personNote, "done")); err != nil {
		t.Fatal(err)
	}
	if len(sink.posts) != 2 || sink.posts[0].root != personPost || sink.posts[1].root != personPost {
		t.Fatalf("%+v", sink.posts)
	}
	if strings.Contains(sink.posts[0].text, "@dembaca") || strings.Contains(sink.posts[0].text, "@channel") {
		t.Fatalf("a mention survived: %q", sink.posts[0].text)
	}
	if sink.posts[0].props["x_nats_id"] != "n1:home:5" {
		t.Fatalf("props: %+v", sink.posts[0].props)
	}
	if p := st.posts[5]; p.RootID != personPost {
		t.Fatalf("the map: %+v", p)
	}
}

func TestWithPiece7OnAReactionAndItsRemovalLandOnThePost(t *testing.T) {
	m, sink, st, _, _ := targetMirror(t, true)
	ctx := context.Background()
	if err := m.Handle(ctx, channel, "agent-a", 5, react(message.Reaction, personPost)); err != nil {
		t.Fatal(err)
	}
	if err := m.Handle(ctx, channel, "agent-a", 6, react(message.Unreaction, personPost)); err != nil {
		t.Fatal(err)
	}
	if len(sink.reacted) != 1 || sink.reacted[0] != personPost+"/eyes" ||
		len(sink.unreacted) != 1 || sink.unreacted[0] != personPost+"/eyes" {
		t.Fatalf("reacted %v unreacted %v", sink.reacted, sink.unreacted)
	}
	// no way back from the person's post to the reaction's sequence
	if p, ok := st.posts[5]; !ok || p.ID != "" {
		t.Fatalf("the map: %+v %v", p, ok)
	}
}

func TestWithPiece7OnTheSendersOwnAskFromBeforeTheSwitchTakesItsCheckMarkAndNote(t *testing.T) {
	m, sink, _, _, _ := targetMirror(t, true)
	ctx := context.Background()
	if err := m.Handle(ctx, channel, "agent-a", 5, react(message.Reaction, oldAsk)); err != nil {
		t.Fatal(err)
	}
	if err := m.Handle(ctx, channel, "agent-a", 6, reply(oldAsk, "merged")); err != nil {
		t.Fatal(err)
	}
	if len(sink.reacted) != 1 || len(sink.posts) != 1 || sink.posts[0].root != oldAsk {
		t.Fatalf("reacted %v posts %+v", sink.reacted, sink.posts)
	}
}

// Fail closed: nothing is written for a post of another channel, a deleted,
// system or webhook post, another bot's post, an unknown or unreadable one,
// and nothing at all with the piece off.
func TestAMattermostPostThatFailsTheCheckGetsNothing(t *testing.T) {
	ctx := context.Background()
	for name, tc := range map[string]struct {
		on     bool
		sender string
		target Target
	}{
		"piece off":       {false, "agent-a", Target{ID: personPost, ChannelID: "chan"}},
		"other channel":   {true, "agent-a", Target{ID: personPost, ChannelID: "elsewhere"}},
		"deleted":         {true, "agent-a", Target{ID: personPost, ChannelID: "chan", DeleteAt: 1}},
		"system post":     {true, "agent-a", Target{ID: personPost, ChannelID: "chan", Type: "system_join_channel"}},
		"webhook":         {true, "agent-a", Target{ID: personPost, ChannelID: "chan", Webhook: true}},
		"another's bot":   {true, "agent-a", Target{ID: personPost, ChannelID: "chan", AuthorName: "agent-b-bot", AuthorBot: true}},
		"unlinked sender": {true, "agent-z", Target{ID: personPost, ChannelID: "chan", AuthorName: "", AuthorBot: true}},
		"another post":    {true, "agent-a", Target{ID: personNote, ChannelID: "chan"}},
		"unknown":         {true, "agent-a", Target{}},
	} {
		m, sink, st, tg, logs := targetMirror(t, tc.on)
		tg.posts = map[string]Target{}
		if tc.target.ID != "" {
			tg.posts[personPost] = tc.target
		}
		if err := m.Handle(ctx, channel, tc.sender, 5, reply(personPost, "hi")); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if err := m.Handle(ctx, channel, tc.sender, 6, react(message.Reaction, personPost)); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if len(sink.posts) != 0 || len(sink.reacted) != 0 {
			t.Errorf("%s: written: %+v %v", name, sink.posts, sink.reacted)
		}
		if st.pos != 6 || !strings.Contains(logs.String(), "naming a Mattermost post is not written") {
			t.Errorf("%s: position %d, log %q", name, st.pos, logs.String())
		}
	}
}

func TestAPostReadThatMayPassIsTriedAgain(t *testing.T) {
	for _, err := range []error{errors.New("connection refused"), statusErr(502), statusErr(429)} {
		m, sink, st, tg, _ := targetMirror(t, true)
		tg.err = err
		if got := m.Handle(context.Background(), channel, "agent-a", 5, reply(personPost, "hi")); got == nil {
			t.Fatalf("%v: no error, so the reader moves on", err)
		}
		if len(sink.posts) != 0 || st.pos != 0 {
			t.Fatalf("%v: written or moved on", err)
		}
	}
	m, _, st, tg, _ := targetMirror(t, true)
	tg.err = statusErr(403) // the bridge bot cannot read it: not in its channel
	if err := m.Handle(context.Background(), channel, "agent-a", 5, reply(personPost, "hi")); err != nil || st.pos != 5 {
		t.Fatalf("a refusal skips: %v %d", err, st.pos)
	}
}

// A reminder of an ask from before the switch keeps its one mention by piece
// 6's rule (ask.go), also with piece 6 off; any other reply names nobody.
func TestWithPiece7OnAReminderInAMattermostThreadKeepsTheAsksMention(t *testing.T) {
	m, sink, _, _, _ := targetMirror(t, true)
	m.People = &fakePeople{people: map[string]bool{"dembaca": true}}
	m.Directory = fakeDirectory{"id-dembaca": "dembaca"}
	ctx := context.Background()
	rem := reply(oldAsk, "@dembaca still waiting")
	rem.Props = map[string]any{"x_a2a_kind": "reminder", "x_ask_to": "dembaca"}
	if err := m.Handle(ctx, channel, "agent-a", 5, rem); err != nil {
		t.Fatal(err)
	}
	plain := reply(oldAsk, "@dembaca thanks")
	plain.Props = map[string]any{"x_ask_to": "dembaca"}
	if err := m.Handle(ctx, channel, "agent-a", 6, plain); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(sink.posts[0].text, "@dembaca still") || strings.Contains(sink.posts[1].text, "@dembaca") {
		t.Fatalf("%q / %q", sink.posts[0].text, sink.posts[1].text)
	}
	if _, ok := sink.posts[0].props["x_ask_to"]; ok {
		t.Fatalf("ask_to reached Mattermost: %+v", sink.posts[0].props)
	}
}
