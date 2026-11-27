// Package history answers the bridge's history read (ADR-014 phase 5, plan B
// piece 5): a session asks on <req>.history.<agent> what people wrote while it
// was away, and the bridge answers from Mattermost with that agent's own
// bridge bot (RT-6.44), only in a channel the agent announced a membership of
// (RT-6.37) and only while piece 5 is switched on there (RT-6.39 step 5).
//
// The request's payload is JSON:
//
//	{"channel": "<channel token>", "since": <epoch ms>, "limit": <posts>}
//	{"channel": "<channel token>", "thread": "<Mattermost post id>", "limit": <posts>}
//
// channel is the channel's token on the bus, the same the membership
// (presence.member.<channel>.<name>) and the plan B switch carry; the bridge
// resolves it to its Mattermost channel by name in CB_BRIDGE_MM_TEAM. since is
// a channel read's lower bound on create_at, at most 14 days old; thread reads
// that post's thread instead and refuses a since beside it. limit is 1 to 200
// posts, 200 without it.
//
// The result is one part of an answer (the bridge numbers the parts 1..n and
// marks the last, so a caller reads until `last`):
//
//	{"v": 1, "channel": "<channel token>", "channel_id": "<Mattermost id>",
//	 "thread": "<root id>", "posts": [ ... ]}
//
// and the last part adds {"next": <create_at ms>, "more": true} while posts
// remain past what this answer carried (the byte or count bound, or
// Mattermost's own 1000-post `since` cap): the caller then asks again with
// since=next and drops a post it already has by id (the bound is inclusive).
// A thread read is bounded by the limit, and `more` says its read hit that
// bound: the thread is longer.
//
// A post is Mattermost's own shape, the fields the MCP server's reads use:
// id, user_id, channel_id, root_id, type, message, props, file_count,
// create_at, update_at, delete_at, and metadata.reactions where the read
// carried metadata. Beside them:
//
//   - anchor is the post author's anchor from the login service's account
//     records (inbound.Accounts), "" for an account no record binds;
//   - account is that record, in the shape the login service publishes it
//     ({"v":1,"account","anchor","name","approver","token_iat","iat","exp"}),
//     absent when no record binds the account. The session checks it by
//     RT-6.42's freshness rule exactly as it checks a live person's event, so
//     an approval read back from history counts only with a fresh approver
//     record, and a post without one approves nothing. The bridge answers the
//     record it holds now, never one of the post's time, and judges neither
//     its `exp` nor its `approver`;
//   - bot marks a post whose author is a bot account (an agent's post, an ask,
//     a copy); such a post names no person and carries no record;
//   - each reaction of metadata carries its sender's anchor and account in the
//     same way, and a reaction of a bot is left out.
//
// A post that is nobody's ordinary post is left out: a deleted one, one of a
// non-empty type (a join, a header change), and a webhook's (from_webhook or
// override_username in its props). An edited post is answered as Mattermost
// holds it now.
package history

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"regexp"
	"sync"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/inbound"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
)

// The piece's number in plan B's switches (RT-6.39 step 5): a channel without
// it reads no history through the bridge.
const Piece = 5

const (
	// MaxPosts is the most posts one answer carries, and the default limit.
	MaxPosts = 200
	// MaxAge is how far back a channel read may start: never the whole
	// channel history.
	MaxAge = 14 * 24 * time.Hour
	// maxPartBytes is one part's result: a caller holds every part in memory.
	maxPartBytes = 128 << 10
	// maxAnswerBytes bounds all parts of one answer together.
	maxAnswerBytes = 512 << 10
	// requestTimeout bounds one read, so one slow channel stalls no other
	// agent's request for longer.
	requestTimeout = 30 * time.Second
	// userCacheMax bounds the bot marks kept between reads.
	userCacheMax = 4096
)

// errNotChannel is the answer for a channel the agent did not announce a
// membership of. It names neither the channel's switch nor its posts.
var errNotChannel = errors.New("not a channel of that agent")

// channelRE is a channel's token on the bus, the same presence and plan B use.
var channelRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// BotClient is the agents' own bots: a Mattermost client for the agent's own
// token, joined to the channel, or the error why it cannot be had. It is
// agentbots.Bots (its Client method), and it must never be the bridge bot.
type BotClient interface {
	Client(ctx context.Context, name, channel string) (*mm.Client, error)
}

// Membership is the presence snapshot's memberships: whether the agent
// announced a membership of the channel within MemberKeep (RT-6.37).
type Membership interface {
	MemberFresh(channel, name string) bool
}

// Switches are plan B's switches per channel (presence.Switches).
type Switches interface {
	On(piece int, channel string) bool
}

// People are the login service's account records (inbound.Accounts): the
// anchor an account binds, the record itself, and whether the records have
// been read at all.
type People interface {
	Anchor(userID string) string
	Record(userID string) (inbound.Account, bool)
	CaughtUp() bool
}

// Handler answers one history request. Every field is set once at start.
type Handler struct {
	Log      *slog.Logger
	Team     string // CB_BRIDGE_MM_TEAM
	Bots     BotClient
	Presence Membership
	Switches Switches
	People   People
	Now      func() time.Time // nil: time.Now

	mu     sync.Mutex
	isBots map[string]bool // Mattermost user id -> is_bot, for accounts without a record
}

// Piece is the requests.Handler.Handlers entry for the kind "history".
func (h *Handler) Piece(ctx context.Context, agent string, req requests.Request) ([]requests.Reply, error) {
	p, err := decode(req.Payload)
	if err != nil {
		return nil, err
	}
	// The membership first: a channel that is not the agent's is refused
	// alike whether the piece is on there or not, so the answer tells nothing
	// about another agent's channels.
	if h.Presence == nil || !h.Presence.MemberFresh(p.Channel, agent) {
		return nil, errNotChannel
	}
	if h.Switches == nil || !h.Switches.On(Piece, p.Channel) {
		return nil, fmt.Errorf("piece 5 is off in %s", p.Channel)
	}
	if p.Thread == "" && p.Since <= 0 {
		return nil, errors.New("the history read needs since or a thread")
	}
	if p.Thread != "" && p.Since != 0 {
		return nil, errors.New("since and thread name two reads; give one")
	}
	if p.Since != 0 && p.Since < h.now().Add(-MaxAge).UnixMilli() {
		return nil, fmt.Errorf("since is older than %s; read no further back", MaxAge)
	}
	if h.Bots == nil {
		return nil, errors.New("the bridge has no bots for the agents; a history read needs the agent's own")
	}
	if h.People == nil || !h.People.CaughtUp() {
		return nil, errors.New("the bridge's account records are not read yet; ask again")
	}
	limit := p.Limit
	if limit <= 0 || limit > MaxPosts {
		limit = MaxPosts
	}

	ctx, cancel := context.WithTimeout(ctx, requestTimeout)
	defer cancel()
	bot, err := h.Bots.Client(ctx, agent, p.Channel)
	if err != nil {
		return nil, fmt.Errorf("the agent's bot: %w", err)
	}
	cid, err := bot.ChannelIDByName(ctx, h.Team, p.Channel)
	if err != nil {
		return nil, fmt.Errorf("the channel ~%s: %w", p.Channel, err)
	}

	var posts []mm.Post
	capped := false // the read hit its own bound: posts may remain
	if p.Thread != "" {
		posts, err = bot.Thread(ctx, p.Thread, limit)
		if err != nil {
			return nil, err
		}
		if len(posts) == 0 {
			return nil, fmt.Errorf("post %s has no thread", p.Thread)
		}
		for _, post := range posts {
			if post.ChannelID != cid {
				return nil, fmt.Errorf("the thread is not in ~%s", p.Channel)
			}
		}
		// The thread is the root's, also when a reply's id was asked: the
		// answer names the root the caller would read.
		if posts[0].RootID == "" {
			p.Thread = posts[0].ID
		} else {
			p.Thread = posts[0].RootID
		}
		capped = len(posts) >= limit
	} else {
		posts, err = bot.PostsSince(ctx, cid, p.Since)
		if err != nil {
			return nil, err
		}
		// Mattermost's since answers by update_at, an edited or reacted post
		// of any age among the rest: a history read keeps what was written
		// from since on.
		read := len(posts)
		kept := posts[:0]
		for _, post := range posts {
			if post.CreateAt >= p.Since {
				kept = append(kept, post)
			}
		}
		posts = kept
		capped = read >= 1000 // Mattermost's own since cap: the read may be cut
	}

	shaped := make([]answerPost, 0, len(posts))
	for i, post := range posts {
		if p.Thread != "" {
			// The root is the thread's context even when the root itself is
			// a bot's (an ask) or gone; a reply follows the drop rules.
			if i > 0 && drop(post) {
				continue
			}
		} else if drop(post) {
			continue
		}
		shaped = append(shaped, h.shape(ctx, bot, post))
	}

	return h.parts(p, cid, shaped, limit, capped), nil
}

// now is the handler's clock.
func (h *Handler) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// payload is the request's body.
type payload struct {
	Channel string `json:"channel"`
	Since   int64  `json:"since"`
	Thread  string `json:"thread"`
	Limit   int    `json:"limit"`
}

// decode reads the payload strictly: one object, no unknown key, so a caller
// that misspells a bound hears it instead of reading the whole channel.
func decode(raw json.RawMessage) (payload, error) {
	var p payload
	if len(bytes.TrimSpace(raw)) == 0 {
		return p, errors.New("the history read has no payload")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil {
		return p, fmt.Errorf("the history read's payload: %w", err)
	}
	var after json.RawMessage
	if err := dec.Decode(&after); !errors.Is(err, io.EOF) {
		return p, errors.New("the history read's payload: data after the object")
	}
	if !channelRE.MatchString(p.Channel) {
		return p, fmt.Errorf("channel %q is no channel token", p.Channel)
	}
	return p, nil
}

// answerPost is one post of the answer: Mattermost's fields the MCP server's
// reads use, plus the author's anchor and record.
type answerPost struct {
	ID        string         `json:"id"`
	UserID    string         `json:"user_id"`
	ChannelID string         `json:"channel_id"`
	RootID    string         `json:"root_id"`
	Type      string         `json:"type,omitempty"`
	Message   string         `json:"message"`
	Props     map[string]any `json:"props,omitempty"`
	FileCount int            `json:"file_count,omitempty"`
	CreateAt  int64          `json:"create_at"`
	UpdateAt  int64          `json:"update_at"`
	DeleteAt  int64          `json:"delete_at,omitempty"`
	Metadata  *answerMeta    `json:"metadata,omitempty"`
	Anchor    string         `json:"anchor"`
	Account   *answerRecord  `json:"account,omitempty"`
	Bot       bool           `json:"bot,omitempty"`
}

// answerMeta is Mattermost's post metadata, as far as a read carries it.
type answerMeta struct {
	Reactions []answerReaction `json:"reactions"`
}

// answerReaction is one reaction: Mattermost's fields plus its sender's
// anchor and record, as a post's author has them.
type answerReaction struct {
	UserID    string        `json:"user_id"`
	EmojiName string        `json:"emoji_name"`
	CreateAt  int64         `json:"create_at"`
	Anchor    string        `json:"anchor"`
	Account   *answerRecord `json:"account,omitempty"`
}

// answerRecord is the login service's account record, in its own shape: what
// the session checks by RT-6.42's freshness rule.
type answerRecord struct {
	V        int    `json:"v"`
	Account  string `json:"account"`
	Anchor   string `json:"anchor"`
	Name     string `json:"name"`
	Approver bool   `json:"approver"`
	TokenIat int64  `json:"token_iat"`
	Iat      int64  `json:"iat"`
	Exp      int64  `json:"exp"`
}

// result is one part of the answer.
type result struct {
	V         int          `json:"v"`
	Channel   string       `json:"channel"`
	ChannelID string       `json:"channel_id"`
	Thread    string       `json:"thread,omitempty"`
	Posts     []answerPost `json:"posts"`
	Next      int64        `json:"next,omitempty"`
	More      bool         `json:"more,omitempty"`
}

// shape turns one post into the answer's post: the author's anchor and record,
// and each reaction's, with a bot's reaction left out.
func (h *Handler) shape(ctx context.Context, bot *mm.Client, post mm.Post) answerPost {
	anchor, rec, isBot := h.person(ctx, bot, post.UserID)
	out := answerPost{
		ID: post.ID, UserID: post.UserID, ChannelID: post.ChannelID, RootID: post.RootID,
		Type: post.Type, Message: post.Message, Props: post.Props, FileCount: post.FileCount,
		CreateAt: post.CreateAt, UpdateAt: post.UpdateAt, DeleteAt: post.DeleteAt,
		Anchor: anchor, Account: rec, Bot: isBot,
	}
	if post.HasMetadata {
		out.Metadata = &answerMeta{Reactions: []answerReaction{}}
		for _, re := range post.Reactions {
			rAnchor, rRec, rBot := h.person(ctx, bot, re.UserID)
			if rBot {
				continue
			}
			out.Metadata.Reactions = append(out.Metadata.Reactions, answerReaction{
				UserID: re.UserID, EmojiName: re.EmojiName, CreateAt: re.CreateAt,
				Anchor: rAnchor, Account: rRec,
			})
		}
	}
	return out
}

// person is a Mattermost account as the answer names it: its anchor and record
// from the login service's records, else whether Mattermost says it is a bot.
// A bound record is never a bot's (the login service writes them for people),
// so only an unbound account is looked up, and cached.
func (h *Handler) person(ctx context.Context, bot *mm.Client, uid string) (string, *answerRecord, bool) {
	anchor := h.People.Anchor(uid)
	if anchor != "" {
		if rec, ok := h.People.Record(uid); ok {
			return anchor, recordOf(rec), false
		}
		return anchor, nil, false
	}
	isBot, err := h.botness(ctx, bot, uid)
	if err != nil {
		// Mattermost did not say: the account names no person and is no bot
		// it can prove; the answer carries it without a record, as an unbound
		// one.
		h.log().Warn("history: no user read for a post's author", "account", uid, "error", err)
	}
	return "", nil, isBot
}

// botness says whether Mattermost calls the account a bot, cached: bots do not
// turn into people, and a read must not cost a call per post.
func (h *Handler) botness(ctx context.Context, bot *mm.Client, uid string) (bool, error) {
	h.mu.Lock()
	if isBot, ok := h.isBots[uid]; ok {
		h.mu.Unlock()
		return isBot, nil
	}
	if h.isBots == nil || len(h.isBots) >= userCacheMax {
		h.isBots = map[string]bool{}
	}
	h.mu.Unlock()
	u, err := bot.GetUser(ctx, uid)
	if err != nil {
		return false, err
	}
	h.mu.Lock()
	h.isBots[uid] = u.IsBot
	h.mu.Unlock()
	return u.IsBot, nil
}

// recordOf writes the decoded record in the shape the login service publishes.
func recordOf(rec inbound.Account) *answerRecord {
	return &answerRecord{V: 1, Account: rec.Account, Anchor: rec.Anchor, Name: rec.Name,
		Approver: rec.Approver, TokenIat: rec.TokenIat, Iat: rec.Iat, Exp: rec.Exp}
}

// drop says whether a post is no one's ordinary post, as a read shows it
// (RT-6.42 step 2): a deleted one, a system or plugin one, a webhook's.
func drop(p mm.Post) bool {
	if p.DeleteAt != 0 || p.Type != "" {
		return true
	}
	_, webhook := p.Props["from_webhook"]
	_, override := p.Props["override_username"]
	return webhook || override
}

// parts splits the shaped posts into the answer's parts, each within the count
// and byte bounds, and marks the last with next and more while posts remain.
func (h *Handler) parts(p payload, cid string, posts []answerPost, limit int, capped bool) []requests.Reply {
	var (
		chunks [][]answerPost
		cur    []answerPost
		used   int
		total  int
		taken  int
		lastAt int64
	)
	more := capped
	for _, post := range posts {
		if taken >= limit {
			more = true
			break
		}
		raw, err := json.Marshal(post)
		if err != nil {
			h.log().Warn("history: a post does not marshal", "post", post.ID, "error", err)
			continue
		}
		size := len(raw) + 1 // the array's comma
		if len(cur) > 0 && (len(cur) >= MaxPosts || used+size > maxPartBytes) {
			chunks = append(chunks, cur)
			cur, used = nil, 0
		}
		if len(cur) == 0 && taken > 0 && total+size > maxAnswerBytes {
			more = true
			break
		}
		if len(cur) == 0 && size > maxPartBytes {
			// One post larger than a part: it goes alone, never lost.
			h.log().Info("history: a post is larger than a part", "post", post.ID, "bytes", size)
		}
		cur = append(cur, post)
		used += size
		total += size
		taken++
		lastAt = post.CreateAt
	}
	if len(cur) > 0 {
		chunks = append(chunks, cur)
	}
	if len(posts) > taken {
		more = true
	}
	if len(chunks) == 0 {
		chunks = [][]answerPost{{}} // a read with nothing: one empty part
	}
	out := make([]requests.Reply, 0, len(chunks))
	for i, chunk := range chunks {
		r := result{V: 1, Channel: p.Channel, ChannelID: cid, Thread: p.Thread, Posts: chunk}
		if i == len(chunks)-1 && more {
			r.Next = lastAt
			r.More = true
		}
		body, err := json.Marshal(r)
		if err != nil {
			// A result of these fields never fails to marshal; if it does, a
			// part is missing and the caller sees an answer that does not end.
			h.log().Warn("history: the answer does not marshal", "error", err)
			continue
		}
		out = append(out, requests.Reply{Result: body})
	}
	if len(out) == 0 {
		out = append(out, requests.Reply{Result: json.RawMessage(`{"v":1}`)})
	}
	return out
}

// log is the handler's logger.
func (h *Handler) log() *slog.Logger {
	if h.Log != nil {
		return h.Log
	}
	return slog.Default()
}
