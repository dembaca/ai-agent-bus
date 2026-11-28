package permission

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/inbound"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
)

const (
	acct  = "aaaaaaaaaaaaaaaaaaaaaaaaaa"
	other = "bbbbbbbbbbbbbbbbbbbbbbbbbb"
	rid   = "abcde"
)

var quiet = slog.New(slog.NewTextHandler(io.Discard, nil))

type fakePoster struct {
	mu       sync.Mutex
	name     string
	created  []created
	posts    []mm.Post // what PostsSince returns
	failMake error
	failByID error
	byID     map[string]mm.Post
	clock    int64
	n        int
}

type created struct{ dm, text, root, id string }

func (f *fakePoster) DirectChannel(_ context.Context, user string) (string, error) {
	return "dm-" + user, nil
}

func (f *fakePoster) Create(_ context.Context, dm, text, root string, _ map[string]any) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failMake != nil {
		return "", f.failMake
	}
	f.n++
	id := fmt.Sprintf("%s-post%d", f.name, f.n)
	f.created = append(f.created, created{dm, text, root, id})
	return id, nil
}

func (f *fakePoster) PostByID(_ context.Context, id string) (mm.Post, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failByID != nil {
		return mm.Post{}, f.failByID
	}
	if p, ok := f.byID[id]; ok {
		return p, nil
	}
	return mm.Post{ID: id, CreateAt: f.clock}, nil
}

func (f *fakePoster) PostsSince(context.Context, string, int64) ([]mm.Post, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]mm.Post(nil), f.posts...), nil
}

type fakeBots map[string]Poster

func (b fakeBots) Bot(_ context.Context, agent, _ string) Poster { return b[agent] }

type fakeAccounts struct {
	owner  map[string]string
	rec    map[string]inbound.Account
	anchor map[string]string
	proof  map[string]string // account -> anchor, while auth is "openid"/"sub1"
}

func (a *fakeAccounts) Owner(anchor string) string { return a.owner[anchor] }
func (a *fakeAccounts) Record(u string) (inbound.Account, bool) {
	r, ok := a.rec[u]
	return r, ok
}
func (a *fakeAccounts) Anchor(u string) string { return a.anchor[u] }
func (a *fakeAccounts) Bound(u, svc, data string) string {
	if svc == "openid" && data == "sub1" {
		return a.proof[u]
	}
	return ""
}

type fakeUsers struct {
	u   mm.User
	err error
}

func (f *fakeUsers) GetUser(context.Context, string) (mm.User, error) { return f.u, f.err }

type switches map[int]bool

func (s switches) On(piece int, _ string) bool { return s[piece] }

type fakePub struct {
	mu   sync.Mutex
	subj []string
	data [][]byte
	fail bool
}

func (p *fakePub) Publish(subject string, data []byte) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.fail {
		return errors.New("refused")
	}
	p.subj = append(p.subj, subject)
	p.data = append(p.data, data)
	return nil
}

type rig struct {
	h      *Handler
	bridge *fakePoster
	own    *fakePoster
	acc    *fakeAccounts
	users  *fakeUsers
	pub    *fakePub
	sw     switches
	now    time.Time
}

func newRig(t *testing.T) *rig {
	t.Helper()
	r := &rig{
		bridge: &fakePoster{name: "bridge", clock: 1000},
		own:    &fakePoster{name: "own", clock: 1000},
		acc: &fakeAccounts{owner: map[string]string{"alice": acct},
			rec:    map[string]inbound.Account{acct: {Account: acct, Anchor: "alice", Name: "Alice"}},
			anchor: map[string]string{acct: "alice"}, proof: map[string]string{acct: "alice"}},
		users: &fakeUsers{u: mm.User{ID: acct, AuthService: "openid", AuthData: "sub1"}},
		pub:   &fakePub{},
		sw:    switches{Piece: true},
		now:   time.Unix(1_700_000_000, 0),
	}
	r.h = &Handler{Log: quiet, Homes: []string{"home"}, Switches: r.sw, Accounts: r.acc, Users: r.users,
		Bridge: r.bridge, Publisher: r.pub, BusPrefix: "bus", Now: func() time.Time { return r.now }}
	return r
}

func (r *rig) do(agent, payload string) (map[string]any, error) {
	rep, err := r.h.Piece(context.Background(), agent, requests.Request{V: 1, Kind: "permission", Payload: json.RawMessage(payload)})
	if err != nil {
		return nil, err
	}
	var out map[string]any
	if e := json.Unmarshal(rep[0].Result, &out); e != nil {
		return nil, e
	}
	return out, nil
}

func prompt10(id, to, name, text string) string {
	b, _ := json.Marshal(map[string]any{"op": "prompt", "channel": "home", "id": id, "to": to, "name": name, "text": text})
	return string(b)
}

func note10(id, text string, closeIt bool) string {
	b, _ := json.Marshal(map[string]any{"op": "note", "channel": "home", "id": id, "text": text, "close": closeIt})
	return string(b)
}

// RT-6.13, plan B piece 10: a prompt is posted with the agent's own bot when it
// has one (the text as given), else with the bridge bot under an agent line.
func TestRT613PlanBPiece10PromptPostsWithOwnBotOrBridgeBot(t *testing.T) {
	r := newRig(t)
	res, err := r.do("coder", prompt10(rid, "alice", "alice", "Bash: ls"))
	if err != nil || res["post"] != "bridge-post1" || res["v"] != float64(1) {
		t.Fatalf("%v %v", res, err)
	}
	want := "From agent `coder`:\n\nBash: ls"
	if len(r.bridge.created) != 1 || r.bridge.created[0].text != want || r.bridge.created[0].dm != "dm-"+acct {
		t.Fatalf("bridge bot posts: %+v", r.bridge.created)
	}
	r.h.Bots = fakeBots{"coder2": r.own}
	res, err = r.do("coder2", prompt10("fghij", "alice", "ALICE", "Edit: x"))
	if err != nil || res["post"] != "own-post1" || r.own.created[0].text != "Edit: x" {
		t.Fatalf("own bot: %v %v %+v", res, err, r.own.created)
	}
}

func TestRT613PlanBPiece10PromptRefusals(t *testing.T) {
	big := strings.Repeat("x", MaxPromptText+1)
	for _, tc := range []struct {
		name    string
		mut     func(r *rig)
		agent   string
		payload string
		want    string
	}{
		{"switch off", func(r *rig) { delete(r.sw, Piece) }, "a", prompt10(rid, "alice", "alice", "x"), "plan B piece 10 is off in ~home"},
		{"wrong home", nil, "a", strings.Replace(prompt10(rid, "alice", "alice", "x"), `"home"`, `"elsewhere"`, 1), "serves no home"},
		{"bad id l", nil, "a", prompt10("abcdl", "alice", "alice", "x"), "request id"},
		{"bad id digit", nil, "a", prompt10("abc1e", "alice", "alice", "x"), "request id"},
		{"bad id long", nil, "a", prompt10("abcdef", "alice", "alice", "x"), "request id"},
		{"too long", nil, "a", prompt10(rid, "alice", "alice", big), "longer than"},
		{"empty", nil, "a", prompt10(rid, "alice", "alice", "  "), "empty"},
		{"unknown field", nil, "a", `{"op":"prompt","channel":"home","id":"abcde","to":"alice","name":"alice","text":"x","more":1}`, "payload is"},
		{"trailing", nil, "a", prompt10(rid, "alice", "alice", "x") + ` {}`, "payload is"},
		{"bad op", nil, "a", `{"op":"x","channel":"home","id":"abcde"}`, "not prompt or note"},
		{"bad anchor", nil, "a", prompt10(rid, "Alice!", "alice", "x"), "no anchor"},
		{"no account", nil, "a", prompt10(rid, "bob", "bob", "x"), "no single Mattermost account"},
		{"ambiguous", func(r *rig) { r.acc.owner = map[string]string{} }, "a", prompt10(rid, "alice", "alice", "x"), "no single Mattermost account"},
		{"no record", func(r *rig) { r.acc.rec = nil }, "a", prompt10(rid, "alice", "alice", "x"), "name is not"},
		{"name mismatch", nil, "a", prompt10(rid, "alice", "mallory", "x"), "name is not"},
		{"anchor differs", func(r *rig) { r.acc.anchor[acct] = "bob" }, "a", prompt10(rid, "alice", "alice", "x"), "not proven"},
		{"piece 4 no admin token", func(r *rig) { r.sw[inbound.ProofPiece] = true; r.h.Users = nil }, "a", prompt10(rid, "alice", "alice", "x"), "not proven"},
		{"piece 4 wrong auth", func(r *rig) { r.sw[inbound.ProofPiece] = true; r.users.u.AuthData = "sub2" }, "a", prompt10(rid, "alice", "alice", "x"), "not proven"},
		{"piece 4 no proof", func(r *rig) { r.sw[inbound.ProofPiece] = true; r.acc.proof = nil }, "a", prompt10(rid, "alice", "alice", "x"), "not proven"},
		{"piece 4 bot", func(r *rig) { r.sw[inbound.ProofPiece] = true; r.users.u.IsBot = true }, "a", prompt10(rid, "alice", "alice", "x"), "not proven"},
		{"piece 4 deactivated", func(r *rig) { r.sw[inbound.ProofPiece] = true; r.users.u.Deactivated = true }, "a", prompt10(rid, "alice", "alice", "x"), "not proven"},
		{"piece 4 guest", func(r *rig) { r.sw[inbound.ProofPiece] = true; r.users.u.Guest = true }, "a", prompt10(rid, "alice", "alice", "x"), "not proven"},
		{"piece 4 read fails", func(r *rig) { r.sw[inbound.ProofPiece] = true; r.users.err = errors.New("down") }, "a", prompt10(rid, "alice", "alice", "x"), "not read"},
		{"prompt with close", nil, "a", `{"op":"prompt","channel":"home","id":"abcde","to":"alice","name":"alice","text":"x","close":true}`, "no close"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := newRig(t)
			if tc.mut != nil {
				tc.mut(r)
			}
			_, err := r.do(tc.agent, tc.payload)
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error %v, want %q", err, tc.want)
			}
			if len(r.bridge.created) != 0 || len(r.h.open) != 0 {
				t.Fatalf("something was posted or kept: %+v %d", r.bridge.created, len(r.h.open))
			}
		})
	}
}

func TestRT613PlanBPiece10PromptWithProofOn(t *testing.T) {
	r := newRig(t)
	r.sw[inbound.ProofPiece] = true
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
}

func TestRT613PlanBPiece10MentionsAreDefused(t *testing.T) {
	r := newRig(t)
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "@channel and @bob")); err != nil {
		t.Fatal(err)
	}
	got := r.bridge.created[0].text
	if strings.Contains(got, "@c") || strings.Contains(got, "@b") || !strings.Contains(got, "@​channel") {
		t.Fatalf("mentions: %q", got)
	}
}

func TestRT613PlanBPiece10DuplicateAndLimits(t *testing.T) {
	r := newRig(t)
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err == nil || !strings.Contains(err.Error(), "already open") {
		t.Fatalf("duplicate: %v", err)
	}
	// the same id of another agent is another prompt
	if _, err := r.do("b", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
	// a 17th of one agent drops its oldest
	ids := []string{"bbbba", "bbbbb", "bbbbc", "bbbbd", "bbbbe", "bbbbf", "bbbbg", "bbbbh", "bbbbi", "bbbbj", "bbbbk", "bbbbm", "bbbbn", "bbbbo", "bbbbp", "bbbbq", "bbbbr"}
	for _, id := range ids {
		r.now = r.now.Add(time.Second)
		if _, err := r.do("c", prompt10(id, "alice", "alice", "x")); err != nil {
			t.Fatal(err)
		}
	}
	n := 0
	for k := range r.h.open {
		if k.agent == "c" {
			n++
		}
	}
	if n != PerAgent {
		t.Fatalf("agent c has %d open, want %d", n, PerAgent)
	}
	if _, ok := r.h.open[key{"c", "bbbba"}]; ok {
		t.Fatal("the oldest stayed")
	}
	// 256 in all, then a refusal
	for i := 0; len(r.h.open) < Total; i++ {
		r.h.open[key{fmt.Sprintf("x%d", i/PerAgent), fmt.Sprintf("id%d", i)}] = &prompt{opened: r.now, home: "home"}
	}
	if _, err := r.do("zz", prompt10("cccca", "alice", "alice", "x")); err == nil || !strings.Contains(err.Error(), "too many open prompts") {
		t.Fatalf("full: %v", err)
	}
}

func TestRT613PlanBPiece10NoteCloseAndUnknown(t *testing.T) {
	r := newRig(t)
	if _, err := r.do("a", note10(rid, "hi", false)); err == nil || !strings.Contains(err.Error(), "no open prompt") {
		t.Fatalf("unknown: %v", err)
	}
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
	res, err := r.do("a", note10(rid, "seen @here", false))
	if err != nil || res["posted"] != true {
		t.Fatalf("%v %v", res, err)
	}
	c := r.bridge.created[1]
	if c.root != "bridge-post1" || c.dm != "dm-"+acct || strings.Contains(c.text, "@h") {
		t.Fatalf("note: %+v", c)
	}
	// another agent's note, another home's note, too-long text: refused
	if _, err := r.do("b", note10(rid, "hi", false)); err == nil {
		t.Fatal("another agent's note")
	}
	other := strings.Replace(note10(rid, "hi", false), `"home"`, `"elsewhere"`, 1)
	if _, err := r.do("a", other); err == nil {
		t.Fatal("another home's note")
	}
	if _, err := r.do("a", note10(rid, strings.Repeat("x", MaxNoteText+1), false)); err == nil {
		t.Fatal("a long note")
	}
	// close forgets, also when the post fails
	r.bridge.failMake = errors.New("mm down")
	if _, err := r.do("a", note10(rid, "bye", true)); err == nil {
		t.Fatal("a failing post")
	}
	if len(r.h.open) != 0 {
		t.Fatal("close did not forget")
	}
	r.bridge.failMake = nil
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
	if _, err := r.do("a", note10(rid, "bye", true)); err != nil || len(r.h.open) != 0 {
		t.Fatalf("close: %v", err)
	}
}

func TestRT613PlanBPiece10ExpiryAfter24Hours(t *testing.T) {
	r := newRig(t)
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatal(err)
	}
	r.now = r.now.Add(Lifetime - time.Second)
	if _, err := r.do("a", note10(rid, "still", false)); err != nil {
		t.Fatal(err)
	}
	r.now = r.now.Add(time.Second)
	if _, err := r.do("a", note10(rid, "late", false)); err == nil {
		t.Fatal("an expired prompt takes a note")
	}
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err != nil {
		t.Fatalf("the id is free again: %v", err)
	}
}

func TestRT613PlanBPiece10PostFailureForgets(t *testing.T) {
	r := newRig(t)
	r.bridge.failMake = errors.New("mm down")
	if _, err := r.do("a", prompt10(rid, "alice", "alice", "x")); err == nil {
		t.Fatal("no error")
	}
	if len(r.h.open) != 0 {
		t.Fatal("kept")
	}
}
