package mirror

import (
	"context"
	"io"
	"log/slog"
	"math/bits"
	"strings"
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
	pos       uint64
	posts     map[uint64]Post
	reactions map[string]int // a bit set of senders per post/emoji
}

func (f *fakeStore) Position(context.Context, string) (uint64, bool, error) {
	return f.pos, f.pos != 0, nil
}
func (f *fakeStore) Post(_ context.Context, _ string, seq uint64) (Post, bool, error) {
	p, ok := f.posts[seq]
	return p, ok, nil
}
func (f *fakeStore) Save(_ context.Context, _ string, seq uint64, p Post) error {
	f.posts[seq] = p
	f.pos = seq
	return nil
}

func (f *fakeStore) Reaction(_ context.Context, home string, seq uint64, emoji, sender string, add bool) (int, error) {
	k := home + "/" + emoji
	set := f.reactions[k]
	if add {
		if set|(1<<f.senderBit(sender)) == set {
			return bits.OnesCount(uint(set)), nil
		}
		set |= 1 << f.senderBit(sender)
	} else {
		set &^= 1 << f.senderBit(sender)
	}
	f.reactions[k] = set
	return bits.OnesCount(uint(set)), nil
}

func (f *fakeStore) senderBit(sender string) int {
	if sender == "agent-c" {
		return 1
	}
	return 0
}

func newMirror(t *testing.T) (*Mirror, *fakeSink, *fakeStore) {
	sink, st := &fakeSink{}, &fakeStore{posts: map[uint64]Post{}, reactions: map[string]int{}}
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
	if got := sink.posts[0].text; got != SenderMark+"**agent-a**\n\nhello" {
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

func TestOnlyTheSendersBusPropsPassAndMentionsAreDefused(t *testing.T) {
	sink, st := &fakeSink{}, &fakeStore{posts: map[uint64]Post{}, reactions: map[string]int{}}
	m := &Mirror{Home: "home", Prefix: "x_", Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Sink: sink, Store: st, Links: LinksNone{}}
	msg := message.Msg{Kind: message.Post, Text: "@channel please, @alice too", Props: map[string]any{
		"x_ok": "kept", "x_system": "1", "x_nats_id": "forged", "x_nats_root": "n1:home:9",
		"from_webhook": "true", "override_username": "admin", "attachments": []any{},
		"a2a_kind": "TASK",
	}}
	if err := m.Handle(context.Background(), channel, "agent-a", 4, msg); err != nil {
		t.Fatal(err)
	}
	props := sink.posts[0].props
	if props["x_ok"] != "kept" || props["x_nats_id"] != "n1:home:4" {
		t.Fatalf("props: %+v", props)
	}
	for _, k := range []string{"x_system", "x_nats_root", "from_webhook", "override_username", "attachments", "a2a_kind"} {
		if _, ok := props[k]; ok {
			t.Errorf("%s reached the mirror: %+v", k, props)
		}
	}
	if strings.Contains(sink.posts[0].text, "@channel") || strings.Contains(sink.posts[0].text, "@alice") {
		t.Fatalf("a mention would notify: %q", sink.posts[0].text)
	}
	if !strings.Contains(sink.posts[0].text, "@\u200bchannel") {
		t.Fatalf("the mention is not defused with a zero-width space: %q", sink.posts[0].text)
	}
	// too many bytes of the sender's props: all of them go, the bridge's stay
	big := message.Msg{Kind: message.Post, Text: "x", Props: map[string]any{
		"x_ok": "kept", "x_big": strings.Repeat("a", 2100)}}
	if err := m.Handle(context.Background(), channel, "agent-a", 5, big); err != nil {
		t.Fatal(err)
	}
	props = sink.posts[1].props
	if _, ok := props["x_ok"]; ok || props["x_nats_id"] != "n1:home:5" {
		t.Fatalf("oversize props: %+v", props)
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
	// a second agent with the same emoji: one bot, one reaction
	if err := m.Handle(context.Background(), channel, "agent-c", 4, message.Msg{Kind: message.Reaction, Ref: &ref, Emoji: "thumbsup"}); err != nil {
		t.Fatal(err)
	}
	if len(sink.reacted) != 1 {
		t.Fatalf("a second agent's reaction doubled the bot's: %+v", sink.reacted)
	}
	// one of the two takes theirs back: the bot keeps the reaction
	if err := m.Handle(context.Background(), channel, "agent-b", 5, message.Msg{Kind: message.Unreaction, Ref: &ref, Emoji: "thumbsup"}); err != nil {
		t.Fatal(err)
	}
	if len(sink.unreacted) != 0 {
		t.Fatalf("the bot's reaction went with the first agent's: %+v", sink.unreacted)
	}
	// the last one takes theirs back: the bot removes it
	if err := m.Handle(context.Background(), channel, "agent-c", 6, message.Msg{Kind: message.Unreaction, Ref: &ref, Emoji: "thumbsup"}); err != nil {
		t.Fatal(err)
	}
	if len(sink.unreacted) != 1 {
		t.Fatalf("%+v", sink.unreacted)
	}
	// a reaction to a post the bridge does not know is skipped, not failed
	unknown := uint64(99)
	if err := m.Handle(context.Background(), channel, "agent-b", 7, message.Msg{Kind: message.Reaction, Ref: &unknown, Emoji: "x"}); err != nil {
		t.Fatal(err)
	}
	if len(sink.reacted) != 1 || len(sink.posts) != 1 {
		t.Fatalf("unknown ref: %+v %+v", sink.reacted, sink.posts)
	}
}

// fakeBots gives agent-a its own bot; everyone else posts with the bridge bot.
type fakeBots struct {
	own    map[string]*fakeSink
	forgot []string
}

func (f *fakeBots) Bot(_ context.Context, name, _ string) Sink {
	if s, ok := f.own[name]; ok {
		return s
	}
	return nil
}
func (f *fakeBots) Forget(name string) { f.forgot = append(f.forgot, name) }

// RT-6.44: with agent bots on, a sender's post goes under its own bot,
// without the attribution line and with nats_id and shown_as; a sender
// without one posts with the bridge bot as before. Its reactions go under its
// own bot, one each.
func TestTheMirrorPostsUnderTheSendersOwnBot(t *testing.T) {
	m, bridge, _ := newMirror(t)
	own := &fakeSink{}
	m.Bots = &fakeBots{own: map[string]*fakeSink{"agent-a": own}}
	ctx := context.Background()
	if err := m.Handle(ctx, channel, "agent-a", 2, message.Msg{Kind: message.Post, Text: "hi @channel"}); err != nil {
		t.Fatal(err)
	}
	if err := m.Handle(ctx, channel, "agent-b", 3, message.Msg{Kind: message.Post, Text: "hello"}); err != nil {
		t.Fatal(err)
	}
	if len(own.posts) != 1 || len(bridge.posts) != 1 {
		t.Fatalf("own %+v, bridge %+v", own.posts, bridge.posts)
	}
	if own.posts[0].text != "hi @\u200bchannel" {
		t.Fatalf("own text %q", own.posts[0].text)
	}
	if own.posts[0].props["a2a.nats_id"] != "n1:home:2" || own.posts[0].props["a2a.shown_as"] != "agent-a" {
		t.Fatalf("own props %+v", own.posts[0].props)
	}
	if bridge.posts[0].text != SenderMark+"**agent-b**\n\nhello" || bridge.posts[0].props["a2a.nats_id"] != "n1:home:3" {
		t.Fatalf("fallback %+v", bridge.posts[0])
	}
	ref := uint64(3)
	for i, k := range []message.Kind{message.Reaction, message.Unreaction} {
		if err := m.Handle(ctx, channel, "agent-a", uint64(4+i), message.Msg{Kind: k, Ref: &ref, Emoji: "eyes"}); err != nil {
			t.Fatal(err)
		}
	}
	if len(own.reacted) != 1 || own.reacted[0] != "mm1/eyes" || len(own.unreacted) != 1 || len(bridge.reacted) != 0 {
		t.Fatalf("own %v/%v, bridge %v", own.reacted, own.unreacted, bridge.reacted)
	}
}
