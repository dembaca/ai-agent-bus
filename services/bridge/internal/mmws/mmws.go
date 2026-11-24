// Package mmws reads Mattermost's WebSocket: it dials with the bridge
// bot's token, reconnects with backoff when the socket drops, and emits
// typed events for the three of Mattermost's events that people's posts
// and reactions need — posted, reaction_added, reaction_removed — plus a
// Reconnected marker that carries the catch-up hint. mmws emits what
// Mattermost says and nothing else: whose post is a bot's (a drop) or a
// person's (carried on NATS) is the caller's decision, so it filters,
// stores and publishes nowhere and reads no NATS.
package mmws

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

// Posted is one `posted` event: what somebody wrote in a channel.
type Posted struct {
	PostID    string
	UserID    string
	ChannelID string
	RootID    string
	CreateAt  int64 // ms, Mattermost's clock
	UpdateAt  int64
	EditAt    int64 // 0 when the post was never edited
	Message   string
}

// Reaction is one `reaction_added` (Added true) or `reaction_removed`
// (Added false) event. Its CreateAt bounds it, not its post's.
type Reaction struct {
	PostID    string
	UserID    string
	EmojiName string
	CreateAt  int64 // ms, Mattermost's clock
	Added     bool
}

// Reconnected says the reader is back on a socket. Since is the greatest
// CreateAt of the posts and reactions this reader handed out before — 0
// when it handed out none — the hint for the caller's catch-up read. It
// is a hint, not a promise: the caller may have carried more from its
// own reading, and what it carries stays its decision.
type Reconnected struct {
	Since int64
}

// Event is what the reader emits: *Posted, *Reaction or *Reconnected.
type Event interface{ event() }

func (*Posted) event()      {}
func (*Reaction) event()    {}
func (*Reconnected) event() {}

// The backoff's shape and the frame cap, Mattermost's clock aside:
//
//	first, max — start 1 s, double per drop, stop at 30 s. A connection
//	that stayed up for steady resets the wait: a socket that drops every
//
// second waits longer each time, one that carried the bus for half a
// minute starts over.
//
//	maxFrame — a bigger frame is a broken peer, not a post; the socket is
//	dropped and taken again (Mattermost's longest message is far below).
const (
	firstBackoff = time.Second
	maxBackoff   = 30 * time.Second
	steady       = 30 * time.Second
	maxFrame     = 1 << 20 // 1 MiB
)

// Reader reads Mattermost's WebSocket until its context ends. Nothing
// else stops it: a refused dial, a refused token or a lost socket waits
// the backoff and connects again. One Reader runs from one goroutine;
// Run is its only entry.
type Reader struct {
	base   string // Mattermost's base URL; the ws:// URL is derived from it
	token  string // the bridge bot's token, sent as the auth challenge
	first  time.Duration
	max    time.Duration
	events chan Event

	since int64 // the greatest CreateAt handed out, posts and reactions alike
}

// New returns a reader for baseURL (no trailing slash) with a bot token.
// The backoff starts at 1 s and doubles per drop up to 30 s; WithBackoff
// changes both, which the tests do to keep their waits short.
func New(baseURL, token string) *Reader {
	return &Reader{
		base:   strings.TrimRight(baseURL, "/"),
		token:  token,
		first:  firstBackoff,
		max:    maxBackoff,
		events: make(chan Event, 128),
	}
}

// WithBackoff sets the reconnect backoff: the first wait after a drop and
// its cap. Both can be as small as a test may sleep; a cap below the
// first waits the first forever.
func (r *Reader) WithBackoff(first, max time.Duration) *Reader {
	if first > 0 {
		r.first = first
	}
	if max > 0 {
		r.max = max
	}
	return r
}

// Events is where the typed events arrive. Run stops reading from
// Mattermost while nobody takes them: an event is never dropped on the
// floor to keep the socket moving.
func (r *Reader) Events() <-chan Event { return r.events }

// Run connects, reads and reconnects until ctx is cancelled, whose error
// it then returns. The Reconnected marker goes out after every successful
// connect, the first one included (with the Since of everything seen so
// far, 0 at a cold start).
func (r *Reader) Run(ctx context.Context) error {
	wait := r.first
	for ctx.Err() == nil {
		if up := r.connect(ctx); up >= steady {
			wait = r.first // the socket carried the bus for a while: start over
		}
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
		wait = min(2*wait, r.max)
	}
	return ctx.Err()
}

// connect dials, proves the token and reads one socket to its end. It
// returns how long the socket was up — 0 when it never came up — after
// logging why it ended; Run decides the wait, never the caller.
func (r *Reader) connect(ctx context.Context) time.Duration {
	addr, err := r.url()
	if err != nil {
		slog.Error("mmws: not a Mattermost base URL; retrying", "error", err)
		return 0
	}
	conn, _, err := websocket.DefaultDialer.DialContext(ctx, addr, nil)
	if err != nil {
		slog.Error("mmws: no Mattermost WebSocket; retrying",
			"url", addr, "error", err)
		return 0
	}
	defer conn.Close()
	if err := r.prove(ctx, conn); err != nil {
		var auth *AuthError
		if errors.As(err, &auth) {
			// Distinctly logged: a refused token is not a lost socket, it
			// answers and says no — and the retry costs the same, since the
			// token may be re-issued while the bridge runs.
			slog.Error("mmws: the bot token was refused; retrying",
				"url", addr, "status", auth.Status)
		} else {
			slog.Error("mmws: the auth challenge failed; retrying",
				"url", addr, "error", err)
		}
		return 0
	}
	r.emit(ctx, &Reconnected{Since: r.since})
	up := time.Now()
	r.read(ctx, conn)
	return time.Since(up)
}

// AuthError is Mattermost's refusal of the bot token: the socket is fine,
// the answer says the token is no good. It is retried like a dropped
// connection, only logged as what it is.
type AuthError struct {
	Status string // Mattermost's reply status, never "OK"
}

func (e *AuthError) Error() string {
	return "mmws: the auth challenge was refused: status " + e.Status
}

// prove sends Mattermost's auth challenge and waits for its reply. The
// token then lives in the socket alone; nothing else of the connection is
// asked again.
func (r *Reader) prove(ctx context.Context, conn *websocket.Conn) error {
	challenge := struct {
		Seq    int               `json:"seq"`
		Action string            `json:"action"`
		Data   map[string]string `json:"data"`
	}{Seq: 1, Action: "auth_challenge", Data: map[string]string{"token": r.token}}
	// The write is bounded too: a server that stalls after the handshake
	// must not outlive the context.
	conn.SetWriteDeadline(time.Now().Add(20 * time.Second))
	if err := conn.WriteJSON(challenge); err != nil {
		return fmt.Errorf("mmws: sending the auth challenge: %w", err)
	}
	conn.SetReadDeadline(time.Now().Add(20 * time.Second))
	var reply struct {
		Status string `json:"status"`
	}
	err := conn.ReadJSON(&reply)
	conn.SetReadDeadline(time.Time{})
	if err != nil {
		return fmt.Errorf("mmws: reading the auth reply: %w", err)
	}
	if reply.Status != "OK" {
		return &AuthError{Status: reply.Status}
	}
	return nil
}

// read hands out typed events until the socket drops or the context ends.
// Every event but the three is ignored — Mattermost sends many, and none
// of them may stop the reader.
func (r *Reader) read(ctx context.Context, conn *websocket.Conn) {
	conn.SetReadLimit(maxFrame)
	// Answer pings; a pong is all Mattermost asks of a quiet client.
	conn.SetPingHandler(func(ping string) error {
		err := conn.WriteControl(websocket.PongMessage, []byte(ping),
			time.Now().Add(5*time.Second))
		if err == websocket.ErrCloseSent {
			return nil
		}
		return err
	})
	// A blocked read ends only with the socket: the context closes it.
	stop := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			conn.Close()
		case <-stop:
		}
	}()
	defer close(stop)
	for {
		var frame struct {
			Event string          `json:"event"`
			Data  json.RawMessage `json:"data"`
		}
		if err := conn.ReadJSON(&frame); err != nil {
			if ctx.Err() == nil {
				slog.Warn("mmws: the WebSocket dropped; reconnecting", "error", err)
			}
			return
		}
		switch frame.Event {
		case "posted":
			r.takePosted(ctx, frame.Data)
		case "reaction_added", "reaction_removed":
			r.takeReaction(ctx, frame.Data, frame.Event == "reaction_added")
		default:
			// hello, typing and every other event: not the reader's business
		}
	}
}

// takePosted decodes one `posted` event. Mattermost carries the post as
// a JSON string inside the event's data, not as an object.
func (r *Reader) takePosted(ctx context.Context, data json.RawMessage) {
	var outer struct {
		Post string `json:"post"`
	}
	if err := json.Unmarshal(data, &outer); err != nil || outer.Post == "" {
		slog.Warn("mmws: a posted event without its post; ignored")
		return
	}
	var post struct {
		ID        string `json:"id"`
		UserID    string `json:"user_id"`
		ChannelID string `json:"channel_id"`
		RootID    string `json:"root_id"`
		CreateAt  int64  `json:"create_at"`
		UpdateAt  int64  `json:"update_at"`
		EditAt    int64  `json:"edit_at"`
		Message   string `json:"message"`
	}
	if err := json.Unmarshal([]byte(outer.Post), &post); err != nil {
		slog.Warn("mmws: a post that does not decode; ignored", "error", err)
		return
	}
	r.since = max(r.since, post.CreateAt)
	r.emit(ctx, &Posted{
		PostID:    post.ID,
		UserID:    post.UserID,
		ChannelID: post.ChannelID,
		RootID:    post.RootID,
		CreateAt:  post.CreateAt,
		UpdateAt:  post.UpdateAt,
		EditAt:    post.EditAt,
		Message:   post.Message,
	})
}

// takeReaction decodes one `reaction_added` or `reaction_removed`. The
// reaction too travels as a JSON string inside the event's data.
func (r *Reader) takeReaction(ctx context.Context, data json.RawMessage, added bool) {
	var outer struct {
		Reaction string `json:"reaction"`
	}
	if err := json.Unmarshal(data, &outer); err != nil || outer.Reaction == "" {
		slog.Warn("mmws: a reaction event without its reaction; ignored")
		return
	}
	var reaction struct {
		UserID    string `json:"user_id"`
		PostID    string `json:"post_id"`
		EmojiName string `json:"emoji_name"`
		CreateAt  int64  `json:"create_at"`
	}
	if err := json.Unmarshal([]byte(outer.Reaction), &reaction); err != nil {
		slog.Warn("mmws: a reaction that does not decode; ignored", "error", err)
		return
	}
	r.since = max(r.since, reaction.CreateAt)
	r.emit(ctx, &Reaction{
		PostID:    reaction.PostID,
		UserID:    reaction.UserID,
		EmojiName: reaction.EmojiName,
		CreateAt:  reaction.CreateAt,
		Added:     added,
	})
}

// emit hands the event out. It waits for a taker as long as the context
// lives, so an event is never lost to a full channel.
func (r *Reader) emit(ctx context.Context, ev Event) {
	select {
	case r.events <- ev:
	case <-ctx.Done():
	}
}

// url derives the WebSocket URL from the base URL: https becomes wss,
// http ws, on Mattermost's own path below whatever path the base carries
// (a Mattermost behind a proxy keeps its prefix).
func (r *Reader) url() (string, error) {
	u, err := url.Parse(r.base)
	if err != nil {
		return "", fmt.Errorf("mmws: %q: %w", r.base, err)
	}
	switch u.Scheme {
	case "https":
		u.Scheme = "wss"
	case "http":
		u.Scheme = "ws"
	default:
		return "", fmt.Errorf("mmws: %q: neither http nor https", r.base)
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/api/v4/websocket"
	return u.String(), nil
}
