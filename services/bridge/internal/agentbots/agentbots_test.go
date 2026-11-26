package agentbots

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/mattermost"
)

// fakeMM is the part of Mattermost's API the bot management and a bot's
// own client use (RT-6.44).
type fakeMM struct {
	mu      sync.Mutex
	srv     *httptest.Server
	users   map[string]map[string]any // by id: id, username, is_bot, roles
	bots    map[string]map[string]any // by user id: description, owner_id
	tokens  map[string][]map[string]any
	byToken map[string]string // token -> user id
	members map[string][]string
	created int
	revoked []string
	posts   []map[string]any
	seq     int
	// reactions by "user/post/emoji"; removedBy the callers of each removal
	reactions map[string]bool
	removedBy []string
}

func newFake(t *testing.T) *fakeMM {
	f := &fakeMM{users: map[string]map[string]any{}, bots: map[string]map[string]any{},
		tokens: map[string][]map[string]any{}, byToken: map[string]string{}, members: map[string][]string{},
		reactions: map[string]bool{}}
	f.users["admin"] = map[string]any{"id": "admin", "username": "bus-admin", "is_bot": true, "roles": "system_user system_admin"}
	f.users["poster"] = map[string]any{"id": "poster", "username": "bridge", "is_bot": true, "roles": "system_user"}
	f.users["alice"] = map[string]any{"id": "alice", "username": "alice.x", "roles": "system_user"}
	f.users["bob"] = map[string]any{"id": "bob", "username": "bob", "roles": "system_user"}
	f.byToken["admin-token"], f.byToken["post-token"] = "admin", "poster"
	f.srv = httptest.NewServer(http.HandlerFunc(f.serve))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeMM) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	caller := f.byToken[strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")]
	if caller == "" {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	p := strings.Split(strings.TrimPrefix(r.URL.Path, "/api/v4/"), "/")
	out := func(v any) { _ = json.NewEncoder(w).Encode(v) }
	key := r.Method + " " + strings.Join(p, "/")
	switch {
	case key == "GET users/me":
		out(f.users[caller])
	case r.Method == "GET" && len(p) == 2 && p[0] == "users":
		if u, ok := f.users[p[1]]; ok {
			out(u)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	case key == "POST reactions":
		f.reactions[caller+"/"+body["post_id"].(string)+"/"+body["emoji_name"].(string)] = true
		out(map[string]any{})
	case r.Method == "DELETE" && len(p) == 6 && p[0] == "users" && p[2] == "posts" && p[4] == "reactions":
		if p[1] != caller && !strings.Contains(f.users[caller]["roles"].(string), "system_admin") {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		k := p[1] + "/" + p[3] + "/" + p[5]
		if !f.reactions[k] {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		delete(f.reactions, k)
		f.removedBy = append(f.removedBy, caller)
		out(map[string]any{})
	case r.Method == "GET" && len(p) == 3 && p[0] == "users" && p[1] == "username":
		for _, u := range f.users {
			if u["username"] == p[2] {
				out(u)
				return
			}
		}
		w.WriteHeader(http.StatusNotFound)
	case key == "POST bots":
		f.seq++
		id := fmt.Sprintf("bot%d", f.seq)
		f.created++
		f.users[id] = map[string]any{"id": id, "username": body["username"], "is_bot": true, "roles": "system_user"}
		f.bots[id] = map[string]any{"user_id": id, "username": body["username"], "description": body["description"], "owner_id": caller}
		out(f.bots[id])
	case r.Method == "GET" && len(p) == 2 && p[0] == "bots":
		if b, ok := f.bots[p[1]]; ok {
			out(b)
			return
		}
		w.WriteHeader(http.StatusNotFound)
	case r.Method == "POST" && len(p) == 4 && p[0] == "bots" && p[2] == "assign":
		f.bots[p[1]]["owner_id"] = p[3]
		out(f.bots[p[1]])
	case key == "POST users/tokens/revoke":
		id := body["token_id"].(string)
		f.revoked = append(f.revoked, id)
		for _, ts := range f.tokens {
			for _, t := range ts {
				if t["id"] == id {
					t["is_active"] = false
					delete(f.byToken, t["token"].(string))
				}
			}
		}
	case len(p) == 3 && p[0] == "users" && p[2] == "tokens" && r.Method == "GET":
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		per, _ := strconv.Atoi(r.URL.Query().Get("per_page"))
		if per == 0 {
			per = 60
		}
		list := []map[string]any{}
		for i, t := range f.tokens[p[1]] {
			if i >= page*per && i < (page+1)*per {
				list = append(list, map[string]any{"id": t["id"], "description": t["description"], "is_active": t["is_active"]})
			}
		}
		out(list)
	case len(p) == 3 && p[0] == "users" && p[2] == "tokens" && r.Method == "POST":
		f.seq++
		tok := map[string]any{"id": fmt.Sprintf("tid%d", f.seq), "token": fmt.Sprintf("tok%d", f.seq),
			"description": body["description"], "is_active": true}
		f.tokens[p[1]] = append(f.tokens[p[1]], tok)
		f.byToken[tok["token"].(string)] = p[1]
		out(tok)
	case key == "GET teams/name/team":
		out(map[string]any{"id": "tid"})
	case key == "POST teams/tid/members":
		out(map[string]any{})
	case r.Method == "GET" && len(p) == 5 && p[2] == "channels" && p[3] == "name":
		out(map[string]any{"id": "c-" + p[4], "type": "O"})
	case r.Method == "POST" && len(p) == 3 && p[0] == "channels" && p[2] == "members":
		f.members[p[1]] = append(f.members[p[1]], body["user_id"].(string))
		out(map[string]any{})
	case key == "PUT users/me/preferences", key == "POST channels/members/me/view":
		out(map[string]any{})
	case key == "POST channels/direct":
		out(map[string]any{"id": "dm", "type": "D"})
	case key == "POST posts":
		body["user_id"] = caller
		f.posts = append(f.posts, body)
		out(map[string]any{"id": fmt.Sprintf("p%d", len(f.posts))})
	default:
		w.WriteHeader(http.StatusNotImplemented)
	}
}

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// owners stands for the login service's account records: anchor to user id.
type owners map[string]string

func (o owners) Owner(anchor string) string { return o[anchor] }

// aliceRecord binds alice's anchor to her account; her username is alice.x
// in Mattermost too, but nothing reads that.
func aliceRecord() owners { return owners{"alice.x": "alice"} }

func newBots(t *testing.T, f *fakeMM, snap *presence.Snapshot) *Bots {
	t.Helper()
	return newBotsOf(t, f, snap, aliceRecord())
}

func newBotsOf(t *testing.T, f *fakeMM, snap *presence.Snapshot, o Owners) *Bots {
	t.Helper()
	adm, err := mattermost.New(mattermost.Config{URL: f.srv.URL, Token: "admin-token", Team: "team",
		HideBotDM: true, TokenDescription: TokenDescription}, quiet())
	if err != nil {
		t.Fatal(err)
	}
	b := &Bots{Admin: adm, Presence: snap, Owners: o, Prefix: "bb-", URL: f.srv.URL, Log: quiet()}
	if err := b.Check(context.Background(), "post-token"); err != nil {
		t.Fatal(err)
	}
	return b
}

func aliceAgent() *presence.Snapshot {
	s := presence.New()
	s.Observed("agent-a", "alice~x", "", presence.Online, time.Now())
	return s
}

// RT-6.44: the bot is created once, owned by the presence owner, joined to
// the home, and posts with a token of its own; after a restart the bridge
// finds it, mints a new token and revokes the earlier one.
func TestABotIsCreatedOnceAndFoundAfterARestart(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	b := newBots(t, f, aliceAgent())
	s := b.Bot(ctx, "agent-a", "home")
	if s == nil {
		t.Fatal("no bot")
	}
	if b.Bot(ctx, "agent-a", "home") != s {
		t.Fatal("a second use set the bot up again")
	}
	if _, err := s.Create(ctx, "c-home", "hi", "", map[string]any{"x_nats_id": "n1:home:1"}); err != nil {
		t.Fatal(err)
	}
	f.mu.Lock()
	bot := f.bots["bot1"]
	if f.created != 1 || bot["username"] != "bb-agent-a" || bot["owner_id"] != "alice" ||
		!strings.HasPrefix(bot["description"].(string), Marker) {
		t.Fatalf("created %d: %v", f.created, bot)
	}
	if f.posts[0]["user_id"] != "bot1" {
		t.Fatalf("posted as %v", f.posts[0]["user_id"])
	}
	first := f.tokens["bot1"][0]["id"]
	f.mu.Unlock()

	restarted := newBots(t, f, aliceAgent())
	if restarted.Bot(ctx, "agent-a", "home") == nil {
		t.Fatal("the bot was not found after a restart")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.created != 1 {
		t.Fatalf("created again: %d", f.created)
	}
	if len(f.revoked) != 1 || f.revoked[0] != first || len(f.tokens["bot1"]) != 2 {
		t.Fatalf("revoked %v, tokens %v", f.revoked, f.tokens["bot1"])
	}
}

// RT-6.44: a token of the bot that the bridge did not mint is kept.
func TestOnlyTheBridgesEarlierTokensAreRevoked(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	newBots(t, f, aliceAgent()).Bot(ctx, "agent-a", "home")
	f.mu.Lock()
	f.tokens["bot1"] = append(f.tokens["bot1"], map[string]any{"id": "other", "token": "x", "description": "someone's", "is_active": true})
	f.mu.Unlock()
	newBots(t, f, aliceAgent()).Bot(ctx, "agent-a", "home")
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.revoked) != 1 || f.revoked[0] == "other" {
		t.Fatalf("revoked %v", f.revoked)
	}
}

// RT-6.44: the bot joins each home once, on its first use there.
func TestTheBotJoinsEachHomeOnce(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	b := newBots(t, f, aliceAgent())
	for _, h := range []string{"home", "home", "other"} {
		if b.Bot(ctx, "agent-a", h) == nil {
			t.Fatal("no bot")
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.members["c-home"]) != 1 || len(f.members["c-other"]) != 1 || len(f.tokens["bot1"]) != 1 {
		t.Fatalf("members %v, tokens %v", f.members, f.tokens["bot1"])
	}
}

// RT-6.44: a bot of that name the bridge did not make, or that another
// person owns, is never posted with: the bridge bot posts instead.
func TestAForeignBotOfTheSameNameGivesTheFallback(t *testing.T) {
	ctx := context.Background()
	for _, tc := range []struct {
		name string
		bot  map[string]any // nil: a person's account
	}{
		{"unmarked", map[string]any{"description": "someone's bot", "owner_id": "alice"}},
		{"another agent's mark", map[string]any{"description": Marker + "agent-ab", "owner_id": "alice"}},
		{"the mark with more after it", map[string]any{"description": Marker + "agent-a and more", "owner_id": "alice"}},
		{"another owner", map[string]any{"description": Marker + "agent-a", "owner_id": "bob"}},
		{"a person", nil},
	} {
		f := newFake(t)
		f.users["x"] = map[string]any{"id": "x", "username": "bb-agent-a", "is_bot": tc.bot != nil}
		if tc.bot != nil {
			f.bots["x"] = tc.bot
		}
		b := newBots(t, f, aliceAgent())
		if b.Bot(ctx, "agent-a", "home") != nil {
			t.Errorf("%s: posted with it", tc.name)
		}
		if f.created != 0 || len(f.tokens["x"]) != 0 {
			t.Errorf("%s: created %d, tokens %v", tc.name, f.created, f.tokens["x"])
		}
	}
}

// RT-6.44: without a presence there is no owner to bind: the bridge bot
// posts, and the bot is made once the presence is there.
func TestUnknownPresenceGivesTheFallback(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	snap := presence.New()
	snap.Observed("agent-n", "-", "", presence.Online, time.Now())
	b := newBots(t, f, snap)
	if b.Bot(ctx, "agent-a", "home") != nil || b.Bot(ctx, "agent-n", "home") != nil || f.created != 0 {
		t.Fatal("a bot without a known owner")
	}
	snap.Observed("agent-a", "alice~x", "", presence.Online, time.Now())
	if b.Bot(ctx, "agent-a", "home") == nil {
		t.Fatal("no bot once the presence is known")
	}
}

// RT-6.44: a failure falls back and is not tried again within the backoff;
// Forget does the same after Mattermost failed a post.
func TestAFailureBacksOffPerName(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	now := time.Unix(1000, 0)
	b := newBots(t, f, aliceAgent())
	b.Now = func() time.Time { return now }
	delete(f.users, "alice") // the owner cannot be resolved
	if b.Bot(ctx, "agent-a", "home") != nil {
		t.Fatal("a bot without its owner")
	}
	f.users["alice"] = map[string]any{"id": "alice", "username": "alice.x"}
	if b.Bot(ctx, "agent-a", "home") != nil {
		t.Fatal("tried again within the backoff")
	}
	now = now.Add(DefaultBackoff)
	if b.Bot(ctx, "agent-a", "home") == nil {
		t.Fatal("not tried again after the backoff")
	}
	b.Forget("agent-a")
	if b.Bot(ctx, "agent-a", "home") != nil {
		t.Fatal("a forgotten bot was used within the backoff")
	}
}

// RT-6.44: many posts of a new agent at once make one bot.
func TestOneCreationPerNameAtATime(t *testing.T) {
	f := newFake(t)
	b := newBots(t, f, aliceAgent())
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); b.Bot(context.Background(), "agent-a", "home") }()
	}
	wg.Wait()
	if f.created != 1 || len(f.tokens["bot1"]) != 1 {
		t.Fatalf("created %d, tokens %v", f.created, f.tokens["bot1"])
	}
}

// RT-6.44: the start refuses an admin account that is no bot with
// system_admin, and a posting bot with system_admin.
func TestTheStartChecks(t *testing.T) {
	for _, tc := range []struct {
		name         string
		admin, poser string // roles
		adminBot     bool
		ok           bool
	}{
		{"as configured", "system_user system_admin", "system_user", true, true},
		{"the admin is no bot", "system_user system_admin", "system_user", false, false},
		{"the admin has no system_admin", "system_user", "system_user", true, false},
		{"the posting bot has system_admin", "system_user system_admin", "system_user system_admin", true, false},
		{"the posting bot may post everywhere", "system_user system_admin", "system_user system_post_all", true, true},
		{"the posting bot has system_manager", "system_user system_admin", "system_user system_manager", true, false},
		{"the posting bot has system_user_manager", "system_user system_admin", "system_user system_user_manager", true, false},
		{"the posting bot has system_read_only_admin", "system_user system_admin", "system_user system_read_only_admin", true, false},
		{"the posting bot has a custom role", "system_user system_admin", "system_user custom_role", true, false},
	} {
		f := newFake(t)
		f.users["admin"]["roles"], f.users["admin"]["is_bot"] = tc.admin, tc.adminBot
		f.users["poster"]["roles"] = tc.poser
		adm, err := mattermost.New(mattermost.Config{URL: f.srv.URL, Token: "admin-token", Team: "team"}, quiet())
		if err != nil {
			t.Fatal(err)
		}
		err = (&Bots{Admin: adm}).Check(context.Background(), "post-token")
		if (err == nil) != tc.ok {
			t.Errorf("%s: %v", tc.name, err)
		}
	}
}

// RT-6.44: the owner is the account the login service's record binds to the
// presence owner's anchor, never whoever holds that anchor as a username.
func TestTheOwnerComesFromTheAccountRecordNotTheUsername(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	f.users["mallory"] = map[string]any{"id": "mallory", "username": "ali", "roles": "system_user"}
	snap := presence.New()
	snap.Observed("agent-a", "ali", "", presence.Online, time.Now())
	b := newBotsOf(t, f, snap, owners{"ali": "alice"})
	if b.Bot(ctx, "agent-a", "home") == nil {
		t.Fatal("no bot")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if owner := f.bots["bot1"]["owner_id"]; owner != "alice" {
		t.Fatalf("owned by %v", owner)
	}
}

// RT-6.44: no record for the anchor gives the bridge bot, never a lookup by
// username; the bot is made once the record is there.
func TestNoAccountRecordGivesTheFallback(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	o := owners{}
	b := newBotsOf(t, f, aliceAgent(), o)
	if b.Bot(ctx, "agent-a", "home") != nil || f.created != 0 {
		t.Fatal("a bot without an account record")
	}
	o["alice.x"] = "alice"
	if b.Bot(ctx, "agent-a", "home") == nil {
		t.Fatal("no bot once the record is there")
	}
	if (&Bots{Admin: b.Admin, Presence: aliceAgent(), Prefix: "bb-", Log: quiet()}).Bot(ctx, "agent-a", "home") != nil {
		t.Fatal("a bot without the account records")
	}
}

// RT-6.44: when the record moves the anchor to another account, the bot
// bound to the earlier one is not used any more, now or after a restart.
func TestARecordChangeStopsTheBot(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	o := aliceRecord()
	b := newBotsOf(t, f, aliceAgent(), o)
	if b.Bot(ctx, "agent-a", "home") == nil {
		t.Fatal("no bot")
	}
	o["alice.x"] = "bob"
	if b.Bot(ctx, "agent-a", "home") != nil {
		t.Fatal("the bot of the earlier owner was used")
	}
	if newBotsOf(t, f, aliceAgent(), owners{"alice.x": "bob"}).Bot(ctx, "agent-a", "home") != nil {
		t.Fatal("the bot of the earlier owner was used after a restart")
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.created != 1 || f.bots["bot1"]["owner_id"] != "alice" {
		t.Fatalf("created %d, owner %v", f.created, f.bots["bot1"]["owner_id"])
	}
}

// RT-6.44: a restart revokes all the bridge's earlier tokens of the bot,
// beyond the first page of the list.
func TestEveryPageOfEarlierTokensIsRevoked(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	newBots(t, f, aliceAgent()).Bot(ctx, "agent-a", "home")
	f.mu.Lock()
	for i := range 450 {
		f.tokens["bot1"] = append(f.tokens["bot1"], map[string]any{"id": fmt.Sprintf("old%d", i),
			"token": fmt.Sprintf("old%d", i), "description": TokenDescription, "is_active": true})
	}
	f.mu.Unlock()
	newBots(t, f, aliceAgent()).Bot(ctx, "agent-a", "home")
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.revoked) != 451 {
		t.Fatalf("revoked %d", len(f.revoked))
	}
}

// RT-6.44: the removal of a bot's reaction goes through its own client while
// it has one; within the backoff, and after a restart without an owner, the
// admin bot removes it as the bot. One gone already is no error.
func TestABotsReactionIsRemovedWhateverStateTheBotIsIn(t *testing.T) {
	f := newFake(t)
	ctx := context.Background()
	b := newBots(t, f, aliceAgent())
	s := b.Bot(ctx, "agent-a", "home")
	for _, post := range []string{"p1", "p2", "p3"} {
		if err := s.React(ctx, post, "eyes"); err != nil {
			t.Fatal(err)
		}
	}
	if err := b.Unreact(ctx, "agent-a", "p1", "eyes"); err != nil {
		t.Fatal(err)
	}
	b.Forget("agent-a") // within the backoff now
	if err := b.Unreact(ctx, "agent-a", "p2", "eyes"); err != nil {
		t.Fatal(err)
	}
	restarted := newBotsOf(t, f, aliceAgent(), owners{})
	if err := restarted.Unreact(ctx, "agent-a", "p3", "eyes"); err != nil {
		t.Fatal(err)
	}
	if err := restarted.Unreact(ctx, "agent-a", "p3", "eyes"); err != nil {
		t.Fatalf("gone already: %v", err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.reactions) != 0 || strings.Join(f.removedBy, " ") != "bot1 admin admin" {
		t.Fatalf("left %v, removed by %v", f.reactions, f.removedBy)
	}
}
