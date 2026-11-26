package admin

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
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
}

func (s *statsChat) ChannelPosts(_ context.Context, ch string, _ time.Time) ([]mattermost.Post, error) {
	if ch != "agents-stats" {
		return nil, errors.New("no such channel")
	}
	return append([]mattermost.Post(nil), s.posts...), nil
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

// archiveLab starts a JetStream server with the bus's streams and the
// archive, and returns the archive and a connection.
func archiveLab(t *testing.T) (*consumers.Archive, *nats.Conn) {
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
	return a, nc
}

type archived struct {
	subject string
	body    string
	hdr     nats.Header
}

func archiveLines(t *testing.T, a *consumers.Archive) []archived {
	t.Helper()
	var out []archived
	if err := a.Each(ctx(t), func(subject string, body []byte, hdr nats.Header) error {
		out = append(out, archived{subject, string(body), hdr})
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

func statsSetup(t *testing.T, chat *statsChat, store ArchiveStore) *env {
	t.Helper()
	return setupWith(t, Config{Burst: 100, Stats: &StatsImport{Channel: "agents-stats", Source: chat, Store: store, CatchUp: 5 * time.Second}},
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

// The import writes each valid line on <archive>.<bot> byte for byte, marked
// as imported with its original time; skips what the archive holds (here a
// line JetStream copied from STATS) and what the run already wrote; counts
// each bad line under its reason; and a second run imports nothing.
func TestStatsImportMarksDedupesAndDrops(t *testing.T) {
	a, nc := archiveLab(t)
	if err := nc.Publish("stats.agent-a", []byte(`{"sid":"aaaaaaaaaaaa","part":1}`)); err != nil {
		t.Fatal(err)
	}
	keep := `{"v":"0.80.0","sid":"bbbbbbbbbbbb","part":2, "x":{"odd":[1,2]}}`
	older := `{"sid":"cccccccccccc","part":1,"final":true}`
	chat := &statsChat{users: map[string]mattermost.User{
		"ua": bot("ua", "agent-a"), "ub": bot("ub", "agent-b"), "uc": bot("uc", "agent-c"),
		"ud": bot("ud", "dotted.name"), "uh": {ID: "uh", Username: "human"},
	}, posts: []mattermost.Post{
		post("p1", "ua", 5, `STATS {"sid":"aaaaaaaaaaaa","part":1,"v":"0.90.0"}`), // copied from STATS
		post("p2", "ua", 6, "STATS "+keep),
		post("p3", "ub", 7, `STATS {"sid":"bbbbbbbbbbbb","part":2}`), // the run wrote it already
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
	}}
	if n := len(chat.posts[7].Message) - len("STATS "); n != 8001 {
		t.Fatalf("the long line is %d bytes", n)
	}
	e := statsSetup(t, chat, a)
	rep := runStats(t, e, e.admin, 200)
	wantRep := StatsReport{Read: 13, Imported: 2, NotStats: 1, SkippedDuplicate: 2, SkippedInvalid: 5, SkippedTooBig: 1, SkippedName: 2,
		Oldest: t0.Add(time.Minute).Format(time.RFC3339), Newest: t0.Add(6 * time.Minute).Format(time.RFC3339)}
	if rep != wantRep {
		t.Fatalf("report\n%+v, want\n%+v", rep, wantRep)
	}
	lines := archiveLines(t, a)
	if len(lines) != 3 {
		t.Fatalf("%d lines in the archive: %+v", len(lines), lines)
	}
	if l := lines[0]; l.subject != "stats-archive.agent-a" || l.hdr.Get("Cb-Source") != "" {
		t.Fatalf("the copied line: %+v", l)
	}
	for i, w := range []struct {
		subject, body string
		at            time.Time
	}{
		{"stats-archive.agent-c", older, t0.Add(time.Minute)}, // oldest post first
		{"stats-archive.agent-a", keep, t0.Add(6 * time.Minute)},
	} {
		l := lines[i+1]
		if l.subject != w.subject || l.body != w.body {
			t.Errorf("line %d: %s %q, want %s %q", i, l.subject, l.body, w.subject, w.body)
		}
		if l.hdr.Get("Cb-Source") != "mattermost-import" || l.hdr.Get("Cb-Orig-Time") != w.at.Format(time.RFC3339Nano) {
			t.Errorf("line %d headers: %v", i, l.hdr)
		}
	}
	again := runStats(t, e, e.admin, 200)
	if again.Imported != 0 || again.SkippedDuplicate != 4 || again.Oldest != "" {
		t.Fatalf("second run: %+v", again)
	}
	if n := len(archiveLines(t, a)); n != 3 {
		t.Fatalf("%d lines after the second run", n)
	}
}

// failing stops after n stored lines, as a run that is cut off.
type failing struct {
	ArchiveStore
	n int
}

func (f *failing) Put(ctx context.Context, agent string, body []byte, hdr map[string]string) error {
	if f.n == 0 {
		return errors.New("cut off")
	}
	f.n--
	return f.ArchiveStore.Put(ctx, agent, body, hdr)
}

// A run that stops part way answers 502 and keeps what it wrote; the next run
// completes it without a duplicate.
func TestAnInterruptedStatsImportIsCompletedByTheNext(t *testing.T) {
	a, _ := archiveLab(t)
	chat := &statsChat{users: map[string]mattermost.User{"ua": bot("ua", "agent-a")}}
	for i := range 5 {
		chat.posts = append(chat.posts, post(fmt.Sprint("p", i), "ua", i, fmt.Sprintf(`STATS {"sid":"%012x","part":%d}`, i, i+1)))
	}
	cut := statsSetup(t, chat, &failing{ArchiveStore: a, n: 2})
	runStats(t, cut, cut.admin, 502)
	if n := len(archiveLines(t, a)); n != 2 {
		t.Fatalf("%d lines after the cut", n)
	}
	e := statsSetup(t, chat, a)
	if rep := runStats(t, e, e.admin, 200); rep.Imported != 3 || rep.SkippedDuplicate != 2 {
		t.Fatalf("the next run: %+v", rep)
	}
	if n := len(archiveLines(t, a)); n != 5 {
		t.Fatalf("%d lines in the end", n)
	}
}

// Only the admin group imports, and without an archive the route is off.
func TestTheStatsImportWantsTheAdminGroupAndIsOffWithoutAnArchive(t *testing.T) {
	a, _ := archiveLab(t)
	e := statsSetup(t, &statsChat{}, a)
	runStats(t, e, e.bob, 403)
	runStats(t, e, "", 401)
	off := setup(t, Config{Burst: 100})
	runStats(t, off, off.admin, 404)
	runStats(t, off, off.bob, 403)
}
