package mirror

import (
	"context"
	"io"
	"log/slog"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/message"
)

type fakeSink struct {
	posts []struct {
		text, root string
		props      map[string]any
	}
	reacted   []string
	unreacted []string
}

func (f *fakeSink) Create(_ context.Context, _, text, root string, props map[string]any) (string, error) {
	f.posts = append(f.posts, struct {
		text, root string
		props      map[string]any
	}{text, root, props})
	return "mm" + string(rune('0'+len(f.posts))), nil
}
func (f *fakeSink) React(_ context.Context, post, emoji string) error {
	f.reacted = append(f.reacted, post+"/"+emoji)
	return nil
}
func (f *fakeSink) Unreact(_ context.Context, post, emoji string) error {
	f.unreacted = append(f.unreacted, post+"/"+emoji)
	return nil
}

type fakeStore struct {
	pos   uint64
	posts map[uint64]Post
}

func (f *fakeStore) Position(context.Context) (uint64, bool, error) { return f.pos, f.pos != 0, nil }
func (f *fakeStore) Post(_ context.Context, _ string, seq uint64) (Post, bool, error) {
	p, ok := f.posts[seq]
	return p, ok, nil
}
func (f *fakeStore) Save(_ context.Context, _ string, seq uint64, p Post) error {
	f.posts[seq] = p
	f.pos = seq
	return nil
}

func newMirror(t *testing.T) (*Mirror, *fakeSink, *fakeStore) {
	sink, st := &fakeSink{}, &fakeStore{posts: map[uint64]Post{}}
	return &Mirror{Home: "home", Prefix: "a2a.", Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Sink: sink, Store: st, Links: LinksNone{}}, sink, st
}

var channel = func(context.Context) (string, error) { return "chan", nil }

func TestAPostIsMirroredOnceWithTheNATSIdAndAttribution(t *testing.T) {
	m, sink, st := newMirror(t)
	msg := message.Msg{Kind: message.Post, Text: "hello", Props: map[string]any{"a2a.a2a": map[string]any{"kind": "TASK"}}}
	if err := m.Handle(context.Background(), channel, "agent-a", 4, msg); err != nil {
		t.Fatal(err)
	}
	if len(sink.posts) != 1 || sink.posts[0].root != "" {
		t.Fatalf("%+v", sink.posts)
	}
	if got := sink.posts[0].text; got != "**agent-a**\n\nhello" {
		t.Fatalf("attribution: %q", got)
	}
	if sink.posts[0].props["a2a.nats_id"] != "n1:home:4" || sink.posts[0].props["a2a.shown_as"] != "agent-a" {
		t.Fatalf("props: %+v", sink.posts[0].props)
	}
	if _, ok := sink.posts[0].props["a2a.a2a"]; ok {
		t.Fatalf("an a2a prop from the NATS side reached Mattermost: %+v", sink.posts[0].props)
	}
	// the same message again (a replay): no second post, position moved on
	if err := m.Handle(context.Background(), channel, "agent-a", 4, msg); err != nil {
		t.Fatal(err)
	}
	if len(sink.posts) != 1 || st.pos != 4 {
		t.Fatalf("replay: %+v pos %d", sink.posts, st.pos)
	}
}

func TestAReplyThreadsOnItsRootAndFallsBackToTheProp(t *testing.T) {
	m, sink, _ := newMirror(t)
	root := uint64(2)
	if err := m.Handle(context.Background(), channel, "agent-a", 2, message.Msg{Kind: message.Post, Text: "root"}); err != nil {
		t.Fatal(err)
	}
	if err := m.Handle(context.Background(), channel, "agent-a", 3, message.Msg{Kind: message.Post, Text: "reply", Root: &root}); err != nil {
		t.Fatal(err)
	}
	if len(sink.posts) != 2 || sink.posts[1].root != "mm1" {
		t.Fatalf("thread: %+v", sink.posts)
	}
	unknown := uint64(99)
	if err := m.Handle(context.Background(), channel, "agent-a", 5, message.Msg{Kind: message.Post, Text: "old root", Root: &unknown}); err != nil {
		t.Fatal(err)
	}
	if sink.posts[2].root != "" || sink.posts[2].props["a2a.nats_root"] != "n1:home:99" {
		t.Fatalf("fallback: %+v", sink.posts[2])
	}
}

func TestReactionsMapToTheMirroredPost(t *testing.T) {
	m, sink, _ := newMirror(t)
	if err := m.Handle(context.Background(), channel, "agent-a", 2, message.Msg{Kind: message.Post, Text: "root"}); err != nil {
		t.Fatal(err)
	}
	ref := uint64(2)
	if err := m.Handle(context.Background(), channel, "agent-b", 3, message.Msg{Kind: message.Reaction, Ref: &ref, Emoji: "thumbsup"}); err != nil {
		t.Fatal(err)
	}
	if len(sink.reacted) != 1 || sink.reacted[0] != "mm1/thumbsup" {
		t.Fatalf("%+v", sink.reacted)
	}
	if err := m.Handle(context.Background(), channel, "agent-b", 4, message.Msg{Kind: message.Unreaction, Ref: &ref, Emoji: "thumbsup"}); err != nil {
		t.Fatal(err)
	}
	if len(sink.unreacted) != 1 {
		t.Fatalf("%+v", sink.unreacted)
	}
	// a reaction to a post the bridge does not know is skipped, not failed
	unknown := uint64(99)
	if err := m.Handle(context.Background(), channel, "agent-b", 5, message.Msg{Kind: message.Reaction, Ref: &unknown, Emoji: "x"}); err != nil {
		t.Fatal(err)
	}
	if len(sink.reacted) != 1 || len(sink.posts) != 1 {
		t.Fatalf("unknown ref: %+v %+v", sink.reacted, sink.posts)
	}
}
