package admin

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/mattermost"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// The STATS import (RT-6.45): the lines the stats channel holds, published
// into the STATS stream on <stats>.import.<bot>, a subject no agent may
// publish, marked as imported and with their original time. It can run
// again: a line STATS already holds is skipped, so a second run publishes
// nothing, and a run that stopped is completed by the next. STATS has no_ack
// (no acknowledgement of a publish) and a duplicate window of 100 ms, so the
// check of what STATS holds is the only duplicate check, and a re-read after
// the run confirms what arrived. The line is stored byte for byte as the
// channel holds it; the strict decoder of whoever reads it applies the allow
// list. Lines from before 0.91.0 have no sid and part: they are imported
// too, known by their post's id.

// StatsSource is what the import reads from Mattermost (mattermost.Client).
type StatsSource interface {
	// EachChannelPost calls fn with each post of the channel, newest first;
	// an error of fn ends the reading.
	EachChannelPost(ctx context.Context, channelName string, since time.Time, fn func(mattermost.Post) error) error
	Users(ctx context.Context, ids []string) ([]mattermost.User, error)
}

// StatsStore is the STATS stream (consumers.Stats).
type StatsStore interface {
	// ReadAll calls fn with the name (the subject after the prefix: an
	// agent's name, or import.<bot>), the body and the headers of every line
	// STATS holds; begin runs before the pass.
	ReadAll(ctx context.Context, begin func(), fn func(name string, body []byte, hdr nats.Header) error) error
	// Put publishes one line on <stats>.import.<bot> with its headers.
	Put(ctx context.Context, bot string, body []byte, hdr map[string]string) error
	// Room is how many bytes STATS takes before it drops its oldest lines.
	Room(ctx context.Context) (int64, error)
}

// StatsImport configures the import; a nil one in Config turns it off.
type StatsImport struct {
	Channel string // the stats channel (CB_MM_STATS_CHANNEL)
	Source  StatsSource
	Store   StatsStore
	// MinAge: a post younger than this when the run starts is left for the
	// next run, since its line may still be on its way to STATS (default
	// 2 min).
	MinAge time.Duration
	// MaxLines bounds the lines one run holds to publish (default 100,000);
	// posts that are no line to publish (already in STATS, invalid, …) do not
	// count, so a rerun gets past them. The bytes it holds are bounded by the
	// room STATS has left (Room), so the import never pushes STATS's own
	// lines out, and by MaxHeld (default 64 MiB, the memory one run takes).
	// At either bound the reading ends there: the newest posts are read
	// first, the older ones wait, and the report says truncated and why.
	MaxLines int
	MaxHeld  int
}

// Headers of an imported line.
const (
	HeaderSource     = "Cb-Source"
	HeaderOrigTime   = "Cb-Orig-Time"
	HeaderPostID     = "Cb-Post-Id"
	SourceMattermost = "mattermost-import"
)

const (
	statsPrefix = "STATS "
	// StatsLineMax is the largest line the import takes, in bytes of its
	// JSON, as the MCP server's NATS publish (stats.py NATS_BODY_MAX): with
	// its headers within the 8 KiB per message of STATS.
	StatsLineMax = 8000
	// statsPerName is how many lines the import keeps per author, the cap
	// per subject of STATS: the newest ones, so a second run stores the same
	// set.
	statsPerName = consumers.StatsMaxMsgsPerSubject
	// defaultMaxLines bounds the lines one run holds to publish.
	defaultMaxLines = 100000
	// lineOverhead is what a line takes in STATS beside its body: subject,
	// the three headers and the store's own record, counted generously
	// against the room.
	lineOverhead = 256
	// importPrefix is how an imported line's name starts in ReadAll.
	importPrefix = consumers.ImportToken + "."
)

var (
	sidRE       = regexp.MustCompile(`^[0-9a-f]{12}$`)
	agentNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)
)

// Reasons a run was truncated.
const (
	truncatedLines = "lines" // MaxLines
	truncatedRoom  = "room"  // the room STATS has left, or MaxHeld
)

// StatsReport is what the import did. Every post read is counted once: as
// published, as no STATS line, or under one reason it was skipped.
type StatsReport struct {
	Read                int    `json:"read"`
	Published           int    `json:"published"`
	PublishedWithoutSid int    `json:"published_without_sid"` // of Published: lines from before 0.91.0
	Confirmed           int    `json:"confirmed"`             // of Published: found in STATS when read again after the run
	NotStats            int    `json:"not_stats"`
	SkippedDuplicate    int    `json:"skipped_duplicate"`
	SkippedInvalid      int    `json:"skipped_invalid"`
	SkippedTooBig       int    `json:"skipped_too_big"`
	SkippedName         int    `json:"skipped_name"`
	SkippedRecent       int    `json:"skipped_recent"`
	SkippedOverCap      int    `json:"skipped_over_cap"`
	Truncated           string `json:"truncated,omitempty"` // older posts were not read: "lines" or "room"
	Oldest              string `json:"oldest,omitempty"`    // original time of the oldest published line
	Newest              string `json:"newest,omitempty"`
}

type statsKey struct {
	sid  string
	part int64
}

// Kinds of a line: invalid, with sid and part, or from before 0.91.0
// (neither key).
const (
	lineInvalid = iota
	lineKeyed
	lineOld
)

// statsLine reads a STATS line's JSON: an object, with sid (12 lower-case
// hex digits) and an integer part, or with neither (a line from before
// 0.91.0). Nothing else of it is looked at.
func statsLine(body []byte) (statsKey, int) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(body, &obj); err != nil || obj == nil {
		return statsKey{}, lineInvalid
	}
	rawSid, hasSid := obj["sid"]
	rawPart, hasPart := obj["part"]
	if !hasSid && !hasPart {
		return statsKey{}, lineOld
	}
	var k statsKey
	if err := json.Unmarshal(rawSid, &k.sid); err != nil || !sidRE.MatchString(k.sid) {
		return statsKey{}, lineInvalid
	}
	var part json.Number
	d := json.NewDecoder(bytes.NewReader(rawPart))
	d.UseNumber()
	if err := d.Decode(&part); err != nil {
		return statsKey{}, lineInvalid
	}
	n, err := part.Int64()
	if err != nil || strings.ContainsAny(part.String(), ".eE") {
		return statsKey{}, lineInvalid
	}
	k.part = n
	return k, lineKeyed
}

// candidate is a valid line of the channel, to be published.
type candidate struct {
	key    statsKey
	keyed  bool
	postID string
	userID string
	at     time.Time
	body   []byte
}

// errEnough ends the channel's reading at a bound.
var errEnough = errors.New("enough read")

// held reads what STATS holds: every line by (sid, part), and the imported
// lines by their post's id, read only on the import's own subjects (no
// agent can publish there, so no agent can hide a post that way).
func held(ctx context.Context, store StatsStore) (map[statsKey]bool, map[string]bool, error) {
	copied, posts := map[statsKey]bool{}, map[string]bool{}
	err := store.ReadAll(ctx, func() { clear(copied); clear(posts) }, func(name string, body []byte, hdr nats.Header) error {
		if k, kind := statsLine(body); kind == lineKeyed {
			copied[k] = true
		}
		if id := hdr.Get(HeaderPostID); id != "" && strings.HasPrefix(name, importPrefix) {
			posts[id] = true
		}
		return nil
	})
	return copied, posts, err
}

// importStats runs the import (RT-6.45) at the time now.
func importStats(ctx context.Context, si *StatsImport, now time.Time) (StatsReport, error) {
	var rep StatsReport
	minAge, maxLines, maxHeld := si.MinAge, si.MaxLines, si.MaxHeld
	if minAge <= 0 {
		minAge = 2 * time.Minute
	}
	if maxLines <= 0 {
		maxLines = defaultMaxLines
	}
	if maxHeld <= 0 {
		maxHeld = int(consumers.DefaultStatsMaxBytes)
	}
	room, err := si.Store.Room(ctx)
	if err != nil {
		return rep, fmt.Errorf("the stats stream: %w", err)
	}
	maxHeld = int(min(int64(maxHeld), room))
	// STATS first: only posts older than now-minAge are taken, and their
	// lines were on STATS by then. By (sid, part): the same line has the
	// agent's NATS name on STATS and its bot's Mattermost name in the
	// channel, so names cannot be compared.
	copied, importedPosts, err := held(ctx, si.Store)
	if err != nil {
		return rep, fmt.Errorf("the stats stream: %w", err)
	}
	// The channel, newest first, one page at a time: only the lines to
	// publish are held, at most statsPerName per author (the newest; the
	// ones already imported count), at most maxLines lines and maxHeld bytes.
	var todo []candidate
	perUser := map[string]int{}
	used := 0
	cutoff := now.Add(-minAge)
	err = si.Source.EachChannelPost(ctx, si.Channel, time.Time{}, func(p mattermost.Post) error {
		rep.Read++
		if !strings.HasPrefix(p.Message, statsPrefix) {
			rep.NotStats++
			return nil
		}
		if p.Created.After(cutoff) {
			rep.SkippedRecent++
			return nil
		}
		body := p.Message[len(statsPrefix):]
		if len(body) > StatsLineMax {
			rep.SkippedTooBig++
			return nil
		}
		k, kind := statsLine([]byte(body))
		switch {
		case kind == lineInvalid:
			rep.SkippedInvalid++
			return nil
		case importedPosts[p.ID] && perUser[p.UserID] < statsPerName: // STATS holds it on the import's subject
			perUser[p.UserID]++
			rep.SkippedDuplicate++
			return nil
		case importedPosts[p.ID]: // held, but older than the newest of its author: STATS drops it next
			rep.SkippedOverCap++
			return nil
		case kind == lineKeyed && copied[k]: // an agent published it, or an earlier run
			rep.SkippedDuplicate++
			return nil
		case perUser[p.UserID] >= statsPerName:
			rep.SkippedOverCap++
			return nil
		case len(todo) >= maxLines:
			rep.Read--
			rep.Truncated = truncatedLines
			return errEnough
		case used+len(body)+lineOverhead > maxHeld:
			rep.Read--
			rep.Truncated = truncatedRoom
			return errEnough
		}
		perUser[p.UserID]++
		used += len(body) + lineOverhead
		todo = append(todo, candidate{k, kind == lineKeyed, p.ID, p.UserID, p.Created.UTC(), []byte(body)})
		return nil
	})
	if err != nil && !errors.Is(err, errEnough) {
		return rep, fmt.Errorf("the stats channel: %w", err)
	}
	users, err := authors(ctx, si.Source, todo)
	if err != nil {
		return rep, err
	}
	sort.SliceStable(todo, func(i, j int) bool { // oldest first
		if !todo[i].at.Equal(todo[j].at) {
			return todo[i].at.Before(todo[j].at)
		}
		return todo[i].postID < todo[j].postID
	})
	published := map[string]bool{}
	var oldest, newest time.Time
	for _, c := range todo {
		u, known := users[c.userID]
		if !known || !u.IsBot || !agentNameRE.MatchString(u.Username) {
			rep.SkippedName++
			continue
		}
		if c.keyed && copied[c.key] { // the run published it already
			rep.SkippedDuplicate++
			continue
		}
		hdr := map[string]string{
			HeaderSource:   SourceMattermost,
			HeaderOrigTime: c.at.Format(time.RFC3339Nano),
			HeaderPostID:   c.postID,
		}
		if err := si.Store.Put(ctx, u.Username, c.body, hdr); err != nil {
			return rep, fmt.Errorf("the stats stream took %d lines, then: %w", rep.Published, err)
		}
		if c.keyed {
			copied[c.key] = true
		}
		published[c.postID] = true
		rep.Published++
		if !c.keyed {
			rep.PublishedWithoutSid++
		}
		if oldest.IsZero() || c.at.Before(oldest) {
			oldest = c.at
		}
		if c.at.After(newest) {
			newest = c.at
		}
	}
	if rep.Published > 0 {
		rep.Oldest, rep.Newest = oldest.Format(time.RFC3339), newest.Format(time.RFC3339)
		// STATS acknowledges nothing (no_ack): read it again and count what
		// arrived. A line that did not is published again by the next run.
		_, after, err := held(ctx, si.Store)
		if err != nil {
			return rep, fmt.Errorf("the stats stream, read again: %w", err)
		}
		for id := range published {
			if after[id] {
				rep.Confirmed++
			}
		}
	}
	return rep, nil
}

// authors reads the accounts of the lines' authors, by id.
func authors(ctx context.Context, src StatsSource, todo []candidate) (map[string]mattermost.User, error) {
	ids := map[string]bool{}
	for _, c := range todo {
		ids[c.userID] = true
	}
	out := map[string]mattermost.User{}
	if len(ids) == 0 {
		return out, nil
	}
	list := make([]string, 0, len(ids))
	for id := range ids {
		list = append(list, id)
	}
	sort.Strings(list)
	got, err := src.Users(ctx, list)
	if err != nil {
		return nil, fmt.Errorf("the stats channel's authors: %w", err)
	}
	for _, u := range got {
		out[u.ID] = u
	}
	return out, nil
}

// errStatsRunning: another import runs.
var errStatsRunning = errors.New("a stats import is running")

func (h *handler) runStatsImport(w http.ResponseWriter, r *http.Request, id token.Identity) {
	si := h.cfg.Stats
	if si == nil {
		fail(w, http.StatusNotFound, "the stats import is off (no stats channel)")
		return
	}
	if !h.statsMu.TryLock() {
		fail(w, http.StatusConflict, errStatsRunning.Error())
		return
	}
	defer h.statsMu.Unlock()
	rep, err := importStats(r.Context(), si, h.cfg.Now())
	if err != nil {
		h.cfg.Log.Error("stats import stopped; it can be run again", "reason", err.Error(), "sub", id.Subject,
			"published", rep.Published)
		fail(w, http.StatusBadGateway, fmt.Sprintf("the stats import stopped after %d lines; run it again, it skips what is there", rep.Published))
		return
	}
	h.logWrite(id, "stats import", si.Channel, rep)
	reply(w, http.StatusOK, rep)
}
