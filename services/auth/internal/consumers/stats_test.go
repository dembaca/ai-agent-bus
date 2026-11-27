package consumers

import (
	"bytes"
	"context"
	"errors"
	"log/slog"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// RT-6.45: the STATS history's tests (the stream kept a year, updated in
// place, its fill, the import's reading and publishing).

func tctx(t *testing.T) context.Context {
	t.Helper()
	c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	return c
}

// logRecorder keeps the level and message of every record.
type logRecorder struct{ msgs []string }

func (r *logRecorder) Enabled(context.Context, slog.Level) bool { return true }
func (r *logRecorder) Handle(_ context.Context, rec slog.Record) error {
	r.msgs = append(r.msgs, rec.Level.String()+" "+rec.Message)
	return nil
}
func (r *logRecorder) WithAttrs([]slog.Attr) slog.Handler { return r }
func (r *logRecorder) WithGroup(string) slog.Handler      { return r }

func (r *logRecorder) count(msg string) int {
	n := 0
	for _, m := range r.msgs {
		if strings.Contains(m, msg) {
			n++
		}
	}
	return n
}

// statsLab is an embedded JetStream server with two users: admin may do
// anything, js has the service's documented rights except the update.
func statsLab(t *testing.T) (admin, js *nats.Conn) {
	t.Helper()
	var allow []string
	for _, s := range []string{BusStream, PresenceStream, StatsStream} {
		allow = append(allow, "$JS.API.STREAM.CREATE."+s, "$JS.API.STREAM.INFO."+s, "$JS.API.CONSUMER.*."+s, "$JS.API.CONSUMER.*."+s+".>")
	}
	allow = append(allow, "stats.import.*") // the import's right (RT-6.45)
	srv, err := server.NewServer(&server.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
		StoreDir: t.TempDir(), NoLog: true, NoSigs: true,
		Users: []*server.User{{Username: "admin", Password: "pwd"}, {Username: "js", Password: "pwd", Permissions: &server.Permissions{
			Publish:   &server.SubjectPermission{Allow: allow},
			Subscribe: &server.SubjectPermission{Allow: []string{"_INBOX.>"}},
		}}}})
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)
	conn := func(user string) *nats.Conn {
		nc, err := nats.Connect(srv.ClientURL(), nats.UserInfo(user, "pwd"), nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {}))
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(nc.Close)
		return nc
	}
	return conn("admin"), conn("js")
}

func manager(t *testing.T, nc *nats.Conn, log *slog.Logger, maxBytes int64) *Manager {
	t.Helper()
	m, err := New(nc, Config{Bus: "bus", Presence: "presence", Log: log, StatsMaxBytes: maxBytes})
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func statsConfig(t *testing.T, m *Manager) map[string]any {
	t.Helper()
	var info struct {
		Config map[string]any `json:"config"`
	}
	if err := m.api(tctx(t), "$JS.API.STREAM.INFO."+StatsStream, nil, &info); err != nil {
		t.Fatal(err)
	}
	return info.Config
}

// An existing STATS made by an older service (30 days, 100 per subject) is
// brought to a year and 500 per subject in place, its lines kept; a second
// start changes nothing; a larger max_bytes is kept, a smaller one raised.
func TestAnOldStatsStreamIsUpdatedInPlace(t *testing.T) {
	admin, _ := statsLab(t)
	rec := &logRecorder{}
	m := manager(t, admin, slog.New(rec), 0)
	old := strings.Replace(StatsStreamJSON("stats", 32<<20), `"max_age":`+itoa(int64(StatsMaxAge)), `"max_age":`+itoa(int64(30*24*time.Hour)), 1)
	old = strings.Replace(old, `"max_msgs_per_subject":500`, `"max_msgs_per_subject":100`, 1)
	if err := m.api(tctx(t), "$JS.API.STREAM.CREATE."+StatsStream, old, nil); err != nil {
		t.Fatal(err)
	}
	if err := admin.Publish("stats.agent-a", []byte(`{"sid":"aaaaaaaaaaaa","part":1}`)); err != nil {
		t.Fatal(err)
	}
	if err := m.EnsureStreams(tctx(t)); err != nil {
		t.Fatalf("the old stream is safe: %v", err)
	}
	if got := statsConfig(t, m); got["max_age"] != float64(30*24*time.Hour) {
		t.Fatalf("the old stream is not old: %v", got["max_age"])
	}
	for range 2 {
		if err := m.Stats().Update(tctx(t)); err != nil {
			t.Fatal(err)
		}
	}
	got := statsConfig(t, m)
	if got["max_age"] != float64(365*24*time.Hour) || got["max_msgs_per_subject"] != float64(500) || got["max_bytes"] != float64(64<<20) {
		t.Fatalf("not updated: %v %v %v", got["max_age"], got["max_msgs_per_subject"], got["max_bytes"])
	}
	if err := checkStream(StatsStream, "stats.>", got); err != nil {
		t.Fatal(err)
	}
	if rec.count("stream limits updated") != 1 {
		t.Fatalf("want one update: %v", rec.msgs)
	}
	n := 0
	if err := m.Stats().ReadAll(tctx(t), func() {}, func(string, []byte, nats.Header) error { n++; return nil }); err != nil || n != 1 {
		t.Fatalf("the line: %d, %v", n, err)
	}
	// An operator's larger max_bytes stays.
	big := statsConfig(t, m)
	big["max_bytes"] = float64(128 << 20)
	if err := m.api(tctx(t), "$JS.API.STREAM.UPDATE."+StatsStream, big, nil); err != nil {
		t.Fatal(err)
	}
	if err := m.Stats().Update(tctx(t)); err != nil || statsConfig(t, m)["max_bytes"] != float64(128<<20) {
		t.Fatalf("the larger size: %v, %v", statsConfig(t, m)["max_bytes"], err)
	}
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }

// An unsafe STATS is refused and not updated; a JetStream user without the
// update right gets an error that names it.
func TestTheStatsUpdateRefusesUnsafeAndNamesItsRight(t *testing.T) {
	admin, js := statsLab(t)
	m := manager(t, admin, nil, 0)
	if err := m.EnsureStreams(tctx(t)); err != nil {
		t.Fatal(err)
	}
	cfg := statsConfig(t, m)
	cfg["max_age"] = float64(30 * 24 * time.Hour)
	if err := m.api(tctx(t), "$JS.API.STREAM.UPDATE."+StatsStream, cfg, nil); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	err := manager(t, js, nil, 0).Stats().Update(ctx)
	if err == nil || !strings.Contains(err.Error(), "$JS.API.STREAM.UPDATE.STATS") {
		t.Fatalf("without the right: %v", err)
	}
	cfg = statsConfig(t, m)
	cfg["allow_direct"] = true
	if err := m.api(tctx(t), "$JS.API.STREAM.UPDATE."+StatsStream, cfg, nil); err != nil {
		t.Fatal(err)
	}
	if err := m.Stats().Update(tctx(t)); !errors.Is(err, ErrUnsafeStream) {
		t.Fatalf("unsafe: %v", err)
	}
	if statsConfig(t, m)["max_age"] != float64(30*24*time.Hour) {
		t.Fatal("an unsafe stream was updated")
	}
}

// The fill warning (RT-6.45): under 80 % nothing, past it one warning, a
// second check while still over none.
func TestTheStatsFillIsWatched(t *testing.T) {
	rec := &logRecorder{}
	admin, _ := statsLab(t)
	m := manager(t, admin, slog.New(rec), 20<<10)
	if err := m.EnsureStreams(tctx(t)); err != nil {
		t.Fatal(err)
	}
	s := m.Stats()
	line := bytes.Repeat([]byte("x"), 2000)
	put := func(from, to int) {
		t.Helper()
		for i := from; i < to; i++ {
			if err := s.Put(tctx(t), "bot-"+string(rune('a'+i)), line, nil); err != nil {
				t.Fatal(err)
			}
		}
	}
	fill := func() int {
		t.Helper()
		pct, err := s.Fill(tctx(t))
		if err != nil {
			t.Fatal(err)
		}
		return pct
	}
	put(0, 7)
	if pct := fill(); pct < 60 || pct >= StatsWarnPercent {
		t.Fatalf("fill %d %%", pct)
	}
	s.CheckFill(tctx(t))
	if rec.count("full") != 0 {
		t.Fatalf("warned: %v", rec.msgs)
	}
	put(7, 9)
	if pct := fill(); pct < StatsWarnPercent {
		t.Fatalf("fill %d %%", pct)
	}
	s.CheckFill(tctx(t))
	s.CheckFill(tctx(t))
	if rec.count("WARN stats stream over 80 % full") != 1 {
		t.Fatalf("want one warning: %v", rec.msgs)
	}
}

// Put publishes on <stats>.import.<bot> with its headers, refuses a name that
// is no bot's username, and ReadAll gives the name, body and headers back.
func TestTheImportPublishesUnderItsOwnToken(t *testing.T) {
	admin, js := statsLab(t)
	if err := manager(t, admin, nil, 0).EnsureStreams(tctx(t)); err != nil {
		t.Fatal(err)
	}
	s := manager(t, js, nil, 0).Stats()
	for _, bad := range []string{"a.b", "*", ">", "", "Agent"} {
		if err := s.Put(tctx(t), bad, []byte("{}"), nil); !errors.Is(err, ErrNoBot) {
			t.Errorf("%q: %v", bad, err)
		}
	}
	if err := s.Put(tctx(t), "agent-mac-1", []byte(`{"v":"0.85.0"}`), map[string]string{"Cb-Post-Id": "p1"}); err != nil {
		t.Fatal(err)
	}
	type line struct{ name, body, post string }
	var got []line
	deadline := time.Now().Add(5 * time.Second)
	for len(got) == 0 && time.Now().Before(deadline) {
		err := s.ReadAll(tctx(t), func() { got = nil }, func(n string, b []byte, h nats.Header) error {
			got = append(got, line{n, string(b), h.Get("Cb-Post-Id")})
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	if len(got) != 1 || got[0] != (line{"import.agent-mac-1", `{"v":"0.85.0"}`, "p1"}) {
		t.Fatalf("%v", got)
	}
}

// A shorter age or cap later (or a rollback to one) brings an existing
// stream down at the next start: checkStream lets a longer one up to the
// ceilings pass, so the update can run; beyond them, or unlimited, it is
// refused (RT-6.45).
func TestTheStatsLimitsGoDownToo(t *testing.T) {
	if StatsMaxAge > statsAgeCeiling || StatsMaxMsgsPerSubject > statsPerSubjectCeiling {
		t.Fatal("the current limits pass the ceilings")
	}
	admin, _ := statsLab(t)
	m := manager(t, admin, nil, 0)
	if err := m.EnsureStreams(tctx(t)); err != nil {
		t.Fatal(err)
	}
	if err := admin.Publish("stats.agent-a", []byte(`{"sid":"aaaaaaaaaaaa","part":1}`)); err != nil {
		t.Fatal(err)
	}
	short := m.Stats()
	short.age, short.perSubject = 30*24*time.Hour, 100
	for range 2 {
		if err := short.Update(tctx(t)); err != nil {
			t.Fatal(err)
		}
	}
	if got := statsConfig(t, m); got["max_age"] != float64(30*24*time.Hour) || got["max_msgs_per_subject"] != float64(100) {
		t.Fatalf("not brought down: %v %v", got["max_age"], got["max_msgs_per_subject"])
	}
	// and up again
	if err := m.Stats().Update(tctx(t)); err != nil || statsConfig(t, m)["max_age"] != float64(StatsMaxAge) {
		t.Fatalf("up again: %v, %v", statsConfig(t, m)["max_age"], err)
	}
	// A configured age beyond the ceiling never reaches the stream.
	long := m.Stats()
	long.age = statsAgeCeiling + time.Hour
	if err := long.Update(tctx(t)); err == nil {
		t.Fatal("an age beyond the ceiling")
	}
	// An existing stream beyond the ceiling, or without an age, is refused.
	for _, age := range []time.Duration{statsAgeCeiling + 24*time.Hour, 0} {
		cfg := statsConfig(t, m)
		cfg["max_age"] = float64(age)
		if err := m.api(tctx(t), "$JS.API.STREAM.UPDATE."+StatsStream, cfg, nil); err != nil {
			t.Fatal(err)
		}
		if err := m.Stats().Update(tctx(t)); !errors.Is(err, ErrUnsafeStream) {
			t.Fatalf("age %s: %v", age, err)
		}
	}
}

// Room is what STATS takes before it drops its oldest lines.
func TestTheStatsRoom(t *testing.T) {
	admin, _ := statsLab(t)
	m := manager(t, admin, nil, 20<<10)
	if err := m.EnsureStreams(tctx(t)); err != nil {
		t.Fatal(err)
	}
	s := m.Stats()
	before, err := s.Room(tctx(t))
	if err != nil || before != 20<<10 {
		t.Fatalf("empty: %d, %v", before, err)
	}
	if err := s.Put(tctx(t), "bot-a", bytes.Repeat([]byte("x"), 1000), nil); err != nil {
		t.Fatal(err)
	}
	if after, err := s.Room(tctx(t)); err != nil || after > before-1000 {
		t.Fatalf("after a line: %d, %v", after, err)
	}
}
