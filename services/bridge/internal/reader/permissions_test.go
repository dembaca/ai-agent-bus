package reader

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/store"
)

// The bridge's NATS user has exactly the rights the example configuration
// documents: its own named consumers, its own KV bucket, and no publish on the
// bus. This test runs the readers with that user against a real server.
func TestTheDocumentedBridgeRightsSufficeAndNoMore(t *testing.T) {
	dir := t.TempDir()
	conf := filepath.Join(dir, "nats.conf")
	// The example configuration's bridge user, with its placeholders filled for
	// the home this test mirrors. A password user stands in for the nkey: the
	// permissions are what is under test.
	publish, subscribe := documentedBridgeRights(t, "home")
	err := os.WriteFile(conf, []byte(fmt.Sprintf(`
port: -1
host: 127.0.0.1
jetstream { store_dir: %q }
accounts {
  BUS { jetstream: enabled, users: [
    { user: admin, password: pw },
    { user: bridge, password: pw, permissions: {
      publish: [ %s ]
      subscribe: [ %s ]
    } }
  ] }
}
`, filepath.Join(dir, "js"), quoted(publish), quoted(subscribe))), 0o600)
	if err != nil {
		t.Fatal(err)
	}
	opts, err := natsserver.ProcessConfigFile(conf)
	if err != nil {
		t.Fatal(err)
	}
	opts.NoLog, opts.NoSigs = true, true
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)

	admin, err := nats.Connect(srv.ClientURL(), nats.UserInfo("admin", "pw"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(admin.Close)
	ajs, _ := jetstream.New(admin)
	ctx := context.Background()
	if _, err := ajs.CreateStream(ctx, jetstream.StreamConfig{Name: "BUS", Subjects: []string{"bus.>"},
		NoAck: true, Storage: jetstream.MemoryStorage}); err != nil {
		t.Fatal(err)
	}
	if _, err := ajs.CreateStream(ctx, jetstream.StreamConfig{Name: "PRESENCE", Subjects: []string{"presence.>"},
		NoAck: true, Storage: jetstream.MemoryStorage}); err != nil {
		t.Fatal(err)
	}

	denied := make(chan error, 4)
	bridgeNC, err := nats.Connect(srv.ClientURL(), nats.UserInfo("bridge", "pw"),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) { denied <- err }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(bridgeNC.Close)
	bjs, err := jetstream.New(bridgeNC)
	if err != nil {
		t.Fatal(err)
	}
	// the bucket is created by the bridge on its first start
	kv, err := store.Open(ctx, bridgeNC, "BUS_BRIDGE")
	if err != nil {
		var all []string
		for {
			select {
			case derr := <-denied:
				all = append(all, derr.Error())
			default:
				t.Fatalf("the bridge may not create its bucket: %v (denied: %v)", err, all)
			}
		}
	}

	s := &sink{got: make(chan [3]string, 4), react: make(chan string, 4)}
	m := &mirror.Mirror{Home: "home", Prefix: "x_", Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Sink: s, Store: kv, Links: mirror.LinksNone{}}
	r := &Reader{Home: "home", Log: m.Log, Mirror: m, Store: kv, Stream: "BUS", Prefix: "bus", PropsPrefix: "x_"}
	cancel, done := startWith(context.Background(), r, bjs)
	defer cancel()
	waitNamedConsumer(t, ajs, "BUS", "bridge_home", denied)
	if err := admin.Publish("bus.ch.home.agent-a", []byte(`{"v":1,"kind":"post","text":"hello"}`)); err != nil {
		t.Fatal(err)
	}
	_ = admin.Flush()
	select {
	case got := <-s.got:
		if !strings.Contains(got[0], "hello") {
			t.Fatalf("mirror: %+v", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("no mirror with the documented rights")
	}
	// the presence consumer is granted on its one name
	snap := presence.New()
	pr := &presence.Reader{Log: m.Log, Stream: "PRESENCE", Into: snap}
	pcancel, pdone := startPresence(context.Background(), pr, bjs)
	defer pcancel()
	waitNamedConsumer(t, ajs, "PRESENCE", presence.ConsumerName, denied)
	if err := admin.Publish("presence.alice.agent-a", []byte(`{"v":1,"status":"online","bot":"b1"}`)); err != nil {
		t.Fatal(err)
	}
	_ = admin.Flush()
	deadline := time.Now().Add(3 * time.Second)
	for {
		if _, ok := snap.Get("agent-a"); ok {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("presence not read with the documented rights")
		}
		time.Sleep(20 * time.Millisecond)
	}
	// and the bus is read-only: a publish there is refused
	if err := bridgeNC.Publish("bus.ch.home.agent-b", []byte("x")); err != nil {
		t.Fatal(err)
	}
	_ = bridgeNC.Flush()
	select {
	case err := <-denied:
		if !strings.Contains(err.Error(), "Permissions Violation") {
			t.Fatalf("not a permission error: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("the bridge published on the bus")
	}
	cancel()
	pcancel()
	<-done
	<-pdone
}

// startWith runs a BUS reader on a given connection.
func startWith(ctx context.Context, r *Reader, js jetstream.JetStream) (context.CancelFunc, chan error) {
	cctx, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() {
		done <- r.Run(cctx, js, func(context.Context) (string, error) { return "chan", nil })
	}()
	return cancel, done
}

func startPresence(ctx context.Context, r *presence.Reader, js jetstream.JetStream) (context.CancelFunc, chan error) {
	cctx, cancel := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- r.Run(cctx, js) }()
	return cancel, done
}

func waitNamedConsumer(t *testing.T, js jetstream.JetStream, stream, name string, denied chan error) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for {
		st, err := js.Stream(context.Background(), stream)
		if err == nil {
			// the name matters: the rights are granted on exactly that consumer
			if info, err := st.Info(context.Background()); err == nil && info.State.Consumers > 0 {
				if _, err := st.Consumer(context.Background(), name); err == nil {
					return
				}
			}
		}
		if time.Now().After(deadline) {
			var all []string
			for {
				select {
				case derr := <-denied:
					all = append(all, derr.Error())
				default:
					t.Fatalf("no consumer %s on %s (denied: %v)", name, stream, all)
				}
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// documentedBridgeRights reads the bridge user's publish and subscribe lists
// from the example server configuration, so this test fails when the code and
// the rights the lab is told to grant drift apart.
func documentedBridgeRights(t *testing.T, home string) (publish, subscribe []string) {
	t.Helper()
	raw, err := os.ReadFile("../../../auth/nats-server.example.conf")
	if err != nil {
		t.Fatalf("the example configuration: %v", err)
	}
	text := string(raw)
	i := strings.Index(text, `nkey: "<BRIDGE_USER_PUBLIC_NKEY>"`)
	if i < 0 {
		t.Fatal("the example configuration has no bridge user")
	}
	block := text[i:]
	if end := strings.Index(block, "\n      }"); end > 0 {
		block = block[:end]
	}
	publish, subscribe = listIn(block, "publish:"), listIn(block, "subscribe:")
	if len(publish) < 5 || len(subscribe) == 0 {
		t.Fatalf("the bridge user's lists: %v / %v", publish, subscribe)
	}
	for _, list := range [][]string{publish, subscribe} {
		for j := range list {
			list[j] = strings.ReplaceAll(list[j], "<HOME>", home)
		}
	}
	// and it stays narrow: no whole-stream or whole-consumer wildcards
	for _, subject := range publish {
		for _, broad := range []string{"CONSUMER.CREATE.BUS.>", "CONSUMER.DELETE.BUS.>", "bus.>", "presence.>"} {
			if strings.Contains(subject, broad) {
				t.Errorf("the bridge's rights are too broad: %s", subject)
			}
		}
	}
	return publish, subscribe
}

// listIn returns the quoted entries of the first [ ... ] after field.
func listIn(block, field string) []string {
	i := strings.Index(block, field)
	if i < 0 {
		return nil
	}
	rest := block[i+len(field):]
	end := strings.Index(rest, "]")
	if end < 0 {
		return nil
	}
	rest = rest[:end]
	var out []string
	for {
		a := strings.Index(rest, `"`)
		if a < 0 {
			return out
		}
		rest = rest[a+1:]
		b := strings.Index(rest, `"`)
		if b < 0 {
			return out
		}
		out = append(out, rest[:b])
		rest = rest[b+1:]
	}
}

// quoted renders a list as one config line of quoted entries.
func quoted(list []string) string {
	parts := make([]string, len(list))
	for i, v := range list {
		parts[i] = `"` + v + `"`
	}
	return strings.Join(parts, ", ")
}
