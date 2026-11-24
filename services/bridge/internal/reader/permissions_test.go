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

	denied := make(chan error, 16)
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
	// The three ways a broad right would let the bridge read what it must not
	// (164534's cross-model probes): a consumer of its own name with a DM
	// filter, the short create form with a body that names one, and a bucket
	// whose source is a DM filter.
	if err := probe(ctx, func(c context.Context) error {
		_, err := bjs.CreateStream(c, jetstream.StreamConfig{Name: "KV_BUS_BRIDGE",
			Subjects: []string{"$KV.BUS_BRIDGE.>"},
			Sources:  []*jetstream.StreamSource{{Name: "BUS", FilterSubject: "bus.dm.>"}}})
		return err
	}); err == nil {
		t.Fatal("the bridge created a stream that reads the DMs")
	}
	if err := probe(ctx, func(c context.Context) error {
		_, err := bjs.CreateOrUpdateConsumer(c, "BUS", jetstream.ConsumerConfig{
			Name: "evil", FilterSubject: "bus.dm.>", AckPolicy: jetstream.AckNonePolicy})
		return err
	}); err == nil {
		t.Fatal("the bridge created a consumer over the DMs")
	}
	if err := bridgeNC.Publish("$JS.API.CONSUMER.CREATE.BUS.bridge_home",
		[]byte(`{"stream_name":"BUS","config":{"name":"bridge_home","filter_subject":"bus.dm.>"}}`)); err != nil {
		t.Fatal(err)
	}
	_ = bridgeNC.Flush()
	deniedOn(t, denied, "$JS.API.CONSUMER.CREATE.BUS.bridge_home")

	// The operator makes the bucket (direct get off); the bridge only binds.
	if err := store.CreateBucket(ctx, ajs); err != nil {
		t.Fatal(err)
	}
	kv, err := store.Open(ctx, bridgeNC, "BUS_BRIDGE")
	if err != nil {
		t.Fatalf("the bridge may not bind to its bucket: %v", err)
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
	deniedOn(t, denied, "bus.ch.home.agent-b")
	// The reply subject of a request is not checked against publish
	// permissions (the server refuses only replies to $JS. since 2.15), so the
	// bridge's answers can land on any subject. With direct get off, the answer
	// carries wrapped JSON, not a key's value: noise, never a post or an
	// attestation (164534's cross-model probe).
	if err := bridgeNC.Publish("$KV.BUS_BRIDGE.attack1", []byte(`{"v":1,"kind":"post","text":"forged"}`)); err != nil {
		t.Fatal(err)
	}
	if err := bridgeNC.PublishMsg(&nats.Msg{Subject: "$JS.API.STREAM.MSG.GET.KV_BUS_BRIDGE",
		Reply: "presence.attest.victim.v1",
		Data:  []byte(`{"last_by_subj":"$KV.BUS_BRIDGE.attack1"}`)}); err != nil {
		t.Fatal(err)
	}
	if err := bridgeNC.PublishMsg(&nats.Msg{Subject: "$KV.BUS_BRIDGE.attack2",
		Reply: "presence.attest.victim2.v1",
		Data:  []byte(`{"v":1,"kind":"post","text":"forged"}`)}); err != nil {
		t.Fatal(err)
	}
	_ = bridgeNC.Flush()
	for _, subject := range []string{"presence.attest.victim.v1", "presence.attest.victim2.v1"} {
		body := storedOn(t, ajs, "PRESENCE", subject)
		if _, _, err := presence.Decode(body); err == nil {
			t.Fatalf("%s stored a valid presence: %s", subject, body)
		}
	}
	// and a direct get is not granted at all
	if err := bridgeNC.Publish("$JS.API.DIRECT.GET.KV_BUS_BRIDGE.$KV.BUS_BRIDGE.attack1", nil); err != nil {
		t.Fatal(err)
	}
	_ = bridgeNC.Flush()
	deniedOn(t, denied, "DIRECT.GET")
	cancel()
	pcancel()
	<-done
	<-pdone
}

// storedOn waits until the stream holds a message on subject and returns it.
func storedOn(t *testing.T, js jetstream.JetStream, stream, subject string) []byte {
	t.Helper()
	st, err := js.Stream(context.Background(), stream)
	if err != nil {
		t.Fatalf("stream %s: %v", stream, err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		info, err := st.Info(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		for seq := info.State.FirstSeq; seq <= info.State.LastSeq; seq++ {
			msg, err := st.GetMsg(context.Background(), seq)
			if err != nil || msg.Subject != subject {
				continue
			}
			return msg.Data
		}
		if time.Now().After(deadline) {
			t.Fatalf("nothing on %s", subject)
		}
		time.Sleep(50 * time.Millisecond)
	}
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

// probe runs one request that must not reach the server (its right is absent),
// on a context of its own so the timeout of one probe cannot cancel the next.
func probe(ctx context.Context, f func(context.Context) error) error {
	c, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	return f(c)
}

// deniedOn waits for a refusal naming want, skipping refusals of earlier probes.
func deniedOn(t *testing.T, denied chan error, want string) {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case err := <-denied:
			if strings.Contains(err.Error(), want) {
				return
			}
		case <-deadline:
			t.Fatalf("no refusal naming %s", want)
		}
	}
}
