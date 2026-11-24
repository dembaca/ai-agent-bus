package store

import (
	"context"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

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
	if _, err := js.CreateKeyValue(context.Background(), jetstream.KeyValueConfig{
		Bucket: "BUS_BRIDGE", History: 1}); err != nil {
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
		n, err := kv.Reaction(ctx, "home", 1, "thumbsup", step.sender, step.add)
		if err != nil || n != step.want {
			t.Fatalf("step %+v: %d %v", step, n, err)
		}
	}
	// a different emoji is its own set
	if n, err := kv.Reaction(ctx, "home", 1, "eyes", "a", true); err != nil || n != 1 {
		t.Fatalf("eyes: %d %v", n, err)
	}
}
