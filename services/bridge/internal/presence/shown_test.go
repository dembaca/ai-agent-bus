package presence

import (
	"context"
	"io"
	"log/slog"
	"strconv"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

func TestDecodeShownReadsAStatus(t *testing.T) {
	body := `{"v":1,"status":"online","shown":{"home":"home","emoji":"zzz","text":"idle  ·\n~home","until":1900000000,"crown":true}}`
	s, err := DecodeShown([]byte(body))
	if err != nil || s == nil {
		t.Fatalf("got %v, %v", s, err)
	}
	if s.Home != "home" || s.Emoji != "zzz" || s.Text != "idle · ~home" || !s.Until.Equal(time.Unix(1900000000, 0)) || !s.Crown {
		t.Fatalf("got %+v", s)
	}
	// no crown field: false
	s, err = DecodeShown([]byte(`{"shown":{"home":"home","emoji":"ear","text":"x","until":1900000000}}`))
	if err != nil || s == nil || s.Crown {
		t.Fatalf("got %+v, %v", s, err)
	}
}

func TestDecodeShownNoneIsNilWithoutError(t *testing.T) {
	for _, body := range []string{`{"v":1,"status":"online"}`, `{"v":1,"shown":null}`} {
		s, err := DecodeShown([]byte(body))
		if s != nil || err != nil {
			t.Fatalf("%s: got %v, %v", body, s, err)
		}
	}
}

func TestDecodeShownOutOfBoundsIsAnError(t *testing.T) {
	for name, shown := range map[string]string{
		"unknown emoji":  `{"home":"home","emoji":"crown","text":"x","until":1900000000}`,
		"no emoji":       `{"home":"home","text":"x","until":1900000000}`,
		"no home":        `{"emoji":"zzz","text":"x","until":1900000000}`,
		"bad home":       `{"home":"Home","emoji":"zzz","text":"x","until":1900000000}`,
		"no text":        `{"home":"home","emoji":"zzz","until":1900000000}`,
		"empty text":     `{"home":"home","emoji":"zzz","text":"  ","until":1900000000}`,
		"no until":       `{"home":"home","emoji":"zzz","text":"x"}`,
		"until too old":  `{"home":"home","emoji":"zzz","text":"x","until":1500000000}`,
		"until too far":  `{"home":"home","emoji":"zzz","text":"x","until":4200000000}`,
		"until fraction": `{"home":"home","emoji":"zzz","text":"x","until":1900000000.5}`,
		"until string":   `{"home":"home","emoji":"zzz","text":"x","until":"1900000000"}`,
		"not an object":  `"zzz"`,
	} {
		s, err := DecodeShown([]byte(`{"shown":` + shown + `}`))
		if s != nil || err == nil {
			t.Errorf("%s: got %v, %v", name, s, err)
		}
	}
	if _, err := DecodeShown([]byte(`not json`)); err == nil {
		t.Error("a body that is no JSON gave no error")
	}
}

func TestCleanText(t *testing.T) {
	if got := CleanText("  a\tb ​ c\x00d  ", 100); got != "a b c d" {
		t.Fatalf("got %q", got)
	}
	if got := CleanText(strings.Repeat("ä", 150), ShownTextMax); len([]rune(got)) != ShownTextMax {
		t.Fatalf("got %d runes", len([]rune(got)))
	}
	if got := CleanText("ab cd", 3); got != "ab" {
		t.Fatalf("got %q", got)
	}
}

func TestShownStore(t *testing.T) {
	var s ShownStore
	t0 := time.Date(2026, 11, 27, 10, 0, 0, 0, time.UTC)
	if s.Get("a") != nil {
		t.Fatal("a status from nothing")
	}
	newer, older := &Shown{Text: "new"}, &Shown{Text: "old"}
	s.Observed("a", newer, t0.Add(time.Minute))
	s.Observed("a", older, t0) // older: ignored
	if got := s.Get("a"); got != newer {
		t.Fatalf("got %v", got)
	}
	s.Observed("a", nil, t0.Add(2*time.Minute)) // a newer one without a status clears it
	if s.Get("a") != nil {
		t.Fatal("a newer presence without a status left one")
	}
	if s.Get("b") != nil {
		t.Fatal("b has a status")
	}
}

func TestShownStoreWarnsOnceUntilAGoodStatus(t *testing.T) {
	var s ShownStore
	t0 := time.Now()
	if !s.Warn("a") || s.Warn("a") {
		t.Fatal("Warn must say true once, then false")
	}
	if !s.Warn("b") {
		t.Fatal("Warn is per name")
	}
	s.Observed("a", nil, t0)
	if s.Warn("a") {
		t.Fatal("a presence without a status re-armed the warning")
	}
	s.Observed("a", &Shown{Text: "x"}, t0.Add(time.Second))
	if !s.Warn("a") {
		t.Fatal("a good status did not re-arm the warning")
	}
}

func TestTheReaderFillsTheShownStore(t *testing.T) {
	nc, js := natsJS(t, jetstream.StreamConfig{Name: "PRESENCE", Subjects: []string{"presence.>"},
		NoAck: true, Storage: jetstream.MemoryStorage})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if _, err := js.CreateOrUpdateConsumer(ctx, "PRESENCE", jetstream.ConsumerConfig{
		Name: ConsumerName, Durable: ConsumerName, FilterSubject: "presence.*.*",
		DeliverSubject: "deliver.bridge.presence", DeliverPolicy: jetstream.DeliverLastPerSubjectPolicy,
		AckPolicy: jetstream.AckNonePolicy,
	}); err != nil {
		t.Fatal(err)
	}
	store := &ShownStore{}
	snap := New()
	r := &Reader{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Deliver: "deliver.bridge.presence",
		Into: snap, Ready: make(chan struct{}), Shown: store}
	done := make(chan error, 1)
	go func() { done <- r.Run(ctx, nc) }()
	select {
	case <-r.Ready:
	case <-time.After(5 * time.Second):
		t.Fatal("the presence reader did not subscribe")
	}
	end := time.Now().Add(600 * time.Second).Unix()
	mustPublish(t, nc, "presence.alice.agent-a",
		`{"v":1,"status":"online","shown":{"home":"home","emoji":"zzz","text":"idle \u00b7 ~home","until":`+strconv.FormatInt(end, 10)+`}}`)
	until(t, func() bool { return store.Get("agent-a") != nil })
	got := store.Get("agent-a")
	if got.Emoji != "zzz" || got.Text != "idle \u00b7 ~home" || got.Until.Unix() != end {
		t.Fatalf("got %+v", got)
	}
	time.Sleep(10 * time.Millisecond)
	mustPublish(t, nc, "presence.alice.agent-a", `{"v":1,"status":"online"}`)
	until(t, func() bool { return store.Get("agent-a") == nil })
	// out of bounds: shows nothing, the presence itself still counts
	mustPublish(t, nc, "presence.alice.agent-b", `{"v":1,"status":"online","shown":{"home":"home","emoji":"x","text":"y","until":1}}`)
	until(t, func() bool { _, ok := snap.Get("agent-b"); return ok })
	if store.Get("agent-b") != nil {
		t.Fatal("an out-of-bounds status was stored")
	}
	cancel()
	<-done
}

// natsJS starts an embedded server with one stream and connects to it.
func natsJS(t *testing.T, cfg jetstream.StreamConfig) (*nats.Conn, jetstream.JetStream) {
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
	if _, err := js.CreateStream(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	return nc, js
}
