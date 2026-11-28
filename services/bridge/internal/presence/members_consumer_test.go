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

// RT-6.37, RT-6.42 step 11: the bridge reads the memberships through its own
// consumer, made at every start, last per subject: a restart replays every
// membership the stream holds, also one written while the bridge was down, and
// the operator's presence consumer needs no second filter.
func TestTheBridgeReadsTheMembershipsAfterEveryRestart(t *testing.T) {
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
		Subjects: []string{"presence.>"}, NoAck: true, MaxMsgsPerSubject: 5,
		Storage: jetstream.MemoryStorage}); err != nil {
		t.Fatal(err)
	}
	// written before the bridge is up: the replay carries them
	mustPublish(t, nc, "presence.member.home.agent-a", `{"v":1}`)
	mustPublish(t, nc, "presence.member.guest.agent-a", `{"v":1}`)
	mustPublish(t, nc, "presence.member.home.agent-b", `{"v":1}`)

	start := func() (*Snapshot, *MembersReader, func()) {
		snap := New()
		r := &MembersReader{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Deliver: "deliver.bridge.members",
			Stream: "PRESENCE", Into: snap, CaughtUp: make(chan struct{}),
			Heartbeat: 100 * time.Millisecond, Inactive: 500 * time.Millisecond}
		cctx, cancel := context.WithCancel(ctx)
		done := make(chan error, 1)
		go func() { done <- r.Run(cctx, nc) }()
		select {
		case <-r.CaughtUp:
		case <-time.After(10 * time.Second):
			t.Fatal("the memberships were not read")
		}
		return snap, r, func() { cancel(); <-done }
	}
	snap, _, stop := start()
	for _, pair := range [][2]string{{"home", "agent-a"}, {"guest", "agent-a"}, {"home", "agent-b"}} {
		if !snap.MemberFresh(pair[0], pair[1]) {
			t.Fatalf("no membership of %s in %s", pair[1], pair[0])
		}
	}
	if snap.MemberFresh("home", "agent-c") || snap.MemberFresh("elsewhere", "agent-a") {
		t.Fatal("a membership nobody announced")
	}
	// last per subject: a later record of a pair replaces the earlier one
	stream, err := js.Stream(ctx, "PRESENCE")
	if err != nil {
		t.Fatal(err)
	}
	lister := stream.ListConsumers(ctx)
	var policy jetstream.DeliverPolicy
	found := false
	for info := range lister.Info() {
		if info.Name == MembersConsumer {
			policy, found = info.Config.DeliverPolicy, true
		}
	}
	if !found || policy != jetstream.DeliverLastPerSubjectPolicy {
		t.Fatalf("the consumer's deliver policy is %v (found %v), want last per subject", policy, found)
	}
	// a membership written while the bridge runs arrives too
	mustPublish(t, nc, "presence.member.other.agent-b", `{"v":1}`)
	until(t, func() bool { return snap.MemberFresh("other", "agent-b") })
	stop()

	// a restart, with no new record written: the replay has every membership
	snap2, _, stop2 := start()
	if !snap2.MemberFresh("home", "agent-a") || !snap2.MemberFresh("other", "agent-b") {
		t.Fatal("a restart lost a membership")
	}
	stop2()
	// the stream's time runs on: a membership older than MemberKeep is stale,
	// so a revoked membership opens no read past it
	snap2.Clock = func() time.Time { return time.Now().Add(MemberKeep + time.Hour) }
	if snap2.MemberFresh("home", "agent-a") {
		t.Fatal("a membership past MemberKeep is fresh")
	}
}
