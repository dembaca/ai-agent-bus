package reader

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/store"
)

type sink struct {
	got   chan [3]string // text, root, nats_id
	react chan string
}

func (s *sink) Create(_ context.Context, _, text, root string, props map[string]any) (string, error) {
	id, _ := props["a2a.nats_id"].(string)
	s.got <- [3]string{text, root, id}
	return "mm-" + id, nil
}
func (s *sink) React(_ context.Context, post, emoji string) error {
	s.react <- post + "/" + emoji
	return nil
}
func (s *sink) Unreact(context.Context, string, string) error { return nil }

func lab(t *testing.T) (*nats.Conn, jetstream.JetStream) {
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
	if _, err := js.CreateStream(context.Background(), jetstream.StreamConfig{Name: "BUS",
		Subjects: []string{"bus.>"}, NoAck: true, Storage: jetstream.MemoryStorage}); err != nil {
		t.Fatal(err)
	}
	operator(t, js)
	return nc, js
}

// operator is the step the bridge cannot do itself: the operator creates the
// push consumer, and the bridge only subscribes to what it delivers (a pull
// would let the credential replay the home). The test's ack wait is short, so
// a message published while the bridge is down comes back promptly.
func operator(t *testing.T, js jetstream.JetStream) {
	t.Helper()
	if _, err := js.CreateOrUpdateConsumer(context.Background(), "BUS", jetstream.ConsumerConfig{
		Name: "bridge_home", Durable: "bridge_home", FilterSubject: "bus.ch.home.*",
		DeliverSubject: "deliver.bridge.home", DeliverPolicy: jetstream.DeliverNewPolicy,
		AckPolicy: jetstream.AckExplicitPolicy, AckWait: 2 * time.Second, MaxAckPending: 8,
	}); err != nil {
		t.Fatal(err)
	}
}

func start(t *testing.T, nc *nats.Conn, js jetstream.JetStream, kv *store.KV,
	s *sink) (context.CancelFunc, chan error) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	m := &mirror.Mirror{Home: "home", Prefix: "a2a.", Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Sink: s, Store: kv, Links: mirror.LinksNone{}}
	r := &Reader{Home: "home", Deliver: "deliver.bridge.home", Stream: "BUS", Log: m.Log, Mirror: m,
		Store: kv, Prefix: "bus", PropsPrefix: "a2a.", Ready: make(chan struct{})}
	go func() {
		done <- r.Run(ctx, nc, func(context.Context) (string, error) { return "chan", nil })
	}()
	select {
	case <-r.Ready:
	case <-time.After(5 * time.Second):
		t.Fatal("the reader did not subscribe")
	}
	return cancel, done
}

// A message on the delivery subject that is no delivery — an idle heartbeat,
// flow control, or a publish by whoever may write there — has no ack reply and
// no sequence: it must not reset the position (it once set it to 0, and every
// later post was mirrored twice).
func TestAMessageThatIsNoDeliveryTouchesNothing(t *testing.T) {
	nc, js := lab(t)
	kv := operatorBucket(t, nc, js)
	s := &sink{got: make(chan [3]string, 8), react: make(chan string, 4)}
	cancel, _ := start(t, nc, js, kv, s)
	defer cancel()
	mustPublish(t, nc, "bus.ch.home.agent-a", `{"v":1,"kind":"post","text":"one"}`)
	until(t, s, mirror.SenderMark+"**agent-a**\n\none")
	deadline := time.Now().Add(3 * time.Second)
	for {
		if pos, ok, err := kv.Position(context.Background(), "home"); err == nil && ok && pos == 1 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the position did not reach 1")
		}
		time.Sleep(20 * time.Millisecond)
	}
	// a heartbeat-like empty message and a forged one with a bus reply
	if err := nc.Publish("deliver.bridge.home", nil); err != nil {
		t.Fatal(err)
	}
	if err := nc.PublishMsg(&nats.Msg{Subject: "deliver.bridge.home", Reply: "bus.ch.home.zz",
		Data: []byte(`{"v":1,"kind":"post","text":"forged"}`)}); err != nil {
		t.Fatal(err)
	}
	_ = nc.Flush()
	time.Sleep(300 * time.Millisecond)
	if pos, ok, err := kv.Position(context.Background(), "home"); err != nil || !ok || pos != 1 {
		t.Fatalf("the position moved: %d %v %v", pos, ok, err)
	}
	select {
	case got := <-s.got:
		t.Fatalf("a stray message was mirrored: %+v", got)
	default:
	}
}

func until(t *testing.T, s *sink, want string) [3]string {
	t.Helper()
	select {
	case got := <-s.got:
		if want != "" && got[0] != want {
			t.Fatalf("text %q, want %q", got[0], want)
		}
		return got
	case <-time.After(5 * time.Second):
		t.Fatal("no mirror")
	}
	return [3]string{}
}

// operatorBucket is the operator's step the bridge cannot do itself: it makes
// the KV bucket, then the bridge binds to it.
func operatorBucket(t *testing.T, nc *nats.Conn, js jetstream.JetStream) *store.KV {
	t.Helper()
	if err := store.CreateBucket(context.Background(), js); err != nil {
		t.Fatal(err)
	}
	kv, err := store.Open(context.Background(), nc, "BUS_BRIDGE")
	if err != nil {
		t.Fatal(err)
	}
	return kv
}

func TestAPostAReplyAndAReactionOverARealServer(t *testing.T) {
	nc, js := lab(t)
	kv := operatorBucket(t, nc, js)
	s := &sink{got: make(chan [3]string, 8), react: make(chan string, 4)}
	cancel, _ := start(t, nc, js, kv, s)
	defer cancel()

	mustPublish(t, nc, "bus.ch.home.agent-a", `{"v":1,"kind":"post","text":"hello"}`)
	first := until(t, s, mirror.SenderMark+"**agent-a**\n\nhello")
	if first[1] != "" || first[2] != "n1:home:1" {
		t.Fatalf("first: %+v", first)
	}
	mustPublish(t, nc, "bus.ch.home.agent-b", `{"v":1,"kind":"post","text":"reply","root":1}`)
	reply := until(t, s, mirror.SenderMark+"**agent-b**\n\nreply")
	if reply[1] != "mm-n1:home:1" {
		t.Fatalf("reply threads on %q", reply[1])
	}
	mustPublish(t, nc, "bus.ch.home.agent-b", `{"v":1,"kind":"reaction","ref":1,"emoji":"thumbsup"}`)
	select {
	case got := <-s.react:
		if got != "mm-n1:home:1/thumbsup" {
			t.Fatalf("reaction %q", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("reaction not mirrored")
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		pos, ok, err := kv.Position(context.Background(), "home")
		if err != nil {
			t.Fatal(err)
		}
		if ok && pos == 3 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("position %d, want 3", pos)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestARestartNeitherRepeatsNorLoses(t *testing.T) {
	nc, js := lab(t)
	kv := operatorBucket(t, nc, js)
	s := &sink{got: make(chan [3]string, 8), react: make(chan string, 4)}
	cancel, done := start(t, nc, js, kv, s)
	mustPublish(t, nc, "bus.ch.home.agent-a", `{"v":1,"kind":"post","text":"one"}`)
	until(t, s, mirror.SenderMark+"**agent-a**\n\none")
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("reader did not stop")
	}
	// while the bridge is down a post arrives, and the old one is not repeated
	mustPublish(t, nc, "bus.ch.home.agent-a", `{"v":1,"kind":"post","text":"two"}`)
	cancel2, _ := start(t, nc, js, kv, s)
	defer cancel2()
	got := until(t, s, mirror.SenderMark+"**agent-a**\n\ntwo")
	if got[2] != "n1:home:2" {
		t.Fatalf("after restart: %+v", got)
	}
	select {
	case again := <-s.got:
		t.Fatalf("a post was repeated after the restart: %+v", again)
	case <-time.After(300 * time.Millisecond):
	}
}

func mustPublish(t *testing.T, nc *nats.Conn, subject, body string) {
	t.Helper()
	if err := nc.Publish(subject, []byte(body)); err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
}
