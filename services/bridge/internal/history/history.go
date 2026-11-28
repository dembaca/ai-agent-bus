// Package history answers the bridge's history read (ADR-014 phase 5, plan B
// piece 5): a session asks on <req>.history.<agent> what people wrote while it
// was away, and the bridge answers from Mattermost with that agent's own
// bridge bot (RT-6.44), only in a channel the agent announced a membership of
// (RT-6.37) and only while piece 5 is switched on there (RT-6.39 step 5).
//
// The memberships come through the bridge's own consumer, bridge_members,
// made at every start (presence.MembersReader): the operator's presence
// consumer carries only presence.*.*, and after a restart the membership
// records must arrive again. A membership counts 7 h (presence.MemberKeep),
// just over the 6 h the MCP server renews it, so a missed renewal closes the
// channel; a revocation is bounded by the same keep until the login service
// writes a tombstone at one (a later piece, #244).
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
// One read per agent runs at a time: a second request from the same agent
// while one is running is answered "busy: a history read for this agent is
// already running, ask again", so a session's own reads do not multiply the
// bridge's Mattermost work.
//
// The result is one part of an answer (the bridge numbers the parts 1..n and
// marks the last, so a caller reads until `last`):
//
//	{"v": 1, "channel": "<channel token>", "channel_id": "<Mattermost id>",
//	 "thread": "<root id>", "posts": [ ... ]}
//
// and the last part adds {"next": <create_at ms>, "more": true} while posts
// remain past what this answer carried (the limit, the byte or count bound, or
// the page walk's bound): the caller asks again with since=next, which is
// EXCLUSIVE - the last post's create_at plus one - so a run of posts with one
// create_at is never walked twice (the run is carried whole). A thread read is
// bounded by the limit and names no next: `more` says the thread goes on.
//
// A channel read pages Mattermost's posts with page/per_page, 200 a page, from
// the newest back to since: never with `since` alone, whose answer Mattermost
// cuts at a thousand posts in no set order (sqlstore getPostsSince: no paging,
// LIMIT 1000), so a busy channel lost its newest. The answer carries the oldest
// limit posts from since on, so `next` walks forward across requests; a window
// with more posts than one read walks (mm.MaxReadPosts) is refused with an
// error naming it, never answered in part. A thread read pages the root's
// thread the same way, keeping the root even when it is a bot's (an ask) and
// dropping a reply by the rules below.
//
// A post is Mattermost's own shape, the fields the MCP server's reads use:
// id, user_id, channel_id, root_id, type, message, props, file_count,
// create_at, update_at, edit_at, delete_at, and metadata.reactions where the
// read carried metadata. The piece judges a post's freshness by create_at,
// never update_at or edit_at: a later reaction moves update_at and an edit
// moves edit_at, and neither writes the post into history again. Beside those
// fields:
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
// holds it now, its edit_at naming the edit.
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

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/agentbots"
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
	// maxClientBytes is the most the MCP client takes from one answer's parts
	// together (MAX_TOTAL in requests.py, counted over the parts as sent), and
	// partEnvelope what each part's envelope adds to its result: a run of equal
	// create_at that would go over it is refused, not answered (#278).
	maxClientBytes = 4 << 20
	partEnvelope   = 1 << 10
	// requestTimeout bounds one read, so one slow channel stalls no other
	// agent's request for longer.
	requestTimeout = 30 * time.Second
	// userCacheMax bounds the bot marks kept between reads.
	userCacheMax = 4096
)

// errNotChannel is the answer for a channel the agent did not announce a
// membership of. It names neither the channel's switch nor its posts.
// errRunTooLarge is the answer for a window whose one create_at holds more
// posts than the client takes in one answer.
var errRunTooLarge = errors.New("too large: the posts of one millisecond are more than an answer may carry")

var errNotChannel = errors.New("not a channel of that agent")

// errBusy is the answer to a second read from an agent whose first one still
// runs: one read per agent at a time bounds the bridge's Mattermost work.
var errBusy = errors.New("busy: a history read for this agent is already running, ask again")

// channelRE is a channel's token on the bus, the same presence and plan B use.
var channelRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// BotClient is the agents' own bots: a Mattermost client for the agent's own
// token. It is agentbots.Bots (its Client method), and it must never be the
// bridge bot. Client refuses a bot that is not a member of the channel; a read
// never joins it there (agentbots.NotInChannel).
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

	mu      sync.Mutex
	isBots  map[string]bool // Mattermost user id -> is_bot, for accounts without a record
	reading map[string]bool // the agents whose history read runs now
}

// begin marks the agent's read as running; false while one already runs.
func (h *Handler) begin(agent string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.reading == nil {
		h.reading = map[string]bool{}
	}
	if h.reading[agent] {
		return false
	}
	h.reading[agent] = true
	return true
}

// end clears the agent's mark.
func (h *Handler) end(agent string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	delete(h.reading, agent)
}

// Piece is the requests.Handler.Handlers entry for the kind "history".
func (h *Handler) Piece(ctx context.Context, agent string, req requests.Request) ([]requests.Reply, error) {
	p, err := decode(req.Payload)
	if err != nil {
		return nil, err
	}
	// One read per agent at a time: a second request while one runs is told
	// so, never queued behind it in the bridge.
	if !h.begin(agent) {
		return nil, errBusy
	}
	defer h.end(agent)
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
		// The bot's absence from the channel is the read's own answer, in its
		// own words; any other failure says whose bot it was.
		var notIn *agentbots.NotInChannel
		if errors.As(err, &notIn) {
			return nil, err
		}
		return nil, fmt.Errorf("the agent's bot: %w", err)
	}
	cid, err := bot.ChannelIDByName(ctx, h.Team, p.Channel)
	if err != nil {
		return nil, fmt.Errorf("the channel ~%s: %w", p.Channel, err)
	}

	var (
		shaped      []answerPost
		capped      bool  // the read hit its own bound: posts may remain
		readThrough int64 // the last post read (dropped included), the cursor then
	)
	if p.Thread != "" {
		root, posts, more, err := h.threadRead(ctx, bot, cid, p.Channel, p.Thread, limit)
		if err != nil {
			return nil, err
		}
		p.Thread, shaped, capped = root, posts, more
	} else {
		posts, more, err := bot.PostsFrom(ctx, cid, p.Since, limit)
		if err != nil {
			return nil, err
		}
		capped = more
		// The cursor of a read that hit the read's own bound is the last post
		// READ, dropped ones included: `more` counts what was read, and a run of
		// dropped posts would otherwise leave next at 0 or at since (an endless
		// loop). A part that cut the answer uses the last post it kept instead.
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if len(posts) > 0 {
			readThrough = posts[len(posts)-1].CreateAt
		}
		for _, post := range posts {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			if drop(post) {
				continue
			}
			shaped = append(shaped, h.shape(ctx, bot, post))
			if err := ctx.Err(); err != nil {
				return nil, err
			}
		}
	}

	return h.parts(p, cid, shaped, limit, capped, readThrough)
}

// maxThreadPages bounds one thread read's pages (200 posts each): a thread of
// many posts that are all dropped still ends, and the answer says more.
const maxThreadPages = 20

// threadRead pages a thread's root and replies, oldest first, keeping the root
// even when it is a bot's (an ask) and shaping a reply by the drop rules, until
// limit posts are kept or the thread ends. root is the thread the caller would
// read, also when a reply's id was asked; more says the thread goes on past
// what the answer carries (its limit, or the page bound of a thread whose posts
// are nearly all dropped). It refuses a thread that lies in another channel.
func (h *Handler) threadRead(ctx context.Context, bot *mm.Client, cid, channel, asked string,
	limit int) (root string, shaped []answerPost, more bool, err error) {
	var (
		from   string
		fromAt int64
		seen   = map[string]bool{}
		first  = true
	)
	for page := 0; page < maxThreadPages; page++ {
		posts, hasNext, err := bot.ThreadPage(ctx, asked, from, fromAt, mm.ThreadCap)
		if err != nil {
			return "", nil, false, err
		}
		if len(posts) == 0 {
			if first {
				return "", nil, false, fmt.Errorf("post %s has no thread", asked)
			}
			return root, shaped, false, nil
		}
		fresh := 0
		for _, post := range posts {
			if seen[post.ID] {
				continue // the root comes with every page
			}
			seen[post.ID] = true
			fresh++
			if post.ChannelID != cid {
				return "", nil, false, fmt.Errorf("the thread is not in ~%s", channel)
			}
			if first {
				// The thread is the root's, also when a reply's id was asked.
				if post.RootID == "" {
					root = post.ID
				} else {
					root = post.RootID
				}
			}
			keep := first || !drop(post)
			first = false
			if !keep {
				continue
			}
			if err := ctx.Err(); err != nil {
				return "", nil, false, err
			}
			if len(shaped) < limit {
				shaped = append(shaped, h.shape(ctx, bot, post))
			} else {
				more = true
			}
		}
		if len(shaped) >= limit {
			return root, shaped, more || hasNext, nil
		}
		if !hasNext || fresh == 0 {
			return root, shaped, false, nil
		}
		last := posts[len(posts)-1]
		from, fromAt = last.ID, last.CreateAt
	}
	return root, shaped, true, nil
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
	EditAt    int64          `json:"edit_at"`
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
		CreateAt: post.CreateAt, UpdateAt: post.UpdateAt, EditAt: post.EditAt, DeleteAt: post.DeleteAt,
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
		// The read's deadline is the answer's: no more lookups, no warning per
		// post. Anything else is one warning, and the post stays unbound.
		if ctx.Err() != nil {
			return "", nil, false
		}
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
func (h *Handler) parts(p payload, cid string, posts []answerPost, limit int, capped bool,
	readThrough int64) ([]requests.Reply, error) {
	var (
		chunks [][]answerPost
		cur    []answerPost
		used   int
		total  int
		taken  int
		lastAt int64
	)
	more := capped
	for i, post := range posts {
		// Never cut inside a run of equal create_at: the cursor is exclusive, so
		// a run a cut split would be walked again from its start for ever
		// (Fable's review of #270). The run is carried whole, over the bounds.
		run := i > 0 && post.CreateAt == posts[i-1].CreateAt
		if taken >= limit && !run {
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
		if len(cur) == 0 && taken > 0 && total+size > maxAnswerBytes && !run {
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
	wire := 0
	for i, chunk := range chunks {
		r := result{V: 1, Channel: p.Channel, ChannelID: cid, Thread: p.Thread, Posts: chunk}
		if i == len(chunks)-1 && more {
			// A channel answer's cursor is a create_at; a thread read has no
			// cursor (the request takes none beside the thread), only more.
			if p.Thread == "" {
				// The part cut the answer: the next read starts after the last
				// post kept. Only a read the GET said had more, with nothing cut
				// here, starts after the last post read - a run of dropped posts
				// must still move the cursor.
				// Exclusive: the next read starts after the cursor, so a run of
				// equal create_at is never walked twice.
				r.Next = lastAt + 1
				if capped && len(posts) <= taken && readThrough > lastAt {
					r.Next = readThrough + 1
				}
			}
			r.More = true
		}
		body, err := json.Marshal(r)
		if err != nil {
			// A result of these fields never fails to marshal; if it does, a
			// part is missing and the caller sees an answer that does not end.
			h.log().Warn("history: the answer does not marshal", "error", err)
			continue
		}
		wire += len(body) + partEnvelope
		if wire > maxClientBytes {
			// A run of one create_at is never cut, so it can only be refused: the
			// client would refuse every retry of this window anyway.
			h.log().Warn("history: a run of equal create_at is over the client's answer bound",
				"channel", p.Channel, "bytes", wire)
			return nil, errRunTooLarge
		}
		out = append(out, requests.Reply{Result: body})
	}
	if len(out) == 0 {
		out = append(out, requests.Reply{Result: json.RawMessage(`{"v":1}`)})
	}
	return out, nil
}

// log is the handler's logger.
func (h *Handler) log() *slog.Logger {
	if h.Log != nil {
		return h.Log
	}
	return slog.Default()
}
