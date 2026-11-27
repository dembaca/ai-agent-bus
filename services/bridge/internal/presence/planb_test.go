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
// the record the login service writes from the directory, delivered by the
// operator's presence consumer beside the presences.
func TestTheBridgeReadsAChannelsPlanBPieces(t *testing.T) {
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
	// the operator's consumer with its second filter (nats-server.example.conf)
	if _, err := js.CreateOrUpdateConsumer(ctx, "PRESENCE", jetstream.ConsumerConfig{
		Name: ConsumerName, Durable: ConsumerName, FilterSubjects: []string{"presence.*.*", "presence.planb.*.v1"},
		DeliverSubject: "deliver.bridge.presence", DeliverPolicy: jetstream.DeliverLastPerSubjectPolicy,
		AckPolicy: jetstream.AckNonePolicy,
	}); err != nil {
		t.Fatal(err)
	}
	// written before the bridge is up: last-per-subject delivers them
	mustPublish(t, nc, "presence.planb.home.v1", `{"v":1,"channel":"home","pieces":[1,3]}`)
	mustPublish(t, nc, "presence.planb.none.v1", `{"v":1,"channel":"none","pieces":[]}`)
	mustPublish(t, nc, "presence.planb.odd.v1", `{"v":1,"channel":"odd","pieces":[2,14]}`)

	sw := &Switches{}
	r := &Reader{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Deliver: "deliver.bridge.presence",
		Into: New(), PlanB: sw, Ready: make(chan struct{})}
	cctx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- r.Run(cctx, nc) }()
	<-r.Ready
	until(t, func() bool { return sw.On(3, "home") })
	if got := sw.Pieces("home"); !slices.Equal(got, []int{1, 3}) || sw.On(2, "home") {
		t.Fatalf("home: %v", got)
	}
	// none switched on, a record that does not decode, no record at all: off
	if sw.Pieces("none") != nil || sw.Pieces("odd") != nil || sw.Pieces("elsewhere") != nil || sw.On(2, "odd") {
		t.Fatalf("none %v odd %v", sw.Pieces("none"), sw.Pieces("odd"))
	}
	// a newer record replaces the channel's; [] switches every piece off
	mustPublish(t, nc, "presence.planb.home.v1", `{"v":1,"channel":"home","pieces":[]}`)
	until(t, func() bool { return sw.Pieces("home") == nil })
	cancel()
	<-done
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
