package consumers

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"maps"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// RT-6.45: the archive's tests.

func archiveSafe() map[string]any {
	g := maps.Clone(safe(StatsStream))
	g["no_ack"] = false
	g["max_age"], g["max_msgs_per_subject"], g["subjects"] = float64(ArchiveMaxAge), float64(5000), []any{"stats-archive.>"}
	g["sources"] = []any{map[string]any{"name": "STATS", "subject_transforms": []any{
		map[string]any{"src": "stats.*", "dest": "stats-archive.{{wildcard(1)}}"}}}}
	return g
}

func archiveWith(edit func(map[string]any)) map[string]any {
	g := archiveSafe()
	edit(g)
	return g
}

func source(edit func(s map[string]any)) []any {
	s := map[string]any{"name": "STATS", "subject_transforms": []any{
		map[string]any{"src": "stats.*", "dest": "stats-archive.{{wildcard(1)}}"}}}
	edit(s)
	return []any{s}
}

// The archive's creation request says what RT-6.45 says: its own subjects,
// one source (STATS onto the archive's subjects), file storage, 365 days,
// 64 MiB, 8 KiB per line, 5000 per subject, the oldest discarded,
// acknowledged.
func TestTheArchiveStreamDefinition(t *testing.T) {
	var got map[string]any
	if err := json.Unmarshal([]byte(ArchiveStreamJSON("arch", "st", 0)), &got); err != nil {
		t.Fatal(err)
	}
	for k, want := range map[string]any{
		"name": "STATS_ARCHIVE", "storage": "file", "retention": "limits", "discard": "old", "no_ack": false,
		"max_age": float64(365 * 24 * time.Hour), "max_bytes": float64(64 << 20), "max_msg_size": float64(8192),
		"max_msgs_per_subject": float64(5000), "num_replicas": float64(1), "deny_delete": true, "deny_purge": true,
		"duplicate_window": float64(DuplicateWindow),
	} {
		if got[k] != want {
			t.Errorf("%s = %v, want %v", k, got[k], want)
		}
	}
	if subs, _ := got["subjects"].([]any); len(subs) != 1 || subs[0] != "arch.>" {
		t.Errorf("subjects %v", got["subjects"])
	}
	src, _ := json.Marshal(got["sources"])
	if string(src) != `[{"name":"STATS","subject_transforms":[{"dest":"arch.{{wildcard(1)}}","src":"st.*"}]}]` {
		t.Errorf("sources %s", src)
	}
	for k, v := range got {
		if strings.HasPrefix(k, "allow_") && v != false {
			t.Errorf("%s = %v", k, v)
		}
	}
	if err := json.Unmarshal([]byte(ArchiveStreamJSON("arch", "st", 10<<10)), &got); err != nil || got["max_bytes"] != float64(10<<10) {
		t.Errorf("max_bytes %v, %v", got["max_bytes"], err)
	}
}

// The archive's check (RT-6.45): exactly the one STATS source with its
// transform, at most a year, 8 KiB per line; it may acknowledge.
func TestCheckStreamArchive(t *testing.T) {
	for name, c := range map[string]struct {
		got    map[string]any
		unsafe bool
	}{
		"exact":                {archiveSafe(), false},
		"no_ack on is fine":    {archiveWith(func(g map[string]any) { g["no_ack"] = true }), false},
		"shorter max_age":      {archiveWith(func(g map[string]any) { g["max_age"] = float64(30 * 24 * time.Hour) }), false},
		"empty filter is fine": {archiveWith(func(g map[string]any) { g["sources"] = source(func(s map[string]any) { s["filter_subject"] = "" }) }), false},
		"wrong source":         {archiveWith(func(g map[string]any) { g["sources"] = source(func(s map[string]any) { s["name"] = "BUS" }) }), true},
		"extra source": {archiveWith(func(g map[string]any) {
			g["sources"] = append(source(func(map[string]any) {}), map[string]any{"name": "BUS"})
		}), true},
		"no sources":     {archiveWith(func(g map[string]any) { delete(g, "sources") }), true},
		"empty sources":  {archiveWith(func(g map[string]any) { g["sources"] = []any{} }), true},
		"max_age 366 d":  {archiveWith(func(g map[string]any) { g["max_age"] = float64(366 * 24 * time.Hour) }), true},
		"max_age none":   {archiveWith(func(g map[string]any) { g["max_age"] = float64(0) }), true},
		"allow_direct":   {archiveWith(func(g map[string]any) { g["allow_direct"] = true }), true},
		"allow_rollup":   {archiveWith(func(g map[string]any) { g["allow_rollup_hdrs"] = true }), true},
		"deletes":        {archiveWith(func(g map[string]any) { g["deny_delete"] = false }), true},
		"purges":         {archiveWith(func(g map[string]any) { g["deny_purge"] = false }), true},
		"discards new":   {archiveWith(func(g map[string]any) { g["discard"] = "new" }), true},
		"no size limit":  {archiveWith(func(g map[string]any) { g["max_bytes"] = float64(-1) }), true},
		"larger lines":   {archiveWith(func(g map[string]any) { g["max_msg_size"] = float64(16384) }), true},
		"5001 a subject": {archiveWith(func(g map[string]any) { g["max_msgs_per_subject"] = float64(5001) }), true},
		"in memory":      {archiveWith(func(g map[string]any) { g["storage"] = "memory" }), true},
		"other subject":  {archiveWith(func(g map[string]any) { g["subjects"] = []any{"stats.>"} }), true},
		"is a mirror":    {archiveWith(func(g map[string]any) { g["mirror"] = map[string]any{"name": "STATS"} }), true},
		"republishes":    {archiveWith(func(g map[string]any) { g["republish"] = map[string]any{"src": ">", "dest": "x.>"} }), true},
		"wrong dest": {archiveWith(func(g map[string]any) {
			g["sources"] = source(func(s map[string]any) {
				s["subject_transforms"] = []any{map[string]any{"src": "stats.*", "dest": "bus.{{wildcard(1)}}"}}
			})
		}), true},
		"wrong src": {archiveWith(func(g map[string]any) {
			g["sources"] = source(func(s map[string]any) {
				s["subject_transforms"] = []any{map[string]any{"src": "bus.*", "dest": "stats-archive.{{wildcard(1)}}"}}
			})
		}), true},
		"no transform": {archiveWith(func(g map[string]any) {
			g["sources"] = source(func(s map[string]any) { delete(s, "subject_transforms") })
		}), true},
		"two transforms": {archiveWith(func(g map[string]any) {
			g["sources"] = source(func(s map[string]any) {
				s["subject_transforms"] = append(s["subject_transforms"].([]any), map[string]any{"src": "bus.*", "dest": "stats-archive.x.{{wildcard(1)}}"})
			})
		}), true},
		"a filter": {archiveWith(func(g map[string]any) {
			g["sources"] = source(func(s map[string]any) { s["filter_subject"] = "stats.x" })
		}), true},
		"external": {archiveWith(func(g map[string]any) {
			g["sources"] = source(func(s map[string]any) { s["external"] = map[string]any{"api": "$JS.x.API"} })
		}), true},
		"a start": {archiveWith(func(g map[string]any) {
			g["sources"] = source(func(s map[string]any) { s["opt_start_seq"] = float64(5) })
		}), true},
		"stream xform":  {archiveWith(func(g map[string]any) { g["subject_transform"] = map[string]any{"src": ">", "dest": "x.>"} }), true},
		"wide dup win":  {archiveWith(func(g map[string]any) { g["duplicate_window"] = float64(2 * time.Minute) }), true},
		"interest only": {archiveWith(func(g map[string]any) { g["retention"] = "interest" }), true},
	} {
		t.Run(name, func(t *testing.T) {
			err := checkArchive("stats-archive", "stats", c.got)
			if c.unsafe != errors.Is(err, ErrUnsafeStream) || (!c.unsafe && err != nil) {
				t.Errorf("unsafe %v, got %v", c.unsafe, err)
			}
		})
	}
	// Without the STATS prefix the source cannot be the expected one.
	if err := checkArchive("stats-archive", "", archiveSafe()); !errors.Is(err, ErrUnsafeStream) {
		t.Errorf("no stats prefix: %v", err)
	}
	// The generic check, as the three other streams get it, refuses the
	// archive's source: only checkArchive allows one.
	if err := checkStream(StatsStream, "stats.>", with(StatsStream, func(g map[string]any) { g["sources"] = archiveSafe()["sources"] })); !errors.Is(err, ErrUnsafeStream) {
		t.Errorf("STATS with the archive's source: %v", err)
	}
}

// lab starts an embedded JetStream server and a manager on it with the three
// streams made.
func lab(t *testing.T, log *slog.Logger) (*Manager, *nats.Conn) {
	t.Helper()
	nc := embedded(t)
	m, err := New(nc, Config{Bus: "bus", Presence: "presence", Log: log})
	if err != nil {
		t.Fatal(err)
	}
	if err := m.EnsureStreams(tctx(t)); err != nil {
		t.Fatal(err)
	}
	return m, nc
}

func tctx(t *testing.T) context.Context {
	t.Helper()
	c, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	return c
}

// A STATS line reaches the archive on <archive>.<agent>, with the body byte
// for byte (RT-6.45). The archive's time of a sourced line is its own store
// time, not the STATS line's: NATS stores a sourced message anew (it keeps
// the origin only in the Nats-Stream-Source header). The test shows it by
// making the archive a second after the line.
func TestTheArchiveSourcesStats(t *testing.T) {
	m, nc := lab(t, nil)
	body := []byte(`{"v":"0.103.0","sid":"0123456789ab","part":1}`)
	if err := nc.Publish("stats.agent1", body); err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(1100 * time.Millisecond)
	a, err := m.EnsureArchive(tctx(t), "", 0)
	if err != nil {
		t.Fatal(err)
	}
	var bodies [][]byte
	if err := a.ReadAll(tctx(t), func() { bodies = nil }, func(b []byte) error { bodies = append(bodies, b); return nil }); err != nil || len(bodies) != 1 {
		t.Fatalf("ReadAll: %d lines, %v", len(bodies), err)
	}
	statsMsg, err := m.js.GetMsg(StatsStream, 1)
	if err != nil {
		t.Fatal(err)
	}
	var n int
	err = a.Each(tctx(t), func(subject string, b []byte, hdr nats.Header) error {
		n++
		if subject != "stats-archive.agent1" || !bytes.Equal(b, body) {
			t.Errorf("archived %s %q", subject, b)
		}
		if seq, ok := SourceSeq(hdr); !ok || seq != 1 {
			t.Errorf("origin %q", hdr.Get("Nats-Stream-Source"))
		}
		if hdr.Get("Nats-Stream-Source") == "" {
			t.Errorf("no origin header: %v", hdr)
		}
		return nil
	})
	if err != nil || n != 1 {
		t.Fatalf("%d lines, %v", n, err)
	}
	// the archive's own time: read it through a stream message get
	am, err := m.js.GetMsg(ArchiveStream, 1)
	if err != nil {
		t.Fatal(err)
	}
	if d := am.Time.Sub(statsMsg.Time); d < time.Second {
		t.Errorf("the archive kept the STATS time (%s apart): update RT-6.45, which says it does not", d)
	}
	t.Logf("STATS time %s, archive time %s, origin %q", statsMsg.Time, am.Time, am.Header.Get("Nats-Stream-Source"))
	// A line agents publish after the archive exists is copied too.
	_ = nc.Publish("stats.agent2", []byte(`{"sid":"bbbbbbbbbbbb","part":2}`))
	deadline := time.Now().Add(5 * time.Second)
	for {
		info, err := a.info(tctx(t))
		if err != nil {
			t.Fatal(err)
		}
		if info.State.Msgs == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the second line was not copied: %+v", info.State)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// EnsureArchive makes the stream once, finds it again, and refuses an
// existing one that is unsafe; a prefix that collides is refused (RT-6.45).
func TestEnsureArchiveIsIdempotentAndRefusesUnsafe(t *testing.T) {
	m, _ := lab(t, nil)
	for range 2 {
		if _, err := m.EnsureArchive(tctx(t), "arch", 0); err != nil {
			t.Fatal(err)
		}
	}
	// the same stream seen with another prefix: its subjects and transform differ
	if _, err := m.EnsureArchive(tctx(t), "other", 0); !errors.Is(err, ErrUnsafeStream) {
		t.Fatalf("other prefix: %v", err)
	}
	for _, p := range []string{"bus", "presence", "stats", DeliverPrefix, "a.b", ">"} {
		if _, err := m.EnsureArchive(tctx(t), p, 0); err == nil || errors.Is(err, ErrUnsafeStream) {
			t.Errorf("prefix %q: %v", p, err)
		}
	}
	// An archive someone made by hand without the source is refused.
	m2, nc2 := lab(t, nil)
	cfg := strings.Replace(ArchiveStreamJSON("arch", "stats", 0), `"sources":[{"name":"STATS","subject_transforms":[{"src":"stats.*","dest":"arch.{{wildcard(1)}}"}]}],`, "", 1)
	if err := m2.api(tctx(t), "$JS.API.STREAM.CREATE."+ArchiveStream, cfg, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := m2.EnsureArchive(tctx(t), "arch", 0); !errors.Is(err, ErrUnsafeStream) || !strings.Contains(err.Error(), "sources") {
		t.Fatalf("archive without its source: %v", err)
	}
	_ = nc2
}

// logRecorder keeps the messages of the records at or above Info.
type logRecorder struct {
	msgs []string
}

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

// The fill warning (RT-6.45): under 80 % nothing, past it one warning, a
// second check while still over none.
func TestArchiveFill(t *testing.T) {
	rec := &logRecorder{}
	m, _ := lab(t, slog.New(rec))
	a, err := m.EnsureArchive(tctx(t), "", 10<<10)
	if err != nil {
		t.Fatal(err)
	}
	line := bytes.Repeat([]byte("x"), 1000)
	put := func(i int) {
		t.Helper()
		if err := a.Put(tctx(t), "agent-"+string(rune('a'+i)), line, nil); err != nil {
			t.Fatal(err)
		}
	}
	for i := range 7 {
		put(i)
	}
	pct, err := a.Fill(tctx(t))
	if err != nil || pct < 60 || pct >= ArchiveWarnPercent {
		t.Fatalf("fill %d %%, %v", pct, err)
	}
	a.CheckFill(tctx(t))
	if rec.count("full") != 0 {
		t.Fatalf("warned at %d %%: %v", pct, rec.msgs)
	}
	put(7)
	put(8)
	if pct, _ = a.Fill(tctx(t)); pct < ArchiveWarnPercent {
		t.Fatalf("fill %d %%", pct)
	}
	a.CheckFill(tctx(t))
	a.CheckFill(tctx(t))
	if rec.count("WARN stats archive over 80 % full") != 1 {
		t.Fatalf("want one warning: %v", rec.msgs)
	}
}

// Put refuses a name that is no subject token, and the archive acknowledges
// what it stores (RT-6.45).
func TestArchivePut(t *testing.T) {
	m, _ := lab(t, nil)
	a, err := m.EnsureArchive(tctx(t), "", 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"a.b", "*", ">", "", "Agent"} {
		if err := a.Put(tctx(t), bad, []byte("{}"), nil); !errors.Is(err, ErrArchiveRefused) {
			t.Errorf("%q: %v", bad, err)
		}
	}
	if err := a.Put(tctx(t), "agent-a", []byte(`{}`), map[string]string{"Cb-Source": "mattermost-import"}); err != nil {
		t.Fatal(err)
	}
	if err := a.Put(tctx(t), "agent-a", bytes.Repeat([]byte("x"), 8193), nil); !errors.Is(err, ErrArchiveRefused) {
		t.Errorf("an oversized line: %v", err)
	}
}

// A JetStream user without the archive's rights (an upgrade that skipped
// them) gets an error that names the two rights, not a bare timeout (RT-6.45).
func TestMissingArchiveRightsAreNamed(t *testing.T) {
	var allow []string
	for _, s := range []string{BusStream, PresenceStream, StatsStream} {
		allow = append(allow, "$JS.API.STREAM.CREATE."+s, "$JS.API.STREAM.INFO."+s)
	}
	srv, err := server.NewServer(&server.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
		StoreDir: t.TempDir(), NoLog: true, NoSigs: true,
		Users: []*server.User{{Username: "js", Password: "pwd", Permissions: &server.Permissions{
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
	nc, err := nats.Connect(srv.ClientURL(), nats.UserInfo("js", "pwd"), nats.ErrorHandler(func(*nats.Conn, *nats.Subscription, error) {}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(nc.Close)
	m, err := New(nc, Config{Bus: "bus", Presence: "presence"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := m.EnsureStreams(ctx); err != nil {
		t.Fatal(err)
	}
	_, err = m.EnsureArchive(ctx, "", 0)
	if err == nil || !strings.Contains(err.Error(), "$JS.API.STREAM.INFO.STATS_ARCHIVE") || !strings.Contains(err.Error(), "$JS.API.STREAM.CREATE.STATS_ARCHIVE") {
		t.Fatalf("EnsureArchive: %v", err)
	}
}
