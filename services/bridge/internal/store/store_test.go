package store

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/message"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
)

func open(t *testing.T) (*KV, *nats.Conn) {
	t.Helper()
	srv, err := natsserver.NewServer(&natsserver.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
		StoreDir: t.TempDir(), NoLog: true, NoSigs: true})
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	// the operator's step, not the bridge's
	if err := CreateBucket(context.Background(), js); err != nil {
		t.Fatal(err)
	}
	kv, err := Open(context.Background(), nc, "BUS_BRIDGE")
	if err != nil {
		t.Fatal(err)
	}
	return kv, nc
}

// Two homes share the stream's sequence numbers but not their positions: one
// home's replay must never move past another's unread posts (#90 review).
func TestEveryHomeHasItsOwnPosition(t *testing.T) {
	kv, _ := open(t)
	ctx := context.Background()
	if err := kv.Save(ctx, "home-a", 5, mirror.Post{ID: "mm-5"}); err != nil {
		t.Fatal(err)
	}
	if err := kv.Save(ctx, "home-b", 120, mirror.Post{ID: "mm-120"}); err != nil {
		t.Fatal(err)
	}
	if pos, ok, err := kv.Position(ctx, "home-a"); err != nil || !ok || pos != 5 {
		t.Fatalf("home-a: %d %v %v", pos, ok, err)
	}
	if pos, ok, err := kv.Position(ctx, "home-b"); err != nil || !ok || pos != 120 {
		t.Fatalf("home-b: %d %v %v", pos, ok, err)
	}
	if pos, ok, err := kv.Position(ctx, "home-c"); err != nil || ok || pos != 0 {
		t.Fatalf("home-c: %d %v %v", pos, ok, err)
	}
	if p, ok, _ := kv.Post(ctx, "home-a", 5); !ok || p.ID != "mm-5" {
		t.Fatalf("map: %+v %v", p, ok)
	}
}

func TestReactionsAreCountedPerMessageAndEmoji(t *testing.T) {
	kv, _ := open(t)
	ctx := context.Background()
	// a sender reacting twice is one reaction; the last one leaving removes it
	for _, step := range []struct {
		sender string
		add    bool
		want   int
	}{{"a", true, 1}, {"a", true, 1}, {"b", true, 2}, {"a", false, 1}, {"b", false, 0}} {
		n, err := kv.Reaction(ctx, "home", "1", "thumbsup", step.sender, step.add)
		if err != nil || n != step.want {
			t.Fatalf("step %+v: %d %v", step, n, err)
		}
	}
	// a different emoji is its own set
	if n, err := kv.Reaction(ctx, "home", "1", "eyes", "a", true); err != nil || n != 1 {
		t.Fatalf("eyes: %d %v", n, err)
	}
}

// RT-6.44: an own bot's reaction is recorded per sender and emoji, beside
// the counts, until its removal clears it.
func TestOwnReactionsAreRecordedPerSenderAndEmoji(t *testing.T) {
	kv, _ := open(t)
	ctx := context.Background()
	if err := kv.SetOwnReaction(ctx, "home", "1", "thumbsup", "agent-a", true); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		emoji, sender string
		want          bool
	}{{"thumbsup", "agent-a", true}, {"eyes", "agent-a", false}, {"thumbsup", "agent-b", false}} {
		if had, err := kv.OwnReaction(ctx, "home", "1", c.emoji, c.sender); err != nil || had != c.want {
			t.Fatalf("%+v: %v %v", c, had, err)
		}
	}
	if n, _ := kv.Reaction(ctx, "home", "1", "thumbsup", "agent-b", true); n != 1 {
		t.Fatalf("the counts saw the own bot's reaction: %d", n)
	}
	for range 2 { // clearing twice is no error
		if err := kv.SetOwnReaction(ctx, "home", "1", "thumbsup", "agent-a", false); err != nil {
			t.Fatal(err)
		}
	}
	if had, err := kv.OwnReaction(ctx, "home", "1", "thumbsup", "agent-a"); err != nil || had {
		t.Fatalf("still recorded: %v %v", had, err)
	}
}

// A bucket with direct get on would answer a request with a key's value onto
// the request's reply subject, which the server does not check against publish
// permissions; the bridge refuses such a bucket instead of using it.
func TestABucketWithDirectGetIsRefused(t *testing.T) {
	srv, err := natsserver.NewServer(&natsserver.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
		StoreDir: t.TempDir(), NoLog: true, NoSigs: true})
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	// `nats kv add`: the client's own create, which turns direct get on
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := js.CreateKeyValue(context.Background(), jetstream.KeyValueConfig{
		Bucket: "BUS_BRIDGE", History: 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(context.Background(), nc, "BUS_BRIDGE"); err == nil {
		t.Fatal("the bridge accepted a bucket with direct get")
	}
}

// The code and the operator's file agree: the embedded configuration is a KV
// bucket with direct get off and one value per key.
func TestTheOperatorConfigIsTheBucketTheStoreNeeds(t *testing.T) {
	cfg, err := OperatorConfig()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Name != "KV_BUS_BRIDGE" || cfg.AllowDirect || cfg.MaxMsgsPerSubject != 1 ||
		len(cfg.Subjects) != 1 || cfg.Subjects[0] != "$KV.BUS_BRIDGE.>" {
		t.Fatalf("bucket.json: %+v", cfg)
	}
}

// RT-6.42 steps 1 and 5: a mirrored post's way back from its Mattermost id to
// its sequence, and a home's watermark.
func TestTheWayBackAndTheWatermark(t *testing.T) {
	kv, _ := open(t)
	ctx := context.Background()
	if _, ok, _ := kv.Seq(ctx, "home", "q1w2e3r4t5y6u7i8o9p0a1s2d3"); ok {
		t.Fatal("a sequence before any save")
	}
	if err := kv.Save(ctx, "home", 12, mirror.Post{ID: "q1w2e3r4t5y6u7i8o9p0a1s2d3"}); err != nil {
		t.Fatal(err)
	}
	if seq, ok, err := kv.Seq(ctx, "home", "q1w2e3r4t5y6u7i8o9p0a1s2d3"); err != nil || !ok || seq != 12 {
		t.Fatalf("%d %v %v", seq, ok, err)
	}
	if _, ok, _ := kv.Seq(ctx, "other", "q1w2e3r4t5y6u7i8o9p0a1s2d3"); ok {
		t.Fatal("the way back crosses homes")
	}
	if _, ok, _ := kv.Watermark(ctx, "home"); ok {
		t.Fatal("a watermark before any")
	}
	if err := kv.SetWatermark(ctx, "home", 1700000000123); err != nil {
		t.Fatal(err)
	}
	if wm, ok, err := kv.Watermark(ctx, "home"); err != nil || !ok || wm != 1700000000123 {
		t.Fatalf("%d %v %v", wm, ok, err)
	}
}

// RT-6.42 step 1: the seen bucket is the operator's, with an age limit and
// discard old; a bucket without them is refused, and a reaction's kind is
// replaced by the opposite one.
func TestTheSeenBucket(t *testing.T) {
	_, nc := open(t)
	ctx := context.Background()
	js, _ := jetstream.New(nc)
	if _, err := OpenSeen(ctx, nc, "BUS_BRIDGE_SEEN"); err == nil {
		t.Fatal("bound to a bucket nobody made")
	}
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{Name: "KV_BAD", Subjects: []string{"$KV.BAD.>"},
		MaxMsgsPerSubject: 1, Discard: jetstream.DiscardNew}); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenSeen(ctx, nc, "BAD"); err == nil {
		t.Fatal("bound to a bucket without an age limit")
	}
	if err := CreateSeenBucket(ctx, js); err != nil {
		t.Fatal(err)
	}
	seen, err := OpenSeen(ctx, nc, "BUS_BRIDGE_SEEN")
	if err != nil {
		t.Fatal(err)
	}
	post := "q1w2e3r4t5y6u7i8o9p0a1s2d3"
	if done, err := seen.Post(ctx, post); done || err != nil {
		t.Fatalf("%v %v", done, err)
	}
	if err := seen.MarkPost(ctx, post); err != nil {
		t.Fatal(err)
	}
	if done, err := seen.Post(ctx, post); !done || err != nil {
		t.Fatalf("%v %v", done, err)
	}
	if err := seen.SetReactions(ctx, post, map[string]string{"+1 u": "reaction"}); err != nil {
		t.Fatal(err)
	}
	if err := seen.SetReactions(ctx, post, map[string]string{"+1 u": "unreaction"}); err != nil {
		t.Fatal(err)
	}
	if kinds, err := seen.Reactions(ctx, post); err != nil || kinds["+1 u"] != "unreaction" {
		t.Fatalf("%v %v", kinds, err)
	}
	if err := seen.MarkPost(ctx, "a.b"); err == nil {
		t.Fatal("a key that is no id")
	}
}

// RT-6.42 step 1: a seen bucket that keeps its keys less than an hour lets an
// event go out twice; it is refused.
func TestASeenBucketShorterThanAnHourIsRefused(t *testing.T) {
	_, nc := open(t)
	ctx := context.Background()
	js, _ := jetstream.New(nc)
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{Name: "KV_SHORT", Subjects: []string{"$KV.SHORT.>"},
		MaxMsgsPerSubject: 1, Discard: jetstream.DiscardOld, MaxAge: time.Minute}); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenSeen(ctx, nc, "SHORT"); err == nil {
		t.Fatal("bound to a bucket that keeps its keys a minute")
	}
}

type idSink struct{ n int }

func (s *idSink) Create(context.Context, string, string, string, map[string]any) (string, error) {
	s.n++
	return fmt.Sprintf("mm%d", s.n), nil
}
func (*idSink) React(context.Context, string, string) error   { return nil }
func (*idSink) Unreact(context.Context, string, string) error { return nil }

// RT-6.36 step 4, RT-6.42 step 5, #297: an agent's reaction mirrored onto the
// copy never replaces the copy's way back, so a person's reply or reaction on
// the copy names the post's sequence, also after the entry is read again.
func TestAMirroredReactionLeavesThePostsWayBack(t *testing.T) {
	kv, _ := open(t)
	ctx := context.Background()
	m := &mirror.Mirror{Home: "home", Prefix: "a2a.", Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Sink: &idSink{}, Store: kv, Links: mirror.LinksNone{}}
	ch := func(context.Context) (string, error) { return "chan", nil }
	if err := m.Handle(ctx, ch, "agent-b", 5, message.Msg{Kind: message.Post, Text: "hi"}); err != nil {
		t.Fatal(err)
	}
	ref := uint64(5)
	for seq, kind := range map[uint64]message.Kind{6: message.Reaction, 7: message.Unreaction, 8: message.Reaction} {
		if err := m.Handle(ctx, ch, "agent-c", seq, message.Msg{Kind: kind, Ref: &ref, Emoji: "thumbsup"}); err != nil {
			t.Fatal(err)
		}
	}
	// a replay of the reaction after a crash changes nothing either
	if err := m.Handle(ctx, ch, "agent-c", 6, message.Msg{Kind: message.Reaction, Ref: &ref, Emoji: "thumbsup"}); err != nil {
		t.Fatal(err)
	}
	// the root of a person's reply and the ref of a person's reaction both come from Seq
	if seq, ok, err := kv.Seq(ctx, "home", "mm1"); err != nil || !ok || seq != 5 {
		t.Fatalf("the post's way back: %d %v %v", seq, ok, err)
	}
	if p, ok, err := kv.Post(ctx, "home", 6); err != nil || !ok || p.ID != "mm1" || !p.Reaction {
		t.Fatalf("the reaction's entry: %+v %v %v", p, ok, err)
	}
	if p, ok, err := kv.Post(ctx, "home", 5); err != nil || !ok || p.ID != "mm1" || p.Reaction {
		t.Fatalf("the post's entry: %+v %v %v", p, ok, err)
	}
}
