package mmws

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
)

func TestMain(m *testing.M) {
	// The reader logs its retries by design; a test does not want them on
	// stderr (TestAuthFailureRetries swaps the default for its own ear).
	slog.SetDefault(slog.New(slog.NewTextHandler(io.Discard, nil)))
	os.Exit(m.Run())
}

// fakeMM is a scripted Mattermost: one httptest server that serves both
// the WebSocket (/api/v4/websocket) and the REST path the catch-up reads,
// as one Mattermost does. Each WebSocket connection runs the next script
// of the list; the last script repeats for every later connection, so a
// test that cuts the socket says with its scripts what comes back.
type fakeMM struct {
	mu      sync.Mutex
	scripts []func(*websocket.Conn)
	n       int              // WebSocket connections taken
	posts   http.HandlerFunc // /api/v4/channels/<id>/posts
	srv     *httptest.Server
}

func newFake(t *testing.T) *fakeMM {
	t.Helper()
	f := &fakeMM{}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v4/websocket", func(w http.ResponseWriter, r *http.Request) {
		conn, err := (&websocket.Upgrader{}).Upgrade(w, r, nil)
		if err != nil {
			return
		}
		defer conn.Close()
		f.mu.Lock()
		f.n++
		script := f.scripts[min(f.n-1, len(f.scripts)-1)]
		f.mu.Unlock()
		if script != nil {
			script(conn)
		}
	})
	mux.HandleFunc("/api/v4/channels/", func(w http.ResponseWriter, r *http.Request) {
		if f.posts == nil || !strings.HasSuffix(r.URL.Path, "/posts") {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		f.posts(w, r)
	})
	f.srv = httptest.NewServer(mux)
	t.Cleanup(f.srv.Close)
	return f
}

// connections says how many WebSocket connections the fake has taken.
func (f *fakeMM) connections() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.n
}

func write(conn *websocket.Conn, v any) {
	// A write the reader did not live to read fails with its hang-up;
	// what the reader then does is the scripts' business.
	_ = conn.WriteJSON(v)
}

// readChallenge reads the reader's first frame as Mattermost does: only
// the action "authentication_challenge" with a token is one. Any other
// action makes a real server drop the socket without a close frame (1006),
// so the fake does the same and says no — a misspelt action fails every
// test that authenticates instead of slipping through.
func readChallenge(conn *websocket.Conn) (struct{ Seq int }, bool) {
	var frame struct {
		Seq    int               `json:"seq"`
		Action string            `json:"action"`
		Data   map[string]string `json:"data"`
	}
	if err := conn.ReadJSON(&frame); err != nil {
		return struct{ Seq int }{}, false
	}
	if frame.Action != "authentication_challenge" || frame.Data["token"] == "" {
		_ = conn.Close()
		return struct{ Seq int }{}, false
	}
	return struct{ Seq int }{frame.Seq}, true
}

// authOK reads the auth challenge and answers it as Mattermost does: the
// hello is queued before the reply (HubRegister precedes the OK), so
// hello goes first — the reader must not take it for the reply — then
// the OK reply, then the steps on the socket.
func authOK(steps ...func(*websocket.Conn)) func(*websocket.Conn) {
	return func(conn *websocket.Conn) {
		challenge, ok := readChallenge(conn)
		if !ok {
			return // the reader hung up before asking
		}
		write(conn, map[string]any{"event": "hello",
			"data": map[string]any{"connection_id": "c1"}, "seq": 0})
		write(conn, map[string]any{"status": "OK", "seq_reply": challenge.Seq,
			"data": map[string]any{}})
		for _, step := range steps {
			step(conn)
		}
	}
}

// authClosed answers the challenge the way a real Mattermost answers a
// bad token: not at all — the socket simply closes (its HubRegister has
// no session to queue hello for), which reads as the drop it then
// behaves like.
func authClosed(conn *websocket.Conn) {
	if _, ok := readChallenge(conn); !ok {
		return
	}
	_ = conn.WriteControl(websocket.CloseMessage,
		websocket.FormatCloseMessage(1008, "bad token"),
		time.Now().Add(time.Second))
}

// authRefused answers the challenge with an in-band refusal — a server
// that says no instead of closing. Mattermost does not; the reader still
// knows the shape.
func authRefused(conn *websocket.Conn) {
	challenge, ok := readChallenge(conn)
	if !ok {
		return
	}
	write(conn, map[string]any{"status": "FAIL", "seq_reply": challenge.Seq,
		"error": map[string]any{"id": "api.context.session_expired.app_error",
			"message": "Invalid or expired session"}})
}

// still holds a connection open reading until the reader hangs up: the
// quiet connection every connected test ends on.
func still(conn *websocket.Conn) {
	for {
		if _, _, err := conn.ReadMessage(); err != nil {
			return
		}
	}
}

func postEvent(conn *websocket.Conn, post map[string]any) {
	// Mattermost carries the post as a JSON string inside the event.
	raw, _ := json.Marshal(post)
	write(conn, map[string]any{"event": "posted",
		"data": map[string]any{"post": string(raw)}})
}

// reactionEvent writes a reaction event as Mattermost does: the
// reaction as a JSON string inside data, the channel in the broadcast
// beside it. create_at is what the caller says — a removal carries 0.
func reactionEvent(conn *websocket.Conn, name, channelID string,
	reaction map[string]any) {
	raw, _ := json.Marshal(reaction)
	write(conn, map[string]any{"event": name,
		"data":      map[string]any{"reaction": string(raw)},
		"broadcast": map[string]any{"channel_id": channelID}})
}

// ping sends a ping and fails the test when no pong, with the same
// payload, comes back: the reader must answer pings, or Mattermost closes
// a quiet socket.
func ping(t *testing.T) func(*websocket.Conn) {
	return func(conn *websocket.Conn) {
		got := make(chan string, 1)
		conn.SetPongHandler(func(d string) error {
			select {
			case got <- d:
			default:
			}
			return nil
		})
		if err := conn.WriteControl(websocket.PingMessage, []byte("1"),
			time.Now().Add(time.Second)); err != nil {
			t.Errorf("the fake could not ping: %v", err)
			return
		}
		select {
		case d := <-got:
			if d != "1" {
				t.Errorf("got pong %q, want the ping's payload \"1\"", d)
			}
		case <-time.After(2 * time.Second):
			t.Error("the reader did not answer the ping")
		}
	}
}

// run starts the reader against the fake. The backoff is what a test may
// sleep: a reconnect happens in milliseconds, not seconds.
func run(t *testing.T, f *fakeMM) *Reader {
	t.Helper()
	return runReader(t, New(f.srv.URL, "tok").
		WithBackoff(time.Millisecond, 4*time.Millisecond))
}

// runReader starts a reader the test built, on the same ending.
func runReader(t *testing.T, r *Reader) *Reader {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- r.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Error("the reader did not stop")
		}
	})
	return r
}

// next takes the next event, or fails the test when none comes in time.
func next(t *testing.T, r *Reader) Event {
	t.Helper()
	select {
	case ev := <-r.Events():
		return ev
	case <-time.After(2 * time.Second):
		t.Fatal("no event in time")
		return nil
	}
}

func reconnected(t *testing.T, ev Event, want int64) {
	t.Helper()
	rc, ok := ev.(*Reconnected)
	if !ok {
		t.Fatalf("got %T, want *Reconnected", ev)
	}
	if rc.Since != want {
		t.Errorf("got Reconnected{Since: %d}, want %d", rc.Since, want)
	}
}

// Given a connected reader, when Mattermost sends a post, a ping and the
// two reaction events — with an event it must ignore among them — the
// three arrive typed with all their fields, the ping is answered and the
// rest changes nothing.
func TestEventsTyped(t *testing.T) {
	f := newFake(t)
	f.scripts = []func(*websocket.Conn){authOK(
		func(conn *websocket.Conn) {
			postEvent(conn, map[string]any{
				"id": "p1", "user_id": "u1", "channel_id": "ch1", "root_id": "r1",
				"type": "custom_card", "props": map[string]any{
					"fromusername": "dembaca",
				},
				"file_ids":  []string{"file1", "file2"},
				"create_at": 1000, "update_at": 1000, "edit_at": 0,
				"delete_at": 0,
				"message":   "go ahead",
			})
			// An event the reader must ignore, never crash on.
			write(conn, map[string]any{"event": "status_change",
				"data": map[string]any{"status": "away"}, "seq": 2})
			reactionEvent(conn, "reaction_added", "ch1", map[string]any{
				"user_id": "u2", "post_id": "p2", "emoji_name": "+1",
				"create_at": 1500,
			})
			// As on the wire: a removal carries no create_at of its own —
			// Mattermost's delete handler sends the bare object.
			reactionEvent(conn, "reaction_removed", "ch1", map[string]any{
				"user_id": "u3", "post_id": "p3", "emoji_name": "smile",
			})
		},
		// The script ends here, which cuts the socket; the reader comes
		// back on a quiet connection.
		still,
	)}
	r := run(t, f)

	// The first connect says the catch-up hint of a cold start: none yet.
	reconnected(t, next(t, r), 0)

	post, ok := next(t, r).(*Posted)
	if !ok {
		t.Fatalf("got %T, want *Posted", post)
	}
	wantPost := Posted{
		PostID: "p1", UserID: "u1", ChannelID: "ch1", RootID: "r1",
		Type:      "custom_card",
		Props:     map[string]any{"fromusername": "dembaca"},
		FileCount: 2,
		CreateAt:  1000, UpdateAt: 1000, EditAt: 0, DeleteAt: 0,
		Message: "go ahead",
	}
	// Props and FileCount hold maps: a DeepEqual, not a plain compare.
	if !reflect.DeepEqual(*post, wantPost) {
		t.Errorf("got post %+v, want %+v", *post, wantPost)
	}

	// The ping was answered inside the script; the events after it still
	// arrive, so the answer broke nothing.
	added, ok := next(t, r).(*Reaction)
	if !ok {
		t.Fatalf("got %T, want *Reaction", added)
	}
	if want := (Reaction{PostID: "p2", UserID: "u2", ChannelID: "ch1",
		EmojiName: "+1", CreateAt: 1500, Added: true}); *added != want {
		t.Errorf("got reaction %+v, want %+v", *added, want)
	}
	removed, ok := next(t, r).(*Reaction)
	if !ok {
		t.Fatalf("got %T, want *Reaction", removed)
	}
	if want := (Reaction{PostID: "p3", UserID: "u3", ChannelID: "ch1",
		EmojiName: "smile", CreateAt: 0, Added: false}); *removed != want {
		t.Errorf("got reaction %+v, want %+v (a removal carries no time)", *removed, want)
	}
}

// Given a reader that saw a post and a reaction before its socket was
// cut, when it comes back, it says Reconnected with the greatest create_at
// of what it saw — 1500, the reaction's, not the post's — and the
// catch-up read with that hint finds the post written in the gap.
func TestReconnectCatchUp(t *testing.T) {
	f := newFake(t)
	f.scripts = []func(*websocket.Conn){
		// The first socket: a post, a reaction, then the cut — the
		// script's end is the hang-up.
		authOK(
			func(conn *websocket.Conn) {
				postEvent(conn, map[string]any{
					"id": "seen", "user_id": "u1", "channel_id": "ch7",
					"create_at": 1000, "update_at": 1000, "message": "before the cut",
				})
				reactionEvent(conn, "reaction_added", "ch7", map[string]any{
					"user_id": "u2", "post_id": "seen", "emoji_name": "+1",
					"create_at": 1500,
				})
			},
		),
		// The second socket: quiet, the connection the reader ends on.
		authOK(still),
	}
	// The REST side: the post written while no socket was up, which the
	// WebSocket never carried — the catch-up finds it by the hint.
	f.posts = func(w http.ResponseWriter, r *http.Request) {
		if got := r.URL.Query().Get("since"); got != "1500" {
			t.Errorf("got since %q, want 1500", got)
		}
		json.NewEncoder(w).Encode(map[string]any{
			"order": []string{"missed"},
			"posts": map[string]any{"missed": map[string]any{
				"id": "missed", "user_id": "u9", "channel_id": "ch7",
				"create_at": 1800, "update_at": 1800,
				"message": "written in the gap",
			}},
		})
	}
	r := run(t, f)

	reconnected(t, next(t, r), 0) // the first connect
	if post, ok := next(t, r).(*Posted); !ok || post.PostID != "seen" {
		t.Fatalf("got %v, want the post seen before the cut", post)
	}
	if react, ok := next(t, r).(*Reaction); !ok || react.CreateAt != 1500 {
		t.Fatalf("got %v, want the reaction seen before the cut", react)
	}

	// The cut, the backoff and the return: only a second connection can
	// say this marker, and it carries the hint of everything the reader
	// ever handed out — the reaction's create_at, the greater one.
	ev, ok := next(t, r).(*Reconnected)
	if !ok {
		t.Fatalf("got %T, want *Reconnected", ev)
	}
	if ev.Since != 1500 {
		t.Fatalf("got Reconnected{Since: %d}, want 1500: the reaction's create_at, the greater one", ev.Since)
	}
	if got := f.connections(); got < 2 {
		t.Fatalf("got %d connections, want the reader back on a second", got)
	}

	// The catch-up the hint is for: the caller reads the channel over
	// REST and finds the post the socket missed.
	posts, err := mm.New(f.srv.URL, "tok").PostsSince(context.Background(), "ch7", ev.Since)
	if err != nil {
		t.Fatal(err)
	}
	if len(posts) != 1 || posts[0].ID != "missed" || posts[0].CreateAt != 1800 {
		t.Fatalf("got posts %+v, want the one written in the gap", posts)
	}
}

// Given a Mattermost that answers and then goes quiet without closing —
// a half-open socket — when the idle deadline passes, the reader drops
// it and comes back: the stall cannot hang it.
func TestStalledSocket(t *testing.T) {
	f := newFake(t)
	// Both scripts hold the connection silently after the handshake; the
	// second connection's marker is the only thing that tells them apart.
	f.scripts = []func(*websocket.Conn){authOK(still), authOK(still)}
	r := runReader(t, New(f.srv.URL, "tok").
		WithBackoff(time.Millisecond, 4*time.Millisecond).
		WithIdle(20*time.Millisecond))

	reconnected(t, next(t, r), 0) // the first connect
	// The socket stays fine-but-silent; the read deadline takes it and
	// the second connect says so within the test's time.
	reconnected(t, next(t, r), 0)
	if got := f.connections(); got < 2 {
		t.Fatalf("got %d connections, want the silent socket taken again", got)
	}
}

// Given a handshake that interleaves — a post overtakes the auth reply —
// when the reader connects, the reply is still found (by its seq_reply,
// never by being first... the reply-first order is the other legal one),
// and the early event comes out strictly AFTER the connection's
// Reconnected marker: the marker always heads a connection, so the
// caller's catch-up never reads past an event it cannot place.
func TestEarlyEventsFollowTheMarker(t *testing.T) {
	f := newFake(t)
	f.scripts = []func(*websocket.Conn){func(conn *websocket.Conn) {
		challenge, ok := readChallenge(conn)
		if !ok {
			return // the reader hung up before asking
		}
		// The reply first — the other legal order — then a post; the
		// script ends, which cuts the socket for the second connection.
		write(conn, map[string]any{"status": "OK", "seq_reply": challenge.Seq,
			"data": map[string]any{}})
		postEvent(conn, map[string]any{
			"id": "early", "user_id": "u1", "channel_id": "ch1",
			"create_at": 100, "update_at": 100, "message": "before the handshake ended",
		})
	}}
	// A second script with an event between hello (first, as the wire) and
	// the reply: the marker must still head the connection.
	f.scripts = append(f.scripts, func(conn *websocket.Conn) {
		challenge, ok := readChallenge(conn)
		if !ok {
			return
		}
		write(conn, map[string]any{"event": "hello",
			"data": map[string]any{"connection_id": "c1"}, "seq": 0})
		reactionEvent(conn, "reaction_added", "ch1", map[string]any{
			"user_id": "u2", "post_id": "early", "emoji_name": "+1",
			"create_at": 200,
		})
		write(conn, map[string]any{"status": "OK", "seq_reply": challenge.Seq,
			"data": map[string]any{}})
		still(conn)
	})
	r := run(t, f)

	// The first connection: the reply came first, the post while reading
	// normally — the marker heads it all.
	reconnected(t, next(t, r), 0)
	if post, ok := next(t, r).(*Posted); !ok || post.PostID != "early" {
		t.Fatalf("got %v, want the early post after its marker", post)
	}
	// The second connection: the reaction overtook the reply and waits —
	// the marker comes first, the reaction only then.
	reconnected(t, next(t, r), 100)
	if react, ok := next(t, r).(*Reaction); !ok || react.CreateAt != 200 {
		t.Fatalf("got %v, want the overtaking reaction after the marker", react)
	}
}

// Given a Mattermost that refuses the token once and then answers, when
// the reader runs, it retries the refusal like a dropped socket — but
// says in its log that the token was refused — and the second connection
// carries.
func TestAuthFailureRetries(t *testing.T) {
	// Log capture: the reader must log a refusal as a refusal, never as a
	// lost socket, so an operator reads what to fix.
	logs := &recorder{}
	old := slog.Default()
	slog.SetDefault(slog.New(logs))
	defer slog.SetDefault(old)

	f := newFake(t)
	// Three faces of a refused token: an in-band refusal, the silent close
	// a real Mattermost gives, and then a good one.
	f.scripts = []func(*websocket.Conn){authRefused, authClosed, authOK(still)}
	r := run(t, f)

	reconnected(t, next(t, r), 0) // the third connection's marker
	if got := f.connections(); got < 3 {
		t.Fatalf("got %d connections, want both refusals and the close retried", got)
	}
	if !logs.has("refused") {
		t.Errorf("the logs do not say the token was refused: %v", logs.msgs)
	}
}

// Given the wait's rules, when a connection ends, one that stayed up for
// steady earns the first wait again and anything else doubles up to the
// cap: the reset and the cap, pinned without sleeping either side.
func TestWaitAfter(t *testing.T) {
	r := New("http://x", "tok").WithBackoff(time.Second, 4*time.Second)
	if got := r.waitAfter(steady, 3*time.Second); got != time.Second {
		t.Errorf("got %v after a steady connection, want the first wait again (1s)", got)
	}
	if got := r.waitAfter(steady, time.Duration(1<<59)); got != time.Second {
		t.Errorf("got %v after a steady connection, want the reset", got)
	}
	if got := r.waitAfter(0, time.Second); got != 2*time.Second {
		t.Errorf("got %v after a drop, want the doubled wait", got)
	}
	if got := r.waitAfter(time.Second, 4*time.Second); got != 4*time.Second {
		t.Errorf("got %v past the cap, want the cap held", got)
	}
	if got := r.waitAfter(steady-1, 4*time.Second); got != 4*time.Second {
		t.Errorf("got %v just under steady, want the wait kept", got)
	}
}

// Given a reader on a quiet socket, when its context is cancelled, Run
// returns the context's error — a backoff never has to pass first.
func TestContextCancellation(t *testing.T) {
	f := newFake(t)
	f.scripts = []func(*websocket.Conn){authOK(still)}
	ctx, cancel := context.WithCancel(context.Background())
	r := New(f.srv.URL, "tok").WithBackoff(time.Hour, time.Hour) // no retry may pass
	done := make(chan error, 1)
	go func() { done <- r.Run(ctx) }()
	reconnected(t, next(t, r), 0) // connected, reading

	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("got %v, want context.Canceled", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return on the cancellation")
	}
}

// Given a frame bigger than the read cap, when it arrives, the socket is
// refused and dropped, nothing of it is handed out, and the reader comes
// back on the next connection and carries again.
func TestOversizedFrame(t *testing.T) {
	f := newFake(t)
	f.scripts = []func(*websocket.Conn){
		authOK(func(conn *websocket.Conn) {
			// Far over the cap: no post is that big, so the frame is a
			// broken peer's, not Mattermost's.
			conn.WriteMessage(websocket.TextMessage,
				bytes.Repeat([]byte("x"), maxFrame+1024))
		}),
		authOK(
			func(conn *websocket.Conn) {
				postEvent(conn, map[string]any{
					"id": "after", "user_id": "u1", "channel_id": "ch1",
					"create_at": 500, "update_at": 500, "message": "still reading",
				})
			},
			still,
		),
	}
	r := run(t, f)

	reconnected(t, next(t, r), 0) // the first connect

	// The oversized frame refused the socket and dropped it; the marker
	// of the second connection says the reader is back. Only then can a
	// connection count say so.
	reconnected(t, next(t, r), 0)
	if got := f.connections(); got < 2 {
		t.Fatalf("got %d connections, want the oversized frame dropped", got)
	}
	post, ok := next(t, r).(*Posted)
	if !ok {
		t.Fatalf("got %T, want *Posted", post)
	}
	if post.PostID != "after" {
		t.Errorf("got post %+v, want the one after the drop", *post)
	}
}

// recorder keeps the messages the reader logs, for a test that checks
// what an operator would read.
type recorder struct {
	mu   sync.Mutex
	msgs []string
}

func (h *recorder) Enabled(context.Context, slog.Level) bool { return true }

func (h *recorder) Handle(_ context.Context, r slog.Record) error {
	h.mu.Lock()
	h.msgs = append(h.msgs, r.Message)
	h.mu.Unlock()
	return nil
}

func (h *recorder) WithAttrs([]slog.Attr) slog.Handler { return h }

func (h *recorder) WithGroup(string) slog.Handler { return h }

// has says whether a logged message contains s.
func (h *recorder) has(s string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, msg := range h.msgs {
		if strings.Contains(msg, s) {
			return true
		}
	}
	return false
}

// Given a socket that drops after its handshake and one that stays, the
// markers number the connections and Up names the one that is up: the
// bridge's signal goes out only while the socket it caught up on is the
// live one (RT-6.42 step 7).
func TestUpNamesTheLiveConnection(t *testing.T) {
	f := newFake(t)
	f.scripts = []func(*websocket.Conn){authOK(), authOK(still)}
	r := run(t, f)
	if ev, ok := next(t, r).(*Reconnected); !ok || ev.Conn != 1 {
		t.Fatalf("first marker %+v", ev)
	}
	if ev, ok := next(t, r).(*Reconnected); !ok || ev.Conn != 2 {
		t.Fatalf("second marker %+v", ev)
	}
	deadline := time.Now().Add(2 * time.Second)
	for r.Up() != 2 {
		if time.Now().After(deadline) {
			t.Fatalf("Up %d, want 2", r.Up())
		}
		time.Sleep(5 * time.Millisecond)
	}
}
