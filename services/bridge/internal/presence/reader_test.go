package presence

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// The operator's push consumer delivers the presences; the bridge subscribes
// and reads them into the snapshot, its timestamps from the stream.
func TestThePushedPresencesReachTheSnapshot(t *testing.T) {
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
	ctx := context.Background()
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{Name: "PRESENCE",
		Subjects: []string{"presence.>"}, NoAck: true, Storage: jetstream.MemoryStorage}); err != nil {
		t.Fatal(err)
	}
	// the operator's consumer: the latest presence per subject, no acks
	if _, err := js.CreateOrUpdateConsumer(ctx, "PRESENCE", jetstream.ConsumerConfig{
		Name: ConsumerName, Durable: ConsumerName, FilterSubject: "presence.*.*",
		DeliverSubject: "deliver.bridge.presence", DeliverPolicy: jetstream.DeliverLastPerSubjectPolicy,
		AckPolicy: jetstream.AckNonePolicy,
	}); err != nil {
		t.Fatal(err)
	}
	// published before the bridge is up: last-per-subject delivers it
	mustPublish(t, nc, "presence.alice.agent-a", `{"v":1,"status":"online","bot":"bot-a"}`)
	mustPublish(t, nc, "presence.alice.reader", `not json`)

	snap := New()
	r := &Reader{Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Deliver: "deliver.bridge.presence", Into: snap, Ready: make(chan struct{})}
	cctx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- r.Run(cctx, nc) }()
	select {
	case <-r.Ready:
	case err := <-done:
		t.Fatalf("the presence reader did not subscribe: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("the presence reader did not subscribe")
	}
	until(t, func() bool { _, ok := snap.Get("agent-a"); return ok })
	if i, _ := snap.Get("agent-a"); i.Owner != "alice" || i.Bot != "bot-a" || i.Status != Online {
		t.Fatalf("agent-a: %+v", i)
	}
	// an unreadable presence is skipped, and it does not stop the next one
	if _, ok := snap.Get("reader"); ok {
		t.Fatal("an unreadable presence reached the snapshot")
	}

	// a later presence replaces the earlier one
	mustPublish(t, nc, "presence.alice.agent-a", `{"v":1,"status":"bye","bot":"bot-a"}`)
	until(t, func() bool {
		i, ok := snap.Get("agent-a")
		return ok && i.Status == Bye
	})
	cancel()
	<-done
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

func until(t *testing.T, ok func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !ok() {
		if time.Now().After(deadline) {
			t.Fatal("the snapshot did not arrive")
		}
		time.Sleep(20 * time.Millisecond)
	}
}
