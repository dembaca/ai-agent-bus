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

// RT-6.45: the STATS history's tests (the archive and the import stream).

func archiveSafe() map[string]any {
	g := maps.Clone(safe(StatsStream))
	g["max_age"], g["max_msgs_per_subject"], g["subjects"] = float64(ArchiveMaxAge), float64(500), []any{"stats-archive.>"}
	g["max_msg_size"] = float64(16384)
	g["sources"] = []any{map[string]any{"name": "STATS", "subject_transforms": []any{
		map[string]any{"src": "stats.*", "dest": "stats-archive.{{wildcard(1)}}"}}}}
	return g
}

func archiveWith(edit func(map[string]any)) map[string]any {
	g := archiveSafe()
	edit(g)
	return g
}

func importSafe() map[string]any {
	g := maps.Clone(safe(StatsStream))
	g["no_ack"], g["discard"], g["discard_new_per_subject"] = false, "new", false
	g["max_age"], g["max_msgs_per_subject"], g["subjects"] = float64(ArchiveMaxAge), float64(500), []any{"stats-import.>"}
	g["max_msg_size"] = float64(16384)
	return g
}

func importWith(edit func(map[string]any)) map[string]any {
	g := importSafe()
	edit(g)
	return g
}

func source(edit func(s map[string]any)) []any {
	s := map[string]any{"name": "STATS", "subject_transforms": []any{
		map[string]any{"src": "stats.*", "dest": "stats-archive.{{wildcard(1)}}"}}}
	edit(s)
	return []any{s}
}

func decode(t *testing.T, s string) map[string]any {
	t.Helper()
	var got map[string]any
	if err := json.Unmarshal([]byte(s), &got); err != nil {
		t.Fatal(err)
	}
	return got
}

// The archive's creation request says what RT-6.45 says: its own subjects,
// one source (STATS onto the archive's subjects), file storage, 365 days,
// 64 MiB, 16 KiB per message, 500 per subject, the oldest discarded, no_ack
// like the other three.
func TestTheArchiveStreamDefinition(t *testing.T) {
	got := decode(t, ArchiveStreamJSON("arch", "st", 0))
	for k, want := range map[string]any{
		"name": "STATS_ARCHIVE", "storage": "file", "retention": "limits", "discard": "old", "no_ack": true,
		"max_age": float64(365 * 24 * time.Hour), "max_bytes": float64(64 << 20), "max_msg_size": float64(16384),
		"max_msgs_per_subject": float64(500), "num_replicas": float64(1), "deny_delete": true, "deny_purge": true,
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
	if got := decode(t, ArchiveStreamJSON("arch", "st", 10<<10)); got["max_bytes"] != float64(10<<10) {
		t.Errorf("max_bytes %v", got["max_bytes"])
	}
}

// The import stream's creation request (RT-6.45): its fixed subjects, no
// source, file storage, 365 days, 64 MiB, 16 KiB per message, 500 per name, a
// full stream refusing new lines (not per subject), acknowledged.
func TestTheImportStreamDefinition(t *testing.T) {
	got := decode(t, ImportStreamJSON(0))
	for k, want := range map[string]any{
		"name": "STATS_IMPORT", "storage": "file", "retention": "limits", "discard": "new", "discard_new_per_subject": false,
		"no_ack": false, "max_age": float64(365 * 24 * time.Hour), "max_bytes": float64(64 << 20), "max_msg_size": float64(16384),
		"max_msgs_per_subject": float64(500), "num_replicas": float64(1), "deny_delete": true, "deny_purge": true,
		"duplicate_window": float64(DuplicateWindow),
	} {
		if got[k] != want {
			t.Errorf("%s = %v, want %v", k, got[k], want)
		}
	}
	if subs, _ := got["subjects"].([]any); len(subs) != 1 || subs[0] != "stats-import.>" {
		t.Errorf("subjects %v", got["subjects"])
	}
	if _, ok := got["sources"]; ok {
		t.Error("the import stream has a source")
	}
	if err := checkImport(got); err != nil {
		t.Errorf("the request fails its own check: %v", err)
	}
}

// The archive's check (RT-6.45): exactly the one STATS source with its
// transform, at most a year, 16 KiB per message, no_ack.
func TestCheckStreamArchive(t *testing.T) {
	for name, c := range map[string]struct {
		got    map[string]any
		unsafe bool
	}{
		"exact":                {archiveSafe(), false},
		"shorter max_age":      {archiveWith(func(g map[string]any) { g["max_age"] = float64(30 * 24 * time.Hour) }), false},
		"empty filter is fine": {archiveWith(func(g map[string]any) { g["sources"] = source(func(s map[string]any) { s["filter_subject"] = "" }) }), false},
		"acknowledges":         {archiveWith(func(g map[string]any) { g["no_ack"] = false }), true},
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
		"8 KiB messages": {archiveWith(func(g map[string]any) { g["max_msg_size"] = float64(8192) }), true},
		"larger lines":   {archiveWith(func(g map[string]any) { g["max_msg_size"] = float64(32768) }), true},
		"501 a subject":  {archiveWith(func(g map[string]any) { g["max_msgs_per_subject"] = float64(501) }), true},
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

// The import stream's check (RT-6.45): no source, discard new but not per
// subject, acknowledged, at most a year, 16 KiB per message, 500 per name.
func TestCheckStreamImport(t *testing.T) {
	for name, c := range map[string]struct {
		got    map[string]any
		unsafe bool
	}{
		"exact":           {importSafe(), false},
		"fewer per name":  {importWith(func(g map[string]any) { g["max_msgs_per_subject"] = float64(100) }), false},
		"no per-subj key": {importWith(func(g map[string]any) { delete(g, "discard_new_per_subject") }), false},
		"discards old":    {importWith(func(g map[string]any) { g["discard"] = "old" }), true},
		"new per subject": {importWith(func(g map[string]any) { g["discard_new_per_subject"] = true }), true},
		"no ack":          {importWith(func(g map[string]any) { g["no_ack"] = true }), true},
		"a source":        {importWith(func(g map[string]any) { g["sources"] = archiveSafe()["sources"] }), true},
		"a mirror":        {importWith(func(g map[string]any) { g["mirror"] = map[string]any{"name": "STATS"} }), true},
		"501 per name":    {importWith(func(g map[string]any) { g["max_msgs_per_subject"] = float64(501) }), true},
		"all per name":    {importWith(func(g map[string]any) { delete(g, "max_msgs_per_subject") }), true},
		"max_age 366 d":   {importWith(func(g map[string]any) { g["max_age"] = float64(366 * 24 * time.Hour) }), true},
		"8 KiB messages":  {importWith(func(g map[string]any) { g["max_msg_size"] = float64(8192) }), true},
		"allow_direct":    {importWith(func(g map[string]any) { g["allow_direct"] = true }), true},
		"deletes":         {importWith(func(g map[string]any) { g["deny_delete"] = false }), true},
		"other subject":   {importWith(func(g map[string]any) { g["subjects"] = []any{"stats-archive.>"} }), true},
		"no size limit":   {importWith(func(g map[string]any) { g["max_bytes"] = float64(-1) }), true},
		"in memory":       {importWith(func(g map[string]any) { g["storage"] = "memory" }), true},
	} {
		t.Run(name, func(t *testing.T) {
			err := checkImport(c.got)
			if c.unsafe != errors.Is(err, ErrUnsafeStream) || (!c.unsafe && err != nil) {
				t.Errorf("unsafe %v, got %v", c.unsafe, err)
			}
		})
	}
	// The other streams keep discard old and refuse discard_new_per_subject.
	if err := checkStream(StatsStream, "stats.>", with(StatsStream, func(g map[string]any) { g["discard_new_per_subject"] = true })); !errors.Is(err, ErrUnsafeStream) {
		t.Errorf("STATS with discard_new_per_subject: %v", err)
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

func waitMsgs(t *testing.T, h *History, n uint64) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		info, err := h.info(tctx(t))
		if err != nil {
			t.Fatal(err)
		}
		if info.State.Msgs == n {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("%d messages, want %d: %+v", info.State.Msgs, n, info)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

// A STATS line reaches the archive on <archive>.<agent>, with the body byte
// for byte (RT-6.45). The archive's time of a sourced line is its own store
// time, not the STATS line's: NATS stores a sourced message anew (it keeps
// the origin only in the Nats-Stream-Source header). The test shows it by
// making the archive a second after the line. A line as large as STATS takes
// is copied too, its source header included, and the copy goes on after it.
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
	type line struct{ name, body string }
	var lines []line
	if err := a.ReadAll(tctx(t), func() { lines = nil }, func(n string, b []byte, _ nats.Header) error {
		lines = append(lines, line{n, string(b)})
		return nil
	}); err != nil || len(lines) != 1 || lines[0] != (line{"agent1", string(body)}) {
		t.Fatalf("ReadAll: %v, %v", lines, err)
	}
	statsMsg, err := m.js.GetMsg(StatsStream, 1)
	if err != nil {
		t.Fatal(err)
	}
	err = a.Each(tctx(t), func(subject string, b []byte, hdr nats.Header) error {
		if subject != "stats-archive.agent1" || !bytes.Equal(b, body) {
			t.Errorf("archived %s %q", subject, b)
		}
		if seq, ok := SourceSeq(hdr); !ok || seq != 1 {
			t.Errorf("origin %q", hdr.Get("Nats-Stream-Source"))
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	am, err := m.js.GetMsg(ArchiveStream, 1)
	if err != nil {
		t.Fatal(err)
	}
	if d := am.Time.Sub(statsMsg.Time); d < time.Second {
		t.Errorf("the archive kept the STATS time (%s apart): update RT-6.45, which says it does not", d)
	}
	t.Logf("STATS time %s, archive time %s, origin %q", statsMsg.Time, am.Time, am.Header.Get("Nats-Stream-Source"))
	// The largest line STATS takes (8192 bytes with its subject), then one more.
	big := append([]byte(`{"sid":"cccccccccccc","part":1,"pad":"`), bytes.Repeat([]byte("x"), 8192-len("stats.agent-long-name")-40)...)
	big = append(big, '"', '}')
	if err := nc.Publish("stats.agent-long-name", big); err != nil {
		t.Fatal(err)
	}
	if err := nc.Publish("stats.agent2", []byte(`{"sid":"bbbbbbbbbbbb","part":2}`)); err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond)
	statsInfo, err := m.js.StreamInfo(StatsStream)
	if err != nil || statsInfo.State.Msgs != 3 {
		t.Fatalf("STATS did not take the large line: %+v %v", statsInfo, err)
	}
	waitMsgs(t, a, 3)
	if info, _ := a.info(tctx(t)); len(info.Sources) != 1 || info.Sources[0].Error != nil {
		t.Fatalf("the source: %+v", info.Sources)
	}
}

// EnsureArchive and EnsureImport make their streams once, find them again,
// and refuse an existing one that is unsafe; a prefix that collides is
// refused (RT-6.45).
func TestEnsureHistoryIsIdempotentAndRefusesUnsafe(t *testing.T) {
	m, _ := lab(t, nil)
	for range 2 {
		if _, err := m.EnsureArchive(tctx(t), "arch", 0); err != nil {
			t.Fatal(err)
		}
		if _, err := m.EnsureImport(tctx(t), 0); err != nil {
			t.Fatal(err)
		}
	}
	// the same stream seen with another prefix: its subjects and transform differ
	if _, err := m.EnsureArchive(tctx(t), "other", 0); !errors.Is(err, ErrUnsafeStream) {
		t.Fatalf("other prefix: %v", err)
	}
	for _, p := range []string{"bus", "presence", "stats", ImportPrefix, DeliverPrefix, "a.b", ">"} {
		if _, err := m.EnsureArchive(tctx(t), p, 0); err == nil || errors.Is(err, ErrUnsafeStream) {
			t.Errorf("prefix %q: %v", p, err)
		}
	}
	clash := &Manager{cfg: Config{Bus: "bus", Presence: "presence", Stats: ImportPrefix}}
	if _, err := clash.EnsureImport(tctx(t), 0); err == nil || errors.Is(err, ErrUnsafeStream) {
		t.Errorf("stats prefix %q: %v", ImportPrefix, err)
	}
	// An archive someone made by hand without the source is refused, and an
	// import stream that discards old.
	m2, _ := lab(t, nil)
	cfg := strings.Replace(ArchiveStreamJSON("arch", "stats", 0), `"sources":[{"name":"STATS","subject_transforms":[{"src":"stats.*","dest":"arch.{{wildcard(1)}}"}]}],`, "", 1)
	if err := m2.api(tctx(t), "$JS.API.STREAM.CREATE."+ArchiveStream, cfg, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := m2.EnsureArchive(tctx(t), "arch", 0); !errors.Is(err, ErrUnsafeStream) || !strings.Contains(err.Error(), "sources") {
		t.Fatalf("archive without its source: %v", err)
	}
	if err := m2.api(tctx(t), "$JS.API.STREAM.CREATE."+ImportStream, strings.Replace(ImportStreamJSON(0), `"discard":"new"`, `"discard":"old"`, 1), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := m2.EnsureImport(tctx(t), 0); !errors.Is(err, ErrUnsafeStream) || !strings.Contains(err.Error(), "discard") {
		t.Fatalf("import stream that discards old: %v", err)
	}
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
// second check while still over none; and the full import stream refuses a
// new line (discard new).
func TestHistoryFill(t *testing.T) {
	rec := &logRecorder{}
	m, _ := lab(t, slog.New(rec))
	h, err := m.EnsureImport(tctx(t), 20<<10)
	if err != nil {
		t.Fatal(err)
	}
	line := bytes.Repeat([]byte("x"), 2000)
	put := func(i int) error {
		return h.Put(tctx(t), "agent-"+string(rune('a'+i)), line, nil)
	}
	for i := range 7 {
		if err := put(i); err != nil {
			t.Fatal(err)
		}
	}
	pct, err := h.Fill(tctx(t))
	if err != nil || pct < 60 || pct >= ArchiveWarnPercent {
		t.Fatalf("fill %d %%, %v", pct, err)
	}
	h.CheckFill(tctx(t))
	if rec.count("full") != 0 {
		t.Fatalf("warned at %d %%: %v", pct, rec.msgs)
	}
	for i := 7; i < 9; i++ {
		if err := put(i); err != nil {
			t.Fatal(err)
		}
	}
	if pct, _ = h.Fill(tctx(t)); pct < ArchiveWarnPercent {
		t.Fatalf("fill %d %%", pct)
	}
	h.CheckFill(tctx(t))
	h.CheckFill(tctx(t))
	if rec.count("WARN stats history over 80 % full") != 1 {
		t.Fatalf("want one warning: %v", rec.msgs)
	}
	var refused error
	for i := 9; i < 12 && refused == nil; i++ {
		refused = put(i)
	}
	if !errors.Is(refused, ErrRefused) {
		t.Fatalf("a full import stream took more: %v", refused)
	}
}

// The archive's copy of STATS is watched (RT-6.45): an error or no source is
// logged at once, a lag only at the second check in a row, each once per
// episode, and its end as a note.
func TestTheCopyOfStatsIsWatched(t *testing.T) {
	rec := &logRecorder{}
	h := &History{m: &Manager{log: slog.New(rec)}, stream: ArchiveStream}
	lag := []sourceInfo{{Name: "STATS", Lag: 3}}
	ok := []sourceInfo{{Name: "STATS"}}
	h.checkSource(lag)
	if rec.count("failing") != 0 {
		t.Fatalf("one lag warned: %v", rec.msgs)
	}
	h.checkSource(lag)
	h.checkSource(lag)
	if rec.count("WARN stats archive: the copy of STATS is failing") != 1 {
		t.Fatalf("want one warning for the lag: %v", rec.msgs)
	}
	h.checkSource(ok)
	if rec.count("INFO stats archive: the copy of STATS is well again") != 1 {
		t.Fatalf("no note: %v", rec.msgs)
	}
	h.checkSource([]sourceInfo{{Name: "STATS", Error: &apiError{Code: 500, Desc: "too big"}}})
	h.checkSource(nil)
	if rec.count("WARN stats archive: the copy of STATS is failing") != 2 {
		t.Fatalf("want a second warning for the error: %v", rec.msgs)
	}
}

// Put refuses a name that is no subject token and an oversized line, takes
// a line with the import's headers, and says when the stream took a line as
// a duplicate (RT-6.45).
func TestImportPut(t *testing.T) {
	m, _ := lab(t, nil)
	h, err := m.EnsureImport(tctx(t), 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"a.b", "*", ">", "", "Agent"} {
		if err := h.Put(tctx(t), bad, []byte("{}"), nil); !errors.Is(err, ErrRefused) {
			t.Errorf("%q: %v", bad, err)
		}
	}
	hdr := map[string]string{"Cb-Source": "mattermost-import", "Nats-Msg-Id": "stats-import:agent-a:aaaaaaaaaaaa:1"}
	if err := h.Put(tctx(t), "agent-a", []byte(`{}`), hdr); err != nil {
		t.Fatal(err)
	}
	if err := h.Put(tctx(t), "agent-a", []byte(`{}`), hdr); !errors.Is(err, ErrDuplicate) {
		t.Errorf("the same message id at once: %v", err)
	}
	if err := h.Put(tctx(t), "agent-a", bytes.Repeat([]byte("x"), 16385), nil); !errors.Is(err, ErrRefused) {
		t.Errorf("an oversized line: %v", err)
	}
}

// A JetStream user without the history's rights (an upgrade that skipped
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
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Second)
	defer cancel()
	if err := m.EnsureStreams(ctx); err != nil {
		t.Fatal(err)
	}
	for stream, ensure := range map[string]func() error{
		ArchiveStream: func() error { _, err := m.EnsureArchive(ctx, "", 0); return err },
		ImportStream:  func() error { _, err := m.EnsureImport(ctx, 0); return err },
	} {
		err := ensure()
		if err == nil || !strings.Contains(err.Error(), "$JS.API.STREAM.INFO."+stream) || !strings.Contains(err.Error(), "$JS.API.STREAM.CREATE."+stream) {
			t.Fatalf("%s: %v", stream, err)
		}
	}
}
