package history

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/inbound"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
)

const (
	goodID  = "abcdef0123456789"
	agent   = "agent-alice"
	botTok  = "bot-token"
	channel = "home"
	cid     = "channel0000000000000000001"
)

var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

// fakeMM is the part of Mattermost the history read uses. Only the agent's own
// bot token is accepted, so a read with the bridge bot's would fail here.
type fakeMM struct {
	mu       sync.Mutex
	srv      *httptest.Server
	calls    []string
	team     string
	channels map[string]string // channel name -> id
	posts    map[string][]map[string]any
	threads  map[string][]map[string]any
	users    map[string]map[string]any
	since    string
}

func newFakeMM(t *testing.T) *fakeMM {
	t.Helper()
	f := &fakeMM{team: "team", channels: map[string]string{}, posts: map[string][]map[string]any{},
		threads: map[string][]map[string]any{}, users: map[string]map[string]any{}}
	f.users["u-alice"] = map[string]any{"id": "u-alice", "username": "alice", "is_bot": false}
	f.users["u-carol"] = map[string]any{"id": "u-carol", "username": "carol", "is_bot": false}
	f.users["u-bot"] = map[string]any{"id": "u-bot", "username": "agent-bot", "is_bot": true}
	f.users["bot-id"] = map[string]any{"id": "bot-id", "username": "bridge-agent-alice", "is_bot": true}
	f.srv = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeMM) client() *mm.Client { return mm.New(f.srv.URL, botTok).As("bot-id") }

func (f *fakeMM) seen() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

func (f *fakeMM) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.calls = append(f.calls, r.Method+" "+r.URL.Path)
	f.mu.Unlock()
	if r.Header.Get("Authorization") != "Bearer "+botTok {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	out := func(v any) { _ = json.NewEncoder(w).Encode(v) }
	switch {
	case r.Method == "GET" && strings.HasPrefix(r.URL.Path, "/api/v4/teams/name/"+f.team+"/channels/name/"):
		name := strings.TrimPrefix(r.URL.Path, "/api/v4/teams/name/"+f.team+"/channels/name/")
		id, ok := f.channels[name]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		out(map[string]any{"id": id, "name": name})
	case r.Method == "GET" && strings.HasPrefix(r.URL.Path, "/api/v4/channels/") && strings.HasSuffix(r.URL.Path, "/posts"):
		ch := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/v4/channels/"), "/posts")
		f.mu.Lock()
		f.since = r.URL.Query().Get("since")
		posts := f.posts[ch]
		f.mu.Unlock()
		out(list(posts))
	case r.Method == "GET" && strings.HasPrefix(r.URL.Path, "/api/v4/posts/") && strings.HasSuffix(r.URL.Path, "/thread"):
		id := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, "/api/v4/posts/"), "/thread")
		f.mu.Lock()
		posts := f.threads[id]
		f.mu.Unlock()
		if posts == nil {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		out(list(posts))
	case r.Method == "GET" && strings.HasPrefix(r.URL.Path, "/api/v4/users/"):
		id := strings.TrimPrefix(r.URL.Path, "/api/v4/users/")
		f.mu.Lock()
		u, ok := f.users[id]
		f.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		out(u)
	default:
		w.WriteHeader(http.StatusNotFound)
	}
}

// list turns posts into Mattermost's PostList, in the order given.
func list(posts []map[string]any) map[string]any {
	order := make([]string, 0, len(posts))
	by := map[string]any{}
	for _, p := range posts {
		order = append(order, p["id"].(string))
		by[p["id"].(string)] = p
	}
	return map[string]any{"order": order, "posts": by}
}

// post builds one Mattermost post.
func post(id, user string, createAt int64) map[string]any {
	return map[string]any{"id": id, "user_id": user, "channel_id": cid, "root_id": "",
		"type": "", "message": "text of " + id, "create_at": createAt, "update_at": createAt,
		"delete_at": 0}
}

// reaction adds a reaction to a post's metadata, as a read carries it.
func reaction(p map[string]any, user, emoji string, at int64) {
	md, _ := p["metadata"].(map[string]any)
	if md == nil {
		md = map[string]any{}
		p["metadata"] = md
	}
	list, _ := md["reactions"].([]any)
	md["reactions"] = append(list, map[string]any{"user_id": user, "emoji_name": emoji, "create_at": at})
}

// bots is the agent's own bot, pointed at the fake.
type bots struct {
	client *mm.Client
	err    error
	mu     sync.Mutex
	called []string
}

func (b *bots) Client(_ context.Context, name, channel string) (*mm.Client, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.called = append(b.called, name+"@"+channel)
	return b.client, b.err
}

// people are the login service's records for the test.
type people struct {
	anchors map[string]string
	recs    map[string]inbound.Account
	waiting bool // the consumer has not read them all yet
}

func (p people) Anchor(uid string) string { return p.anchors[uid] }
func (p people) Record(uid string) (inbound.Account, bool) {
	if p.waiting {
		return inbound.Account{}, false
	}
	r, ok := p.recs[uid]
	return r, ok
}
func (p people) CaughtUp() bool { return !p.waiting }

// rig is one handler with the fake Mattermost, a membership and the switches.
type rig struct {
	h      *Handler
	mm     *fakeMM
	bots   *bots
	snap   *presence.Snapshot
	sw     *presence.Switches
	now    time.Time
	ppl    people
	people people
}

func newRig(t *testing.T, pieces ...int) *rig {
	t.Helper()
	now := time.Now()
	f := newFakeMM(t)
	f.channels[channel] = cid
	snap := presence.New()
	snap.ObservedMember(channel, agent, now, 1)
	sw := &presence.Switches{}
	sw.Observed(channel, pieces, now)
	b := &bots{client: f.client()}
	ppl := people{anchors: map[string]string{"u-alice": "alice"},
		recs: map[string]inbound.Account{"u-alice": {
			Account: "u-alice", Anchor: "alice", Name: "Alice", Approver: true,
			TokenIat: 10, Iat: 11, Exp: now.Add(10 * time.Minute).Unix(),
		}}}
	return &rig{h: &Handler{Log: quiet, Team: "team", Bots: b, Presence: snap, Switches: sw,
		People: ppl}, mm: f, bots: b, snap: snap, sw: sw, now: now, ppl: ppl, people: ppl}
}

// ask runs one history request as the agent.
func (r *rig) ask(t *testing.T, payload string) ([]requests.Reply, error) {
	t.Helper()
	r.h.Now = func() time.Time { return r.now }
	return r.h.Piece(context.Background(), agent,
		requests.Request{V: 1, Request: goodID, Kind: "history", Payload: json.RawMessage(payload)})
}

// results decodes the reply's parts.
func results(t *testing.T, replies []requests.Reply) []result {
	t.Helper()
	out := make([]result, len(replies))
	for i, r := range replies {
		if err := json.Unmarshal(r.Result, &out[i]); err != nil {
			t.Fatalf("part %d: %v: %s", i+1, err, r.Result)
		}
	}
	return out
}

// RT-6.42, ADR-014 phase 5 piece 5: a session reads what people wrote since
// while it was away, with each author's anchor and account record, in its own
// channel and with its own bot.
func TestTheAgentsOwnChannelIsReadBack(t *testing.T) {
	r := newRig(t, 5)
	since := r.now.Add(-time.Hour).UnixMilli()
	alice := post("p1", "u-alice", r.now.Add(-30*time.Minute).UnixMilli())
	reaction(alice, "u-carol", "thumbsup", 1)
	reaction(alice, "u-bot", "tada", 2) // a bot's reaction is no person's
	agentPost := post("p2", "u-bot", r.now.Add(-20*time.Minute).UnixMilli())
	carol := post("p3", "u-carol", r.now.Add(-10*time.Minute).UnixMilli())
	older := post("p0", "u-alice", r.now.Add(-2*time.Hour).UnixMilli())
	older["update_at"] = r.now.UnixMilli() // an old post whose reaction changed: not written since
	deleted := post("p4", "u-alice", r.now.UnixMilli())
	deleted["delete_at"] = 5
	system := post("p5", "u-alice", r.now.UnixMilli())
	system["type"] = "system_join_channel"
	webhook := post("p6", "u-alice", r.now.UnixMilli())
	webhook["props"] = map[string]any{"from_webhook": "true"}
	r.mm.posts[cid] = []map[string]any{carol, alice, agentPost, older, deleted, system, webhook}

	replies, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, since))
	if err != nil {
		t.Fatal(err)
	}
	parts := results(t, replies)
	if len(parts) != 1 || len(parts[0].Posts) != 3 {
		t.Fatalf("got %d parts: %+v", len(parts), parts)
	}
	got := parts[0].Posts
	if got[0].ID != "p1" || got[1].ID != "p2" || got[2].ID != "p3" {
		t.Fatalf("the posts are not oldest first: %s %s %s", got[0].ID, got[1].ID, got[2].ID)
	}
	// the person: anchor and record, checked as a live event's would be
	if got[0].Anchor != "alice" || got[0].Account == nil || !got[0].Account.Approver ||
		got[0].Account.Name != "Alice" || got[0].Account.Exp != r.ppl.recs["u-alice"].Exp {
		t.Fatalf("alice's post: %+v", got[0])
	}
	if got[0].Account.Anchor != "alice" || got[0].Account.V != 1 || got[0].Account.Account != "u-alice" {
		t.Fatalf("the record's shape: %+v", got[0].Account)
	}
	// a bot's post: no person, no record
	if !got[1].Bot || got[1].Anchor != "" || got[1].Account != nil {
		t.Fatalf("a bot's post: %+v", got[1])
	}
	// an unbound person: no record, not a bot
	if got[2].Bot || got[2].Anchor != "" || got[2].Account != nil {
		t.Fatalf("an unbound person's post: %+v", got[2])
	}
	// only the person's reaction is carried, with its anchor
	if got[0].Metadata == nil || len(got[0].Metadata.Reactions) != 1 ||
		got[0].Metadata.Reactions[0].UserID != "u-carol" ||
		got[0].Metadata.Reactions[0].EmojiName != "thumbsup" {
		t.Fatalf("the reactions: %+v", got[0].Metadata)
	}
	if parts[0].Channel != channel || parts[0].ChannelID != cid || parts[0].More || parts[0].Next != 0 {
		t.Fatalf("the result: %+v", parts[0])
	}
	if r.mm.since != fmt.Sprint(since) {
		t.Fatalf("the since read asked for %q, want %d", r.mm.since, since)
	}
	if len(r.bots.called) != 1 || r.bots.called[0] != agent+"@"+channel {
		t.Fatalf("the read used %v", r.bots.called)
	}
}

// A channel the agent announced no membership of is refused, and nothing is
// read: another agent's channel is never touched.
func TestAChannelTheAgentDidNotAnnounceIsRefused(t *testing.T) {
	r := newRig(t, 5)
	_, err := r.ask(t, fmt.Sprintf(`{"channel":"elsewhere","since":%d}`, r.now.UnixMilli()))
	if err == nil || err.Error() != "not a channel of that agent" {
		t.Fatalf("err %v", err)
	}
	if calls := r.mm.seen(); len(calls) != 0 {
		t.Fatalf("Mattermost was read: %v", calls)
	}
	if len(r.bots.called) != 0 {
		t.Fatalf("a bot was asked for: %v", r.bots.called)
	}
	// an old membership is no membership either
	r.snap.Clock = func() time.Time { return r.now.Add(25 * time.Hour) }
	if _, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, r.now.UnixMilli())); err == nil ||
		err.Error() != "not a channel of that agent" {
		t.Fatalf("an old membership: %v", err)
	}
}

// Piece 5 off in the channel: the answer says so and nothing is read.
func TestTheSwitchOffRefusesTheRead(t *testing.T) {
	r := newRig(t) // no piece 5
	_, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, r.now.UnixMilli()))
	if err == nil || err.Error() != "piece 5 is off in home" {
		t.Fatalf("err %v", err)
	}
	if calls := r.mm.seen(); len(calls) != 0 {
		t.Fatalf("Mattermost was read: %v", calls)
	}
	r2 := newRig(t, 4)
	if _, err := r2.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, r2.now.UnixMilli())); err == nil ||
		!strings.Contains(err.Error(), "piece 5 is off") {
		t.Fatalf("another piece on: %v", err)
	}
}

// The bounds: a since older than 14 days, a limit past 200, a misspelt field.
func TestTheReadsBoundsAreRefused(t *testing.T) {
	r := newRig(t, 5)
	old := r.now.Add(-MaxAge - time.Minute).UnixMilli()
	if _, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, old)); err == nil ||
		!strings.Contains(err.Error(), "older than 336h") {
		t.Fatalf("an old since: %v", err)
	}
	if _, err := r.ask(t, fmt.Sprintf(`{"channel":%q}`, channel)); err == nil ||
		!strings.Contains(err.Error(), "needs since or a thread") {
		t.Fatalf("no since: %v", err)
	}
	if _, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":1,"thread":"p1"}`, channel)); err == nil ||
		!strings.Contains(err.Error(), "two reads") {
		t.Fatalf("since and thread: %v", err)
	}
	if _, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":1,"sice":2}`, channel)); err == nil ||
		!strings.Contains(err.Error(), "unknown field") {
		t.Fatalf("a misspelt bound: %v", err)
	}
	if _, err := r.ask(t, `{"channel":"Home","since":1}`); err == nil ||
		!strings.Contains(err.Error(), "no channel token") {
		t.Fatalf("a channel token: %v", err)
	}
	if calls := r.mm.seen(); len(calls) != 0 {
		t.Fatalf("Mattermost was read: %v", calls)
	}
}

// The count: at most the limit in one answer, the parts saying more and where
// to ask on.
func TestTheCountBoundAndItsCursor(t *testing.T) {
	r := newRig(t, 5)
	base := r.now.Add(-time.Hour).UnixMilli()
	for i, id := range []string{"p1", "p2", "p3"} {
		r.mm.posts[cid] = append(r.mm.posts[cid], post(id, "u-carol", base+int64(i)*1000))
	}
	replies, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d,"limit":2}`, channel, base))
	if err != nil {
		t.Fatal(err)
	}
	parts := results(t, replies)
	if len(parts) != 1 || len(parts[0].Posts) != 2 || !parts[0].More ||
		parts[0].Next != base+1000 {
		t.Fatalf("the bound: %+v", parts)
	}
	// the next read from the cursor carries the rest
	replies, err = r.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d,"limit":2}`, channel, parts[0].Next))
	if err != nil {
		t.Fatal(err)
	}
	parts = results(t, replies)
	if len(parts[0].Posts) != 2 || parts[0].Posts[1].ID != "p3" || parts[0].More {
		t.Fatalf("after the cursor: %+v", parts)
	}
}

// The bytes: an answer that would pass its limit is split into parts, each
// within its own bound, `last` on the final one.
func TestALongAnswerIsSplitIntoParts(t *testing.T) {
	r := newRig(t, 5)
	base := r.now.Add(-time.Hour).UnixMilli()
	big := strings.Repeat("x", 60_000)
	for i, id := range []string{"p1", "p2", "p3"} {
		p := post(id, "u-carol", base+int64(i))
		p["message"] = big
		r.mm.posts[cid] = append(r.mm.posts[cid], p)
	}
	replies, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, base))
	if err != nil {
		t.Fatal(err)
	}
	parts := results(t, replies)
	if len(parts) < 2 {
		t.Fatalf("one part of %d bytes", len(replies[0].Result))
	}
	var seen int
	for i, p := range parts {
		if len(replies[i].Result) > maxPartBytes+1024 {
			t.Fatalf("part %d is %d bytes", i+1, len(replies[i].Result))
		}
		seen += len(p.Posts)
		if p.More {
			t.Fatalf("part %d says more although all three fit", i+1)
		}
	}
	if seen != 3 {
		t.Fatalf("%d posts of 3", seen)
	}
}

// A thread: the root's posts, the root kept even when it is an agent's ask,
// the people's replies and reactions, and the thread's channel checked.
func TestAThreadRead(t *testing.T) {
	r := newRig(t, 5)
	root := post("root", "u-bot", r.now.Add(-time.Hour).UnixMilli())
	reaction(root, "u-bot", "tada", 1)
	reaction(root, "u-alice", "thumbsup", 2)
	reply := post("reply", "u-alice", r.now.Add(-50*time.Minute).UnixMilli())
	reply["root_id"] = "root"
	botReply := post("botreply", "u-bot", r.now.Add(-40*time.Minute).UnixMilli())
	botReply["root_id"] = "root"
	system := post("joined", "u-carol", r.now.Add(-30*time.Minute).UnixMilli())
	system["type"] = "system_join_channel"
	system["root_id"] = "root"
	r.mm.threads["root"] = []map[string]any{reply, root, botReply, system}

	replies, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"thread":"root"}`, channel))
	if err != nil {
		t.Fatal(err)
	}
	parts := results(t, replies)
	if len(parts) != 1 || parts[0].Thread != "root" || len(parts[0].Posts) != 3 {
		t.Fatalf("the thread: %+v", parts)
	}
	got := parts[0].Posts
	if got[0].ID != "root" || !got[0].Bot || len(got[0].Metadata.Reactions) != 1 ||
		got[0].Metadata.Reactions[0].Anchor != "alice" {
		t.Fatalf("the root: %+v", got[0])
	}
	if got[1].ID != "reply" || got[1].Anchor != "alice" || got[1].Account == nil {
		t.Fatalf("a person's reply: %+v", got[1])
	}
	if got[2].ID != "botreply" || !got[2].Bot {
		t.Fatalf("a bot's reply: %+v", got[2])
	}
}

// A thread in another channel is refused, even when its id is known.
func TestAThreadOutsideTheChannelIsRefused(t *testing.T) {
	r := newRig(t, 5)
	other := post("root", "u-alice", r.now.UnixMilli())
	other["channel_id"] = "another00000000000000000001"
	r.mm.threads["root"] = []map[string]any{other}
	_, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"thread":"root"}`, channel))
	if err == nil || !strings.Contains(err.Error(), "not in ~home") {
		t.Fatalf("err %v", err)
	}
}

// Without the agent's own bot there is no read, and never the bridge bot's.
func TestNoBotRefusesTheRead(t *testing.T) {
	r := newRig(t, 5)
	r.h.Bots = nil
	_, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, r.now.UnixMilli()))
	if err == nil || !strings.Contains(err.Error(), "no bots for the agents") {
		t.Fatalf("err %v", err)
	}
	r2 := newRig(t, 5)
	r2.bots.err = io.ErrUnexpectedEOF
	if _, err := r2.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, r2.now.UnixMilli())); err == nil ||
		!strings.Contains(err.Error(), "the agent's bot") {
		t.Fatalf("a failed bot: %v", err)
	}
	// the account records not read yet: no person is called unbound
	r3 := newRig(t, 5)
	r3.people.waiting = true
	r3.h.People = r3.people
	if _, err := r3.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, r3.now.UnixMilli())); err == nil ||
		!strings.Contains(err.Error(), "not read yet") {
		t.Fatalf("unread records: %v", err)
	}
}

// The shaping: a post keeps Mattermost's fields, its author's anchor and
// record; a bot's reaction and a webhook, system or deleted post are out.
func TestTheShapingAndTheDropRules(t *testing.T) {
	r := newRig(t, 5)
	ctx := context.Background()
	p := mm.Post{ID: "p1", UserID: "u-alice", ChannelID: cid, RootID: "root",
		Type: "", Message: "hello", Props: map[string]any{"branch": "x"}, FileCount: 2,
		CreateAt: 10, UpdateAt: 11, DeleteAt: 0, HasMetadata: true,
		Reactions: []mm.Reaction{{UserID: "u-bot", EmojiName: "tada", CreateAt: 1},
			{UserID: "u-carol", EmojiName: "thumbsup", CreateAt: 2}}}
	got := r.h.shape(ctx, r.mm.client(), p)
	if got.ID != "p1" || got.UserID != "u-alice" || got.ChannelID != cid || got.RootID != "root" ||
		got.Message != "hello" || got.FileCount != 2 || got.CreateAt != 10 || got.UpdateAt != 11 ||
		got.Props["branch"] != "x" || got.Anchor != "alice" || got.Account == nil || got.Bot {
		t.Fatalf("the shaped post: %+v", got)
	}
	if got.Metadata == nil || len(got.Metadata.Reactions) != 1 ||
		got.Metadata.Reactions[0].UserID != "u-carol" || got.Metadata.Reactions[0].CreateAt != 2 {
		t.Fatalf("the shaped reactions: %+v", got.Metadata)
	}
	// no metadata in the read: none in the answer, so nothing reads as removed
	if p := r.h.shape(ctx, r.mm.client(), mm.Post{ID: "p2", UserID: "u-carol", ChannelID: cid}); p.Metadata != nil {
		t.Fatalf("metadata without a read: %+v", p.Metadata)
	}
	for name, p := range map[string]mm.Post{
		"deleted": {ID: "a", UserID: "u-alice", DeleteAt: 5},
		"system":  {ID: "b", UserID: "u-alice", Type: "system_join_channel"},
		"webhook": {ID: "c", UserID: "u-alice", Props: map[string]any{"from_webhook": "true"}},
		"renamed": {ID: "d", UserID: "u-alice", Props: map[string]any{"override_username": "x"}},
	} {
		if !drop(p) {
			t.Errorf("%s post kept", name)
		}
	}
	if drop(mm.Post{ID: "e", UserID: "u-alice", Props: map[string]any{"branch": "x"}}) {
		t.Fatal("an ordinary post dropped")
	}
}

// The answer's JSON shape, which the MCP server's piece 5b merges: the result's
// keys, a post's Mattermost fields with its anchor and account, and a
// reaction's, so a change here is a change the other side must see.
func TestTheAnswersJSONShape(t *testing.T) {
	r := newRig(t, 5)
	base := r.now.Add(-time.Hour).UnixMilli()
	p := post("p1", "u-alice", base+1)
	p["root_id"] = "root"
	p["props"] = map[string]any{"branch": "x"}
	p["file_ids"] = []any{"f1"}
	reaction(p, "u-alice", "thumbsup", 2)
	r.mm.posts[cid] = []map[string]any{p}
	replies, err := r.ask(t, fmt.Sprintf(`{"channel":%q,"since":%d}`, channel, base))
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(replies[0].Result, &got); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"v", "channel", "channel_id", "posts"} {
		if _, ok := got[key]; !ok {
			t.Fatalf("the result has no %q: %s", key, replies[0].Result)
		}
	}
	posts := got["posts"].([]any)
	if len(posts) != 1 {
		t.Fatalf("posts %v", posts)
	}
	one := posts[0].(map[string]any)
	for _, key := range []string{"id", "user_id", "channel_id", "root_id", "message", "props",
		"file_count", "create_at", "update_at", "anchor", "account", "metadata"} {
		if _, ok := one[key]; !ok {
			t.Fatalf("the post has no %q: %v", key, one)
		}
	}
	acc := one["account"].(map[string]any)
	for _, key := range []string{"v", "account", "anchor", "name", "approver", "token_iat", "iat", "exp"} {
		if _, ok := acc[key]; !ok {
			t.Fatalf("the record has no %q: %v", key, acc)
		}
	}
	re := one["metadata"].(map[string]any)["reactions"].([]any)[0].(map[string]any)
	for _, key := range []string{"user_id", "emoji_name", "create_at", "anchor", "account"} {
		if _, ok := re[key]; !ok {
			t.Fatalf("the reaction has no %q: %v", key, re)
		}
	}
}
