package consumers

import (
	"context"
	"log/slog"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

func embedded(t *testing.T) *nats.Conn {
	t.Helper()
	srv, err := server.NewServer(&server.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
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
	return nc
}

// A bye without JetStream's metadata (no server time) removes nothing: the
// manager never falls back to its own clock (RT-6.35).
func TestAByeWithoutTheServersTimeRemovesNothing(t *testing.T) {
	nc := embedded(t)
	m, err := New(nc, Config{Bus: "bus", Presence: "presence", Log: slog.New(slog.DiscardHandler)})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := m.EnsureStreams(ctx); err != nil {
		t.Fatal(err)
	}
	if err := m.Reconcile(ctx, "agent-a", "alice", "alice", []string{"home"}, nil); err != nil {
		t.Fatal(err)
	}
	m.bye(ctx, &nats.Msg{Subject: "presence.alice.agent-a", Data: []byte(`{"status":"bye"}`)})
	for _, n := range []string{"agent-a_ch_home", "agent-a_dm"} {
		if _, err := m.js.ConsumerInfo(BusStream, n); err != nil {
			t.Errorf("%s gone after a bye without metadata: %v", n, err)
		}
	}
	// The same bye as JetStream delivers it, with the server's time, does
	// remove them.
	sub, err := m.js.SubscribeSync("presence.alice.agent-a", nats.BindStream(PresenceStream), nats.OrderedConsumer())
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Publish("presence.alice.agent-a", []byte(`{"status":"bye"}`)); err != nil {
		t.Fatal(err)
	}
	msg, err := sub.NextMsg(5 * time.Second)
	if err != nil {
		t.Fatal(err)
	}
	m.bye(ctx, msg)
	if _, err := m.js.ConsumerInfo(BusStream, "agent-a_dm"); err == nil {
		t.Error("a bye with the server's time removed nothing")
	}
}
