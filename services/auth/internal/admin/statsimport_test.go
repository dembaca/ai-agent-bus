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

// statsLab starts a JetStream server with the bus's streams and returns the
// STATS stream's history side and a connection.
func statsLab(t *testing.T, maxBytes ...int64) (*consumers.Stats, *nats.Conn) {
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
	m, err := consumers.New(nc, consumers.Config{Bus: "bus", Presence: "presence", StatsMaxBytes: append(maxBytes, 0)[0]})
	if err != nil {
		t.Fatal(err)
	}
	if err := m.EnsureStreams(ctx(t)); err != nil {
		t.Fatal(err)
	}
	return m.Stats(), nc
}

type stored struct {
	name string
	body string
	hdr  nats.Header
}

func lines(t *testing.T, s *consumers.Stats) []stored {
	t.Helper()
	var out []stored
	if err := s.ReadAll(ctx(t), func() { out = nil }, func(name string, body []byte, hdr nats.Header) error {
		out = append(out, stored{name, string(body), hdr})
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

func statsSetup(t *testing.T, chat *statsChat, store StatsStore, edit ...func(*StatsImport)) *env {
	t.Helper()
	si := &StatsImport{Channel: "agents-stats", Source: chat, Store: store}
	for _, e := range edit {
		e(si)
	}
	return setupWith(t, Config{Burst: 100, Stats: si}, testrealm.NewChat("alice", "bob", "carol", "mal"), &fakeMM{})
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

// The import publishes each valid line on <stats>.import.<bot>, byte for
// byte, marked as imported with its original time and post id; skips a line
// STATS holds by (sid, part) (here one an agent published under its NATS
// name), one the run already wrote, and a post younger than two minutes;
// imports a line from before 0.91.0 (no sid, no part) by its post id, two
// posts of the same content as two lines; counts each bad line under its
// reason; and a second run imports nothing.
func TestStatsImportMarksDedupesAndDrops(t *testing.T) {
	s, nc := statsLab(t)
	if err := nc.Publish("stats.agent-nats-name", []byte(`{"sid":"aaaaaaaaaaaa","part":1}`)); err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	keep := `{"v":"0.80.0","sid":"bbbbbbbbbbbb","part":2, "x":{"odd":[1,2]}}`
	older := `{"sid":"cccccccccccc","part":1,"final":true}`
	oldFormat := `{"v":"0.85.0","turns":3,"bus":{"posts":2}}`
	chat := &statsChat{users: map[string]mattermost.User{
		"ua": bot("ua", "agent-a"), "ub": bot("ub", "agent-b"), "uc": bot("uc", "agent-c"),
		"ud": bot("ud", "dotted.name"), "uh": {ID: "uh", Username: "human"},
	}, posts: []mattermost.Post{
		post("p1", "ua", 5, `STATS {"sid":"aaaaaaaaaaaa","part":1,"v":"0.90.0"}`), // an agent published it on STATS
		post("p2", "ua", 6, "STATS "+keep),
		post("p3", "ua", 7, `STATS {"sid":"bbbbbbbbbbbb","part":2}`),  // the run wrote it already
		post("p3b", "ub", 7, `STATS {"sid":"bbbbbbbbbbbb","part":2}`), // so too under another bot
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
	e := statsSetup(t, chat, s)
	rep := runStats(t, e, e.admin, 200)
	wantRep := StatsReport{Read: 17, Published: 4, PublishedWithoutSid: 2, Confirmed: 4, NotStats: 1, SkippedInStats: 1, SkippedImported: 2, SkippedInvalid: 5, SkippedTooBig: 1, SkippedName: 2,
		SkippedRecent: 1, Oldest: t0.Add(time.Minute).Format(time.RFC3339), Newest: t0.Add(18 * time.Minute).Format(time.RFC3339)}
	if rep != wantRep {
		t.Fatalf("report\n%+v, want\n%+v", rep, wantRep)
	}
	got := lines(t, s)
	if len(got) != 5 || got[0].name != "agent-nats-name" || got[0].hdr.Get("Cb-Source") != "" {
		t.Fatalf("%d lines in STATS: %+v", len(got), got)
	}
	for i, w := range []struct {
		name, body, post string
		at               time.Time
	}{
		{"import.agent-c", older, "p11", t0.Add(time.Minute)}, // oldest post first
		{"import.agent-a", keep, "p2", t0.Add(6 * time.Minute)},
		{"import.agent-a", oldFormat, "p15", t0.Add(17 * time.Minute)},
		{"import.agent-a", oldFormat, "p16", t0.Add(18 * time.Minute)},
	} {
		l := got[i+1]
		if l.name != w.name || l.body != w.body || l.hdr.Get("Cb-Post-Id") != w.post {
			t.Errorf("line %d: %s %q %s, want %s %q %s", i, l.name, l.body, l.hdr.Get("Cb-Post-Id"), w.name, w.body, w.post)
		}
		if l.hdr.Get("Cb-Source") != "mattermost-import" || l.hdr.Get("Cb-Orig-Time") != w.at.Format(time.RFC3339Nano) {
			t.Errorf("line %d headers: %v", i, l.hdr)
		}
	}
	// A second run reads the whole channel (a new process knows no earlier
	// run) and publishes nothing; one of the same process stops at what the
	// first went through.
	fresh := statsSetup(t, chat, s)
	again := runStats(t, fresh, fresh.admin, 200)
	if again.Published != 0 || again.SkippedInStats != 1 || again.SkippedImported != 6 || again.Oldest != "" || again.KnownBefore != "" {
		t.Fatalf("second run: %+v", again)
	}
	if same := runStats(t, e, e.admin, 200); same.Read != 1 || same.SkippedRecent != 1 || same.Published != 0 || same.KnownBefore == "" {
		t.Fatalf("a run of the same process: %+v", same)
	}
	if n := len(lines(t, s)); n != 5 {
		t.Fatalf("%d lines after the second run", n)
	}
}

// A post id on an agent's own subject hides nothing: only the import's own
// subjects are read for it.
func TestAPostIdOnAnAgentsSubjectIsIgnored(t *testing.T) {
	s, nc := statsLab(t)
	msg := nats.NewMsg("stats.agent-x")
	msg.Data = []byte(`{"v":"0.86.0"}`)
	msg.Header.Set("Cb-Post-Id", "p1")
	if err := nc.PublishMsg(msg); err != nil {
		t.Fatal(err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatal(err)
	}
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")},
		posts: []mattermost.Post{post("p1", "ua", 1, `STATS {"v":"0.85.0"}`)}}
	e := statsSetup(t, chat, s)
	if rep := runStats(t, e, e.admin, 200); rep.Published != 1 {
		t.Fatalf("%+v", rep)
	}
}

// Above the cap of 500 per name only the newest 500 are imported, so a
// second run stores nothing and pushes nothing out, and a newer post later
// takes the place of the oldest; with lines of both formats mixed.
func TestTheStatsImportKeepsTheNewestPerName(t *testing.T) {
	s, _ := statsLab(t)
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
	e := statsSetup(t, chat, s)
	if rep := runStats(t, e, e.admin, 200); rep.Published != 500 || rep.PublishedWithoutSid != 250 || rep.SkippedOverCap != 2 || rep.Oldest != t0.Add(2*time.Minute).Format(time.RFC3339) {
		t.Fatalf("first run: %+v", rep)
	}
	rerun := func() StatsReport {
		t.Helper()
		fresh := statsSetup(t, chat, s) // a new process: it reads the whole channel
		return runStats(t, fresh, fresh.admin, 200)
	}
	if rep := rerun(); rep.Published != 0 || rep.SkippedOverCap != 2 || rep.SkippedImported != 500 {
		t.Fatalf("second run: %+v", rep)
	}
	chat.posts = append(chat.posts, post("p502", "ua", 502, "STATS "+line(502)))
	if rep := rerun(); rep.Published != 1 || rep.SkippedOverCap != 3 {
		t.Fatalf("a newer post: %+v", rep)
	}
	got := lines(t, s)
	if len(got) != 500 || got[0].body != line(3) {
		t.Fatalf("%d lines, the oldest %q", len(got), got[0].body)
	}
	if rep := rerun(); rep.Published != 0 {
		t.Fatalf("after the newer post: %+v", rep)
	}
}

// failing stops after n published lines, as a run that is cut off.
type failing struct {
	StatsStore
	n int
}

func (f *failing) Put(ctx context.Context, bot string, body []byte, hdr map[string]string) error {
	if f.n == 0 {
		return errors.New("cut off")
	}
	f.n--
	return f.StatsStore.Put(ctx, bot, body, hdr)
}

// A run that stops part way answers 502 and keeps what it wrote; the next run
// completes it without a duplicate.
func TestAnInterruptedStatsImportIsCompletedByTheNext(t *testing.T) {
	s, _ := statsLab(t)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	for i := range 5 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, fmt.Sprintf(`STATS {"sid":"%012x","part":%d}`, i, i+1)))
	}
	cut := statsSetup(t, chat, &failing{StatsStore: s, n: 2})
	runStats(t, cut, cut.admin, 502)
	if n := len(lines(t, s)); n != 2 {
		t.Fatalf("%d lines after the cut", n)
	}
	e := statsSetup(t, chat, s)
	if rep := runStats(t, e, e.admin, 200); rep.Published != 3 || rep.SkippedImported != 2 {
		t.Fatalf("the next run: %+v", rep)
	}
	if n := len(lines(t, s)); n != 5 {
		t.Fatalf("%d lines in the end", n)
	}
}

// Only the admin group imports, and without the stats channel the route is
// off.
func TestTheStatsImportWantsTheAdminGroupAndIsOffWithoutAChannel(t *testing.T) {
	s, _ := statsLab(t)
	e := statsSetup(t, &statsChat{}, s)
	runStats(t, e, e.bob, 403)
	runStats(t, e, "", 401)
	off := setup(t, Config{Burst: 100})
	runStats(t, off, off.admin, 404)
	runStats(t, off, off.bob, 403)
}

// The run's bounds (RT-6.45): at most MaxLines lines and MaxHeld bytes held to
// publish (posts already in STATS do not count, so a rerun gets past them);
// at either the reading ends there, the newest published and the report
// says truncated and why, and the next run takes the older ones.
func TestTheStatsImportIsBounded(t *testing.T) {
	s, _ := statsLab(t)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	for i := range 6 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, fmt.Sprintf(`STATS {"v":"0.85.0","n":%d}`, i)))
	}
	lines := func(si *StatsImport) { si.MaxLines = 2 }
	e := statsSetup(t, chat, s, lines)
	rep := runStats(t, e, e.admin, 200)
	if rep.Read != 2 || rep.Published != 2 || rep.Confirmed != 2 || rep.Truncated != "lines" || rep.Oldest != t0.Add(4*time.Minute).Format(time.RFC3339) || chat.given != 3 {
		t.Fatalf("first run: %+v, %d posts handed out", rep, chat.given)
	}
	e = statsSetup(t, chat, s, lines)
	if rep = runStats(t, e, e.admin, 200); rep.Published != 2 || rep.SkippedImported != 2 || rep.Truncated != "lines" || rep.Oldest != t0.Add(2*time.Minute).Format(time.RFC3339) {
		t.Fatalf("the rerun gets past the published ones: %+v", rep)
	}
	size := len(`{"v":"0.85.0","n":0}`) + lineOverhead
	e = statsSetup(t, chat, s, func(si *StatsImport) { si.MaxHeld = size + 1 })
	if rep = runStats(t, e, e.admin, 200); rep.Published != 1 || rep.SkippedImported != 4 || rep.Truncated != "room" || rep.Oldest != t0.Add(time.Minute).Format(time.RFC3339) {
		t.Fatalf("held bound: %+v", rep)
	}
	e = statsSetup(t, chat, s)
	if rep = runStats(t, e, e.admin, 200); rep.Published != 1 || rep.SkippedImported != 5 || rep.Truncated != "" {
		t.Fatalf("with room: %+v", rep)
	}
}

// The import never publishes more than STATS has room for: it would push
// STATS's own oldest lines out (discard old). The newest that fit go in.
func TestTheStatsImportFitsTheRoomLeft(t *testing.T) {
	s, _ := statsLab(t, 9<<10)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	pad := strings.Repeat("x", 2000)
	for i := range 6 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, fmt.Sprintf(`STATS {"v":"0.85.0","n":%d,"pad":"%s"}`, i, pad)))
	}
	e := statsSetup(t, chat, s)
	rep := runStats(t, e, e.admin, 200)
	if rep.Published != 4 || rep.Confirmed != 4 || rep.Truncated != "room" || rep.Oldest != t0.Add(2*time.Minute).Format(time.RFC3339) {
		t.Fatalf("%+v", rep)
	}
	if n := len(lines(t, s)); n != 4 {
		t.Fatalf("%d lines: STATS dropped some", n)
	}
}

// A run of the same process stops reading where an earlier complete run
// went through every post; a run cut short leaves that mark alone, so the
// next reads on past it (RT-6.45).
func TestARerunStopsWhereTheLastCompleteRunWent(t *testing.T) {
	s, _ := statsLab(t)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	for i := range 6 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, fmt.Sprintf(`STATS {"v":"0.85.0","n":%d}`, i)))
	}
	e := statsSetup(t, chat, s, func(si *StatsImport) { si.MaxLines, si.MinAge = 2, time.Millisecond })
	for run, want := range []struct {
		published int
		truncated string
	}{{2, "lines"}, {2, "lines"}, {2, ""}} {
		if rep := runStats(t, e, e.admin, 200); rep.Published != want.published || rep.Truncated != want.truncated || rep.KnownBefore != "" {
			t.Fatalf("run %d: %+v", run, rep)
		}
	}
	// a post made after that run, so newer than the point it went through
	chat.posts = append(chat.posts, mattermost.Post{ID: "p6", UserID: "ua", Created: time.Now(), Message: `STATS {"v":"0.85.0","n":6}`})
	time.Sleep(5 * time.Millisecond)
	chat.given = 0
	rep := runStats(t, e, e.admin, 200)
	if rep.Published != 1 || rep.Read != 1 || rep.KnownBefore == "" || chat.given != 2 {
		t.Fatalf("after a complete run: %+v, %d posts handed out", rep, chat.given)
	}
}

// fickle is a STATS whose room shrinks between the run's two readings of
// it, and which hides the imported lines from the given reads.
type fickle struct {
	StatsStore
	rooms []int64
	hide  map[int]bool
	reads int
}

func (f *fickle) Room(ctx context.Context) (int64, error) {
	if len(f.rooms) > 0 {
		r := f.rooms[0]
		f.rooms = f.rooms[1:]
		return r, nil
	}
	return f.StatsStore.Room(ctx)
}

func (f *fickle) ReadAll(ctx context.Context, begin func(), fn func(string, []byte, nats.Header) error) error {
	f.reads++
	if !f.hide[f.reads] {
		return f.StatsStore.ReadAll(ctx, begin, fn)
	}
	return f.StatsStore.ReadAll(ctx, begin, func(n string, b []byte, h nats.Header) error {
		if strings.HasPrefix(n, "import.") {
			return nil
		}
		return fn(n, b, h)
	})
}

// STATS keeps growing while the channel is read: its room is read again
// right before publishing, and the oldest held lines go until the rest fit.
func TestTheRoomIsReadAgainBeforePublishing(t *testing.T) {
	s, _ := statsLab(t)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	for i := range 6 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, fmt.Sprintf(`STATS {"v":"0.85.0","n":%d}`, i)))
	}
	size := int64(len(`{"v":"0.85.0","n":0}`) + lineOverhead)
	e := statsSetup(t, chat, &fickle{StatsStore: s, rooms: []int64{1 << 20, 3*size + 1}})
	rep := runStats(t, e, e.admin, 200)
	if rep.Published != 3 || rep.Truncated != "room" || rep.Oldest != t0.Add(3*time.Minute).Format(time.RFC3339) {
		t.Fatalf("%+v", rep)
	}
}

// STATS acknowledges nothing: when the re-read after the run finds fewer
// lines than were published, the import reads once more after a short wait;
// when that still finds fewer, confirmed says so, and the process does not
// take the run as complete.
func TestConfirmedIsReadOnceMore(t *testing.T) {
	s, _ := statsLab(t)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	for i := range 3 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, fmt.Sprintf(`STATS {"v":"0.85.0","n":%d}`, i)))
	}
	wait := func(si *StatsImport) { si.ConfirmWait = 10 * time.Millisecond }
	e := statsSetup(t, chat, &fickle{StatsStore: s, hide: map[int]bool{2: true}}, wait)
	if rep := runStats(t, e, e.admin, 200); rep.Published != 3 || rep.Confirmed != 3 {
		t.Fatalf("once more: %+v", rep)
	}
	chat.posts = append(chat.posts, post("p3", "ua", 3, `STATS {"v":"0.85.0","n":3}`))
	slow := &fickle{StatsStore: s, hide: map[int]bool{2: true, 3: true}}
	e = statsSetup(t, chat, slow, wait)
	if rep := runStats(t, e, e.admin, 200); rep.Published != 1 || rep.Confirmed != 0 {
		t.Fatalf("still missing: %+v", rep)
	}
	// not complete: the next run of the process reads the whole channel again
	if rep := runStats(t, e, e.admin, 200); rep.KnownBefore != "" || rep.Read != 4 || rep.SkippedImported != 4 {
		t.Fatalf("after an unconfirmed run: %+v", rep)
	}
}
