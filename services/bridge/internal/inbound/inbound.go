// Package inbound carries people's posts and reactions in the bound home
// channels from Mattermost onto NATS (RT-6.42, step 2 of ADR-016): it takes
// the WebSocket reader's events (internal/mmws), drops what is no person's
// own post or reaction, maps the account to the person's anchor from the
// login service's records, publishes on `<bus>.hu.<home>.<anchor>` once per
// key, catches up over REST after a reconnect, and signals each home it
// carries on `presence.bridge.<home>.v1`. The author is the event's user id
// alone, never a name, a prop or the text.
package inbound

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mmws"
)

// Mattermost is what the bridge reads there.
type Mattermost interface {
	Me(ctx context.Context) (string, error)
	GetUser(ctx context.Context, userID string) (mm.User, error)
	PostsSince(ctx context.Context, channelID string, since int64) ([]mm.Post, error)
	ChannelIDByName(ctx context.Context, team, name string) (string, error)
	// ServerTime is Mattermost's clock (ms): the watermark is on its clock.
	ServerTime(ctx context.Context) (int64, error)
}

// Publisher publishes without a reply subject (the BUS stream acknowledges
// nothing) and returns only once the server took it: a refused publish (a
// missing right) is an error, before anything is marked carried (Confirmed).
type Publisher interface {
	Publish(subject string, data []byte) error
}

// Store is the part of BUS_BRIDGE this step reads and writes.
type Store interface {
	Seq(ctx context.Context, home, post string) (uint64, bool, error)
	Watermark(ctx context.Context, home string) (int64, bool, error)
	SetWatermark(ctx context.Context, home string, ms int64) error
}

// Seen is BUS_BRIDGE_SEEN (store.Seen).
type Seen interface {
	Post(ctx context.Context, post string) (bool, error)
	MarkPost(ctx context.Context, post string) error
	Reactions(ctx context.Context, post string) (map[string]string, error)
	SetReactions(ctx context.Context, post string, kinds map[string]string) error
}

// Anchors maps a Mattermost account to an anchor, "" when unbound
// (Accounts.Bound): by its id, and only while it logs in as its binding was
// proven (#266). No home carries before CaughtUp: until the records are read,
// a bound person would go out under "-".
type Anchors interface {
	Bound(userID, authService, authData string) string
	CaughtUp() bool
}

// The defaults of the step's bounds (RT-6.42 steps 1 and 7).
const (
	DefaultReach       = 10 * time.Minute // the catch-up reaches back at most this far
	DefaultMargin      = 5 * time.Second  // and from the watermark minus this
	DefaultSignalEvery = time.Minute
	DefaultSignalValid = 3 * time.Minute
	DefaultRetry       = 15 * time.Second // a stopped home tries again this often
	userTTL            = 10 * time.Minute
)

// Bridge is the inbound path of all homes.
type Bridge struct {
	Homes     []string
	Team      string
	BusPrefix string // "bus"
	Log       *slog.Logger
	MM        Mattermost
	// Users reads the accounts behind people's posts and reactions; nil is MM.
	// Only a system admin's token sees how an account logs in (auth_service,
	// auth_data), which binds it to a person (RT-6.42 step 3, #266): the
	// bridge's admin bot's (CB_BRIDGE_MM_ADMIN_TOKEN_FILE). With the posting
	// bot's token, which may hold no such role, no account is bound.
	Users interface {
		GetUser(ctx context.Context, userID string) (mm.User, error)
	}
	NATS    Publisher
	Store   Store
	Seen    Seen
	Anchors Anchors
	Events  <-chan mmws.Event
	Up      func() uint64 // mmws.Reader.Up: the connection that is up now
	Now     func() time.Time
	// BotPrefix is the agents' bot prefix, set only while the mirror posts
	// under the agents' own bots (RT-6.44): the signal names it then.
	BotPrefix string

	Reach, Margin, SignalEvery, SignalValid, Retry time.Duration

	me     string
	offset int64 // Mattermost's clock minus the bridge's, ms, from the last catch-up
	homes  []*home
	byCID  map[string]*home
	users  map[string]cachedUser
}

type home struct {
	name string
	cid  string
	// caughtUp is the connection this home caught up on; 0 before it did,
	// and while the home is stopped.
	caughtUp uint64
	stopped  bool
	logged   time.Time // the last log of its stop, for the pulse
	wm       int64
}

type cachedUser struct {
	u  mm.User
	at time.Time
}

func (b *Bridge) defaults() {
	if b.Now == nil {
		b.Now = time.Now
	}
	if b.Reach == 0 {
		b.Reach = DefaultReach
	}
	if b.Margin == 0 {
		b.Margin = DefaultMargin
	}
	if b.SignalEvery == 0 {
		b.SignalEvery = DefaultSignalEvery
	}
	if b.SignalValid == 0 {
		b.SignalValid = DefaultSignalValid
	}
	if b.Retry == 0 {
		b.Retry = DefaultRetry
	}
	if b.BusPrefix == "" {
		b.BusPrefix = "bus"
	}
	b.users = map[string]cachedUser{}
	b.byCID = map[string]*home{}
	b.homes = b.homes[:0]
	for _, name := range b.Homes {
		b.homes = append(b.homes, &home{name: name, stopped: true})
	}
}

// Run carries until ctx ends.
func (b *Bridge) Run(ctx context.Context) error {
	b.defaults()
	signal := time.NewTicker(b.SignalEvery)
	defer signal.Stop()
	retry := time.NewTicker(b.Retry)
	defer retry.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case ev := <-b.Events:
			switch ev := ev.(type) {
			case *mmws.Reconnected:
				for _, h := range b.homes {
					b.catchUp(ctx, h, ev.Conn)
				}
			case *mmws.Posted:
				if h := b.byCID[ev.ChannelID]; h != nil && b.carrying(h) && b.records(h) {
					b.guard(h, b.post(ctx, h, fromLive(ev), false, 0))
				}
			case *mmws.Reaction:
				if h := b.byCID[ev.ChannelID]; h != nil && b.carrying(h) && b.records(h) {
					b.guard(h, b.reaction(ctx, h, ev.PostID, ev.UserID, ev.EmojiName, ev.CreateAt, ev.Added, false))
				}
			}
		case <-signal.C:
			for _, h := range b.homes {
				b.signal(ctx, h)
			}
		case <-retry.C:
			if up := b.Up(); up != 0 {
				for _, h := range b.homes {
					if h.stopped {
						b.catchUp(ctx, h, up)
					}
				}
			}
		}
	}
}

// carrying: a stopped home takes no live event; its catch-up, when it runs
// again, reads them from the watermark on.
func (b *Bridge) carrying(h *home) bool { return !h.stopped && h.caughtUp != 0 }

// records: while the account records are read afresh (a new consumer), a
// home carries nothing and stops; its retry catches up once they are read.
func (b *Bridge) records(h *home) bool {
	if b.Anchors.CaughtUp() {
		return true
	}
	b.guard(h, errRecordsPending)
	return false
}

// guard stops a home on an error it cannot carry past (a refused bucket write,
// Mattermost out of reach): the signal stops with it, so sessions read that
// channel's people on Mattermost again, and the retry catches up later.
func (b *Bridge) guard(h *home, err error) {
	if err == nil || errors.Is(err, context.Canceled) {
		return
	}
	if errors.Is(err, errRecordsPending) {
		h.stopped, h.caughtUp = true, 0 // waits quietly: no failure
		return
	}
	switch {
	case !h.stopped:
		b.Log.Error("inbound: a home stopped carrying people; retrying", "home", h.name, "error", err)
		h.logged = b.Now()
	case b.Now().Sub(h.logged) >= time.Minute:
		// a pulse while the retries keep failing, not one line per retry
		b.Log.Warn("inbound: a home still does not carry people; retrying", "home", h.name, "error", err)
		h.logged = b.Now()
	}
	h.stopped, h.caughtUp = true, 0
}

// ready is the signal's condition: the socket this home caught up on is the
// one that is up, the home is carrying, and the account records are read (not
// while a new consumer reads them afresh).
func (b *Bridge) ready(h *home) bool {
	return !h.stopped && h.caughtUp != 0 && h.caughtUp == b.Up() && b.Anchors.CaughtUp()
}

// signal publishes the home's signal (RT-6.42 step 7) while it is ready, and
// moves the watermark of a quiet home along: while the socket is up, what is
// older than a minute has come in live, so a later catch-up need not reach
// back to the last carried post (and warn of a gap that was none).
func (b *Bridge) signal(ctx context.Context, h *home) {
	if !b.ready(h) {
		return
	}
	now := b.Now()
	body, err := marshal(struct {
		V      int    `json:"v"`
		Home   string `json:"home"`
		People bool   `json:"people"`
		Iat    int64  `json:"iat"`
		Exp    int64  `json:"exp"`
		// only while on: a host before that release refuses the field
		BotPrefix string `json:"bot_prefix,omitempty"`
	}{1, h.name, true, now.Unix(), now.Add(b.SignalValid).Unix(), b.BotPrefix})
	if err == nil {
		err = b.NATS.Publish("presence.bridge."+h.name+".v1", body)
	}
	if err != nil {
		b.Log.Warn("inbound: the signal was not published", "home", h.name, "error", err)
	}
	if len(b.Events) == 0 {
		// only with nothing queued: a queued event may be older than the slack
		b.guard(h, b.advance(ctx, h, now.UnixMilli()+b.offset-liveSlack.Milliseconds()))
	}
}

// liveSlack is how far behind Mattermost's clock a connected home's watermark
// moves without a carried event: what the socket handed out but the bridge
// has not taken yet stays after it.
const liveSlack = time.Minute

// catchUp reads a home's posts since its watermark over REST and carries what
// the socket may have missed (RT-6.42 step 1, point 11). Nothing from before
// the first start is carried: a home without a watermark gets Mattermost's
// clock as one. The read is carried oldest first and the watermark moves only
// once the whole read went through, to Mattermost's clock at the read's start:
// a read that fails half way is read again from the old watermark, and a reply
// never goes out before its root.
func (b *Bridge) catchUp(ctx context.Context, h *home, conn uint64) {
	err := func() error {
		if !b.Anchors.CaughtUp() {
			return errRecordsPending
		}
		if b.me == "" {
			me, err := b.MM.Me(ctx)
			if err != nil {
				return err
			}
			b.me = me
		}
		if h.cid == "" {
			cid, err := b.MM.ChannelIDByName(ctx, b.Team, h.name)
			if err != nil {
				return err
			}
			h.cid = cid
			b.byCID[cid] = h
		}
		local := b.Now()
		now, err := b.MM.ServerTime(ctx)
		if err != nil {
			return err
		}
		b.offset = now - local.UnixMilli()
		wm, ok, err := b.Store.Watermark(ctx, h.name)
		if err != nil {
			return fmt.Errorf("BUS_BRIDGE: %w", err)
		}
		if !ok {
			if err := b.Store.SetWatermark(ctx, h.name, now); err != nil {
				return fmt.Errorf("BUS_BRIDGE: %w", err)
			}
			h.wm = now
			b.Log.Info("inbound: first start of a home; nothing from before it is carried",
				"home", h.name, "watermark", now)
			return nil
		}
		h.wm = wm
		lower := wm - b.Margin.Milliseconds()
		if floor := now - b.Reach.Milliseconds(); lower < floor {
			b.Log.Warn("inbound: a gap longer than the catch-up reaches; older posts are not carried",
				"home", h.name, "watermark", wm, "from", floor)
			lower = floor
		}
		posts, err := b.MM.PostsSince(ctx, h.cid, lower)
		if err != nil {
			return err
		}
		sort.SliceStable(posts, func(i, j int) bool {
			if posts[i].CreateAt != posts[j].CreateAt {
				return posts[i].CreateAt < posts[j].CreateAt
			}
			return posts[i].ID < posts[j].ID
		})
		for _, p := range posts {
			if p.ChannelID != "" && p.ChannelID != h.cid {
				continue
			}
			if p.CreateAt > lower {
				if err := b.post(ctx, h, fromREST(p), true, lower); err != nil {
					return err
				}
			}
			if err := b.reactionsOf(ctx, h, p, lower); err != nil {
				return err
			}
		}
		return b.advance(ctx, h, now)
	}()
	if err != nil {
		b.guard(h, err)
		return
	}
	if h.stopped {
		b.Log.Info("inbound: carrying people", "home", h.name)
	}
	h.stopped, h.caughtUp = false, conn
	b.signal(ctx, h)
}

// reactionsOf carries what the read says changed in a post's reactions: a
// reaction after the bound that was not carried, and a removal of one that
// was. Mattermost moves a post's update_at on every added or removed
// reaction, so the `since` read returns it with its current reactions. A
// reaction on an agent's post (an ask) is carried like any other; a deleted
// post's reactions are not, its removal is no person taking one back (point 5).
func (b *Bridge) reactionsOf(ctx context.Context, h *home, p mm.Post, lower int64) error {
	if p.DeleteAt != 0 || !mmID.MatchString(p.ID) {
		return nil
	}
	if !p.HasMetadata {
		// Mattermost always prepares the read with metadata; one without it
		// says nothing about reactions, so no removal is read from it. That
		// is a gap only where the bridge carried a reaction on the post.
		kinds, err := b.Seen.Reactions(ctx, p.ID)
		if err != nil {
			return fmt.Errorf("BUS_BRIDGE_SEEN: %w", err)
		}
		if len(kinds) > 0 {
			b.Log.Warn("inbound: a post came back without metadata; its reactions are not compared",
				"home", h.name, "post", p.ID)
		}
		return nil
	}
	reactions := append([]mm.Reaction(nil), p.Reactions...)
	sort.SliceStable(reactions, func(i, j int) bool { return reactions[i].CreateAt < reactions[j].CreateAt })
	current := map[string]bool{}
	for _, r := range reactions {
		current[r.EmojiName+" "+r.UserID] = true
		if r.CreateAt > lower {
			if err := b.reaction(ctx, h, p.ID, r.UserID, r.EmojiName, r.CreateAt, true, true); err != nil {
				return err
			}
		}
	}
	kinds, err := b.Seen.Reactions(ctx, p.ID)
	if err != nil {
		return fmt.Errorf("BUS_BRIDGE_SEEN: %w", err)
	}
	keys := make([]string, 0, len(kinds))
	for key := range kinds {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		if kinds[key] != "reaction" || current[key] {
			continue
		}
		emoji, uid, _ := strings.Cut(key, " ")
		if err := b.reaction(ctx, h, p.ID, uid, emoji, 0, false, true); err != nil {
			return err
		}
	}
	return nil
}

// post is a post from the socket or the catch-up, in one shape.
type post struct {
	id, user, root, kind, text string
	props                      map[string]any
	files                      int
	createAt, editAt, deleteAt int64
}

func fromLive(p *mmws.Posted) post {
	return post{id: p.PostID, user: p.UserID, root: p.RootID, kind: p.Type, text: p.Message,
		props: p.Props, files: p.FileCount, createAt: p.CreateAt, editAt: p.EditAt, deleteAt: p.DeleteAt}
}

func fromREST(p mm.Post) post {
	return post{id: p.ID, user: p.UserID, root: p.RootID, kind: p.Type, text: p.Message,
		props: p.Props, files: p.FileCount, createAt: p.CreateAt, editAt: p.EditAt, deleteAt: p.DeleteAt}
}

// dropReason is why a post is no person's own (RT-6.42 step 2), "" when it is
// one. The bot mark needs Mattermost and is checked after.
func (b *Bridge) dropReason(p post, caught bool) string {
	switch {
	case !mmID.MatchString(p.id) || !mmID.MatchString(p.user) || (p.root != "" && !mmID.MatchString(p.root)):
		return "malformed id"
	case p.user == b.me:
		return "the bridge's own"
	case p.kind != "":
		return "not an ordinary post"
	case p.deleteAt != 0:
		return "deleted"
	case !caught && p.editAt != 0:
		// a new post is never edited; an edit is not carried (point 5)
		return "edited"
	}
	if _, ok := p.props["from_webhook"]; ok {
		return "from a webhook"
	}
	if _, ok := p.props["override_username"]; ok {
		return "override_username"
	}
	return ""
}

// post carries one person's post once. In a catch-up it carries only what was
// created after lower; `since` also returns older posts that were edited, and
// those never go out again.
func (b *Bridge) post(ctx context.Context, h *home, p post, caught bool, lower int64) error {
	if caught && p.createAt <= lower {
		return nil
	}
	if why := b.dropReason(p, caught); why != "" {
		b.Log.Info("inbound: a post dropped", "home", h.name, "post", p.id, "why", why)
		return nil
	}
	u, err := b.user(ctx, p.user)
	if gone(err) {
		b.Log.Info("inbound: a post dropped", "home", h.name, "post", p.id, "why", "no such account")
		return nil
	}
	if err != nil {
		return err
	}
	if u.IsBot {
		b.Log.Info("inbound: a post dropped", "home", h.name, "post", p.id, "why", "a bot's")
		return nil
	}
	done, err := b.Seen.Post(ctx, p.id)
	if err != nil {
		return fmt.Errorf("BUS_BRIDGE_SEEN: %w", err)
	}
	if done {
		return nil
	}
	body := PostBody{Text: p.text, Name: u.Username, MM: p.id, MMRoot: p.root, Files: p.files}
	if p.root != "" {
		seq, ok, err := b.Store.Seq(ctx, h.name, p.root)
		if err != nil {
			return fmt.Errorf("BUS_BRIDGE: %w", err)
		}
		if ok {
			body.Root = seq
		}
	}
	raw, err := EncodePost(body)
	if err != nil {
		b.Log.Warn("inbound: a post that does not encode, dropped", "home", h.name, "post", p.id, "error", err)
		return nil
	}
	if err := b.publish(h, u, raw, "post", p.id); err != nil {
		return err
	}
	if err := b.Seen.MarkPost(ctx, p.id); err != nil {
		return fmt.Errorf("BUS_BRIDGE_SEEN: %w", err)
	}
	if caught {
		return nil // the catch-up moves the watermark once, at its end
	}
	return b.advance(ctx, h, p.createAt)
}

// reaction carries one person's reaction or its removal once per kind: the key
// is the post, emoji, sender and kind, and the opposite kind clears it.
func (b *Bridge) reaction(ctx context.Context, h *home, postID, userID, emoji string, at int64,
	added, caught bool) error {
	if !mmID.MatchString(postID) || !mmID.MatchString(userID) || !emojiRE.MatchString(emoji) {
		b.Log.Info("inbound: a reaction dropped", "home", h.name, "why", "malformed")
		return nil
	}
	if userID == b.me {
		return nil
	}
	u, err := b.user(ctx, userID)
	if gone(err) {
		b.Log.Info("inbound: a reaction dropped", "home", h.name, "post", postID, "why", "no such account")
		return nil
	}
	if err != nil {
		return err
	}
	if u.IsBot {
		return nil
	}
	kind := "unreaction"
	if added {
		kind = "reaction"
	}
	kinds, err := b.Seen.Reactions(ctx, postID)
	if err != nil {
		return fmt.Errorf("BUS_BRIDGE_SEEN: %w", err)
	}
	key := emoji + " " + userID
	if kinds[key] == kind {
		return nil
	}
	body := ReactionBody{Emoji: emoji, Name: u.Username, MM: postID}
	seq, ok, err := b.Store.Seq(ctx, h.name, postID)
	if err != nil {
		return fmt.Errorf("BUS_BRIDGE: %w", err)
	}
	if ok {
		body.Ref = seq
	}
	raw, err := EncodeReaction(body, added)
	if err != nil {
		return err
	}
	if err := b.publish(h, u, raw, kind, postID); err != nil {
		return err
	}
	kinds[key] = kind
	if err := b.Seen.SetReactions(ctx, postID, kinds); err != nil {
		return fmt.Errorf("BUS_BRIDGE_SEEN: %w", err)
	}
	if caught {
		return nil
	}
	return b.advance(ctx, h, at)
}

// publish sends on <bus>.hu.<home>.<anchor>, "-" for an unbound account, and
// logs one INFO line once NATS has taken it: the home, what it was (post,
// reaction or unreaction), the Mattermost post id and the subject, never the
// text or a name beyond the anchor in the subject (RT-6.42 step 4).
func (b *Bridge) publish(h *home, u mm.User, body []byte, what, mmPost string) error {
	subject := b.BusPrefix + ".hu." + h.name + "." + SenderToken(b.anchorOf(u))
	if err := b.NATS.Publish(subject, body); err != nil {
		return fmt.Errorf("publish %s: %w", subject, err)
	}
	b.Log.Info("inbound: carried", "home", h.name, "what", what, "post", mmPost, "subject", subject)
	return nil
}

// advance moves the watermark to the newest create_at carried; a removal has
// none (0) and moves nothing.
func (b *Bridge) advance(ctx context.Context, h *home, at int64) error {
	if at <= h.wm {
		return nil
	}
	if err := b.Store.SetWatermark(ctx, h.name, at); err != nil {
		return fmt.Errorf("BUS_BRIDGE: %w", err)
	}
	h.wm = at
	return nil
}

// user reads an account from Mattermost, cached: whether it is a bot is
// Mattermost's to say (point 6), and its username is shown.
func (b *Bridge) user(ctx context.Context, id string) (mm.User, error) {
	if c, ok := b.users[id]; ok && b.Now().Sub(c.at) < userTTL {
		return c.u, nil
	}
	var users interface {
		GetUser(ctx context.Context, userID string) (mm.User, error)
	} = b.MM
	if b.Users != nil {
		users = b.Users
	}
	u, err := users.GetUser(ctx, id)
	if err != nil {
		return mm.User{}, err
	}
	b.users[id] = cachedUser{u: u, at: b.Now()}
	return u, nil
}

// anchorOf is the person a post or reaction of u goes out under (RT-6.42
// step 3, #266): the anchor the login service bound the account to, while
// Mattermost shows it active, no guest, and logging in as its binding was
// proven (the same auth_service and auth_data, read with the bridge's admin
// token, cached as the user is); "" for every other account. A deactivated
// account, a guest, or one whose login changed binds nothing.
func (b *Bridge) anchorOf(u mm.User) string {
	if u.IsBot || u.Deactivated || u.Guest {
		return ""
	}
	return b.Anchors.Bound(u.ID, u.AuthService, u.AuthData)
}

// gone: Mattermost says the account does not exist; nobody to carry.
func gone(err error) bool {
	var se *mm.StatusError
	return errors.As(err, &se) && se.Status == 404
}

// errRecordsPending: the account records are not read yet (Accounts.CaughtUp).
var errRecordsPending = errors.New("inbound: the account records are not read yet")
