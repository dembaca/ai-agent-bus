package mm

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// fake is Mattermost's REST side for one test: the handler answers what
// the test answers, and the client points at the fake server.
func fake(t *testing.T, handle func(w http.ResponseWriter, r *http.Request)) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(handle))
	t.Cleanup(srv.Close)
	return New(srv.URL, "tok")
}

// Given a user on a fake Mattermost, when GetUser reads it, its username
// and is_bot come back: the mark that tells a bot's post from a person's.
func TestGetUser(t *testing.T) {
	wantBot := true
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/users/u42" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Header.Get("Authorization") != "Bearer tok" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{
			"id": "u42", "username": "dembaca", "is_bot": wantBot,
		})
	})

	u, err := c.GetUser(context.Background(), "u42")
	if err != nil {
		t.Fatal(err)
	}
	if u.ID != "u42" || u.Username != "dembaca" {
		t.Fatalf("got user %q %q, want u42 dembaca", u.ID, u.Username)
	}
	if u.IsBot != wantBot {
		t.Fatalf("got is_bot %v, want %v", u.IsBot, wantBot)
	}

	// The same user without the flag: a person, IsBot false.
	wantBot = false
	u, err = c.GetUser(context.Background(), "u42")
	if err != nil {
		t.Fatal(err)
	}
	if u.IsBot {
		t.Fatal("got is_bot true, want false")
	}

	// A user Mattermost does not have is an error, and it names the user.
	if _, err := c.GetUser(context.Background(), "nobody"); err == nil {
		t.Fatal("got no error for a missing user")
	}
}

// Given a channel's PostList on a fake Mattermost, when PostsSince reads
// it with the reconnect's hint, every post of order comes back sorted by
// create_at ascending — an edited post among them, never filtered here.
func TestPostsSince(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/channels/ch7/posts" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if got := r.URL.Query().Get("since"); got != "1500" {
			t.Errorf("got since %q, want 1500", got)
		}
		json.NewEncoder(w).Encode(map[string]any{
			"order": []string{"newest", "edited"},
			"posts": map[string]any{
				"newest": map[string]any{
					"id": "newest", "user_id": "u1", "channel_id": "ch7",
					"root_id": "root1", "create_at": 4000, "update_at": 4000,
					"edit_at": 0, "message": "written in the gap",
				},
				"edited": map[string]any{
					"id": "edited", "user_id": "u2", "channel_id": "ch7",
					"root_id": "", "create_at": 100, "update_at": 2000,
					"edit_at": 2000, "message": "reworded",
				},
			},
		})
	})

	posts, err := c.PostsSince(context.Background(), "ch7", 1500)
	if err != nil {
		t.Fatal(err)
	}
	if len(posts) != 2 {
		t.Fatalf("got %d posts, want 2", len(posts))
	}
	// Sorted by create_at ascending: the edit carries its old create_at,
	// and its UpdateAt and EditAt come with it for the caller to decide.
	if posts[0].ID != "edited" || posts[1].ID != "newest" {
		t.Fatalf("got order %s, %s; want edited, newest", posts[0].ID, posts[1].ID)
	}
	edited := posts[0]
	if edited.UserID != "u2" || edited.ChannelID != "ch7" || edited.RootID != "" ||
		edited.CreateAt != 100 || edited.UpdateAt != 2000 || edited.EditAt != 2000 ||
		edited.Message != "reworded" {
		t.Fatalf("got the edited post %+v", edited)
	}
	newest := posts[1]
	if newest.RootID != "root1" || newest.EditAt != 0 || newest.CreateAt != 4000 {
		t.Fatalf("got the new post %+v", newest)
	}
}
