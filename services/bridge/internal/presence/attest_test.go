package presence

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

func attestBody(mut map[string]string) string {
	f := map[string]string{
		"v": `1`, "agent": `"coder"`, "home": `"home"`,
		"owner":   `{"anchor":"alice","name":"alice"}`,
		"trusted": `[{"anchor":"bob","name":"bob"}]`,
		"cut":     `false`, "iat": `100`, "exp": `200`,
	}
	for k, v := range mut {
		if v == "" {
			delete(f, k)
		} else {
			f[k] = v
		}
	}
	var parts []string
	for _, k := range []string{"v", "agent", "home", "owner", "trusted", "cut", "iat", "exp", "extra"} {
		if v, ok := f[k]; ok {
			parts = append(parts, fmt.Sprintf("%q:%s", k, v))
		}
	}
	return "{" + strings.Join(parts, ",") + "}"
}

// RT-6.13: the attestation decodes strictly, as the MCP's decode_attestation.
func TestRT613DecodeAttestationIsStrict(t *testing.T) {
	a, err := DecodeAttestation("coder", []byte(attestBody(nil)))
	if err != nil || a.Owner != "alice" || len(a.Trusted) != 1 || a.Trusted[0] != "bob" ||
		a.Home != "home" || !a.HasHome || !a.Exp.Equal(time.Unix(200, 0)) {
		t.Fatalf("%+v %v", a, err)
	}
	if !a.Names("alice") || !a.Names("bob") || a.Names("carol") {
		t.Fatal("Names")
	}
	if a, err := DecodeAttestation("coder", []byte(attestBody(map[string]string{"home": ""}))); err != nil || a.HasHome {
		t.Fatalf("an older login service states no home: %+v %v", a, err)
	}
	if a, err := DecodeAttestation("coder", []byte(attestBody(map[string]string{"home": `""`}))); err != nil || !a.HasHome || a.Home != "" {
		t.Fatalf("an empty home: %+v %v", a, err)
	}
	var many []string
	for i := 0; i < MaxTrusted+1; i++ {
		many = append(many, fmt.Sprintf(`{"anchor":"u%d","name":"u%d"}`, i, i))
	}
	for name, tc := range map[string]struct {
		agent string
		mut   map[string]string
	}{
		"wrong agent":      {"other", nil},
		"extra key":        {"coder", map[string]string{"extra": `1`}},
		"too many trusted": {"coder", map[string]string{"trusted": "[" + strings.Join(many, ",") + "]"}},
		"v 2":              {"coder", map[string]string{"v": `2`}},
		"no v":             {"coder", map[string]string{"v": ""}},
		"bad home":         {"coder", map[string]string{"home": `"Home!"`}},
		"home not string":  {"coder", map[string]string{"home": `1`}},
		"bad owner anchor": {"coder", map[string]string{"owner": `{"anchor":"Alice","name":"alice"}`}},
		"no owner":         {"coder", map[string]string{"owner": `null`}},
		"bad owner name":   {"coder", map[string]string{"owner": `{"anchor":"alice","name":"1a"}`}},
		"bad trusted":      {"coder", map[string]string{"trusted": `[{"anchor":"-x","name":"bob"}]`}},
		"trusted null":     {"coder", map[string]string{"trusted": `null`}},
		"cut not bool":     {"coder", map[string]string{"cut": `1`}},
		"no cut":           {"coder", map[string]string{"cut": ""}},
		"expired":          {"coder", map[string]string{"exp": `100`}},
		"exp before iat":   {"coder", map[string]string{"exp": `50`}},
		"no exp":           {"coder", map[string]string{"exp": ""}},
		"iat string":       {"coder", map[string]string{"iat": `"1"`}},
	} {
		if _, err := DecodeAttestation(tc.agent, []byte(attestBody(tc.mut))); err == nil {
			t.Errorf("%s: decoded", name)
		}
	}
	for _, bad := range []string{``, `[]`, `{}`, attestBody(nil) + ` {}`} {
		if _, err := DecodeAttestation("coder", []byte(bad)); err == nil {
			t.Errorf("%q decoded", bad)
		}
	}
	if ag, ok := AttestAgent("presence.attest.coder.v1"); !ok || ag != "coder" {
		t.Fatal("subject")
	}
	for _, s := range []string{"presence.attest.coder", "presence.attest.coder.v2", "presence.member.a.b", "presence.attest.a.b.v1"} {
		if _, ok := AttestAgent(s); ok {
			t.Errorf("%s", s)
		}
	}
}

func TestRT613AttestationsKeepTheNewestAndExpire(t *testing.T) {
	now := time.Unix(150, 0)
	s := &Attestations{Now: func() time.Time { return now }}
	if _, ok := s.Get("coder"); ok {
		t.Fatal("none yet")
	}
	s.Observed("coder", Attestation{Owner: "alice", Exp: time.Unix(200, 0)}, 5)
	s.Observed("coder", Attestation{Owner: "old", Exp: time.Unix(200, 0)}, 4)
	if a, ok := s.Get("coder"); !ok || a.Owner != "alice" {
		t.Fatalf("older sequence replaced the newer: %+v", a)
	}
	s.Observed("coder", Attestation{Owner: "carol", Exp: time.Unix(200, 0)}, 6)
	if a, _ := s.Get("coder"); a.Owner != "carol" {
		t.Fatal("newer not kept")
	}
	now = time.Unix(201, 0)
	if _, ok := s.Get("coder"); ok {
		t.Fatal("expired")
	}
	now = time.Time{}
	if _, ok := s.Get("coder"); ok {
		t.Fatal("no server time")
	}
	if _, ok := (&Attestations{}).Get("coder"); ok {
		t.Fatal("no clock")
	}
}

// RT-6.13: the bridge reads the attestations through its own consumer made at
// every start, last per subject.
func TestRT613TheBridgeReadsTheAttestationsAfterEveryRestart(t *testing.T) {
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
	far := fmt.Sprint(time.Now().Add(time.Hour).Unix())
	body := func(agent, owner string) string {
		return strings.NewReplacer(`"coder"`, `"`+agent+`"`, `"alice"`, `"`+owner+`"`).Replace(
			attestBody(map[string]string{"iat": `1`, "exp": far}))
	}
	mustPublish(t, nc, "presence.attest.coder.v1", body("coder", "alice"))
	mustPublish(t, nc, "presence.attest.coder.v1", body("coder", "carol")) // the newer
	mustPublish(t, nc, "presence.attest.other.v1", body("coder", "alice")) // another agent's body: dropped
	mustPublish(t, nc, "presence.attest.third.v1", body("third", "alice"))

	start := func() (*Attestations, func()) {
		at := &Attestations{Now: time.Now}
		r := &AttestReader{Log: slog.New(slog.NewTextHandler(io.Discard, nil)), Deliver: "deliver.bridge.attest",
			Stream: "PRESENCE", Into: at, CaughtUp: make(chan struct{}),
			Heartbeat: 100 * time.Millisecond, Inactive: 500 * time.Millisecond}
		cctx, cancel := context.WithCancel(ctx)
		done := make(chan error, 1)
		go func() { done <- r.Run(cctx, nc) }()
		select {
		case <-r.CaughtUp:
		case <-time.After(10 * time.Second):
			t.Fatal("the attestations were not read")
		}
		return at, func() { cancel(); <-done }
	}
	at, stop := start()
	if a, ok := at.Get("coder"); !ok || a.Owner != "carol" {
		t.Fatalf("coder: %+v %v", a, ok)
	}
	if _, ok := at.Get("other"); ok {
		t.Fatal("a body of another agent was kept")
	}
	if _, ok := at.Get("third"); !ok {
		t.Fatal("third")
	}
	mustPublish(t, nc, "presence.attest.late.v1", body("late", "alice"))
	until(t, func() bool { _, ok := at.Get("late"); return ok })
	stop()
	// a restart with no new record: the replay has every agent
	at2, stop2 := start()
	defer stop2()
	if _, ok := at2.Get("late"); !ok {
		t.Fatal("a restart lost an attestation")
	}
	if a, _ := at2.Get("coder"); a.Owner != "carol" {
		t.Fatal("restart replays the newest")
	}
}
