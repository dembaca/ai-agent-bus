package admin

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"sync/atomic"
	"testing"

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
		rep.Next != "set AUTH_DIRECTORY_IMPORTED=true and restart" {
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
	code, body, _ = e.do(t, "POST", "/v1/directory/pending/nobody/confirm", e.admin, "")
	want(t, "confirm nobody", code, 404, body)
	code, body, _ = e.do(t, "POST", "/v1/directory/pending/bob/confirm", e.admin, "")
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
