package admin

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/mattermost"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/testrealm"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// fakeMM is a Source that has nothing (handler tests that do no import).
type fakeMM struct{}

func (*fakeMM) TeamChannels(context.Context) ([]mattermost.Channel, error) { return nil, nil }
func (*fakeMM) ChannelMemberIDs(context.Context, string) ([]string, error) { return nil, nil }
func (*fakeMM) Users(context.Context, []string) ([]mattermost.User, error) { return nil, nil }

// mmServer implements the Mattermost routes the import reads. Setting
// failChannel makes that channel's member list fail.
func mmServer(t *testing.T, failChannel *atomic.Value) *httptest.Server {
	t.Helper()
	users := []map[string]any{
		{"id": "u-alice", "username": "alice", "roles": "system_user", "auth_service": "openid", "auth_data": "sub-alice"},
		{"id": "u-carol", "username": "carol", "roles": "system_user", "auth_service": "openid", "auth_data": "sub-carol"},
		{"id": "u-bob", "username": "bob", "roles": "system_user"},
		{"id": "u-bot", "username": "robot", "roles": "system_user", "is_bot": true},
		{"id": "u-gone", "username": "gone", "roles": "system_user", "delete_at": 9, "auth_service": "openid", "auth_data": "sub-gone"},
		{"id": "u-guest", "username": "gus", "roles": "system_guest", "auth_service": "openid", "auth_data": "sub-gus"},
	}
	members := map[string][]string{
		"c-town":  {"u-alice", "u-bob", "u-bot", "u-gone", "u-guest"},
		"c-vault": {"u-carol", "u-alice"},
		"c-bad":   {"u-alice"},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		send := func(v any) { _ = json.NewEncoder(w).Encode(v) }
		switch p := r.URL.Path; {
		case p == "/api/v4/users/me":
			send(map[string]any{"roles": "system_admin"})
		case p == "/api/v4/teams/name/lab":
			send(map[string]any{"id": "t1"})
		case p == "/api/v4/teams/t1/channels":
			send([]map[string]any{
				{"id": "c-town", "name": "town", "type": "O", "creator_id": "u-alice"},
				{"id": "c-bad", "name": "Bad Name", "type": "O", "creator_id": "u-alice"},
				// a direct message the server should never list here: still never imported
				{"id": "c-dm", "name": "u-alice__u-bob", "type": "D", "creator_id": "u-alice"},
			})
		case p == "/api/v4/teams/t1/channels/private":
			send([]map[string]any{{"id": "c-vault", "name": "vault", "type": "P", "creator_id": "u-bot"}})
		case len(p) > 23 && p[:17] == "/api/v4/channels/" && p[len(p)-8:] == "/members":
			id := p[17 : len(p)-8]
			if f, _ := failChannel.Load().(string); f == id {
				http.Error(w, "", http.StatusInternalServerError)
				return
			}
			var out []map[string]any
			for _, m := range members[id] {
				out = append(out, map[string]any{"user_id": m})
			}
			send(out)
		case p == "/api/v4/users/ids":
			var ids []string
			_ = json.NewDecoder(r.Body).Decode(&ids)
			var out []map[string]any
			for _, u := range users {
				if slices.Contains(ids, u["id"].(string)) {
					out = append(out, u)
				}
			}
			send(out)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func importEnv(t *testing.T, binding string) (*env, *mattermost.Client, *atomic.Value, *httptest.Server) {
	t.Helper()
	fail := &atomic.Value{}
	fail.Store("")
	mm := mmServer(t, fail)
	cl, err := mattermost.New(context.Background(), mm.URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	realm := testrealm.New(t)
	store := newStore(t)
	h, err := New(Config{AdminGroup: testrealm.Group, NameBinding: binding}, realm.Verifier(testrealm.NewChat("alice")), store, cl)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	return &env{realm: realm, store: store, srv: srv, admin: realm.Token("alice", nil)}, cl, fail, mm
}

// RT-6.40 steps 6 and 7: the import from Mattermost, once, and pending claims.
func TestImportCreatesOnceThroughHTTP(t *testing.T) {
	e, _, _, _ := importEnv(t, "open")
	code, body, _ := e.do(t, "POST", "/v1/directory/import", e.admin, "")
	want(t, "import", code, 200, body)
	var rep Report
	if err := json.Unmarshal([]byte(body), &rep); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(rep.ChannelsCreated, []string{"town", "vault"}) || len(rep.ChannelsSkipped) != 2 ||
		rep.ChannelsSkipped[0].Name != "Bad Name" || rep.ChannelsSkipped[1].Reason != "not a team channel" || rep.Identities != 2 || rep.PendingAdded != 1 ||
		rep.Next != "set CB_AUTH_DIRECTORY_IMPORTED=true and restart" {
		t.Fatalf("report: %+v", rep)
	}
	reasons := map[string]string{}
	for _, a := range rep.AccountsSkipped {
		reasons[a.Username] = a.Reason
	}
	if reasons["robot"] != "bot" || reasons["gone"] != "deactivated" || reasons["gus"] != "guest" || len(reasons) != 3 {
		t.Fatalf("skipped accounts: %v", reasons)
	}
	town, _, ok, _ := e.store.Channel(ctx(t), "town")
	if !ok || town.Owner != "sub-alice" || !town.Public || !slices.Equal(town.Members, []string{"sub-alice"}) ||
		town.Bindings["mattermost"] != "town" {
		t.Fatalf("town: %+v", town)
	}
	vault, _, _, _ := e.store.Channel(ctx(t), "vault")
	if vault.Owner != "" || vault.Public || !slices.Equal(vault.Members, []string{"sub-alice", "sub-carol"}) {
		t.Fatalf("vault (created by a bot): %+v", vault)
	}
	if done, _ := e.store.Imported(ctx(t)); !done {
		t.Fatal("no mark")
	}
	if id, _, ok, _ := e.store.Identity(ctx(t), "sub-carol"); !ok || id.Anchor != "carol" {
		t.Fatalf("identity: %+v", id)
	}
	// RT-6.39 step 2, #86: an identity the import wrote keeps its anchor at a
	// login after a rename, and a newcomer with its old name gets its own sub.
	a := directory.Anchors{Store: e.store}
	if got, err := a.Anchor(ctx(t), "sub-carol", "carol.renamed"); err != nil || got != "carol" {
		t.Fatalf("the imported identity's anchor moved: %q %v", got, err)
	}
	if got, err := a.Anchor(ctx(t), "sub-newcarol", "carol"); err != nil || got != "sub-newcarol" {
		t.Fatalf("a newcomer took an imported identity's name: %q %v", got, err)
	}
	code, body, _ = e.do(t, "POST", "/v1/directory/import", e.admin, "")
	want(t, "second import", code, 409, body)
}

func TestImportRerunAfterAnInterruption(t *testing.T) {
	e, cl, fail, _ := importEnv(t, "open")
	fail.Store("c-vault")
	if _, err := Import(ctx(t), e.store, cl, "open", token.Identity{}); err == nil {
		t.Fatal("no error")
	}
	if done, _ := e.store.Imported(ctx(t)); done {
		t.Fatal("marked after an interrupted run")
	}
	code, body, _ := e.do(t, "POST", "/v1/directory/import", e.admin, "")
	want(t, "interrupted via HTTP", code, 502, body)
	fail.Store("")
	rep, err := Import(ctx(t), e.store, cl, "open", token.Identity{})
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(rep.ChannelsCreated, []string{"vault"}) || len(rep.ChannelsUpdated) != 0 {
		t.Fatalf("rerun: %+v", rep)
	}
	town, _, _, _ := e.store.Channel(ctx(t), "town")
	if !slices.Equal(town.Members, []string{"sub-alice"}) {
		t.Fatalf("town members: %v", town.Members)
	}
	ps, _ := e.store.ListPending(ctx(t))
	if len(ps) != 1 || !slices.Equal(ps[0].Channels, []string{"town"}) {
		t.Fatalf("pending: %+v", ps)
	}
	if _, err := Import(ctx(t), e.store, cl, "open", token.Identity{}); !errors.Is(err, ErrAlreadyImported) {
		t.Fatalf("third run: %v", err)
	}
}

func TestOpenIDOnlyMakesNoPending(t *testing.T) {
	e, cl, _, _ := importEnv(t, "openid-only")
	rep, err := Import(ctx(t), e.store, cl, "openid-only", token.Identity{})
	if err != nil {
		t.Fatal(err)
	}
	if ps, _ := e.store.ListPending(ctx(t)); len(ps) != 0 || rep.PendingAdded != 0 {
		t.Fatalf("pending: %+v", ps)
	}
	found := false
	for _, a := range rep.AccountsSkipped {
		found = found || a.Username == "bob"
	}
	if !found {
		t.Fatalf("bob not reported: %+v", rep.AccountsSkipped)
	}
}

func TestPendingIsRedeemedOnlyWhenConfirmed(t *testing.T) {
	e, cl, _, _ := importEnv(t, "open")
	if _, err := Import(ctx(t), e.store, cl, "open", token.Identity{}); err != nil {
		t.Fatal(err)
	}
	a := directory.Anchors{Store: e.store}
	if _, err := a.Anchor(ctx(t), "sub-bob", "bob"); err != nil {
		t.Fatal(err)
	}
	if town, _, _, _ := e.store.Channel(ctx(t), "town"); slices.Contains(town.Members, "sub-bob") {
		t.Fatal("an unconfirmed claim was redeemed")
	}
	code, body, _ := e.do(t, "GET", "/v1/directory/pending", e.admin, "")
	want(t, "list", code, 200, body)
	code, body, _ = e.do(t, "POST", "/v1/directory/pending/nobody/confirm", e.admin, "", "If-Match", `"1"`)
	want(t, "confirm nobody", code, 404, body)
	code, body, _ = e.do(t, "POST", "/v1/directory/pending/bob/confirm", e.admin, "", "If-Match", `"`+pendingRev(t, e, "bob")+`"`)
	want(t, "confirm", code, 200, body)
	if _, err := a.Anchor(ctx(t), "sub-bob", "bob"); err != nil {
		t.Fatal(err)
	}
	town, _, _, _ := e.store.Channel(ctx(t), "town")
	if !slices.Contains(town.Members, "sub-bob") {
		t.Fatalf("not redeemed: %+v", town)
	}
	if ps, _ := e.store.ListPending(ctx(t)); len(ps) != 0 {
		t.Fatalf("still pending: %+v", ps)
	}
}

func pendingRev(t *testing.T, e *env, name string) string {
	t.Helper()
	code, body, _ := e.do(t, "GET", "/v1/directory/pending", e.admin, "")
	want(t, "list pending", code, 200, body)
	var ps []struct {
		Name     string `json:"name"`
		Revision uint64 `json:"revision"`
	}
	if err := json.Unmarshal([]byte(body), &ps); err != nil {
		t.Fatal(err)
	}
	for _, p := range ps {
		if p.Name == name && p.Revision > 0 {
			return strconv.FormatUint(p.Revision, 10)
		}
	}
	t.Fatalf("no revision for %s: %s", name, body)
	return ""
}

func TestAConfirmationBindsToTheEntrysRevision(t *testing.T) {
	e, cl, _, _ := importEnv(t, "open")
	if _, err := Import(ctx(t), e.store, cl, "open", token.Identity{}); err != nil {
		t.Fatal(err)
	}
	const path = "/v1/directory/pending/bob/confirm"
	code, body, _ := e.do(t, "POST", path, e.admin, "")
	want(t, "no If-Match", code, 428, body)
	stale := pendingRev(t, e, "bob")
	if err := e.store.AddPending(ctx(t), "bob", "vault"); err != nil {
		t.Fatal(err)
	}
	code, body, _ = e.do(t, "POST", path, e.admin, "", "If-Match", `"`+stale+`"`)
	want(t, "stale", code, 409, body)

	// confirm, then the claim grows: no longer confirmed, not redeemed
	code, body, _ = e.do(t, "POST", path, e.admin, "", "If-Match", `"`+pendingRev(t, e, "bob")+`"`)
	want(t, "confirm", code, 200, body)
	if err := e.store.AddPending(ctx(t), "bob", "zeta"); err != nil {
		t.Fatal(err)
	}
	ps, _ := e.store.ListPending(ctx(t))
	if len(ps) != 1 || ps[0].Confirmed || ps[0].ConfirmedBy != "" {
		t.Fatalf("still confirmed after growing: %+v", ps)
	}
	if _, err := (directory.Anchors{Store: e.store}).Anchor(ctx(t), "sub-bob", "bob"); err != nil {
		t.Fatal(err)
	}
	if town, _, _, _ := e.store.Channel(ctx(t), "town"); slices.Contains(town.Members, "sub-bob") {
		t.Fatal("an entry that grew was redeemed")
	}
}

// fakeSource is an in-memory Source.
type fakeSource struct {
	channels  []mattermost.Channel
	members   map[string][]string
	memberErr map[string]error
	users     []mattermost.User
}

func (f *fakeSource) TeamChannels(context.Context) ([]mattermost.Channel, error) {
	return slices.Clone(f.channels), nil
}
func (f *fakeSource) ChannelMemberIDs(_ context.Context, id string) ([]string, error) {
	if err := f.memberErr[id]; err != nil {
		return nil, err
	}
	return f.members[id], nil
}
func (f *fakeSource) Users(_ context.Context, ids []string) ([]mattermost.User, error) {
	var out []mattermost.User
	for _, u := range f.users {
		if slices.Contains(ids, u.ID) {
			out = append(out, u)
		}
	}
	return out, nil
}

func oidUser(id string) mattermost.User {
	return mattermost.User{ID: id, Username: id, AuthKnown: true, AuthService: token.OpenIDService, AuthData: "sub-" + id}
}

func ownerAfterImport(t *testing.T, creator mattermost.User, members []string) (string, bool) {
	t.Helper()
	src := &fakeSource{
		channels: []mattermost.Channel{{ID: "c1", Name: "town", Type: "O", CreatorID: creator.ID}},
		members:  map[string][]string{"c1": members},
		users:    []mattermost.User{oidUser("ann"), creator},
	}
	store := newStore(t)
	if _, err := Import(ctx(t), store, src, "open", token.Identity{}); err != nil {
		t.Fatal(err)
	}
	c, _, ok, _ := store.Channel(ctx(t), "town")
	return c.Owner, ok
}

func TestTheCreatorOwnsOnlyWhileAnEligibleMember(t *testing.T) {
	cr := oidUser("cat")
	if o, _ := ownerAfterImport(t, cr, []string{"ann", "cat"}); o != "sub-cat" {
		t.Fatalf("eligible creator: owner %q", o)
	}
	if o, ok := ownerAfterImport(t, cr, []string{"ann"}); !ok || o != "" {
		t.Fatalf("creator removed from the channel: owner %q", o)
	}
	bot := oidUser("cat")
	bot.IsBot = true
	if o, _ := ownerAfterImport(t, bot, []string{"ann", "cat"}); o != "" {
		t.Fatalf("bot creator: owner %q", o)
	}
	gone := oidUser("cat")
	gone.Deactivated = true
	if o, _ := ownerAfterImport(t, gone, []string{"ann", "cat"}); o != "" {
		t.Fatalf("deactivated creator: owner %q", o)
	}
	guest := oidUser("cat")
	guest.Guest = true
	if o, _ := ownerAfterImport(t, guest, []string{"ann", "cat"}); o != "" {
		t.Fatalf("guest creator: owner %q", o)
	}
	pw := oidUser("cat")
	pw.AuthService, pw.AuthData = "", ""
	if o, _ := ownerAfterImport(t, pw, []string{"ann", "cat"}); o != "" {
		t.Fatalf("password creator: owner %q", o)
	}
}

func TestAChannelOverMaxMembersIsSkippedAndTheImportCompletes(t *testing.T) {
	src := &fakeSource{
		channels: []mattermost.Channel{{ID: "big", Name: "big", Type: "O"}, {ID: "c1", Name: "town", Type: "O"}},
		members:  map[string][]string{"c1": {"ann"}},
		users:    []mattermost.User{oidUser("ann")},
	}
	for i := range directory.MaxMembers + 1 {
		u := oidUser("u" + strconv.Itoa(i))
		src.users = append(src.users, u)
		src.members["big"] = append(src.members["big"], u.ID)
	}
	store := newStore(t)
	rep, err := Import(ctx(t), store, src, "open", token.Identity{})
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.ChannelsSkipped) != 1 || rep.ChannelsSkipped[0].Name != "big" ||
		rep.ChannelsSkipped[0].Reason != "more than 1000 members" || !slices.Equal(rep.ChannelsCreated, []string{"town"}) {
		t.Fatalf("report: %+v", rep)
	}
	if done, _ := store.Imported(ctx(t)); !done {
		t.Fatal("no mark")
	}
}

type marksDuringImport struct {
	*fakeSource
	store *directory.Store
}

func (m *marksDuringImport) TeamChannels(c context.Context) ([]mattermost.Channel, error) {
	if err := m.store.MarkImported(c, time.Now()); err != nil {
		return nil, err
	}
	return m.fakeSource.TeamChannels(c)
}

func TestALostMarkIsAnAlreadyImported(t *testing.T) {
	src := &fakeSource{}
	store := newStore(t)
	if _, err := Import(ctx(t), store, &marksDuringImport{src, store}, "open", token.Identity{}); !errors.Is(err, ErrAlreadyImported) {
		t.Fatalf("import: %v", err)
	}
	m := &marksDuringImport{fakeSource: src}
	e2 := setupWith(t, Config{}, testrealm.NewChat("alice"), m)
	m.store = e2.store
	code, body, _ := e2.do(t, "POST", "/v1/directory/import", e2.admin, "")
	want(t, "http", code, 409, body)
}

// A token that cannot see how accounts log in aborts the import before it
// writes or marks; a run with a system admin's token completes.
func TestAnImportWithABlindTokenWritesNothing(t *testing.T) {
	mm := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		send := func(v any) { _ = json.NewEncoder(w).Encode(v) }
		switch p := r.URL.Path; {
		case p == "/api/v4/users/me":
			if r.Header.Get("Authorization") == "Bearer admin-token" {
				send(map[string]any{"roles": "system_admin"})
			} else {
				send(map[string]any{"roles": "system_user"})
			}
		case p == "/api/v4/teams/name/lab":
			send(map[string]any{"id": "t1"})
		case p == "/api/v4/teams/t1/channels":
			send([]map[string]any{{"id": "c1", "name": "town", "type": "O", "creator_id": "u-ann"}})
		case p == "/api/v4/teams/t1/channels/private":
			send([]map[string]any{})
		case p == "/api/v4/channels/c1/members":
			send([]map[string]any{{"user_id": "u-ann"}, {"user_id": "u-pw"}})
		case p == "/api/v4/users/ids":
			send([]map[string]any{
				{"id": "u-ann", "username": "ann", "roles": "system_user", "auth_service": "openid", "auth_data": "sub-ann"},
				{"id": "u-pw", "username": "pw", "roles": "system_user"},
			})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(mm.Close)
	store := newStore(t)
	blind, err := mattermost.New(ctx(t), mm.URL, "lab", "user-token")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Import(ctx(t), store, blind, "open", token.Identity{}); !errors.Is(err, ErrAuthInvisible) {
		t.Fatalf("blind import: %v", err)
	}
	if done, _ := store.Imported(ctx(t)); done {
		t.Fatal("marked")
	}
	if ps, _ := store.ListPending(ctx(t)); len(ps) != 0 {
		t.Fatalf("pending written: %+v", ps)
	}
	if chs, _ := store.ListChannels(ctx(t)); len(chs) != 0 {
		t.Fatalf("channels written: %+v", chs)
	}
	seeing, err := mattermost.New(ctx(t), mm.URL, "lab", "admin-token")
	if err != nil {
		t.Fatal(err)
	}
	rep, err := Import(ctx(t), store, seeing, "open", token.Identity{})
	if err != nil {
		t.Fatal(err)
	}
	if rep.PendingAdded != 1 || !slices.Equal(rep.ChannelsCreated, []string{"town"}) {
		t.Fatalf("report: %+v", rep)
	}
}

// The user cache is shared across channels: a creator known as a member of an
// earlier channel, but no longer a member of the one they created, does not
// own it.
func TestACreatorRemovedFromTheirChannelDoesNotOwnIt(t *testing.T) {
	src := &fakeSource{
		channels: []mattermost.Channel{
			{ID: "c1", Name: "aaa", Type: "O", CreatorID: "ann"},
			{ID: "c2", Name: "board", Type: "P", CreatorID: "cat"},
		},
		members: map[string][]string{"c1": {"ann", "cat"}, "c2": {"ann"}},
		users:   []mattermost.User{oidUser("ann"), oidUser("cat")},
	}
	store := newStore(t)
	if _, err := Import(ctx(t), store, src, "open", token.Identity{}); err != nil {
		t.Fatal(err)
	}
	if c, _, ok, _ := store.Channel(ctx(t), "board"); !ok || c.Owner != "" {
		t.Fatalf("board: owner %q (cat was removed from it)", c.Owner)
	}
	if c, _, _, _ := store.Channel(ctx(t), "aaa"); c.Owner != "sub-ann" {
		t.Fatalf("aaa: owner %q", c.Owner)
	}
}

// A channel that would grow past MaxMembers with the import (members already
// in the directory counted), or whose member list is too long to read, is
// left out and named; the import completes.
func TestAChannelTooBigIsSkippedNotFailing(t *testing.T) {
	store := newStore(t)
	var full []string
	for i := range directory.MaxMembers - 1 {
		full = append(full, fmt.Sprintf("sub-x%04d", i))
	}
	if _, err := store.PutChannel(ctx(t), directory.Channel{Name: "crowd", Members: full}, 0); err != nil {
		t.Fatal(err)
	}
	src := &fakeSource{
		channels: []mattermost.Channel{
			{ID: "c1", Name: "crowd", Type: "O"},
			{ID: "c2", Name: "huge", Type: "O"},
			{ID: "c3", Name: "town", Type: "O"},
		},
		members:   map[string][]string{"c1": {"ann", "cat"}, "c3": {"ann"}},
		memberErr: map[string]error{"c2": mattermost.ErrTooMany},
		users:     []mattermost.User{oidUser("ann"), oidUser("cat")},
	}
	rep, err := Import(ctx(t), store, src, "open", token.Identity{})
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.ChannelsSkipped) != 2 || rep.ChannelsSkipped[0].Name != "crowd" || rep.ChannelsSkipped[1].Name != "huge" {
		t.Fatalf("skipped: %+v", rep.ChannelsSkipped)
	}
	if done, _ := store.Imported(ctx(t)); !done {
		t.Fatal("the import did not complete")
	}
	if c, _, _, _ := store.Channel(ctx(t), "crowd"); len(c.Members) != directory.MaxMembers-1 {
		t.Fatalf("crowd changed: %d members", len(c.Members))
	}
}

// A confirmed claim on a full channel does not refuse the login: the full
// channel stays in the claim, the others are given.
func TestAFullChannelStaysInTheClaimAndTheLoginGoesOn(t *testing.T) {
	store := newStore(t)
	var full []string
	for i := range directory.MaxMembers {
		full = append(full, fmt.Sprintf("sub-x%04d", i))
	}
	for _, c := range []directory.Channel{{Name: "crowd", Members: full}, {Name: "town"}} {
		if _, err := store.PutChannel(ctx(t), c, 0); err != nil {
			t.Fatal(err)
		}
	}
	for _, ch := range []string{"crowd", "town"} {
		if err := store.AddPending(ctx(t), "pat", ch); err != nil {
			t.Fatal(err)
		}
	}
	entries, err := store.PendingEntries(ctx(t))
	if err != nil || len(entries) != 1 {
		t.Fatalf("%+v %v", entries, err)
	}
	if err := store.ConfirmPending(ctx(t), "pat", "sub-admin", entries[0].Revision); err != nil {
		t.Fatal(err)
	}
	if _, err := (directory.Anchors{Store: store}).Anchor(ctx(t), "sub-pat", "pat"); err != nil {
		t.Fatalf("the login was refused: %v", err)
	}
	if c, _, _, _ := store.Channel(ctx(t), "town"); !slices.Contains(c.Members, "sub-pat") {
		t.Fatalf("town not given: %v", c.Members)
	}
	left, _ := store.ListPending(ctx(t))
	if len(left) != 1 || !slices.Equal(left[0].Channels, []string{"crowd"}) {
		t.Fatalf("the claim: %+v", left)
	}
}

// A channel that fills up between the read and the write is skipped with the
// reason of the size check; the retry re-checks the size.
func TestUnionThatGrowsTooBigOnRetryIsTooBig(t *testing.T) {
	store := newStore(t)
	if _, err := store.PutChannel(ctx(t), directory.Channel{Name: "crowd", Members: []string{"sub-a"}}, 0); err != nil {
		t.Fatal(err)
	}
	cur, rev, _, err := store.Channel(ctx(t), "crowd")
	if err != nil {
		t.Fatal(err)
	}
	filled := cur
	for i := range directory.MaxMembers - 1 {
		filled.Members = append(filled.Members, fmt.Sprintf("sub-x%04d", i))
	}
	if _, err := store.PutChannel(ctx(t), filled, rev); err != nil { // the concurrent write
		t.Fatal(err)
	}
	if _, err := unionMembers(ctx(t), store, cur, rev, []string{"sub-new1", "sub-new2"}); !errors.Is(err, errTooBig) {
		t.Fatalf("got %v", err)
	}
	if c, _, _, _ := store.Channel(ctx(t), "crowd"); len(c.Members) != directory.MaxMembers {
		t.Fatalf("crowd changed: %d members", len(c.Members))
	}
}

// Two logins with a full channel in the claim: the second writes nothing.
func TestARedeemWithTheSameFullChannelsWritesNothing(t *testing.T) {
	store := newStore(t)
	var full []string
	for i := range directory.MaxMembers {
		full = append(full, fmt.Sprintf("sub-x%04d", i))
	}
	if _, err := store.PutChannel(ctx(t), directory.Channel{Name: "crowd", Members: full}, 0); err != nil {
		t.Fatal(err)
	}
	if err := store.AddPending(ctx(t), "pat", "crowd"); err != nil {
		t.Fatal(err)
	}
	es, _ := store.PendingEntries(ctx(t))
	if err := store.ConfirmPending(ctx(t), "pat", "sub-admin", es[0].Revision); err != nil {
		t.Fatal(err)
	}
	login := func() directory.PendingEntry {
		if _, err := (directory.Anchors{Store: store}).Anchor(ctx(t), "sub-pat", "pat"); err != nil {
			t.Fatal(err)
		}
		es, _ := store.PendingEntries(ctx(t))
		if len(es) != 1 {
			t.Fatalf("%+v", es)
		}
		return es[0]
	}
	first := login()
	if !slices.Equal(first.WaitingFull, []string{"crowd"}) {
		t.Fatalf("waiting_full: %+v", first)
	}
	if second := login(); second.Revision != first.Revision {
		t.Fatalf("rewritten: revision %d, then %d", first.Revision, second.Revision)
	}
}

func TestDroppingAPendingEntry(t *testing.T) {
	e, cl, _, _ := importEnv(t, "open")
	if _, err := Import(ctx(t), e.store, cl, "open", token.Identity{}); err != nil {
		t.Fatal(err)
	}
	const path = "/v1/directory/pending/bob"
	code, body, _ := e.do(t, "DELETE", path, e.admin, "")
	want(t, "no If-Match", code, 428, body)
	e2 := setup(t, Config{Burst: 100})
	code, body, _ = e2.do(t, "DELETE", path, e2.bob, "", "If-Match", `"1"`)
	want(t, "non-admin", code, 403, body)
	code, body, _ = e.do(t, "DELETE", "/v1/directory/pending/nobody", e.admin, "", "If-Match", `"1"`)
	want(t, "missing", code, 404, body)
	stale := pendingRev(t, e, "bob")
	if err := e.store.AddPending(ctx(t), "bob", "zeta"); err != nil {
		t.Fatal(err)
	}
	code, body, _ = e.do(t, "DELETE", path, e.admin, "", "If-Match", `"`+stale+`"`)
	want(t, "stale", code, 409, body)
	if ps, _ := e.store.ListPending(ctx(t)); len(ps) != 1 {
		t.Fatalf("deleted on a stale revision: %+v", ps)
	}
	code, body, _ = e.do(t, "DELETE", path, e.admin, "", "If-Match", `"`+pendingRev(t, e, "bob")+`"`)
	want(t, "drop", code, 200, body)
	if ps, _ := e.store.ListPending(ctx(t)); len(ps) != 0 {
		t.Fatalf("still pending: %+v", ps)
	}
}

// RT-6.40 step 6: a channel that fills up between the size check and the
// write is left out with nothing of it written, its claims neither.
func TestAChannelThatFillsUpMeanwhileGetsNoClaims(t *testing.T) {
	store := newStore(t)
	if _, err := store.PutChannel(ctx(t), directory.Channel{Name: "crowd", Members: []string{"sub-a"}}, 0); err != nil {
		t.Fatal(err)
	}
	pw := mattermost.User{ID: "pat", Username: "pat", AuthKnown: true}
	src := &fakeSource{
		channels: []mattermost.Channel{{ID: "c1", Name: "crowd", Type: "O"}},
		members:  map[string][]string{"c1": {"ann", "pat"}},
		users:    []mattermost.User{oidUser("ann"), pw},
	}
	beforeUnion = func(name string) { // an owner fills the channel meanwhile
		cur, rev, _, _ := store.Channel(ctx(t), name)
		for i := range directory.MaxMembers - len(cur.Members) {
			cur.Members = append(cur.Members, fmt.Sprintf("sub-x%04d", i))
		}
		if _, err := store.PutChannel(ctx(t), cur, rev); err != nil {
			t.Error(err)
		}
	}
	t.Cleanup(func() { beforeUnion = func(string) {} })
	rep, err := Import(ctx(t), store, src, "open", token.Identity{})
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.ChannelsSkipped) != 1 || rep.ChannelsSkipped[0].Name != "crowd" || rep.PendingAdded != 0 {
		t.Fatalf("report: %+v", rep)
	}
	if ps, _ := store.ListPending(ctx(t)); len(ps) != 0 {
		t.Fatalf("claims written for a channel left out: %+v", ps)
	}
}

// A claim that grows loses its confirmation and what it waited on.
func TestAddPendingClearsWaitingFull(t *testing.T) {
	store := newStore(t)
	var full []string
	for i := range directory.MaxMembers {
		full = append(full, fmt.Sprintf("sub-x%04d", i))
	}
	for _, c := range []directory.Channel{{Name: "crowd", Members: full}, {Name: "town"}} {
		if _, err := store.PutChannel(ctx(t), c, 0); err != nil {
			t.Fatal(err)
		}
	}
	if err := store.AddPending(ctx(t), "pat", "crowd"); err != nil {
		t.Fatal(err)
	}
	es, _ := store.PendingEntries(ctx(t))
	if err := store.ConfirmPending(ctx(t), "pat", "sub-admin", es[0].Revision); err != nil {
		t.Fatal(err)
	}
	if _, err := (directory.Anchors{Store: store}).Anchor(ctx(t), "sub-pat", "pat"); err != nil {
		t.Fatal(err)
	}
	if ps, _ := store.ListPending(ctx(t)); len(ps) != 1 || len(ps[0].WaitingFull) != 1 {
		t.Fatalf("before: %+v", ps)
	}
	if err := store.AddPending(ctx(t), "pat", "town"); err != nil {
		t.Fatal(err)
	}
	ps, _ := store.ListPending(ctx(t))
	if len(ps) != 1 || len(ps[0].WaitingFull) != 0 || ps[0].Confirmed {
		t.Fatalf("after growing: %+v", ps)
	}
}

// The import records each OpenID human's Mattermost username as an account of
// the identity; a name two identities claim goes to the first and the second
// is reported as skipped, without stopping the import (attested trust lists).
func TestImportRecordsMattermostAccountsAndReportsAConflict(t *testing.T) {
	twin := oidUser("twin")
	twin.Username = "ann" // the same username as ann, another sub
	src := &fakeSource{
		channels: []mattermost.Channel{{ID: "c1", Name: "town", Type: "O"}},
		members:  map[string][]string{"c1": {"ann", "twin", "bea"}},
		users:    []mattermost.User{oidUser("ann"), twin, oidUser("bea")},
	}
	store := newStore(t)
	rep, err := Import(ctx(t), store, src, "open", token.Identity{})
	if err != nil {
		t.Fatal(err)
	}
	for _, n := range []string{"ann", "bea"} {
		id, ok, err := store.ByAccount(ctx(t), "mattermost", n)
		if err != nil || !ok || id.Sub != "sub-"+n || id.Accounts["mattermost"] != n {
			t.Fatalf("account %s: %+v %v %v", n, id, ok, err)
		}
	}
	if len(rep.AccountsSkipped) != 1 || rep.AccountsSkipped[0].Username != "ann" ||
		rep.AccountsSkipped[0].Reason != "mattermost name held by another identity" {
		t.Fatalf("skipped accounts: %+v", rep.AccountsSkipped)
	}
	if rep.Identities != 3 {
		t.Fatalf("identities %d, want 3", rep.Identities)
	}
}
