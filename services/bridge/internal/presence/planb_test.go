package presence

import (
	"context"
	"io"
	"log/slog"
	"slices"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// RT-6.39 step 5: the bridge reads a channel's switched-on plan B pieces from
// the records the login service writes from the directory, through its own
// consumer made at every start: after a restart it has every switch again
// without a new record, and a record two republish periods old counts as off.
func TestTheBridgeReadsAChannelsPlanBPiecesAfterEveryRestart(t *testing.T) {
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
		Subjects: []string{"presence.>"}, NoAck: true, MaxMsgsPerSubject: 1, Storage: jetstream.MemoryStorage}); err != nil {
		t.Fatal(err)
	}
	// written before the bridge is up: deliver all replays them
	mustPublish(t, nc, "presence.planb.home.v1", `{"v":1,"channel":"home","pieces":[1,3]}`)
	mustPublish(t, nc, "presence.planb.none.v1", `{"v":1,"channel":"none","pieces":[]}`)
	mustPublish(t, nc, "presence.planb.odd.v1", `{"v":1,"channel":"odd","pieces":[2,14]}`)

	start := func() (*Switches, *PlanBReader, func()) {
		sw := &Switches{}
		r := &PlanBReader{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Deliver: "deliver.bridge.planb",
			Stream: "PRESENCE", Into: sw, CaughtUp: make(chan struct{}),
			Heartbeat: 100 * time.Millisecond, Inactive: 500 * time.Millisecond}
		cctx, cancel := context.WithCancel(ctx)
		done := make(chan error, 1)
		go func() { done <- r.Run(cctx, nc) }()
		select {
		case <-r.CaughtUp:
		case <-time.After(10 * time.Second):
			t.Fatal("the plan B records were not read")
		}
		return sw, r, func() { cancel(); <-done }
	}
	sw, _, stop := start()
	if got := sw.Pieces("home"); !slices.Equal(got, []int{1, 3}) || sw.On(2, "home") {
		t.Fatalf("home: %v", got)
	}
	// none switched on, a record that does not decode, no record at all: off
	if sw.Pieces("none") != nil || sw.Pieces("odd") != nil || sw.Pieces("elsewhere") != nil || sw.On(2, "odd") {
		t.Fatalf("none %v odd %v", sw.Pieces("none"), sw.Pieces("odd"))
	}
	// a newer record replaces the channel's; [] switches every piece off
	mustPublish(t, nc, "presence.planb.home.v1", `{"v":1,"channel":"home","pieces":[2]}`)
	until(t, func() bool { return sw.On(2, "home") })
	stop()

	// a restart: a new reader with empty switches, and no new record written
	sw2, _, stop2 := start()
	defer stop2()
	if got := sw2.Pieces("home"); !slices.Equal(got, []int{2}) {
		t.Fatalf("after a restart: %v", got)
	}
	mustPublish(t, nc, "presence.planb.home.v1", `{"v":1,"channel":"home","pieces":[]}`)
	until(t, func() bool { return sw2.Pieces("home") == nil })
}

// After a start the server's time is seeded from the consumer's creation (the
// create answer's `created`), so one old record replayed first reads as off,
// not as fresh for another two hours.
func TestAnOldPlanBRecordReadsAsOffAfterAFreshStart(t *testing.T) {
	t0 := time.Date(2026, 11, 27, 10, 0, 0, 0, time.UTC)
	sw := &Switches{Clock: func() time.Time { return t0.Add(3 * time.Hour) }}
	sw.Begin()
	sw.Seed(t0.Add(3 * time.Hour)) // the server's clock at the create
	sw.Observed("home", []int{1}, t0)
	sw.Commit()
	if sw.On(1, "home") {
		t.Fatal("a record three hours old reads as on after a start")
	}
	// the created time of a real server is read: a record written now is on
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
	js, _ := jetstream.New(nc)
	ctx := context.Background()
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{Name: "PRESENCE",
		Subjects: []string{"presence.>"}, NoAck: true, MaxMsgsPerSubject: 1, Storage: jetstream.MemoryStorage}); err != nil {
		t.Fatal(err)
	}
	mustPublish(t, nc, "presence.planb.home.v1", `{"v":1,"channel":"home","pieces":[1]}`)
	live := &Switches{}
	r := &PlanBReader{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Deliver: "deliver.bridge.planb",
		Stream: "PRESENCE", Into: live, CaughtUp: make(chan struct{})}
	cctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go func() { _ = r.Run(cctx, nc) }()
	select {
	case <-r.CaughtUp:
	case <-time.After(10 * time.Second):
		t.Fatal("not read")
	}
	if !live.On(1, "home") {
		t.Fatal("a record just written reads as off")
	}
	// the create answer alone seeds the server's time: a second consumer
	// name is not allowed by the reader, so the first one's answer is read on
	// a fresh Switches through create itself, with no record observed
	cctx2, cancel2 := context.WithCancel(ctx)
	defer cancel2()
	cancel()
	time.Sleep(700 * time.Millisecond)
	if err := js.DeleteConsumer(ctx, "PRESENCE", PlanBConsumer); err != nil {
		t.Fatal(err)
	}
	only := &Switches{}
	r2 := &PlanBReader{Log: r.Log, Deliver: "deliver.bridge.planb", Stream: "PRESENCE", Into: only}
	if err := r2.create(cctx2, nc, "deliver.bridge.planb.seed"); err != nil {
		t.Fatal(err)
	}
	only.mu.RLock()
	got := only.srvNow
	only.mu.RUnlock()
	if d := time.Since(got); got.IsZero() || d < -5*time.Second || d > 5*time.Second {
		t.Fatalf("the server time seeded by the create answer is %v (%v from now)", got, d)
	}
	// A record with an old stream time cannot be made here: the server stamps
	// every message with its own clock and the test server has no way to
	// set it; the old-record case is the fake-clock part above.
}

// Off is fail-safe: a record older than two republish periods by the stream's
// time counts as off, and the server's time runs on while nothing is delivered.
func TestAPlanBRecordNotRenewedCountsAsOff(t *testing.T) {
	t0 := time.Date(2026, 11, 27, 10, 0, 0, 0, time.UTC)
	local := t0
	sw := &Switches{Clock: func() time.Time { return local }}
	sw.Observed("home", []int{1}, t0)
	local = t0.Add(time.Hour)
	sw.Observed("other", []int{2}, t0.Add(time.Hour))
	if !sw.On(1, "home") {
		t.Fatal("a record of one hour is off")
	}
	local = t0.Add(PlanBStale - time.Second)
	if !sw.On(1, "home") {
		t.Fatal("a record just under two hours is off")
	}
	local = t0.Add(PlanBStale)
	if sw.On(1, "home") || sw.Pieces("home") != nil {
		t.Fatal("a record of two hours is still on")
	}
	if !sw.On(2, "other") {
		t.Fatal("a younger record of another channel is off")
	}
}

func TestDecodePlanBIsStrict(t *testing.T) {
	if got, err := DecodePlanB([]byte(`{"v":1,"channel":"home","pieces":[13,1]}`), "home"); err != nil || !slices.Equal(got, []int{1, 13}) {
		t.Fatalf("%v %v", got, err)
	}
	for _, bad := range []string{
		`{"v":1,"channel":"home","pieces":[0]}`,
		`{"v":1,"channel":"home","pieces":[14]}`,
		`{"v":1,"channel":"home","pieces":[2,2]}`,
		`{"v":1,"channel":"home","pieces":[true]}`,
		`{"v":1,"channel":"home","pieces":["1"]}`,
		`{"v":1,"channel":"home","pieces":null}`,
		`{"v":1,"channel":"other","pieces":[1]}`,
		`{"v":2,"channel":"home","pieces":[1]}`,
		`{"v":1,"channel":"home","pieces":[1],"extra":true}`,
		`{"v":1,"channel":"home"}`,
		`not json`,
	} {
		if _, err := DecodePlanB([]byte(bad), "home"); err == nil {
			t.Errorf("%s decoded", bad)
		}
	}
	for subject, want := range map[string]bool{"presence.planb.home.v1": true, "presence.planb.home": false,
		"presence.planb.Home.v1": false, "presence.planb.home.v2": false, "other.planb.home.v1": false} {
		if _, ok := PlanBSubject(subject); ok != want {
			t.Errorf("%s: %v", subject, ok)
		}
	}
}
