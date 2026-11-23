package mattermost

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
)

func listServer(t *testing.T, posts *int) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		send := func(v any) { _ = json.NewEncoder(w).Encode(v) }
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		if r.Method == http.MethodGet && r.URL.Path != "/api/v4/users/me" && r.URL.Path != "/api/v4/teams/name/lab" && r.URL.Query().Get("per_page") != "200" {
			http.Error(w, "per_page", http.StatusBadRequest)
			return
		}
		switch r.URL.Path {
		case "/api/v4/users/me":
			send(map[string]any{"roles": "system_admin"})
		case "/api/v4/teams/name/lab":
			send(map[string]any{"id": "t1"})
		case "/api/v4/teams/t1/channels": // 250 public channels over two pages
			var out []map[string]any
			for i := page * 200; i < min(250, page*200+200); i++ {
				out = append(out, map[string]any{"id": fmt.Sprint("c", i), "name": fmt.Sprint("n", i), "type": "O"})
			}
			send(out)
		case "/api/v4/teams/t1/channels/private": // one repeats a public id
			if page == 0 {
				send([]map[string]any{{"id": "p1", "name": "secret", "type": "P", "creator_id": "u1"}, {"id": "c3", "name": "n3", "type": "O"}})
			} else {
				send([]any{})
			}
		case "/api/v4/channels/c1/members":
			send([]map[string]any{{"user_id": "u1"}, {"user_id": "u2"}})
		case "/api/v4/users/ids":
			*posts++
			var ids []string
			_ = json.NewDecoder(r.Body).Decode(&ids)
			var out []map[string]any
			for _, id := range ids {
				out = append(out, map[string]any{"id": id, "username": "name-" + id, "roles": "system_guest", "delete_at": 5,
					"is_bot": true, "auth_service": "openid", "auth_data": "sub-" + id})
			}
			send(out)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestTeamChannelsPageAndDedupe(t *testing.T) {
	ctx := context.Background()
	var posts int
	c, err := New(ctx, listServer(t, &posts).URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	chs, err := c.TeamChannels(ctx)
	if err != nil || len(chs) != 251 { // 250 public + 1 private; c3 once
		t.Fatalf("%d channels, %v", len(chs), err)
	}
	if last := chs[len(chs)-1]; last.ID != "p1" || last.Type != "P" || last.CreatorID != "u1" {
		t.Fatalf("%+v", last)
	}
	ids, err := c.ChannelMemberIDs(ctx, "c1")
	if err != nil || len(ids) != 2 || ids[1] != "u2" {
		t.Fatalf("%v %v", ids, err)
	}
}

func TestUsersPost(t *testing.T) {
	ctx := context.Background()
	var posts int
	c, err := New(ctx, listServer(t, &posts).URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	us, err := c.Users(ctx, []string{"a", "b"})
	if err != nil || len(us) != 2 || posts != 1 {
		t.Fatalf("%+v %v %d", us, err, posts)
	}
	u := us[0]
	if u.Username != "name-a" || !u.Guest || !u.Deactivated || !u.IsBot || u.AuthData != "sub-a" || u.AuthService != "openid" {
		t.Fatalf("%+v", u)
	}
}

// A check of the token's rights that fails (a 5xx) is an error, never "the
// token cannot see how accounts log in".
func TestAFailedRightsCheckIsAnErrorNotBlindness(t *testing.T) {
	ctx := context.Background()
	down := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v4/users/me" {
			if down {
				http.Error(w, "", http.StatusBadGateway)
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"roles": "system_admin"})
			return
		}
		_ = json.NewEncoder(w).Encode([]map[string]any{{"id": "a", "username": "a", "auth_service": "openid", "auth_data": "s"}})
	}))
	t.Cleanup(srv.Close)
	c, err := New(ctx, srv.URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	down = true
	c.mu.Lock()
	c.recheck = true
	c.mu.Unlock()
	if us, err := c.Users(ctx, []string{"a"}); err == nil {
		t.Fatalf("a failed rights check answered %+v", us)
	}
}

// A list longer than the client reads is ErrTooMany, for the caller to skip.
func TestAListTooLongIsErrTooMany(t *testing.T) {
	ctx := context.Background()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v4/users/me" {
			_ = json.NewEncoder(w).Encode(map[string]any{"roles": "system_admin"})
			return
		}
		out := make([]map[string]any, 200)
		for i := range out {
			out[i] = map[string]any{"user_id": fmt.Sprint("u", r.URL.Query().Get("page"), "-", i)}
		}
		_ = json.NewEncoder(w).Encode(out)
	}))
	t.Cleanup(srv.Close)
	c, err := New(ctx, srv.URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.ChannelMemberIDs(ctx, "big"); !errors.Is(err, ErrTooMany) {
		t.Fatalf("a never-ending list: %v", err)
	}
}
