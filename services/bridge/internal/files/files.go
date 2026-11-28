// Package files carries an agent's file traffic over the bridge (plan B
// piece 8, ADR-014 phase 5, RT-6.23 and RT-6.33): the MCP side's attach and
// get_file, without a Mattermost call of the session's own.
//
// An upload (the put op) comes as parts of requests on <req>.file.<agent>,
// one part per request, strictly in order, each waiting for its ack: the
// first part declares the whole file's length and SHA-256 hash beside its
// own bytes, at most 700 KiB of raw bytes a part (base64 keeps the request's
// body under the scaffold's 1 MiB bound for the kind "file"), and the bridge
// answers every part with {"ok":true,"part":<i>}, the last with
// {"file_id","name","size"} once it uploaded the whole with the agent's own
// bot (agentbots.Client refuses a bot not in the channel and never joins
// it there, RT-6.44).
//
// A download (the get op) is one request; the bridge answers in parts, the
// runner numbering them 1..n and marking the last: part 1 is the file's
// metadata — what Mattermost keeps about it, its channel, and its author
// with the anchor and record the login service's account records bind, for
// display; the session judges the author by its own records (RT-6.41 step 6)
// — with the length and the SHA-256 hash of the bytes that follow; parts 2..n+1 are those bytes,
// base64 in parts of at most 700 KiB. The piece serves what the MCP side's
// files.kind decides: an image's preview when it has one, else the original
// png/jpeg/gif/webp up to 5 MB; a text file's first 80 KB (files.TEXT_MAX*4);
// anything else metadata only, no bytes.
//
// Both ops check the agent's announced membership of the channel
// (presence.MemberFresh, RT-6.37) and piece 8's switch there (RT-6.39
// step 5), and answer in the same words — before any read, so the answer
// tells nothing about channels the agent may not read. The get op carries
// the channel in its payload beside the file's id; the file's post must be
// of that same channel, or the answer is the membership's own words.
//
// The put op re-implements the file rules of the MCP side's attach.check —
// the sender checked them already, but the bridge answers for itself: a
// clean base name of at most 100 characters with the extension kept, the
// extension (its case disregarded) in the allowed set, the type by the magic
// bytes matching it, SVG refused, the file not empty and at most 10 MB.
// The type check runs on the first part's bytes and again, as the exact
// whole-buffer sniff, on the assembled file before it is uploaded. One
// transfer per agent is open at a time (a new first part replaces it), at
// most 16 across the agents, and no part carries no bytes. An unfinished
// transfer is dropped 60 s after its last part and five minutes after its
// first, and one that asks for more parts than its declared length can hold
// is dropped too — the sweep runs on both ops' entries, so no ticker is
// needed. A gap in the parts, a part for a dropped transfer and a hash that
// does not match each answer their own words, and a broken transfer is
// never half-uploaded: only a whole file whose length, hash and type match
// is sent to Mattermost.
package files

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/agentbots"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/inbound"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mm"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
)

// The piece's number in plan B's switches (RT-6.39 step 5): without it no
// file goes through the bridge.
const Piece = 8

const (
	// MaxFile is the most one upload may hold: the MCP side's attach.FILE_MAX.
	MaxFile = 10 << 20
	// nameMax is the longest a clean file name may be, its extension kept:
	// attach.NAME_MAX.
	nameMax = 100
	// partMax is the most raw bytes one part carries, in either direction:
	// base64 and the envelope stay under the scaffold's 1 MiB body bound for
	// the kind "file" (requests.maxFileBody), so the publish cannot fail.
	partMax = 700 << 10
	// maxOpen is the transfers open across all agents; one more answers busy.
	maxOpen = 16
	// dropAfter is how long an unfinished transfer is kept past its last
	// part: the whole one is at most 15 parts, each answered within its own
	// request's deadline.
	dropAfter = 60 * time.Second
	// lifetime is the absolute bound of one transfer, from its first part:
	// an upload that trickles tiny parts forever is dropped at it even
	// though no 60 s ever passed between two of them.
	lifetime = 5 * time.Minute
	// maxParts is the most parts one transfer may take: a whole at today's
	// size limit in 700 KiB parts, and a couple to spare. A transfer that
	// would take more is dropped: no whole of that length fits in its parts.
	maxParts = (MaxFile+partMax-1)/partMax + 2
	// ImageMax is the most an image is served as: its preview, else the
	// original. The MCP side's files.IMAGE_MAX.
	ImageMax = 5 << 20
	// textBytes is how much of a text file is served: the MCP side's
	// files.TEXT_MAX*4, cut there by its reader again.
	textBytes = 20_000 * 4
	// requestTimeout bounds one request's checks and kept parts: the MCP side
	// waits 5 s for an answer of the kind "file" (requests.REQUEST_SEC), so
	// the bridge gives up before its caller does.
	requestTimeout = 4 * time.Second
	// uploadTimeout bounds the last part's upload alone: it posts up to
	// 10 MB to Mattermost, which builds image previews in that call, so it
	// takes longer than the checks ever do. The MCP side gives the last
	// part the history read's own headroom (requests.HISTORY_SEC, 35 s);
	// the bridge's own deadline stays under it.
	uploadTimeout = 30 * time.Second
	// userCacheMax bounds the accounts read between answers.
	userCacheMax = 4096
)

// errNotChannel is the answer for a channel the agent did not announce a
// membership of, the history read's own words: it tells nothing about another
// agent's channels.
var errNotChannel = errors.New("not a channel of that agent")

// errBusyTransfers is the answer to the 17th open transfer.
var errBusyTransfers = errors.New("busy: too many open transfers, send again")

// errDropped is the answer to a part of a transfer that is no longer open.
var errDropped = errors.New("the transfer was dropped: send again")

// errHash is the answer when the assembled whole does not match what the
// first part declared: its length or its hash.
var errHash = errors.New("the hash does not match: send again")

// channelRE is a channel's token on the bus, the same presence and plan B use.
var channelRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// fileIDRE is a Mattermost file id: 26 characters of [a-z0-9].
var fileIDRE = regexp.MustCompile(`^[a-z0-9]{26}$`)

// sumRE is a SHA-256 hash as the MCP side writes it: 64 hex characters.
var sumRE = regexp.MustCompile(`^[0-9a-f]{64}$`)

// BotClient is the agents' own bots: a Mattermost client for the agent's own
// token. It is agentbots.Bots (its Client method), and it must never be the
// bridge bot. Client refuses a bot that is not a member of the channel; a
// transfer never joins it there (agentbots.NotInChannel).
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

// Handler answers one file request. Every field is set once at start.
type Handler struct {
	Log      *slog.Logger
	Team     string // CB_BRIDGE_MM_TEAM
	Bots     BotClient
	Presence Membership
	Switches Switches
	People   People
	Now      func() time.Time // nil: time.Now
	// RequestTimeout and UploadTimeout override their constants in the
	// tests, which run them short; zero keeps each constant.
	RequestTimeout time.Duration
	UploadTimeout  time.Duration

	mu    sync.Mutex
	open  map[string]*transfer // the open uploads, by agent
	users map[string]mm.User   // Mattermost accounts read, by user id
}

// requestDeadline is the checks' and the kept parts' own short deadline.
func (h *Handler) requestDeadline() time.Duration {
	if h.RequestTimeout > 0 {
		return h.RequestTimeout
	}
	return requestTimeout
}

// uploadDeadline is the last part's upload deadline, longer than the checks':
// a multi-MB upload outlives them.
func (h *Handler) uploadDeadline() time.Duration {
	if h.UploadTimeout > 0 {
		return h.UploadTimeout
	}
	return uploadTimeout
}

// transfer is one agent's upload in progress: the whole the first part
// declared, the parts kept so far, and the agent's own bot that will upload.
type transfer struct {
	name    string // the clean name
	length  int64  // the whole's declared length
	sum     string // the whole's declared SHA-256
	channel string // the channel token, for the answers' words
	cid     string // the channel's Mattermost id
	bot     *mm.Client
	buf     []byte    // the parts kept so far
	next    int       // the part the transfer waits for
	born    time.Time // its first part's moment
	at      time.Time // its last part's moment; dropped 60 s after it, and 5 min after born
}

// Piece is the requests.Handler.Handlers entry for the kind "file".
func (h *Handler) Piece(ctx context.Context, agent string, req requests.Request) ([]requests.Reply, error) {
	p, err := decode(req.Payload)
	if err != nil {
		return nil, err
	}
	switch p.Op {
	case "put":
		return h.put(ctx, agent, p)
	case "get":
		return h.get(ctx, agent, p)
	}
	return nil, fmt.Errorf("the file request's op %q is neither put nor get", p.Op)
}

// put is the upload: the checks of part 1 before any part is kept, then the
// parts in order, the last one assembled, checked and uploaded. The checks
// and the kept parts run under the short deadline; the last part's upload
// takes its own, longer one, so no part holds a worker of the scaffold's
// pool and a multi-MB upload still ends within its caller's wait.
func (h *Handler) put(ctx context.Context, agent string, p payload) ([]requests.Reply, error) {
	// brief is the checks' and the kept parts' own short deadline; the last
	// part's upload — whole() below — wraps the request's context itself,
	// not this one, so the short deadline never caps it.
	brief, cancel := context.WithTimeout(ctx, h.requestDeadline())
	defer cancel()
	// The sweep of the open transfers runs on both ops' entries, so no
	// ticker is needed: a stale transfer is gone before its next part.
	h.dropStale()
	// The membership first, as the history read has it: a channel that is not
	// the agent's is refused alike whether the piece is on there or not.
	if h.Presence == nil || !h.Presence.MemberFresh(p.Channel, agent) {
		return nil, errNotChannel
	}
	if h.Switches == nil || !h.Switches.On(Piece, p.Channel) {
		return nil, fmt.Errorf("piece 8 is off in %s", p.Channel)
	}
	if p.Part == 1 {
		return h.firstPart(brief, ctx, agent, p)
	}
	h.mu.Lock()
	tr := h.open[agent]
	if tr == nil {
		h.mu.Unlock()
		return nil, errDropped
	}
	if p.Part != tr.next {
		next := tr.next
		h.mu.Unlock()
		return nil, fmt.Errorf("wrong part: expected %d", next)
	}
	data, err := base64.StdEncoding.DecodeString(p.Data)
	if err != nil {
		h.mu.Unlock()
		return nil, fmt.Errorf("part %d's data is no base64: %w", p.Part, err)
	}
	if len(data) > partMax {
		h.mu.Unlock()
		return nil, fmt.Errorf("part %d carries %d bytes, more than the %d a part may carry",
			p.Part, len(data), partMax)
	}
	if len(data) == 0 {
		// No part carries no bytes: an empty one would refresh a transfer's
		// life without moving it, and the last of a whole file is never empty.
		h.mu.Unlock()
		return nil, fmt.Errorf("part %d carries no bytes", p.Part)
	}
	tr.buf = append(tr.buf, data...)
	tr.at = h.now()
	tr.next++
	if tr.next > maxParts {
		// More parts than its declared length can hold: the transfer is
		// dropped, its slot free (the review of #286).
		delete(h.open, agent)
		h.mu.Unlock()
		return nil, errDropped
	}
	if int64(len(tr.buf)) > tr.length {
		// More than the whole it declared: the hash can no longer match, and
		// the buffer is not grown past the declared file.
		delete(h.open, agent)
		h.mu.Unlock()
		return nil, errHash
	}
	if !p.Last {
		h.mu.Unlock()
		return []requests.Reply{ack(p.Part)}, nil
	}
	// The last part: the transfer is complete, its slot free whatever the
	// upload answers; only a whole that matches goes to Mattermost.
	delete(h.open, agent)
	h.mu.Unlock()
	return h.whole(ctx, agent, tr)
}

// firstPart checks the file rules and the bot on part 1, before any part is
// kept, and opens the transfer — replacing the agent's open one, and never a
// 17th across the agents. brief is the checks' own short deadline; ctx is
// the request's context, whose own longer deadline the one-part file's
// upload takes in whole().
func (h *Handler) firstPart(brief, ctx context.Context, agent string, p payload) ([]requests.Reply, error) {
	data, name, err := h.check(p)
	if err != nil {
		return nil, err
	}
	if h.Bots == nil {
		return nil, errors.New("the bridge has no bots for the agents; an upload needs the agent's own")
	}
	bot, err := h.Bots.Client(brief, agent, p.Channel)
	if err != nil {
		// The bot's absence from the channel is the upload's own answer, in
		// its own words; any other failure says whose bot it was.
		var notIn *agentbots.NotInChannel
		if errors.As(err, &notIn) {
			return nil, err
		}
		return nil, fmt.Errorf("the agent's bot: %w", err)
	}
	cid, err := bot.ChannelIDByName(brief, h.Team, p.Channel)
	if err != nil {
		return nil, fmt.Errorf("the channel ~%s: %w", p.Channel, err)
	}
	now := h.now()
	tr := &transfer{name: name, length: p.Length, sum: p.SHA256,
		channel: p.Channel, cid: cid, bot: bot, buf: data, next: 2, born: now, at: now}
	// One transfer per agent — a new first part replaces it — and at most 16
	// across the agents: the 17th is told busy. The cap is checked with the
	// store, under one lock, so two first parts cannot both slip past it.
	h.mu.Lock()
	if h.open == nil {
		h.open = map[string]*transfer{}
	}
	if h.open[agent] == nil && len(h.open) >= maxOpen {
		h.mu.Unlock()
		return nil, errBusyTransfers
	}
	h.open[agent] = tr
	h.mu.Unlock()
	if !p.Last {
		return []requests.Reply{ack(p.Part)}, nil
	}
	// A one-part file: complete on arrival.
	h.mu.Lock()
	delete(h.open, agent)
	h.mu.Unlock()
	return h.whole(ctx, agent, tr)
}

// whole checks the assembled file against what its first part declared — the
// length, the SHA-256 of all its parts, and the type of its bytes, the exact
// whole-buffer sniff that part 1 could only run on its own — and only a whole
// that matches is uploaded; a mismatch is its own answer, and the transfer
// is gone (the review of #286: a text first part with other bytes behind it).
// The upload takes its own deadline here, where it begins: it posts up to
// 10 MB and Mattermost builds image previews in that call, longer than the
// checks ever take (round 2 of the review of #286).
func (h *Handler) whole(ctx context.Context, agent string, tr *transfer) ([]requests.Reply, error) {
	ctx, cancel := context.WithTimeout(ctx, h.uploadDeadline())
	defer cancel()
	if int64(len(tr.buf)) != tr.length {
		return nil, errHash
	}
	sum := sha256.Sum256(tr.buf)
	if !strings.EqualFold(hex.EncodeToString(sum[:]), tr.sum) {
		return nil, errHash
	}
	ext := strings.ToLower(tr.name[strings.LastIndex(tr.name, ".")+1:])
	want, ok := types[ext]
	if !ok {
		return nil, errHash // unreachable: check judged the name on part 1
	}
	if got := sniff(tr.buf); got != want {
		what := "no allowed type (binary, or not UTF-8)"
		if got != "" {
			what = "a " + got + " file"
		}
		return nil, fmt.Errorf("%s is named .%s but its bytes are %s", tr.name, ext, what)
	}
	return h.upload(ctx, agent, tr)
}

// upload sends the assembled whole with the agent's own bot and answers the
// final part: the file's Mattermost id, the clean name and the size.
func (h *Handler) upload(ctx context.Context, agent string, tr *transfer) ([]requests.Reply, error) {
	info, err := tr.bot.UploadFile(ctx, tr.cid, tr.name, tr.buf)
	if err != nil {
		return nil, fmt.Errorf("the upload to ~%s: %w", tr.channel, err)
	}
	name := info.Name
	if name == "" {
		name = tr.name
	}
	body, err := json.Marshal(struct {
		FileID string `json:"file_id"`
		Name   string `json:"name"`
		Size   int64  `json:"size"`
	}{FileID: info.ID, Name: name, Size: int64(len(tr.buf))})
	if err != nil {
		return nil, err
	}
	h.log().Info("files: uploaded", "agent", agent, "channel", tr.channel,
		"name", tr.name, "size", len(tr.buf), "file", info.ID)
	return []requests.Reply{{Result: body}}, nil
}

// ack is one kept part's answer.
func ack(part int) requests.Reply {
	body, err := json.Marshal(struct {
		OK   bool `json:"ok"`
		Part int  `json:"part"`
	}{OK: true, Part: part})
	if err != nil {
		return requests.Reply{Result: json.RawMessage(`{"ok":true}`)}
	}
	return requests.Reply{Result: body}
}

// check runs the file rules on the first part, before any part is kept — the
// MCP side's attach.check, its words included: a clean base name, an allowed
// extension (its case disregarded, as attach has it) whose type the part's
// magic bytes must match, SVG refused, the file not empty and at most 10 MB.
// A text file's part may end inside a rune (a part boundary cuts raw bytes);
// the whole's own check at the end is what a later part cannot slip past.
func (h *Handler) check(p payload) (data []byte, name string, err error) {
	name, err = cleanName(p.Name)
	if err != nil {
		return nil, "", err
	}
	if p.Length <= 0 {
		return nil, "", errors.New("the file is empty")
	}
	if p.Length > MaxFile {
		return nil, "", errors.New("the file is over 10 MB")
	}
	ext := strings.ToLower(name[strings.LastIndex(name, ".")+1:])
	if ext == "svg" {
		return nil, "", errors.New("SVG is refused: it can carry script; render it as PNG")
	}
	want, ok := types[ext]
	if !ok {
		return nil, "", fmt.Errorf(".%s is not allowed: %s", ext, allowedExts)
	}
	data, err = base64.StdEncoding.DecodeString(p.Data)
	if err != nil {
		return nil, "", fmt.Errorf("part 1's data is no base64: %w", err)
	}
	if len(data) > partMax {
		return nil, "", fmt.Errorf("part 1 carries %d bytes, more than the %d a part may carry",
			len(data), partMax)
	}
	if len(data) == 0 {
		return nil, "", errors.New("part 1 carries no bytes")
	}
	if got := sniff1(data); got != want {
		what := "no allowed type (binary, or not UTF-8)"
		if got != "" {
			what = "a " + got + " file"
		}
		return nil, "", fmt.Errorf("%s is named .%s but its bytes are %s", name, ext, what)
	}
	return data, name, nil
}

// get is the download: the membership and the switch of the channel the
// payload names, checked before any read, then the file's info and its post
// with the agent's own bot — the post's channel must be that same channel,
// or the answer is the membership's own words, telling nothing about files
// of channels the agent may not read (the review of #286) — and the metadata
// and the bytes answered in parts.
func (h *Handler) get(ctx context.Context, agent string, p payload) ([]requests.Reply, error) {
	// The sweep of the open transfers runs on both ops' entries, so no
	// ticker is needed.
	h.dropStale()
	// The checks first, before any read: a channel that is not the agent's
	// is refused alike whether the piece is on there or not.
	if h.Presence == nil || !h.Presence.MemberFresh(p.Channel, agent) {
		return nil, errNotChannel
	}
	if h.Switches == nil || !h.Switches.On(Piece, p.Channel) {
		return nil, fmt.Errorf("piece 8 is off in %s", p.Channel)
	}
	if h.Bots == nil {
		return nil, errors.New("the bridge has no bots for the agents; a file read needs the agent's own")
	}
	if h.People == nil || !h.People.CaughtUp() {
		return nil, errors.New("the bridge's account records are not read yet; ask again")
	}
	ctx, cancel := context.WithTimeout(ctx, h.requestDeadline())
	defer cancel()
	bot, err := h.Bots.Client(ctx, agent, p.Channel)
	if err != nil {
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
	// The info and its post are read with the agent's own bot, whose access
	// is the file's gate.
	info, err := bot.FileInfo(ctx, p.ID)
	if err != nil {
		return nil, fmt.Errorf("the file %s: %w", p.ID, err)
	}
	if info.PostID == "" {
		return nil, errors.New("the file is attached to no post: its channel cannot be checked")
	}
	post, err := bot.PostByID(ctx, info.PostID)
	if err != nil {
		return nil, fmt.Errorf("the file's post: %w", err)
	}
	if post.ChannelID != cid {
		// A file of another channel: refused in the membership's own words,
		// telling nothing about that channel.
		return nil, errNotChannel
	}
	fid := info.ID
	if fid == "" {
		fid = p.ID
	}
	username, anchor, rec, isBot := h.person(ctx, bot, info.UserID)
	kind := kindOf(info)
	var data []byte
	switch kind {
	case "image":
		// Mirroring the MCP side's fetch: a preview whenever there is one,
		// else the original in a format shown as a picture, up to 5 MB —
		// anything else the reader notes from the metadata alone. The read
		// takes one byte more than the limit, so an image past it (a preview
		// can be) is served as no bytes — the metadata alone, never a
		// truncated picture that reads as a good one (round 2 of the
		// review of #286).
		if imageFormat(info) != "" && (info.HasPreviewImage || info.Size <= ImageMax) {
			raw, rerr := bot.FileBytes(ctx, fid, info.HasPreviewImage, ImageMax+1)
			if rerr != nil {
				return nil, fmt.Errorf("the file's bytes: %w", rerr)
			}
			if int64(len(raw)) <= ImageMax {
				data = raw
			}
		}
	case "text":
		// The read takes the 80 KB served and no more: the rest of a long
		// text file is never buffered (the review of #286).
		if data, err = bot.FileBytes(ctx, fid, false, textBytes); err != nil {
			return nil, fmt.Errorf("the file's bytes: %w", err)
		}
	}
	sum := sha256.Sum256(data)
	meta := metadata{V: 1, ID: fid, Name: info.Name, Extension: strings.ToLower(info.Extension),
		Size: info.Size, MimeType: info.MimeType, Width: info.Width, Height: info.Height,
		PostID: info.PostID, Channel: p.Channel, ChannelID: post.ChannelID,
		Author: info.UserID, AuthorName: username, Anchor: anchor, Account: rec,
		Bot: isBot, Kind: kind, Length: len(data),
		SHA256: hex.EncodeToString(sum[:]), Preview: info.HasPreviewImage}
	return h.parts(meta, data)
}

// parts is the download's answer: the metadata, then the bytes as parts of at
// most 700 KiB raw — base64 keeps each answer's body under the scaffold's
// 1 MiB bound for the kind "file", so the publish cannot fail. Nothing
// follows the metadata when there are no bytes (kind "other", or an image the
// reader notes from the metadata alone): part 1 is the last.
func (h *Handler) parts(meta metadata, data []byte) ([]requests.Reply, error) {
	body, err := json.Marshal(meta)
	if err != nil {
		return nil, err
	}
	out := []requests.Reply{{Result: body}}
	for i := 0; i < len(data); i += partMax {
		end := min(i+partMax, len(data))
		chunk, err := json.Marshal(filePart{Data: base64.StdEncoding.EncodeToString(data[i:end])})
		if err != nil {
			return nil, err
		}
		out = append(out, requests.Reply{Result: chunk})
	}
	return out, nil
}

// dropStale forgets the transfers whose life is over: 60 s past their last
// part, or five minutes past their first — an upload that trickles tiny
// parts forever holds its slot only that long.
func (h *Handler) dropStale() {
	now := h.now()
	h.mu.Lock()
	defer h.mu.Unlock()
	for agent, tr := range h.open {
		if now.Sub(tr.at) > dropAfter || now.Sub(tr.born) > lifetime {
			delete(h.open, agent)
		}
	}
}

// person is the file's uploader as the answer names them (RT-6.16): the
// Mattermost username, the anchor and record the login service's account
// records bind, and whether Mattermost calls the account a bot — a bound
// record is never a bot's, so only an unbound account is looked up, cached.
func (h *Handler) person(ctx context.Context, bot *mm.Client, uid string) (username, anchor string, rec *answerRecord, isBot bool) {
	anchor = h.People.Anchor(uid)
	u, err := h.user(ctx, bot, uid)
	if err != nil {
		// The read's deadline is the answer's: no more lookups, one warning.
		if ctx.Err() == nil {
			h.log().Warn("files: no user read for a file's author", "account", uid, "error", err)
		}
	}
	if anchor != "" {
		if r, ok := h.People.Record(uid); ok {
			rec = recordOf(r)
		}
		return u.Username, anchor, rec, false
	}
	return u.Username, "", nil, u.IsBot
}

// user reads one account, cached: an answer must not cost a call per file.
func (h *Handler) user(ctx context.Context, bot *mm.Client, uid string) (mm.User, error) {
	h.mu.Lock()
	if u, ok := h.users[uid]; ok {
		h.mu.Unlock()
		return u, nil
	}
	if h.users == nil || len(h.users) >= userCacheMax {
		h.users = map[string]mm.User{}
	}
	h.mu.Unlock()
	u, err := bot.GetUser(ctx, uid)
	if err != nil {
		return mm.User{}, err
	}
	h.mu.Lock()
	h.users[uid] = u
	h.mu.Unlock()
	return u, nil
}

// now is the handler's clock.
func (h *Handler) now() time.Time {
	if h.Now != nil {
		return h.Now()
	}
	return time.Now()
}

// log is the handler's logger.
func (h *Handler) log() *slog.Logger {
	if h.Log != nil {
		return h.Log
	}
	return slog.Default()
}

// payload is the request's body: the put's fields beside the get's, one of
// the two per request.
type payload struct {
	Op      string `json:"op"`
	Name    string `json:"name"`
	Channel string `json:"channel"`
	Length  int64  `json:"length"`
	SHA256  string `json:"sha256"`
	Part    int    `json:"part"`
	Last    bool   `json:"last"`
	Data    string `json:"data"`
	ID      string `json:"id"`
}

// decode reads the payload strictly, as the history read does: one object,
// no unknown key, so a caller that misspells a bound hears it.
func decode(raw json.RawMessage) (payload, error) {
	var p payload
	if len(bytes.TrimSpace(raw)) == 0 {
		return p, errors.New("the file request has no payload")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil {
		return p, fmt.Errorf("the file request's payload: %w", err)
	}
	var after json.RawMessage
	if err := dec.Decode(&after); !errors.Is(err, io.EOF) {
		return p, errors.New("the file request's payload: data after the object")
	}
	switch p.Op {
	case "put", "get":
	default:
		return p, fmt.Errorf("the file request's op %q is neither put nor get", p.Op)
	}
	if !channelRE.MatchString(p.Channel) {
		return p, fmt.Errorf("channel %q is no channel token", p.Channel)
	}
	if p.Op == "put" {
		if p.Part < 1 {
			return p, errors.New("the upload's parts start at 1")
		}
		if !sumRE.MatchString(p.SHA256) {
			return p, errors.New("the upload names no sha256 of the whole file")
		}
	}
	if p.Op == "get" && !fileIDRE.MatchString(p.ID) {
		return p, fmt.Errorf("the file id %q is no file id", p.ID)
	}
	return p, nil
}

// metadata is the download's first part: what Mattermost keeps about the
// file, its channel, and its author with the anchor and record the login
// service's account records bind — plus the length and the SHA-256 hash of
// the bytes that follow, so the caller checks them before serving anything.
type metadata struct {
	V          int           `json:"v"`
	ID         string        `json:"id"`
	Name       string        `json:"name"`
	Extension  string        `json:"extension"`
	Size       int64         `json:"size"`
	MimeType   string        `json:"mime_type"`
	Width      int           `json:"width"`
	Height     int           `json:"height"`
	PostID     string        `json:"post_id"`
	Channel    string        `json:"channel"`
	ChannelID  string        `json:"channel_id"`
	Author     string        `json:"author"`
	AuthorName string        `json:"author_name"`
	Anchor     string        `json:"anchor"`
	Account    *answerRecord `json:"account,omitempty"`
	Bot        bool          `json:"bot"`
	Kind       string        `json:"kind"`
	Length     int           `json:"length"`
	SHA256     string        `json:"sha256"`
	Preview    bool          `json:"preview"`
}

// filePart is one part of the bytes that follow the metadata.
type filePart struct {
	Data string `json:"data"`
}

// answerRecord is the login service's account record, in its own shape, as the
// history read's answer carries it. The session does not judge by it: a file's
// author is trusted by the session's own read of the records (RT-6.41 step 6),
// so this is for display only.
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

// recordOf writes the decoded record in the shape the login service publishes.
func recordOf(rec inbound.Account) *answerRecord {
	return &answerRecord{V: 1, Account: rec.Account, Anchor: rec.Anchor, Name: rec.Name,
		Approver: rec.Approver, TokenIat: rec.TokenIat, Iat: rec.Iat, Exp: rec.Exp}
}

// kindOf mirrors the MCP side's files.kind: an image, a text file, or neither
// ("other": metadata only).
func kindOf(info mm.FileInfo) string {
	mime := mimeOf(info.MimeType)
	ext := strings.ToLower(info.Extension)
	if strings.HasPrefix(mime, "image/") || imageExt[ext] {
		return "image"
	}
	if strings.HasPrefix(mime, "text/") || textMime[mime] || textExt[ext] {
		return "text"
	}
	return "other"
}

// imageFormat mirrors files.image_format: a preview is a JPEG whatever the
// original is, else the original's own format, and "" is no format an MCP
// client shows as a picture.
func imageFormat(info mm.FileInfo) string {
	if info.HasPreviewImage {
		return "jpeg"
	}
	return imageFormats[mimeOf(info.MimeType)]
}

// mimeOf is a mime type as the kind reads it: lower case, the parameters cut.
func mimeOf(mimeType string) string {
	return strings.ToLower(strings.TrimSpace(strings.SplitN(mimeType, ";", 2)[0]))
}

var (
	imageFormats = map[string]string{"image/png": "png", "image/jpeg": "jpeg",
		"image/gif": "gif", "image/webp": "webp"}
	imageExt = map[string]bool{"png": true, "jpg": true, "jpeg": true, "gif": true,
		"webp": true, "bmp": true, "tif": true, "tiff": true, "heic": true, "svg": true}
	textMime = map[string]bool{"application/json": true, "application/xml": true,
		"application/yaml": true, "application/x-yaml": true, "application/toml": true,
		"application/javascript": true, "application/x-sh": true, "application/sql": true,
		"application/x-ndjson": true, "application/csv": true}
	textExt = map[string]bool{"txt": true, "md": true, "markdown": true, "csv": true,
		"tsv": true, "json": true, "jsonl": true, "ndjson": true, "log": true,
		"yaml": true, "yml": true, "toml": true, "ini": true, "cfg": true, "conf": true,
		"env": true, "xml": true, "html": true, "css": true, "js": true, "ts": true,
		"py": true, "sh": true, "go": true, "rs": true, "java": true, "kt": true,
		"rb": true, "php": true, "c": true, "h": true, "cpp": true, "sql": true,
		"diff": true, "patch": true, "properties": true}
)

// types maps an extension to the type its bytes must have: the MCP side's
// attach.TYPES.
var types = map[string]string{
	"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp", "pdf": "pdf",
	"txt": "text", "md": "text", "csv": "text", "tsv": "text", "json": "text",
	"log": "text", "yaml": "text", "yml": "text",
}

// allowedExts is the extensions' list for the refusal, sorted, as the MCP
// side writes it.
var allowedExts = func() string {
	exts := make([]string, 0, len(types))
	for ext := range types {
		exts = append(exts, ext)
	}
	sort.Strings(exts)
	return strings.Join(exts, ", ")
}()

// magic is a binary type's first bytes, as the MCP side's attach.sniff reads
// them.
var magic = []struct {
	pre  []byte
	kind string
}{
	{[]byte("\x89PNG\r\n\x1a\n"), "png"},
	{[]byte("\xff\xd8\xff"), "jpeg"},
	{[]byte("GIF87a"), "gif"},
	{[]byte("GIF89a"), "gif"},
	{[]byte("%PDF-"), "pdf"},
}

// sniff is the type of a whole file's bytes, the MCP side's attach.sniff
// exact: the magic prefixes, and text only as valid UTF-8 without NUL.
func sniff(data []byte) string {
	for _, m := range magic {
		if bytes.HasPrefix(data, m.pre) {
			return m.kind
		}
	}
	if len(data) >= 12 && bytes.Equal(data[:4], []byte("RIFF")) && bytes.Equal(data[8:12], []byte("WEBP")) {
		return "webp"
	}
	if bytes.IndexByte(data, 0) >= 0 {
		return ""
	}
	if utf8.Valid(data) {
		return "text"
	}
	return ""
}

// sniff1 is sniff over a first part's bytes: a text file's part may end
// inside a multi-byte character (a part boundary cuts raw bytes), so a
// partial rune at its end reads as text — the whole's own check at the end
// is what the later parts answer to.
func sniff1(data []byte) string {
	got := sniff(data)
	if got == "" && bytes.IndexByte(data, 0) < 0 && cutRune(data) {
		return "text"
	}
	return got
}

// cutRune says the bytes read as text once a partial rune at their end is
// dropped: the part cut a multi-byte character in half.
func cutRune(data []byte) bool {
	for n := 1; n <= 3 && n < len(data); n++ {
		tail := data[len(data)-n:]
		if partialRune(tail) && utf8.Valid(data[:len(data)-n]) {
			return true
		}
	}
	return false
}

// partialRune says whether tail is the start of a multi-byte character, cut
// anywhere: its lead byte and the continuation bytes that follow it.
func partialRune(tail []byte) bool {
	var want int
	switch {
	case tail[0]&0xE0 == 0xC0:
		want = 2
	case tail[0]&0xF0 == 0xE0:
		want = 3
	case tail[0]&0xF8 == 0xF0:
		want = 4
	default:
		return false
	}
	if len(tail) > want {
		return false
	}
	for _, b := range tail[1:] {
		if b&0xC0 != 0x80 {
			return false
		}
	}
	return true
}

// cleanName is the MCP side's attach.clean_name: a plain base name — no
// directory, no control, format or separator characters, at most 100
// characters with the extension kept.
func cleanName(name string) (string, error) {
	base := name
	if i := strings.LastIndexAny(base, `/\`); i >= 0 {
		base = base[i+1:]
	}
	kept := make([]rune, 0, len(base))
	for _, ch := range base {
		if ch != ' ' && (unicode.Is(unicode.C, ch) || unicode.Is(unicode.Z, ch)) {
			continue
		}
		kept = append(kept, ch)
	}
	base = strings.TrimSpace(string(kept))
	dot := strings.LastIndex(base, ".")
	if dot < 0 {
		return "", badName(name)
	}
	stem, ext := base[:dot], base[dot+1:]
	if strings.Trim(stem, " .") == "" || ext == "" {
		return "", badName(name)
	}
	if utf8.RuneCountInString(base) > nameMax {
		rs := []rune(stem)
		keep := nameMax - utf8.RuneCountInString(ext) - 1
		if keep < 0 {
			keep = 0
		}
		if keep > len(rs) {
			keep = len(rs)
		}
		base = strings.TrimRight(string(rs[:keep]), " ") + "." + ext
	}
	return base, nil
}

// badName is the refusal of a name with no usable extension: the first 40
// characters of the name as it came.
func badName(name string) error {
	r := []rune(name)
	if len(r) > 40 {
		r = r[:40]
	}
	return fmt.Errorf("file name %q: a name with an extension, e.g. chart.png", string(r))
}
