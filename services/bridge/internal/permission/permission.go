// Package permission is the bridge's half of plan B piece 10 (RT-6.13, ADR-014
// phase 5): a session whose home runs over NATS asks the bridge, on
// <req>.permission.<agent>, to DM a Claude Code tool permission prompt to its
// responsible human; the bridge posts it in Mattermost as the agent's own bot
// (the bridge bot when the agent has none, with a first line that names the
// agent) and carries the human's answer, a reply or a reaction in that DM,
// back over NATS on <bus>.hu.dm.<agent>.<anchor token>. Everything fails
// closed: nothing is posted for a request that does not check out, and
// nothing is carried that is not the one person's own answer to that prompt.
//
// Two ops, strict JSON (unknown fields and trailing data refused):
//
//	{"op":"prompt","channel":<home>,"id":<request id>,"to":<anchor>,"name":<username>,"text":<string>}
//	  -> {"v":1,"post":"<Mattermost post id>"}
//	{"op":"note","channel":<home>,"id":<request id>,"text":<string>,"close":<bool>}
//	  -> {"v":1,"posted":true}
//
// The anchor's single Mattermost account is read from the login service's
// records; in a home with plan B piece 4 on, its binding must also be proven
// as inbound does (inbound.ProvenAnchor).
//
// Whose request it is, and whom it may reach (the agent names both, so neither
// is taken on its word): a prompt or a note is refused unless the login
// service's fresh attestation of the agent (presence.attest.<agent>.v1) states
// a home equal to the request's channel and the agent announced a fresh
// membership of that channel (presence.Snapshot.MemberFresh); both failures
// say the same "not the agent's home". A prompt's target anchor must be the
// attested owner or one of the attested trusted humans.
//
// The limits: at most PerAgent (16) open prompts per agent, PerOwner (32)
// across the agents attested to one owner and Total (256), each refused with no
// eviction of an older prompt; at most PerMinute (6) prompts a minute per agent
// and per (agent, target anchor).
package permission

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/inbound"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
)

// Piece is plan B's piece for the permission DM (ADR-014 phase 5).
const Piece = 10

// The bounds: text in runes, open prompts, and how long one stays open.
const (
	MaxPromptText = 16000
	MaxNoteText   = 2000
	PerAgent      = 16
	PerOwner      = 32
	Total         = 256
	// PerMinute is how many prompts one agent, and one agent to one person, may
	// open in a sliding minute.
	PerMinute = 6
	Lifetime  = 24 * time.Hour
	// the poll: every FastEvery during a prompt's first FastFor, then SlowEvery
	FastEvery = 2 * time.Second
	FastFor   = 10 * time.Minute
	SlowEvery = 30 * time.Second
	anchorTTL = 60 * time.Second
	// clockSlack is how far before the bridge's clock the poll starts when the
	// prompt's own time could not be read.
	clockSlack = time.Minute
	logEvery   = time.Minute
	rateWindow = time.Minute
)

// idRE is a Claude Code permission request id: five letters a-z without l.
var idRE = regexp.MustCompile(`^[a-km-z]{5}$`)

// anchorRE is the login service's anchor.
var anchorRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// Poster is what the handler does in Mattermost as one bot (mm.Client).
type Poster interface {
	DirectChannel(ctx context.Context, otherUserID string) (string, error)
	Create(ctx context.Context, channelID, text, rootID string, props map[string]any) (string, error)
	PostByID(ctx context.Context, postID string) (mm.Post, error)
	PostsSince(ctx context.Context, channelID string, since int64) ([]mm.Post, error)
}

// Bots gives an agent's own bot, joined to home, or nil while it has none.
type Bots interface {
	Bot(ctx context.Context, agent, home string) Poster
}

// Accounts is inbound.Accounts: the records of the login service.
type Accounts interface {
	Owner(anchor string) string
	Record(userID string) (inbound.Account, bool)
	Anchor(userID string) string
	Bound(userID, authService, authData string) string
}

// Users reads an account with the admin token (how it logs in).
type Users interface {
	GetUser(ctx context.Context, userID string) (mm.User, error)
}

// Switches are plan B's switches per channel.
type Switches interface {
	On(piece int, channel string) bool
}

// Presence is the memberships the agents announced (presence.Snapshot).
type Presence interface {
	MemberFresh(channel, name string) bool
}

// Attest is the login service's attestations of the agents
// (presence.Attestations): false when there is no fresh one.
type Attest interface {
	Get(agent string) (presence.Attestation, bool)
}

// Publisher publishes and returns once the server took the message
// (inbound.Confirmed).
type Publisher interface {
	Publish(subject string, data []byte) error
}

// Handler answers the permission requests and carries the answers.
type Handler struct {
	Log       *slog.Logger
	Homes     []string
	Switches  Switches
	Accounts  Accounts
	Users     Users // nil: no proof can be checked, so no home with piece 4 on
	Bots      Bots  // nil: the bridge bot posts
	Bridge    Poster
	Presence  Presence // nil: every request is refused
	Attest    Attest   // nil: every request is refused
	Publisher Publisher
	BusPrefix string           // "bus"
	Now       func() time.Time // time.Now when nil
	Tick      time.Duration    // how often Run looks for due polls; 1 s when zero

	mu      sync.Mutex
	open    map[key]*prompt
	anchors map[string]cachedAnchor
	logged  map[string]time.Time
	rates   map[string][]time.Time
}

type key struct{ agent, id string }

type cachedAnchor struct {
	anchor string
	at     time.Time
}

type prompt struct {
	home, post, dm, account, anchor, name string
	owner                                 string // the owner's anchor by the agent's attestation
	poster                                Poster
	createAt                              int64 // ms, the prompt post's
	opened, next                          time.Time
	pending                               bool // reserved, not posted yet
	seenPosts, seenReactions              map[string]bool
	warnedAnchor                          bool
}

type payload struct {
	Op      string  `json:"op"`
	Channel string  `json:"channel"`
	ID      string  `json:"id"`
	To      *string `json:"to"`
	Name    *string `json:"name"`
	Text    string  `json:"text"`
	Close   *bool   `json:"close"`
}

func (h *Handler) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// warn logs at most one line per key a minute.
func (h *Handler) warn(k, msg string, args ...any) {
	h.mu.Lock()
	now := h.now()
	if last, ok := h.logged[k]; ok && now.Sub(last) < logEvery {
		h.mu.Unlock()
		return
	}
	if h.logged == nil || len(h.logged) > 1024 {
		h.logged = map[string]time.Time{}
	}
	h.logged[k] = now
	h.mu.Unlock()
	h.Log.Warn(msg, args...)
}

// Piece answers one permission request of agent (the subject's last token).
func (h *Handler) Piece(ctx context.Context, agent string, req requests.Request) ([]requests.Reply, error) {
	var p payload
	dec := json.NewDecoder(bytes.NewReader(req.Payload))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil || dec.More() {
		return nil, errors.New(`a permission request's payload is {"op":"prompt","channel","id","to","name","text"}` +
			` or {"op":"note","channel","id","text","close"}`)
	}
	if !slices.Contains(h.Homes, p.Channel) {
		return nil, fmt.Errorf("the bridge serves no home %q", p.Channel)
	}
	if !h.Switches.On(Piece, p.Channel) {
		return nil, fmt.Errorf("plan B piece 10 is off in ~%s", p.Channel)
	}
	if !idRE.MatchString(p.ID) {
		return nil, errors.New("id is no permission request id (five letters a-z without l)")
	}
	if p.Op != "prompt" && p.Op != "note" {
		return nil, fmt.Errorf("op %q is not prompt or note", p.Op)
	}
	att, err := h.attested(agent, p.Channel)
	if err != nil {
		return nil, err
	}
	if p.Op == "prompt" {
		if p.Close != nil {
			return nil, errors.New("a prompt has no close")
		}
		return h.prompt(ctx, agent, p, att)
	}
	if p.To != nil || p.Name != nil {
		return nil, errors.New("a note has no to or name")
	}
	return h.note(ctx, agent, p)
}

// attested is the agent's fresh attestation, once it states channel as the
// agent's home and the agent announced a fresh membership there. Whichever of
// the two fails, the error is the same: it tells nothing about other agents.
func (h *Handler) attested(agent, channel string) (presence.Attestation, error) {
	if h.Attest == nil {
		return presence.Attestation{}, errors.New("no fresh attestation of the agent")
	}
	att, ok := h.Attest.Get(agent)
	if !ok {
		return presence.Attestation{}, errors.New("no fresh attestation of the agent")
	}
	if !att.HasHome || att.Home != channel || h.Presence == nil || !h.Presence.MemberFresh(channel, agent) {
		return presence.Attestation{}, errors.New("not the agent's home")
	}
	return att, nil
}

// text checks and defuses one text.
func text(s string, max int) (string, error) {
	if !utf8.ValidString(s) {
		return "", errors.New("text is not valid UTF-8")
	}
	if strings.TrimSpace(s) == "" {
		return "", errors.New("text is empty")
	}
	if utf8.RuneCountInString(s) > max {
		return "", fmt.Errorf("text is longer than %d characters", max)
	}
	return mirror.DefuseMentions(s), nil
}

func result(v any) ([]requests.Reply, error) {
	out, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	return []requests.Reply{{Result: out}}, nil
}

func (h *Handler) prompt(ctx context.Context, agent string, p payload, att presence.Attestation) ([]requests.Reply, error) {
	body, err := text(p.Text, MaxPromptText)
	if err != nil {
		return nil, err
	}
	if p.To == nil || !anchorRE.MatchString(*p.To) {
		return nil, errors.New("to is no anchor")
	}
	to := *p.To
	if !att.Names(to) {
		return nil, errors.New("not the agent's owner or one of its trusted humans")
	}
	if p.Name == nil || *p.Name == "" {
		return nil, errors.New("name is empty")
	}
	account := h.Accounts.Owner(to)
	if account == "" {
		return nil, errors.New("no single Mattermost account is bound to that person")
	}
	rec, ok := h.Accounts.Record(account)
	if !ok || !strings.EqualFold(rec.Name, *p.Name) {
		return nil, errors.New("the account's name is not the one given")
	}
	got, err := h.anchorOf(ctx, p.Channel, account, false)
	if err != nil {
		return nil, fmt.Errorf("the account was not read: %w", err)
	}
	if got != to {
		return nil, errors.New("the person's binding is not proven")
	}
	k := key{agent, p.ID}
	pr, err := h.reserve(k, p.Channel, to, att.Owner)
	if err != nil {
		return nil, err
	}
	poster, own := h.poster(ctx, agent, p.Channel)
	if poster == nil {
		h.forget(k, pr)
		return nil, errors.New("the bridge has no bot to post with")
	}
	if !own {
		body = "From agent `" + agent + "`:\n\n" + body
	}
	post, createAt, dm, err := h.send(ctx, poster, account, body)
	if err != nil {
		h.forget(k, pr)
		return nil, err
	}
	h.mu.Lock()
	pr.home, pr.post, pr.dm, pr.account, pr.anchor, pr.name = p.Channel, post, dm, account, to, rec.Name
	pr.poster, pr.createAt, pr.pending = poster, createAt, false
	pr.next = h.now()
	h.mu.Unlock()
	return result(struct {
		V    int    `json:"v"`
		Post string `json:"post"`
	}{1, post})
}

// poster is the agent's own bot, or the bridge bot (own false).
func (h *Handler) poster(ctx context.Context, agent, home string) (poster Poster, own bool) {
	if h.Bots != nil {
		if b := h.Bots.Bot(ctx, agent, home); b != nil {
			return b, true
		}
	}
	if h.Bridge == nil {
		return nil, false
	}
	return h.Bridge, false
}

func (h *Handler) send(ctx context.Context, poster Poster, account, body string) (post string, createAt int64, dm string, err error) {
	dm, err = poster.DirectChannel(ctx, account)
	if err != nil {
		return "", 0, "", fmt.Errorf("the direct channel: %w", err)
	}
	post, err = poster.Create(ctx, dm, body, "", nil)
	if err != nil {
		return "", 0, "", fmt.Errorf("the post: %w", err)
	}
	p, err := poster.PostByID(ctx, post)
	if err != nil || p.CreateAt <= 0 {
		// The DM is out: an error now would leave it with nobody carrying its answer.
		// The bridge's clock less a minute bounds the poll instead; an answer still has
		// to name this prompt (its id, or its thread), so an earlier bound carries no
		// other post.
		h.warn("time "+post, "permission: the prompt's time was not read; polling from the bridge's clock",
			"error", err)
		return post, h.now().Add(-clockSlack).UnixMilli(), dm, nil
	}
	return post, p.CreateAt, dm, nil
}

// reserve takes the (agent, id) slot before anything is posted, within the
// limits: open prompts of the agent, of its owner and in all, then the rates
// of the agent and of the agent to this person. Nothing is evicted.
func (h *Handler) reserve(k key, home, to, owner string) (*prompt, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.open == nil {
		h.open = map[key]*prompt{}
	}
	h.expireLocked()
	if _, dup := h.open[k]; dup {
		return nil, errors.New("already open")
	}
	perAgent, perOwner := 0, 0
	for ok, o := range h.open {
		if ok.agent == k.agent {
			perAgent++
		}
		if o.owner == owner {
			perOwner++
		}
	}
	switch {
	case perAgent >= PerAgent:
		return nil, errors.New("too many open prompts of this agent")
	case perOwner >= PerOwner:
		return nil, errors.New("too many open prompts of this agent's owner")
	case len(h.open) >= Total:
		return nil, errors.New("too many open prompts")
	}
	now := h.now()
	ka, kt := "a\x00"+k.agent, "t\x00"+k.agent+"\x00"+to
	ra, rt := h.recent(ka, now), h.recent(kt, now)
	if len(ra) >= PerMinute || len(rt) >= PerMinute {
		return nil, errors.New("too many prompts; try again in a minute")
	}
	if len(h.rates) > 1024 {
		h.rates = map[string][]time.Time{}
	}
	h.rates[ka], h.rates[kt] = append(ra, now), append(rt, now)
	pr := &prompt{home: home, owner: owner, pending: true, opened: now,
		seenPosts: map[string]bool{}, seenReactions: map[string]bool{}}
	h.open[k] = pr
	return pr, nil
}

// recent is the times under key within the last rateWindow; h.mu is held.
func (h *Handler) recent(key string, now time.Time) []time.Time {
	if h.rates == nil {
		h.rates = map[string][]time.Time{}
	}
	var out []time.Time
	for _, t := range h.rates[key] {
		if now.Sub(t) < rateWindow {
			out = append(out, t)
		}
	}
	return out
}

func (h *Handler) forget(k key, pr *prompt) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.open[k] == pr {
		delete(h.open, k)
	}
}

// expireLocked forgets what opened more than Lifetime ago.
func (h *Handler) expireLocked() {
	now := h.now()
	for k, p := range h.open {
		if now.Sub(p.opened) >= Lifetime {
			delete(h.open, k)
		}
	}
}

func (h *Handler) note(ctx context.Context, agent string, p payload) ([]requests.Reply, error) {
	body, err := text(p.Text, MaxNoteText)
	if err != nil {
		return nil, err
	}
	k := key{agent, p.ID}
	h.mu.Lock()
	h.expireLocked()
	pr := h.open[k]
	if pr == nil || pr.pending || pr.home != p.Channel {
		h.mu.Unlock()
		return nil, errors.New("no open prompt")
	}
	poster, dm, root := pr.poster, pr.dm, pr.post
	h.mu.Unlock()
	if p.Close != nil && *p.Close {
		defer h.forget(k, pr)
	}
	if _, err := poster.Create(ctx, dm, body, root, nil); err != nil {
		return nil, fmt.Errorf("the note: %w", err)
	}
	return result(struct {
		V      int  `json:"v"`
		Posted bool `json:"posted"`
	}{1, true})
}

// anchorOf is the anchor the account binds in home now, "" when none: with
// plan B piece 4 on there, only as the proof holds (inbound.anchorOf's rule,
// inbound.ProvenAnchor; no admin client: nothing), else by the record alone.
// A cached answer is at most anchorTTL old.
func (h *Handler) anchorOf(ctx context.Context, home, account string, cached bool) (string, error) {
	ck := account + "@" + home
	if cached {
		h.mu.Lock()
		c, ok := h.anchors[ck]
		h.mu.Unlock()
		if ok && h.now().Sub(c.at) < anchorTTL {
			return c.anchor, nil
		}
	}
	anchor := ""
	if h.Switches.On(inbound.ProofPiece, home) {
		if h.Users == nil {
			return "", nil
		}
		u, err := h.Users.GetUser(ctx, account)
		if err != nil {
			return "", err
		}
		anchor = inbound.ProvenAnchor(h.Accounts, account, u)
	} else {
		anchor = h.Accounts.Anchor(account)
	}
	h.mu.Lock()
	if h.anchors == nil || len(h.anchors) > 1024 {
		h.anchors = map[string]cachedAnchor{}
	}
	h.anchors[ck] = cachedAnchor{anchor, h.now()}
	h.mu.Unlock()
	return anchor, nil
}
