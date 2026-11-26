package mattermost

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"
)

// postsServer serves 130 posts of the channel agents-stats, newest first in
// pages of per_page, the second page repeating the first page's last post (a
// post that arrived while paging shifts the pages), plus a system post and a
// deleted one.
func postsServer(t *testing.T, base int64) *httptest.Server {
	t.Helper()
	type post struct {
		ID       string `json:"id"`
		CreateAt int64  `json:"create_at"`
		DeleteAt int64  `json:"delete_at"`
		UserID   string `json:"user_id"`
		Type     string `json:"type"`
		Message  string `json:"message"`
	}
	var all []post // newest first
	for i := 129; i >= 0; i-- {
		p := post{ID: fmt.Sprint("p", i), CreateAt: base + int64(i)*1000, UserID: "u1", Message: fmt.Sprintf("STATS {\"n\":%d}", i)}
		switch i {
		case 100:
			p.Type = "system_join_channel"
		case 101:
			p.DeleteAt = base
		}
		all = append(all, p)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		send := func(v any) { _ = json.NewEncoder(w).Encode(v) }
		switch r.URL.Path {
		case "/api/v4/users/me":
			send(map[string]any{"roles": "system_admin"})
		case "/api/v4/teams/name/lab/channels/name/agents-stats":
			send(map[string]any{"id": "cs"})
		case "/api/v4/channels/cs/posts":
			page, _ := strconv.Atoi(r.URL.Query().Get("page"))
			per, _ := strconv.Atoi(r.URL.Query().Get("per_page"))
			if per != postsPage {
				http.Error(w, "per_page", http.StatusBadRequest)
				return
			}
			from := page * per
			if page > 0 {
				from-- // the shift: the last post of the page before comes again
			}
			out := map[string]any{"order": []string{}, "posts": map[string]post{}}
			var order []string
			posts := map[string]post{}
			for i := from; i < min(len(all), page*per+per); i++ {
				order = append(order, all[i].ID)
				posts[all[i].ID] = all[i]
			}
			if order != nil {
				out["order"], out["posts"] = order, posts
			}
			send(out)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// The STATS import's reading (RT-6.45): every page, each post once, no system
// post, no deleted post, the server's milliseconds as UTC times, and since
// stops the paging.
func TestChannelPostsPages(t *testing.T) {
	ctx := context.Background()
	base := time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC).UnixMilli()
	c, err := New(ctx, postsServer(t, base).URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	ps, err := c.ChannelPosts(ctx, "agents-stats", time.Time{})
	if err != nil {
		t.Fatal(err)
	}
	if len(ps) != 128 {
		t.Fatalf("%d posts, want 128 (130 less a system and a deleted one)", len(ps))
	}
	seen := map[string]bool{}
	for _, p := range ps {
		if seen[p.ID] {
			t.Fatalf("%s twice", p.ID)
		}
		seen[p.ID] = true
	}
	if seen["p100"] || seen["p101"] {
		t.Fatal("a system or a deleted post")
	}
	first := ps[0]
	if first.ID != "p129" || first.UserID != "u1" || first.Message != `STATS {"n":129}` ||
		!first.Created.Equal(time.UnixMilli(base+129000)) || first.Created.Location() != time.UTC {
		t.Fatalf("%+v", first)
	}
	since := time.UnixMilli(base + 120000)
	ps, err = c.ChannelPosts(ctx, "agents-stats", since)
	if err != nil || len(ps) != 10 {
		t.Fatalf("since: %d posts, %v", len(ps), err)
	}
	if _, err := c.ChannelPosts(ctx, "missing", time.Time{}); err == nil {
		t.Fatal("a missing channel gave no error")
	}
}

// A full page of the largest posts Mattermost allows (16383 characters, here
// of four bytes each) fits the answer limit of the posts' reading (RT-6.45).
func TestAPageOfTheLargestPostsFits(t *testing.T) {
	big := strings.Repeat("\U0001F600", 16383)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		send := func(v any) { _ = json.NewEncoder(w).Encode(v) }
		switch r.URL.Path {
		case "/api/v4/users/me":
			send(map[string]any{"roles": "system_admin"})
		case "/api/v4/teams/name/lab/channels/name/agents-stats":
			send(map[string]any{"id": "cs"})
		case "/api/v4/channels/cs/posts":
			out := map[string]any{"order": []string{}, "posts": map[string]any{}}
			if r.URL.Query().Get("page") == "0" {
				var order []string
				posts := map[string]any{}
				for i := range postsPage {
					id := fmt.Sprint("p", i)
					order = append(order, id)
					posts[id] = map[string]any{"id": id, "create_at": 1000 + i, "user_id": "u1", "message": big,
						"props": map[string]any{"attachments": []any{map[string]any{"text": strings.Repeat("x", 4000)}}}}
				}
				out["order"], out["posts"] = order, posts
			}
			send(out)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	c, err := New(context.Background(), srv.URL, "lab", "tok")
	if err != nil {
		t.Fatal(err)
	}
	ps, err := c.ChannelPosts(context.Background(), "agents-stats", time.Time{})
	if err != nil || len(ps) != postsPage {
		t.Fatalf("%d posts, %v", len(ps), err)
	}
}
