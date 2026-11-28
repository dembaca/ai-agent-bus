package mm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
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

// ServerTime is Mattermost's clock from the ping's Date header (RT-6.42
// step 1: the watermark is on Mattermost's clock).
func TestServerTime(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/system/ping" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Date", "Wed, 25 Nov 2026 10:00:00 GMT")
		_, _ = w.Write([]byte(`{"status":"OK"}`))
	})
	got, err := c.ServerTime(context.Background())
	if err != nil || got != 1795600800000 {
		t.Fatalf("%d %v", got, err)
	}
}

// Given a Mattermost that answers an API call with a redirect, the client
// does not follow it: Go would resend the Authorization header to a
// same-host redirect, so a 30x to http would carry the token in clear
// text (#131). The 30x is the call's error.
func TestRedirectIsNotFollowed(t *testing.T) {
	followed := false
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/elsewhere" {
			followed = true
			_, _ = w.Write([]byte(`{"id":"u1"}`))
			return
		}
		http.Redirect(w, r, "/elsewhere", http.StatusFound)
	})
	_, err := c.Me(context.Background())
	var se *StatusError
	if !errors.As(err, &se) || se.Status != http.StatusFound {
		t.Fatalf("got %v, want the 302 as a StatusError", err)
	}
	if followed {
		t.Error("the client followed the redirect")
	}
}

// RT-6.44: the mirror drops an agent bot's client only on a refused token,
// told by the status a StatusError carries, wrapped or not.
func TestTheMirrorReadsTheStatusOfAFailure(t *testing.T) {
	for status, want := range map[int]bool{401: true, 403: true, 429: false, 500: false, 503: false} {
		err := fmt.Errorf("post: %w", &StatusError{Status: status, Err: errors.New("x")})
		if mirror.Revoked(err) != want {
			t.Errorf("%d: revoked %v", status, !want)
		}
	}
	if mirror.Revoked(context.DeadlineExceeded) {
		t.Error("a timeout counted as a refused token")
	}
}

// A thread page asks for its bound and the down direction, keeps the root
// first and carries the metadata, reactions and has_next: the history read's
// context.
func TestThreadPage(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/posts/root1/thread" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		q := r.URL.Query()
		if got := q.Get("perPage"); got != "200" {
			t.Errorf("perPage %q, want 200", got)
		}
		if got := q.Get("direction"); got != "down" {
			t.Errorf("direction %q, want down", got)
		}
		if got := q.Get("fromPost"); got != "" {
			t.Errorf("the first page names fromPost %q", got)
		}
		_, _ = w.Write([]byte(`{"order":["reply","root1"],"posts":{` +
			`"reply":{"id":"reply","user_id":"u2","channel_id":"ch7","root_id":"root1","create_at":2000},` +
			`"root1":{"id":"root1","user_id":"u1","channel_id":"ch7","create_at":1000,` +
			`"metadata":{"reactions":[{"user_id":"u2","emoji_name":"+1","create_at":5}]}}},"has_next":true}`))
	})
	posts, hasNext, err := c.ThreadPage(context.Background(), "root1", "", 0, 500)
	if err != nil {
		t.Fatal(err)
	}
	if !hasNext {
		t.Fatal("has_next dropped")
	}
	if len(posts) != 2 || posts[0].ID != "root1" || posts[1].RootID != "root1" {
		t.Fatalf("the thread: %+v", posts)
	}
	if !posts[0].HasMetadata || len(posts[0].Reactions) != 1 ||
		posts[0].Reactions[0].EmojiName != "+1" {
		t.Fatalf("the root's reactions: %+v", posts[0])
	}
	// a thread Mattermost has no post for is an error, never an empty thread
	if _, _, err := c.ThreadPage(context.Background(), "gone", "", 0, 0); err == nil {
		t.Fatal("a missing thread is no error")
	}
}

// Given a page of a thread, the next one goes on from the last post of it:
// fromPost and fromCreateAt, both, as Mattermost asks (ADR-014 phase 5).
func TestThreadPageGoesOnFromTheCursor(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if got := q.Get("fromPost"); got != "reply9" {
			t.Errorf("fromPost %q, want reply9", got)
		}
		if got := q.Get("fromCreateAt"); got != "1234" {
			t.Errorf("fromCreateAt %q, want 1234", got)
		}
		if got := q.Get("perPage"); got != "20" {
			t.Errorf("perPage %q, want 20", got)
		}
		_, _ = w.Write([]byte(`{"order":["reply10"],"posts":{` +
			`"reply10":{"id":"reply10","channel_id":"ch7","root_id":"root1","create_at":2000}}}`))
	})
	posts, hasNext, err := c.ThreadPage(context.Background(), "root1", "reply9", 1234, 20)
	if err != nil {
		t.Fatal(err)
	}
	if hasNext || len(posts) != 1 || posts[0].ID != "reply10" {
		t.Fatalf("%v %v", posts, hasNext)
	}
}

// One page of a channel's posts: page and per_page, no since.
func TestChannelPage(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/channels/ch7/posts" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		q := r.URL.Query()
		if got := q.Get("page"); got != "2" {
			t.Errorf("page %q, want 2", got)
		}
		if got := q.Get("per_page"); got != "200" {
			t.Errorf("per_page %q, want 200", got)
		}
		if got := q.Get("since"); got != "" {
			t.Errorf("the page asked with since %q", got)
		}
		_, _ = w.Write([]byte(`{"order":["b","a"],"posts":{` +
			`"a":{"id":"a","channel_id":"ch7","create_at":1},` +
			`"b":{"id":"b","channel_id":"ch7","create_at":2}}}`))
	})
	posts, err := c.ChannelPage(context.Background(), "ch7", 2, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(posts) != 2 || posts[0].ID != "a" || posts[1].ID != "b" {
		t.Fatalf("%+v", posts)
	}
}

// PostsFrom pages from the newest post back with page/per_page, never
// Mattermost's `since` (whose answer is cut at a thousand posts in no set
// order), keeps the oldest limit posts from since on and says more while they
// remain: a busy channel's read is whole (ADR-014 phase 5, RT-6.42 step 11).
func TestPostsFromPages(t *testing.T) {
	const total, since, limit = 450, 51, 200
	var (
		mu    sync.Mutex
		asked []string
	)
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/channels/ch7/posts" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		q := r.URL.Query()
		if got := q.Get("since"); got != "" {
			t.Errorf("the read asked with since %q", got)
		}
		page, _ := strconv.Atoi(q.Get("page"))
		per, _ := strconv.Atoi(q.Get("per_page"))
		if per != PageSize {
			t.Errorf("per_page %d, want %d", per, PageSize)
		}
		mu.Lock()
		asked = append(asked, q.Get("page")+"/"+q.Get("per_page"))
		mu.Unlock()
		// newest first, as Mattermost's page is: n from total-page*per down
		order := []string{}
		by := map[string]any{}
		for n := total - page*per; n > total-(page+1)*per && n >= 1; n-- {
			id := "p" + strconv.Itoa(n)
			order = append(order, id)
			by[id] = map[string]any{"id": id, "user_id": "u1", "channel_id": "ch7", "create_at": n}
		}
		json.NewEncoder(w).Encode(map[string]any{"order": order, "posts": by})
	})
	posts, more, err := c.PostsFrom(context.Background(), "ch7", since, limit)
	if err != nil {
		t.Fatal(err)
	}
	if len(posts) != limit || !more {
		t.Fatalf("%d posts, more %v", len(posts), more)
	}
	if posts[0].CreateAt != since || posts[len(posts)-1].CreateAt != since+limit-1 {
		t.Fatalf("the oldest limit: %d..%d", posts[0].CreateAt, posts[len(posts)-1].CreateAt)
	}
	// three pages: the two full ones and the short one at the channel's end,
	// where the walk stops (the last page's posts are all before since)
	if got := strings.Join(asked, " "); got != "0/200 1/200 2/200" {
		t.Fatalf("pages asked %q", got)
	}
}

// A channel whose window is smaller than the limit answers it whole, more
// false, and stops at its oldest page.
func TestPostsFromAShortWindow(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if page := q.Get("page"); page != "0" {
			t.Errorf("page %q, want 0: the first page is short", page)
		}
		_, _ = w.Write([]byte(`{"order":["p2","p1"],"posts":{` +
			`"p1":{"id":"p1","channel_id":"ch7","create_at":10},` +
			`"p2":{"id":"p2","channel_id":"ch7","create_at":20}}}`))
	})
	posts, more, err := c.PostsFrom(context.Background(), "ch7", 5, 200)
	if err != nil {
		t.Fatal(err)
	}
	if more || len(posts) != 2 || posts[0].ID != "p1" || posts[1].ID != "p2" {
		t.Fatalf("%v %v", posts, more)
	}
}

// A window with more posts than one read walks is refused, not answered in
// part: an answer cut at the newest side would lose the posts between.
func TestPostsFromRefusesAWindowItCannotWalk(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		order := []string{}
		by := map[string]any{}
		for n := 1; n <= PageSize; n++ {
			id := "p" + strconv.Itoa(n)
			order = append(order, id)
			by[id] = map[string]any{"id": id, "channel_id": "ch7", "create_at": 1000}
		}
		json.NewEncoder(w).Encode(map[string]any{"order": order, "posts": by})
	})
	if _, _, err := c.PostsFrom(context.Background(), "ch7", 1, 200); !errors.Is(err, ErrTooManyPosts) {
		t.Fatalf("got %v, want ErrTooManyPosts", err)
	}
}

// RT-6.11, plan B piece 6: Person reads a username's account: a person is
// active and no bot; an unknown name is no person and no error.
func TestPerson(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/users/usernames" || r.Method != "POST" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		var names []string
		_ = json.NewDecoder(r.Body).Decode(&names)
		users := map[string]map[string]any{
			"dembaca": {"id": "u-dembaca", "username": "dembaca", "is_bot": false, "delete_at": 0},
			"agent-a": {"username": "agent-a", "is_bot": true, "delete_at": 0},
			"gone":    {"username": "gone", "is_bot": false, "delete_at": 1700000000000},
		}
		out := []map[string]any{}
		for _, n := range names {
			if u, ok := users[n]; ok {
				out = append(out, u)
			}
		}
		json.NewEncoder(w).Encode(out)
	})
	for name, want := range map[string]string{"dembaca": "u-dembaca", "agent-a": "", "gone": "", "nobody": ""} {
		got, err := c.Person(context.Background(), name)
		if err != nil || got != want {
			t.Errorf("%s: got %v %v, want %v", name, got, err, want)
		}
	}
	down := fake(t, func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusBadGateway) })
	if _, err := down.Person(context.Background(), "dembaca"); err == nil {
		t.Fatal("a failed read must be an error")
	}
}

func TestMember(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v4/channels/c1/members/in":
			_, _ = w.Write([]byte(`{"user_id":"in"}`))
		case "/api/v4/channels/c1/members/out":
			w.WriteHeader(http.StatusNotFound)
		default:
			w.WriteHeader(http.StatusBadGateway)
		}
	})
	for id, want := range map[string]bool{"in": true, "out": false} {
		if got, err := c.Member(context.Background(), "c1", id); err != nil || got != want {
			t.Errorf("%s: %v %v, want %v", id, got, err, want)
		}
	}
	if _, err := c.Member(context.Background(), "c1", "down"); err == nil {
		t.Fatal("a failed read must be an error")
	}
}

// RT-6.33 step 4: an upload posts the channel and the file as multipart to
// /api/v4/files, and the answer's file_infos entry — the shape the
// create-post answer carries in a post's metadata too — comes back.
func TestUploadFile(t *testing.T) {
	var (
		gotChannel, gotName string
		gotData             []byte
		refused             = http.StatusOK
	)
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/api/v4/files" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Header.Get("Authorization") != "Bearer tok" {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		if !strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/form-data; boundary=") {
			t.Errorf("the content type %q is no multipart", r.Header.Get("Content-Type"))
		}
		if err := r.ParseMultipartForm(1 << 20); err != nil {
			t.Errorf("the multipart does not parse: %v", err)
			return
		}
		gotChannel = r.FormValue("channel_id")
		file, header, err := r.FormFile("files")
		if err != nil {
			t.Errorf("the file field: %v", err)
			return
		}
		gotName, gotData = header.Filename, nil
		gotData, _ = io.ReadAll(file)
		if refused != http.StatusOK {
			w.WriteHeader(refused)
			return
		}
		json.NewEncoder(w).Encode(map[string]any{"file_infos": []any{map[string]any{
			"id": "file0123456789abcdefghijk", "name": "chart.png", "extension": "png",
			"size": 8, "mime_type": "image/png", "width": 2, "height": 1,
			"post_id": "", "user_id": "u1", "has_preview_image": true}}})
	})
	info, err := c.UploadFile(context.Background(), "ch7", "chart.png", []byte("PNGbytes"))
	if err != nil {
		t.Fatal(err)
	}
	if gotChannel != "ch7" || gotName != "chart.png" || string(gotData) != "PNGbytes" {
		t.Fatalf("the upload sent %q %q %q", gotChannel, gotName, gotData)
	}
	if info.ID != "file0123456789abcdefghijk" || info.Name != "chart.png" ||
		info.Extension != "png" || info.Size != 8 || info.MimeType != "image/png" ||
		info.Width != 2 || info.Height != 1 || info.UserID != "u1" || !info.HasPreviewImage {
		t.Fatalf("the file info: %+v", info)
	}
	// Mattermost's refusal is the status it answered.
	refused = http.StatusRequestEntityTooLarge
	_, err = c.UploadFile(context.Background(), "ch7", "chart.png", []byte("PNGbytes"))
	var se *StatusError
	if !errors.As(err, &se) || se.Status != http.StatusRequestEntityTooLarge {
		t.Fatalf("got %v, want the status as the error", err)
	}
}

// An upload whose answer carries no file info is an error, never a made-up
// one: the caller's upload failed.
func TestUploadFileWithoutAFileInfo(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"file_infos":[]}`))
	})
	if _, err := c.UploadFile(context.Background(), "ch7", "chart.png", []byte("x")); err == nil {
		t.Fatal("an upload without a file info is no error")
	}
}

// RT-6.23 step 2: the file's info as /files/{id}/info answers it.
func TestFileInfo(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/files/file0123456789abcdefghijk/info" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write([]byte(`{"id":"file0123456789abcdefghijk","name":"photo.jpg","extension":"jpg",` +
			`"size":18342,"mime_type":"image/jpeg","width":640,"height":480,"post_id":"post12345",` +
			`"user_id":"u-alice","has_preview_image":false}`))
	})
	info, err := c.FileInfo(context.Background(), "file0123456789abcdefghijk")
	if err != nil {
		t.Fatal(err)
	}
	if info.Name != "photo.jpg" || info.Extension != "jpg" || info.Size != 18342 ||
		info.MimeType != "image/jpeg" || info.Width != 640 || info.Height != 480 ||
		info.PostID != "post12345" || info.UserID != "u-alice" || info.HasPreviewImage {
		t.Fatalf("the file info: %+v", info)
	}
	if _, err := c.FileInfo(context.Background(), "missing0000000000000000"); err == nil {
		t.Fatal("a missing file is no error")
	}
}

// A file's bytes: the preview when it has one, else the file itself.
func TestFileBytes(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v4/files/file0123456789abcdefghijk/preview":
			_, _ = w.Write([]byte("the preview"))
		case "/api/v4/files/file0123456789abcdefghijk":
			_, _ = w.Write([]byte("the original"))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	})
	data, err := c.FileBytes(context.Background(), "file0123456789abcdefghijk", true)
	if err != nil || string(data) != "the preview" {
		t.Fatalf("the preview: %q %v", data, err)
	}
	data, err = c.FileBytes(context.Background(), "file0123456789abcdefghijk", false)
	if err != nil || string(data) != "the original" {
		t.Fatalf("the original: %q %v", data, err)
	}
	if _, err := c.FileBytes(context.Background(), "gone000000000000000000000", false); err == nil {
		t.Fatal("a missing file is no error")
	}
}

// One post by id: the fields the file read needs of the post a file is
// attached to, its channel above all.
func TestPostByID(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/posts/post12345" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write([]byte(`{"id":"post12345","user_id":"u-alice","channel_id":"ch7","root_id":"root1",` +
			`"type":"","message":"see this","create_at":1,"update_at":2,"edit_at":0,"delete_at":0,` +
			`"file_ids":["file0123456789abcdefghijk"],` +
			`"metadata":{"reactions":[{"user_id":"u-bot","emoji_name":"tada","create_at":5}]}}`))
	})
	post, err := c.PostByID(context.Background(), "post12345")
	if err != nil {
		t.Fatal(err)
	}
	if post.ID != "post12345" || post.UserID != "u-alice" || post.ChannelID != "ch7" ||
		post.RootID != "root1" || post.Message != "see this" || post.FileCount != 1 ||
		!post.HasMetadata || len(post.Reactions) != 1 || post.Reactions[0].EmojiName != "tada" {
		t.Fatalf("the post: %+v", post)
	}
	if _, err := c.PostByID(context.Background(), "gone123"); err == nil {
		t.Fatal("a missing post is no error")
	}
}

// A channel's name by id: the token the bus carries the channel under.
func TestChannelName(t *testing.T) {
	c := fake(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v4/channels/ch7" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write([]byte(`{"id":"ch7","name":"home","display_name":"Home"}`))
	})
	name, err := c.ChannelName(context.Background(), "ch7")
	if err != nil || name != "home" {
		t.Fatalf("got %q %v, want home", name, err)
	}
	if _, err := c.ChannelName(context.Background(), "gone"); err == nil {
		t.Fatal("a missing channel is no error")
	}
}
