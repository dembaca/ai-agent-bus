package mm

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
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
			"order": []string{"newest", "edited", "deleted"},
			"posts": map[string]any{
				"newest": map[string]any{
					"id": "newest", "user_id": "u1", "channel_id": "ch7",
					"root_id": "root1", "type": "custom_card",
					"props":     map[string]any{"fromusername": "dembaca"},
					"file_ids":  []string{"only-one"},
					"create_at": 4000, "update_at": 4000,
					"edit_at": 0, "delete_at": 0, "message": "written in the gap",
				},
				"deleted": map[string]any{
					"id": "deleted", "user_id": "u3", "channel_id": "ch7",
					"create_at": 3000, "update_at": 3000,
					"delete_at": 4500, "message": "created and deleted in the gap",
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
	if len(posts) != 3 {
		t.Fatalf("got %d posts, want 3", len(posts))
	}
	// Sorted by create_at ascending: the edit carries its old create_at,
	// and its UpdateAt and EditAt come with it for the caller to decide.
	if posts[0].ID != "edited" || posts[1].ID != "deleted" || posts[2].ID != "newest" {
		t.Fatalf("got order %s, %s, %s; want edited, deleted, newest",
			posts[0].ID, posts[1].ID, posts[2].ID)
	}
	edited := posts[0]
	if edited.UserID != "u2" || edited.ChannelID != "ch7" || edited.RootID != "" ||
		edited.CreateAt != 100 || edited.UpdateAt != 2000 || edited.EditAt != 2000 ||
		edited.Message != "reworded" {
		t.Fatalf("got the edited post %+v", edited)
	}
	deleted := posts[1]
	if deleted.DeleteAt != 4500 {
		t.Fatalf("got the deleted post %+v, want its delete mark: the caller drops it", deleted)
	}
	newest := posts[2]
	if newest.RootID != "root1" || newest.EditAt != 0 || newest.CreateAt != 4000 {
		t.Fatalf("got the new post %+v", newest)
	}
	if newest.Type != "custom_card" ||
		newest.Props["fromusername"] != "dembaca" ||
		newest.FileCount != 1 {
		t.Fatalf("got the new post %+v, want its type, props and file count", newest)
	}
}

// Given a response past the cap its read takes, when the read runs, the
// named too-large error comes back — the catch-up's bound says itself, so
// the caller can page instead of retrying a body that cannot fit.
func TestTooLarge(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(w, strings.NewReader(strings.Repeat("x", 600)))
	})
	var out any
	err := c.doLimit(context.Background(), "GET", "/api/v4/channels/ch7/posts",
		nil, &out, 512)
	if !errors.Is(err, ErrTooLarge) {
		t.Fatalf("got %v, want ErrTooLarge", err)
	}
}

// Given a since read whose posts carry their reactions in the metadata, the
// reactions come back with the post, and a post without metadata says so:
// the catch-up reads a removal only from a list it knows (RT-6.42 step 1).
func TestPostsSinceCarriesTheReactions(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"order":["p1","p2"],"posts":{` +
			`"p1":{"id":"p1","create_at":1,"metadata":{"reactions":[{"user_id":"u1","post_id":"p1","emoji_name":"+1","create_at":5}]}},` +
			`"p2":{"id":"p2","create_at":2}}}`))
	})
	posts, err := c.PostsSince(context.Background(), "ch", 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(posts) != 2 || !posts[0].HasMetadata || len(posts[0].Reactions) != 1 ||
		posts[0].Reactions[0] != (Reaction{UserID: "u1", EmojiName: "+1", CreateAt: 5}) || posts[1].HasMetadata {
		t.Fatalf("%+v", posts)
	}
}
