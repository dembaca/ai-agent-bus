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
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/netloc"
)

// Posted is one `posted` event: what somebody wrote in a channel, with
// the fields the drop rules read — the post's type and props, how many
// files it carries and its delete mark — because a post brought back by
// a catch-up or a live stream may be deleted, and only the caller may
// drop it.
type Posted struct {
	PostID    string
	UserID    string
	ChannelID string
	RootID    string
	Type      string         // Mattermost's post type (a custom one, a join…)
	Props     map[string]any // the post's props: fromusername, attachments…
	FileCount int            // its `file_ids` length (model/post.go's field)
	CreateAt  int64          // ms, Mattermost's clock
	UpdateAt  int64
	EditAt    int64 // 0 when the post was never edited
	DeleteAt  int64 // past 0: the post is deleted (a live edit of a deletion)
	Message   string
}

// Reaction is one `reaction_added` (Added true) or `reaction_removed`
// (Added false) event, with the channel it happened in — Mattermost
// broadcasts the post's channel beside the reaction, so the caller needs
// no extra read to place it.
//
// A removed reaction carries CreateAt 0: Mattermost's own delete handler
// sends the bare object it was handed (verified against server/channels/
// api4/reaction.go), so a removal has no time of its own. It never lowers
// this reader's hint, and the caller bounds a removal it caught up by the
// post it belongs to, not by the clock.
type Reaction struct {
	PostID    string
	UserID    string
	ChannelID string
	EmojiName string
	CreateAt  int64 // ms, Mattermost's clock; 0 on a removal
	Added     bool
}

// Reconnected says the reader is back on a socket. Since is the greatest
// CreateAt of the posts and reactions this reader handed out before — 0
// when it handed out none — the hint for the caller's catch-up read. It
// is a hint, not a promise: the caller may have carried more from its
// own reading, and what it carries stays its decision. Mattermost's
// `since` returns only what is strictly after its bound, so the caller
// reads from Since minus a margin (some seconds) and dedups the posts by
// their id and the reactions by their post, user, emoji and kind — the
// live path may have handed out what the read finds again.
//
// Conn numbers the connection (1, 2, …): the caller that caught up after
// this marker compares it with Up, so a socket that dropped and came back
// while the caller was still reading is not taken for the one it caught up
// on (the bridge's signal, RT-6.42 step 7, goes out only while both agree).
type Reconnected struct {
	Since int64
	Conn  uint64
}

// Event is what the reader emits: *Posted, *Reaction or *Reconnected.
type Event interface{ event() }

func (*Posted) event()      {}
func (*Reaction) event()    {}
func (*Reconnected) event() {}

// The backoff's shape, the socket's patience and the frame cap:
// wait starts at a second and doubles per drop up to half a minute; a
// socket that stayed up for steady earns the start again (waitAfter).
// A read quiet beyond idle takes the socket again — a half-open
// connection must not hang the reader — and Mattermost pings far more
// often, so idle only ever fires on a broken peer. A frame past
// maxFrame is a broken peer's, not a post: dropped and taken again,
// during the auth challenge too. The whole challenge, write and reply,
// has authWait; more than maxEarly events before the reply is a broken
// peer's flood, not Mattermost's queue (as many as Events holds).
const (
	firstBackoff = time.Second
	maxBackoff   = 30 * time.Second
	steady       = 30 * time.Second
	readIdle     = 150 * time.Second
	maxFrame     = 1 << 20 // 1 MiB
	authWait     = 20 * time.Second
	maxEarly     = 128
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
	idle   time.Duration
	steady time.Duration // the uptime that earns the first wait again; tests shorten it
	auth   time.Duration // the whole auth challenge's bound; tests shorten it
	http   bool          // plain http to a host that is not loopback allowed (WithPlainHTTP)
	events chan Event

	since int64         // the greatest CreateAt handed out, posts and reactions alike
	conns uint64        // connections proven so far
	up    atomic.Uint64 // the live connection's number, 0 while none is up
}

// Up is the number of the connection that is up now (Reconnected.Conn),
// 0 while the socket is down. It may be read from any goroutine.
func (r *Reader) Up() uint64 { return r.up.Load() }

// New returns a reader for baseURL (no trailing slash) with a bot token.
// The backoff starts at 1 s and doubles per drop up to 30 s; WithBackoff
// changes both, which the tests do to keep their waits short.
func New(baseURL, token string) *Reader {
	return &Reader{
		base:   strings.TrimRight(baseURL, "/"),
		token:  token,
		first:  firstBackoff,
		max:    maxBackoff,
		idle:   readIdle,
		steady: steady,
		auth:   authWait,
		events: make(chan Event, maxEarly),
	}
}

// WithBackoff sets the reconnect backoff: the first wait after a drop
// and the cap the doubling stops at. WithIdle sets the read deadline; a
// quiet socket is dropped then. Both take what a test may sleep.
func (r *Reader) WithBackoff(first, cap time.Duration) *Reader {
	if first > 0 {
		r.first = first
	}
	if cap > 0 {
		r.max = cap
	}
	return r
}

// WithIdle sets how long one read may stay quiet before the socket is
// taken again: half-open protection, which the tests shorten.
func (r *Reader) WithIdle(idle time.Duration) *Reader {
	if idle > 0 {
		r.idle = idle
	}
	return r
}

// WithPlainHTTP allows an http base URL whose host is not loopback. The
// token then crosses the network in clear text (the auth challenge over
// ws://), so it is off by default: an http URL to any other host is
// refused, and only a lab on a network it trusts turns it on
// (CB_BRIDGE_MM_ALLOW_HTTP). A loopback host — a test, a local lab — needs
// no opt-in: its traffic never leaves the machine.
func (r *Reader) WithPlainHTTP(allow bool) *Reader {
	r.http = allow
	return r
}

// Events is where the typed events arrive. Run stops reading from
// Mattermost while nobody takes them: an event is never dropped on the
// floor to keep the socket moving, a wait past a few seconds is logged,
// and a consumer that stalls for good shows up as recurring reconnects
// (unanswered pings close the socket, and the catch-up covers the gap —
// nothing is lost, only the socket is).
func (r *Reader) Events() <-chan Event { return r.events }

// Run connects, reads and reconnects until ctx is cancelled, whose error
// it then returns. The Reconnected marker goes out after every successful
// connect, the first one included (with the Since of everything seen so
// far, 0 at a cold start).
//
// The wait is decided from the connection that just ended, before it is
// slept: a socket that held for steady reconnects after the first wait,
// not after what earlier failures had built up (#131).
func (r *Reader) Run(ctx context.Context) error {
	var wait time.Duration // none yet: the first drop waits `first`
	for ctx.Err() == nil {
		up := r.connect(ctx)
		wait = r.waitAfter(up, wait)
		select {
		case <-ctx.Done():
		case <-time.After(wait):
		}
	}
	return ctx.Err()
}

// waitAfter says what Run waits after a connection ended: one that
// stayed up for steady earned the first wait again — a socket that
// carried the bus for a while starts over — and so does the first drop
// (last 0); anything else doubles the last, capped at max. The first
// wait is `first` whatever max says.
func (r *Reader) waitAfter(up, last time.Duration) time.Duration {
	if up >= r.steady || last <= 0 {
		return r.first
	}
	return min(2*last, r.max)
}

// connect dials, proves the token and reads one socket to its end. It
// returns how long the socket was up — 0 when it never came up — after
// logging why it ended; Run decides the wait, never the caller.
func (r *Reader) connect(ctx context.Context) time.Duration {
	u, err := r.url()
	if err != nil {
		slog.Error("mmws: not a Mattermost base URL; retrying", "error", err)
		return 0
	}
	conn, _, err := websocket.DefaultDialer.DialContext(ctx, u.String(), nil)
	if err != nil {
		slog.Error("mmws: no Mattermost WebSocket; retrying",
			"url", u.Redacted(), "error", err)
		return 0
	}
	defer conn.Close()
	// The frame cap holds from the first frame on: a peer must not send
	// an unbounded one while the reader waits for the auth reply.
	conn.SetReadLimit(maxFrame)
	// The context ends this socket from the first byte — the auth wait
	// included, whose own deadline a cancellation must not have to wait
	// out. The watcher leaves with the connection (stop below).
	stop := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			conn.Close()
		case <-stop:
		}
	}()
	defer close(stop)
	// The hint is snapshotted before the handshake: an event that
	// overtakes the reply is parsed (its create_at raises the running
	// hint) but not handed out until after the marker — so the marker
	// must say what was truly handed out before this connection, or the
	// caller reading with a strict since would skip the early events.
	hint := r.since
	var early []Event
	if early, err = r.prove(ctx, conn); err != nil {
		var auth *AuthError
		if errors.As(err, &auth) {
			// Distinctly logged: a refused token is not a lost socket, it
			// answers and says no — and the retry costs the same, since the
			// token may be re-issued while the bridge runs.
			slog.Error("mmws: the bot token was refused; retrying",
				"url", u.Redacted(), "status", auth.Status)
		} else {
			// A real Mattermost closes the socket on a bad token rather
			// than answering, so a lost handshake reads as what it most
			// likely is — the operator then checks the token first.
			slog.Error("mmws: the socket ended during the auth challenge;"+
				" likely a refused token; retrying",
				"url", u.Redacted(), "error", err)
		}
		// The handshake failed: whatever events it parsed were never handed
		// out, so the hint forgets them — the next marker says only what
		// the caller truly received.
		r.since = hint
		return 0
	}
	// The marker first, whatever the handshake interleaved: the caller
	// ends its catch-up reading where the marker of the next connect
	// says, so an early event must never claim to predate it.
	r.conns++
	r.up.Store(r.conns)
	defer r.up.Store(0)
	r.emit(ctx, &Reconnected{Since: hint, Conn: r.conns})
	for _, ev := range early {
		r.emit(ctx, ev)
	}
	up := time.Now()
	r.read(ctx, conn)
	return time.Since(up)
}

// AuthError is a refusal that answers: a status other than OK in the
// seq_reply frame. A real Mattermost is less polite — a bad token gets
// the socket closed after the challenge, which reads as the drop it
// then behaves like — but a server that says no is named as saying no.
type AuthError struct {
	Status string // the reply's status, never "OK"
}

func (e *AuthError) Error() string {
	return "mmws: the auth challenge was refused: status " + e.Status
}

// prove sends Mattermost's auth challenge and waits for its reply —
// wherever it sits in the stream. The reply carries `seq_reply` and the
// events do not (mattermost's wire model, model/websocket_message.go:
// hello is queued before the reply, and channel events may also come
// first), so the reader takes frames until the reply with its own
// sequence lands. An event that arrives first is carried out with the
// connection's own marker after it, and a reply with any status but OK
// is the refusal.
//
// The whole challenge has one deadline (auth), not one per frame: a peer
// that keeps sending events and never the reply fails it in time, and
// one that floods more than maxEarly events before it fails it at once
// (#131).
func (r *Reader) prove(ctx context.Context, conn *websocket.Conn) ([]Event, error) {
	challenge := struct {
		Seq    int               `json:"seq"`
		Action string            `json:"action"`
		Data   map[string]string `json:"data"`
	}{Seq: 1, Action: "authentication_challenge", Data: map[string]string{"token": r.token}}
	// The write is bounded too: a server that stalls after the handshake
	// must not outlive the context.
	deadline := time.Now().Add(r.auth)
	conn.SetWriteDeadline(deadline)
	conn.SetReadDeadline(deadline)
	if err := conn.WriteJSON(challenge); err != nil {
		return nil, fmt.Errorf("mmws: sending the auth challenge: %w", err)
	}
	// The events that overtake the reply wait here, and connect hands
	// them out after its own marker — theirs, but never before it.
	var early []Event
	for {
		var frame struct {
			Event     string          `json:"event"`
			Status    string          `json:"status"`
			SeqReply  int             `json:"seq_reply"`
			Data      json.RawMessage `json:"data"`
			Broadcast struct {
				ChannelID string `json:"channel_id"`
			} `json:"broadcast"`
		}
		if err := conn.ReadJSON(&frame); err != nil {
			return nil, fmt.Errorf("mmws: reading the auth reply: %w", err)
		}
		if frame.SeqReply == 0 {
			// an event before the reply (hello first is legal): kept for
			// the connection's own marker, which always goes out first
			if ev := r.typedEvent(frame.Event, frame.Data, frame.Broadcast.ChannelID); ev != nil {
				if len(early) == maxEarly {
					return nil, fmt.Errorf("mmws: more than %d events before the auth reply", maxEarly)
				}
				early = append(early, ev)
			}
			continue
		}
		if frame.Status != "OK" {
			return nil, &AuthError{Status: frame.Status}
		}
		return early, nil
	}
}

// read hands out typed events until the socket drops or the context ends.
// Every event but the three is ignored — Mattermost sends many, and none
// of them may stop the reader — and each read is bounded by the idle
// deadline, renewed by every frame and every ping, so a half-open socket
// is taken again instead of hanging.
func (r *Reader) read(ctx context.Context, conn *websocket.Conn) {
	// Answer pings; a pong is all Mattermost asks of a quiet client.
	conn.SetPingHandler(func(ping string) error {
		conn.SetReadDeadline(time.Now().Add(r.idle))
		err := conn.WriteControl(websocket.PongMessage, []byte(ping),
			time.Now().Add(5*time.Second))
		if err == websocket.ErrCloseSent {
			return nil
		}
		return err
	})
	// A blocked read ends only with the socket: connect's watcher closes
	// it on the context.
	for {
		conn.SetReadDeadline(time.Now().Add(r.idle))
		var frame struct {
			Event     string          `json:"event"`
			Data      json.RawMessage `json:"data"`
			Broadcast struct {
				ChannelID string `json:"channel_id"`
			} `json:"broadcast"`
		}
		if err := conn.ReadJSON(&frame); err != nil {
			if ctx.Err() == nil {
				slog.Warn("mmws: the WebSocket dropped; reconnecting", "error", err)
			}
			return
		}
		r.takeEvent(ctx, frame.Event, frame.Data, frame.Broadcast.ChannelID)
	}
}

// takeEvent hands one frame out as its typed event, or ignores it when
// it is none of the reader's three.
func (r *Reader) takeEvent(ctx context.Context, event string, data json.RawMessage,
	channelID string) {
	if ev := r.typedEvent(event, data, channelID); ev != nil {
		r.emit(ctx, ev)
	}
}

// typedEvent parses one frame into its typed event — nil for every event
// that is none of the reader's three (hello, typing and the rest) —
// updating the hint on the ones it takes.
func (r *Reader) typedEvent(event string, data json.RawMessage,
	channelID string) Event {
	switch event {
	case "posted":
		return r.typedPosted(data)
	case "reaction_added", "reaction_removed":
		return r.typedReaction(data, channelID, event == "reaction_added")
	default:
		return nil
	}
}

// typedPosted parses one `posted` event (the post as a JSON string
// inside the data, as Mattermost carries it) into its event, or nil
// when the frame does not decode — never an error past a logged
// warning, so one broken event cannot end the reader.
func (r *Reader) typedPosted(data json.RawMessage) Event {
	var outer struct {
		Post string `json:"post"`
	}
	if err := json.Unmarshal(data, &outer); err != nil || outer.Post == "" {
		slog.Warn("mmws: a posted event without its post; ignored")
		return nil
	}
	var post struct {
		ID        string         `json:"id"`
		UserID    string         `json:"user_id"`
		ChannelID string         `json:"channel_id"`
		RootID    string         `json:"root_id"`
		Type      string         `json:"type"`
		Props     map[string]any `json:"props"`
		FileIDs   []string       `json:"file_ids"`
		CreateAt  int64          `json:"create_at"`
		UpdateAt  int64          `json:"update_at"`
		EditAt    int64          `json:"edit_at"`
		DeleteAt  int64          `json:"delete_at"`
		Message   string         `json:"message"`
	}
	if err := json.Unmarshal([]byte(outer.Post), &post); err != nil {
		slog.Warn("mmws: a post that does not decode; ignored", "error", err)
		return nil
	}
	r.since = max(r.since, post.CreateAt)
	return &Posted{
		PostID:    post.ID,
		UserID:    post.UserID,
		ChannelID: post.ChannelID,
		RootID:    post.RootID,
		Type:      post.Type,
		Props:     post.Props,
		FileCount: len(post.FileIDs),
		CreateAt:  post.CreateAt,
		UpdateAt:  post.UpdateAt,
		EditAt:    post.EditAt,
		DeleteAt:  post.DeleteAt,
		Message:   post.Message,
	}
}

// takeReaction decodes one `reaction_added` or `reaction_removed`. The
// reaction travels as a JSON string inside the event's data, the channel
// in the broadcast beside it; a removal's create_at is 0 (its struct's
// comment says why).
// typedReaction parses one reaction event into its event, or nil when
// the frame does not decode (see typedPosted).
func (r *Reader) typedReaction(data json.RawMessage, channelID string,
	added bool) Event {
	var outer struct {
		Reaction string `json:"reaction"`
	}
	if err := json.Unmarshal(data, &outer); err != nil || outer.Reaction == "" {
		slog.Warn("mmws: a reaction event without its reaction; ignored")
		return nil
	}
	var reaction struct {
		UserID    string `json:"user_id"`
		PostID    string `json:"post_id"`
		EmojiName string `json:"emoji_name"`
		CreateAt  int64  `json:"create_at"`
	}
	if err := json.Unmarshal([]byte(outer.Reaction), &reaction); err != nil {
		slog.Warn("mmws: a reaction that does not decode; ignored", "error", err)
		return nil
	}
	r.since = max(r.since, reaction.CreateAt)
	return &Reaction{
		PostID:    reaction.PostID,
		UserID:    reaction.UserID,
		ChannelID: channelID,
		EmojiName: reaction.EmojiName,
		CreateAt:  reaction.CreateAt,
		Added:     added,
	}
}

// emit hands the event out. It waits for a taker as long as the context
// lives, so an event is never lost to a full channel; a wait long enough
// to starve the socket is logged, because the taker is who must know.
func (r *Reader) emit(ctx context.Context, ev Event) {
	start := time.Now()
	select {
	case r.events <- ev:
	case <-ctx.Done():
		return
	}
	if took := time.Since(start); took > 5*time.Second {
		slog.Warn("mmws: no taker for the events; the reader paused",
			"blocked", took)
	}
}

// url derives the WebSocket URL from the base URL: https becomes wss,
// http ws, on Mattermost's own path below whatever path the base carries
// (a Mattermost behind a proxy keeps its prefix). For the logs the
// caller redacts it; the wire only ever sees the whole form. An http URL
// would carry the token in clear text: it is refused unless its host is
// loopback or WithPlainHTTP allowed it.
func (r *Reader) url() (*url.URL, error) {
	u, err := url.Parse(r.base)
	if err != nil {
		return nil, fmt.Errorf("mmws: the base URL does not parse: %w", err)
	}
	switch u.Scheme {
	case "https":
		u.Scheme = "wss"
	case "http":
		if !r.http && !netloc.Loopback(u.Hostname()) {
			return nil, fmt.Errorf("mmws: %q: http to a host that is not loopback "+
				"would send the bot token in clear text; use https", u.Redacted())
		}
		u.Scheme = "ws"
	default:
		return nil, fmt.Errorf("mmws: %s: neither http nor https", u.Redacted())
	}
	u.Path = strings.TrimRight(u.Path, "/") + "/api/v4/websocket"
	return u, nil
}
