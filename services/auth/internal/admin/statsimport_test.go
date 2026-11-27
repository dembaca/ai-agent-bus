package admin

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/mattermost"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
)

// RT-6.45: the STATS import's tests.

// statsChat is the stats channel and its authors.
type statsChat struct {
	posts []mattermost.Post
	users map[string]mattermost.User
	given int
}

// EachChannelPost gives the posts newest first, as Mattermost does; given
// counts the posts handed out.
func (s *statsChat) EachChannelPost(_ context.Context, ch string, _ time.Time, fn func(mattermost.Post) error) error {
	if ch != "agents-stats" {
		return errors.New("no such channel")
	}
	ps := append([]mattermost.Post(nil), s.posts...)
	sort.SliceStable(ps, func(i, j int) bool { return ps[i].Created.After(ps[j].Created) })
	for _, p := range ps {
		s.given++
		if err := fn(p); err != nil {
			return err
		}
	}
	return nil
}

func (s *statsChat) Users(_ context.Context, ids []string) ([]mattermost.User, error) {
	var out []mattermost.User
	for _, id := range ids {
		if u, ok := s.users[id]; ok {
			out = append(out, u)
		}
	}
	return out, nil
}

// history is the lab's two streams of the STATS history.
type history struct {
	archive, imports *consumers.History
	nc               *nats.Conn
}

// historyLab starts a JetStream server with the bus's streams, the archive
// and the import stream.
func historyLab(t *testing.T) history {
	t.Helper()
	srv, err := server.NewServer(&server.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
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
	m, err := consumers.New(nc, consumers.Config{Bus: "bus", Presence: "presence"})
	if err != nil {
		t.Fatal(err)
	}
	if err := m.EnsureStreams(ctx(t)); err != nil {
		t.Fatal(err)
	}
	a, err := m.EnsureArchive(ctx(t), "", 0)
	if err != nil {
		t.Fatal(err)
	}
	i, err := m.EnsureImport(ctx(t), 0)
	if err != nil {
		t.Fatal(err)
	}
	return history{a, i, nc}
}

type stored struct {
	subject string
	body    string
	hdr     nats.Header
}

func lines(t *testing.T, h *consumers.History) []stored {
	t.Helper()
	var out []stored
	if err := h.Each(ctx(t), func(subject string, body []byte, hdr nats.Header) error {
		out = append(out, stored{subject, string(body), hdr})
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	return out
}

var t0 = time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)

func post(id, user string, minute int, msg string) mattermost.Post {
	return mattermost.Post{ID: id, UserID: user, Created: t0.Add(time.Duration(minute) * time.Minute), Message: msg}
}

func bot(id, name string) mattermost.User {
	return mattermost.User{ID: id, Username: name, IsBot: true}
}

func statsSetup(t *testing.T, chat *statsChat, archive HistoryReader, store ImportStore, edit ...func(*StatsImport)) *env {
	t.Helper()
	si := &StatsImport{Channel: "agents-stats", Source: chat, Archive: archive, Store: store, CatchUp: 5 * time.Second}
	for _, e := range edit {
		e(si)
	}
	return setupWith(t, Config{Burst: 100, Stats: si},
		testrealm.NewChat("alice", "bob", "carol", "mal"), &fakeMM{})
}

func runStats(t *testing.T, e *env, tok string, code int) StatsReport {
	t.Helper()
	got, body, _ := e.do(t, "POST", "/v1/stats/import", tok, "")
	want(t, "stats import", got, code, body)
	var rep StatsReport
	if code == 200 {
		if err := json.Unmarshal([]byte(body), &rep); err != nil {
			t.Fatal(err)
		}
	}
	return rep
}

// The import writes each valid line into the import stream on
// stats-import.<bot>, byte for byte, marked as imported with its original
// time, and never into the archive; skips a line the archive holds (here one
// JetStream copied from STATS under the agent's NATS name), one the run
// already wrote under that name, and a post younger than two minutes; counts
// each bad line under its reason; and a second run imports nothing.
func TestStatsImportMarksDedupesAndDrops(t *testing.T) {
	h := historyLab(t)
	if err := h.nc.Publish("stats.agent-nats-name", []byte(`{"sid":"aaaaaaaaaaaa","part":1}`)); err != nil {
		t.Fatal(err)
	}
	keep := `{"v":"0.80.0","sid":"bbbbbbbbbbbb","part":2, "x":{"odd":[1,2]}}`
	older := `{"sid":"cccccccccccc","part":1,"final":true}`
	other := `{"sid":"bbbbbbbbbbbb","part":2}`
	oldFormat := `{"v":"0.85.0","turns":3,"bus":{"posts":2}}`
	chat := &statsChat{users: map[string]mattermost.User{
		"ua": bot("ua", "agent-a"), "ub": bot("ub", "agent-b"), "uc": bot("uc", "agent-c"),
		"ud": bot("ud", "dotted.name"), "uh": {ID: "uh", Username: "human"},
	}, posts: []mattermost.Post{
		post("p1", "ua", 5, `STATS {"sid":"aaaaaaaaaaaa","part":1,"v":"0.90.0"}`), // copied from STATS
		post("p2", "ua", 6, "STATS "+keep),
		post("p3", "ua", 7, `STATS {"sid":"bbbbbbbbbbbb","part":2}`), // the run wrote it already, same name
		post("p3b", "ub", 7, "STATS "+other),                         // another name: its own line
		post("p4", "ua", 8, "hello"),
		post("p5", "ua", 9, `STATS {"sid":"XYZ","part":1}`),
		post("p6", "ua", 10, `STATS {nope`),
		post("p7", "ua", 11, `STATS {"sid":"dddddddddddd","part":1.5}`),
		post("p8", "ua", 12, `STATS {"sid":"eeeeeeeeeeee","part":1,"pad":"`+strings.Repeat("x", 8001-40)+`"}`),
		post("p9", "ud", 13, `STATS {"sid":"ffffffffffff","part":1}`),
		post("p10", "uh", 14, `STATS {"sid":"ffffffffffff","part":2}`),
		post("p11", "uc", 1, "STATS "+older),
		post("p12", "ua", 15, `STATS ["sid","aaaaaaaaaaab"]`),
		post("p13", "ua", 16, `STATS {"sid":"aaaaaaaaaaab"}`),
		post("p15", "ua", 17, "STATS "+oldFormat), // from before 0.91.0: no sid, no part
		post("p16", "ua", 18, "STATS "+oldFormat), // the same content, another post
		{ID: "p14", UserID: "ua", Created: time.Now().Add(-time.Minute), Message: `STATS {"sid":"aaaaaaaaaaac","part":1}`},
	}}
	if n := len(chat.posts[8].Message) - len("STATS "); n != 8001 {
		t.Fatalf("the long line is %d bytes", n)
	}
	e := statsSetup(t, chat, h.archive, h.imports)
	rep := runStats(t, e, e.admin, 200)
	wantRep := StatsReport{Read: 17, Imported: 5, ImportedWithoutSid: 2, NotStats: 1, SkippedDuplicate: 2, SkippedInvalid: 5, SkippedTooBig: 1, SkippedName: 2,
		SkippedRecent: 1, Oldest: t0.Add(time.Minute).Format(time.RFC3339), Newest: t0.Add(18 * time.Minute).Format(time.RFC3339)}
	if rep != wantRep {
		t.Fatalf("report\n%+v, want\n%+v", rep, wantRep)
	}
	if got := lines(t, h.archive); len(got) != 1 || got[0].subject != "stats-archive.agent-nats-name" {
		t.Fatalf("the archive holds more than the copy: %+v", got)
	}
	got := lines(t, h.imports)
	if len(got) != 5 {
		t.Fatalf("%d lines in the import stream: %+v", len(got), got)
	}
	for i, id := range []string{"p15", "p16"} {
		if l := got[3+i]; l.subject != "stats-import.agent-a" || l.body != oldFormat || l.hdr.Get("Cb-Post-Id") != id ||
			l.hdr.Get("Nats-Msg-Id") != "stats-import:post:"+id {
			t.Errorf("old line %s: %+v", id, l)
		}
	}
	for i, w := range []struct {
		subject, body string
		at            time.Time
	}{
		{"stats-import.agent-c", older, t0.Add(time.Minute)}, // oldest post first
		{"stats-import.agent-a", keep, t0.Add(6 * time.Minute)},
		{"stats-import.agent-b", other, t0.Add(7 * time.Minute)},
	} {
		l := got[i]
		if l.subject != w.subject || l.body != w.body {
			t.Errorf("line %d: %s %q, want %s %q", i, l.subject, l.body, w.subject, w.body)
		}
		if l.hdr.Get("Cb-Source") != "mattermost-import" || l.hdr.Get("Cb-Orig-Time") != w.at.Format(time.RFC3339Nano) {
			t.Errorf("line %d headers: %v", i, l.hdr)
		}
	}
	again := runStats(t, e, e.admin, 200)
	if again.Imported != 0 || again.SkippedDuplicate != 7 || again.Oldest != "" {
		t.Fatalf("second run: %+v", again)
	}
	if n := len(lines(t, h.imports)); n != 5 {
		t.Fatalf("%d lines after the second run", n)
	}
}

// Above the cap of 500 per name only the newest 500 are imported, so a
// second run stores nothing and pushes nothing out, and a newer post later
// takes the place of the oldest; with lines of both formats mixed.
func TestTheStatsImportKeepsTheNewestPerName(t *testing.T) {
	h := historyLab(t)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	line := func(i int) string {
		if i%2 == 1 {
			return fmt.Sprintf(`{"v":"0.85.0","n":%d}`, i) // before 0.91.0
		}
		return fmt.Sprintf(`{"sid":"%012x","part":1}`, i)
	}
	for i := range 502 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, "STATS "+line(i)))
	}
	e := statsSetup(t, chat, h.archive, h.imports)
	if rep := runStats(t, e, e.admin, 200); rep.Imported != 500 || rep.ImportedWithoutSid != 250 || rep.SkippedOverCap != 2 || rep.Oldest != t0.Add(2*time.Minute).Format(time.RFC3339) {
		t.Fatalf("first run: %+v", rep)
	}
	if rep := runStats(t, e, e.admin, 200); rep.Imported != 0 || rep.SkippedOverCap != 2 || rep.SkippedDuplicate != 500 {
		t.Fatalf("second run: %+v", rep)
	}
	chat.posts = append(chat.posts, post("p502", "ua", 502, "STATS "+line(502)))
	if rep := runStats(t, e, e.admin, 200); rep.Imported != 1 || rep.SkippedOverCap != 3 {
		t.Fatalf("a newer post: %+v", rep)
	}
	got := lines(t, h.imports)
	if len(got) != 500 || got[0].body != line(3) {
		t.Fatalf("%d lines, the oldest %q", len(got), got[0].body)
	}
	if rep := runStats(t, e, e.admin, 200); rep.Imported != 0 {
		t.Fatalf("after the newer post: %+v", rep)
	}
}

// failing stops after n stored lines, as a run that is cut off; dup answers
// the first put as a duplicate.
type failing struct {
	ImportStore
	n   int
	dup bool
}

func (f *failing) Put(ctx context.Context, name string, body []byte, hdr map[string]string) error {
	if f.dup {
		f.dup = false
		return consumers.ErrDuplicate
	}
	if f.n == 0 {
		return errors.New("cut off")
	}
	f.n--
	return f.ImportStore.Put(ctx, name, body, hdr)
}

// A run that stops part way answers 502 and keeps what it wrote; the next run
// completes it without a duplicate; a line the stream acknowledges as a
// duplicate is counted as one.
func TestAnInterruptedStatsImportIsCompletedByTheNext(t *testing.T) {
	h := historyLab(t)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	for i := range 5 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, fmt.Sprintf(`STATS {"sid":"%012x","part":%d}`, i, i+1)))
	}
	cut := statsSetup(t, chat, h.archive, &failing{ImportStore: h.imports, n: 2})
	runStats(t, cut, cut.admin, 502)
	if n := len(lines(t, h.imports)); n != 2 {
		t.Fatalf("%d lines after the cut", n)
	}
	e := statsSetup(t, chat, h.archive, &failing{ImportStore: h.imports, n: 5, dup: true})
	if rep := runStats(t, e, e.admin, 200); rep.Imported != 2 || rep.SkippedDuplicate != 3 {
		t.Fatalf("the next run: %+v", rep)
	}
	e = statsSetup(t, chat, h.archive, h.imports)
	if rep := runStats(t, e, e.admin, 200); rep.Imported != 1 || rep.SkippedDuplicate != 4 {
		t.Fatalf("the run after: %+v", rep)
	}
	if n := len(lines(t, h.imports)); n != 5 {
		t.Fatalf("%d lines in the end", n)
	}
}

// Only the admin group imports, and without the stats channel the route is
// off.
func TestTheStatsImportWantsTheAdminGroupAndIsOffWithoutAnArchive(t *testing.T) {
	h := historyLab(t)
	e := statsSetup(t, &statsChat{}, h.archive, h.imports)
	runStats(t, e, e.bob, 403)
	runStats(t, e, "", 401)
	off := setup(t, Config{Burst: 100})
	runStats(t, off, off.admin, 404)
	runStats(t, off, off.bob, 403)
}

// The run's bounds (RT-6.45): at most MaxRead posts read and MaxHeld bytes of
// lines held; at either the reading ends there, the newest posts imported and
// the report truncated, and the next run with room takes the older ones.
func TestTheStatsImportIsBounded(t *testing.T) {
	h := historyLab(t)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	for i := range 6 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, fmt.Sprintf(`STATS {"v":"0.85.0","n":%d}`, i)))
	}
	e := statsSetup(t, chat, h.archive, h.imports, func(si *StatsImport) { si.MaxRead = 2 })
	rep := runStats(t, e, e.admin, 200)
	if rep.Read != 2 || rep.Imported != 2 || !rep.Truncated || rep.Oldest != t0.Add(4*time.Minute).Format(time.RFC3339) || chat.given != 3 {
		t.Fatalf("read bound: %+v, %d posts handed out", rep, chat.given)
	}
	size := len(`{"v":"0.85.0","n":0}`)
	e = statsSetup(t, chat, h.archive, h.imports, func(si *StatsImport) { si.MaxHeld = 3*size + 1 })
	rep = runStats(t, e, e.admin, 200)
	if rep.Imported != 3 || rep.SkippedDuplicate != 2 || !rep.Truncated || rep.Oldest != t0.Add(1*time.Minute).Format(time.RFC3339) {
		t.Fatalf("held bound: %+v", rep)
	}
	e = statsSetup(t, chat, h.archive, h.imports)
	if rep = runStats(t, e, e.admin, 200); rep.Imported != 1 || rep.SkippedDuplicate != 5 || rep.Truncated {
		t.Fatalf("with room: %+v", rep)
	}
}
