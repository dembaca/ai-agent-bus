package mattermost

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// server answers the few REST routes the client uses, as Mattermost does:
// auth_service and auth_data only for an admin's token.
func server(t *testing.T, adminToken bool) *httptest.Server {
	t.Helper()
	users := map[string]map[string]any{
		"alice": {"id": "u1", "username": "alice", "roles": "system_user", "auth_service": "openid", "auth_data": "sub-1"},
		"gus":   {"id": "u2", "username": "gus", "roles": "system_guest"},
		"bot":   {"id": "u3", "username": "bot", "roles": "system_user", "is_bot": true},
		"gone":  {"id": "u4", "username": "gone", "roles": "system_user", "delete_at": 1},
	}
	members := map[string]bool{"c-home/u1": true}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer tok" {
			http.Error(w, "", http.StatusUnauthorized)
			return
		}
		send := func(v any) { _ = json.NewEncoder(w).Encode(v) }
		p := r.URL.Path
		switch {
		case p == "/api/v4/users/me":
			roles := "system_user"
			if adminToken {
				roles = "system_user system_admin"
			}
			send(map[string]any{"id": "me", "roles": roles})
		case strings.HasPrefix(p, "/api/v4/users/username/"):
			u, ok := users[strings.TrimPrefix(p, "/api/v4/users/username/")]
			if !ok {
				http.NotFound(w, r)
				return
			}
			c := map[string]any{}
			for k, v := range u {
				c[k] = v
			}
			if !adminToken {
				c["auth_service"], c["auth_data"] = "", ""
			}
			send(c)
		case p == "/api/v4/teams/name/lab/channels/name/home":
			send(map[string]any{"id": "c-home"})
		case p == "/api/v4/teams/name/lab/channels/name/other":
			send(map[string]any{"id": "c-other"})
		case strings.HasPrefix(p, "/api/v4/channels/"):
			parts := strings.Split(strings.TrimPrefix(p, "/api/v4/channels/"), "/")
			if len(parts) == 3 && members[parts[0]+"/"+parts[2]] {
				send(map[string]any{"channel_id": parts[0], "user_id": parts[2]})
				return
			}
			http.NotFound(w, r)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestUsersAsTheTokenCheckNeedsThem(t *testing.T) {
	ctx := context.Background()
	c, err := New(ctx, server(t, true).URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	u, err := c.User(ctx, "alice")
	if err != nil || !u.AuthKnown || u.AuthService != "openid" || u.AuthData != "sub-1" || u.Guest || u.Bot {
		t.Fatalf("alice: %+v, %v", u, err)
	}
	for name, want := range map[string]func(token.User) bool{
		"gus":  func(u token.User) bool { return u.Guest },
		"bot":  func(u token.User) bool { return u.Bot },
		"gone": func(u token.User) bool { return u.Deactivated },
	} {
		if u, err := c.User(ctx, name); err != nil || !want(u) {
			t.Errorf("%s: %+v, %v", name, u, err)
		}
	}
	if _, err := c.User(ctx, "nobody"); !errors.Is(err, token.ErrNoSuchUser) {
		t.Fatalf("nobody: %v", err)
	}
}

func TestWithoutAdminRightsHowAccountsLogInIsUnknown(t *testing.T) {
	ctx := context.Background()
	c, err := New(ctx, server(t, false).URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	if c.AuthVisible() {
		t.Fatal("auth visible without admin rights")
	}
	if u, err := c.User(ctx, "alice"); err != nil || u.AuthKnown {
		t.Fatalf("alice: %+v, %v", u, err)
	}
}

func TestMembership(t *testing.T) {
	ctx := context.Background()
	c, err := New(ctx, server(t, true).URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		channel, user string
		want          bool
	}{
		{"home", "alice", true},
		{"other", "alice", false},
		{"missing", "alice", false},
		{"home", "nobody", false},
	} {
		got, err := c.Member(ctx, tc.channel, tc.user)
		if err != nil || got != tc.want {
			t.Errorf("%s in %s: %v, %v", tc.user, tc.channel, got, err)
		}
	}
}

func TestABadTokenOrURLStopsTheStart(t *testing.T) {
	ctx := context.Background()
	srv := server(t, true)
	if _, err := New(ctx, srv.URL, "lab", "wrong"); err == nil {
		t.Error("wrong token accepted")
	}
	if _, err := New(ctx, "https://user:pw@chat.example", "lab", "tok"); err == nil {
		t.Error("URL with user info accepted")
	}
}
