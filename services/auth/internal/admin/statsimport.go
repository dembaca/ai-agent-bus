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
// into the STATS stream on <stats>.import-<bot>, a subject no agent may
// publish, marked as imported and with their original time. It can run
// again: a line STATS already holds is skipped, so a second run imports
// nothing, and a run that stopped is completed by the next. STATS has no_ack
// and a duplicate window of 100 ms, so that check of what STATS holds is the
// only duplicate check. The line is stored byte for byte as the channel holds
// it; the strict decoder of whoever reads it applies the allow list. Lines
// from before 0.91.0 have no sid and part: they are imported too, known by
// their post's id.

// StatsSource is what the import reads from Mattermost (mattermost.Client).
type StatsSource interface {
	// EachChannelPost calls fn with each post of the channel, newest first;
	// an error of fn ends the reading.
	EachChannelPost(ctx context.Context, channelName string, since time.Time, fn func(mattermost.Post) error) error
	Users(ctx context.Context, ids []string) ([]mattermost.User, error)
}

// StatsStore is the STATS stream (consumers.Stats).
type StatsStore interface {
	// ReadAll calls fn with the name (the subject's last token), the body and
	// the headers of every line STATS holds; begin runs before the pass.
	ReadAll(ctx context.Context, begin func(), fn func(name string, body []byte, hdr nats.Header) error) error
	// Put publishes one line on <stats>.import-<bot> with its headers.
	Put(ctx context.Context, bot string, body []byte, hdr map[string]string) error
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
	// MaxRead bounds the posts one run reads (default 100,000), MaxHeld the
	// bytes of lines it holds to import (default the STATS stream's size,
	// 64 MiB: more would push its own lines out). Reaching either ends the
	// reading there: the newest posts are read first, the older ones wait,
	// and the report says truncated.
	MaxRead int
	MaxHeld int
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
	// defaultMaxRead bounds the posts one run reads.
	defaultMaxRead = 100000
)

var (
	sidRE       = regexp.MustCompile(`^[0-9a-f]{12}$`)
	agentNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)
)

// StatsReport is what the import did. Every post read is counted once: as
// imported, as no STATS line, or under one reason it was skipped.
type StatsReport struct {
	Read               int    `json:"read"`
	Imported           int    `json:"imported"`
	ImportedWithoutSid int    `json:"imported_without_sid"` // of Imported: lines from before 0.91.0
	NotStats           int    `json:"not_stats"`
	SkippedDuplicate   int    `json:"skipped_duplicate"`
	SkippedInvalid     int    `json:"skipped_invalid"`
	SkippedTooBig      int    `json:"skipped_too_big"`
	SkippedName        int    `json:"skipped_name"`
	SkippedRecent      int    `json:"skipped_recent"`
	SkippedOverCap     int    `json:"skipped_over_cap"`
	Truncated          bool   `json:"truncated"`        // older posts were not read (MaxRead or MaxHeld)
	Oldest             string `json:"oldest,omitempty"` // original time of the oldest imported line
	Newest             string `json:"newest,omitempty"`
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

// candidate is a valid line of the channel, to be imported.
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

// importStats runs the import (RT-6.45) at the time now.
func importStats(ctx context.Context, si *StatsImport, now time.Time) (StatsReport, error) {
	var rep StatsReport
	minAge, maxRead, maxHeld := si.MinAge, si.MaxRead, si.MaxHeld
	if minAge <= 0 {
		minAge = 2 * time.Minute
	}
	if maxRead <= 0 {
		maxRead = defaultMaxRead
	}
	if maxHeld <= 0 {
		maxHeld = int(consumers.DefaultStatsMaxBytes)
	}
	// STATS first: only posts older than now-minAge are taken, and their
	// lines were on STATS by then. Every line by (sid, part): the same line
	// has the agent's NATS name on STATS and its bot's Mattermost name in the
	// channel, so names cannot be compared. Imported lines also by their
	// post's id, read only from the import's own subjects (no agent can
	// publish there, so no agent can hide a post that way).
	copied, importedPosts := map[statsKey]bool{}, map[string]bool{}
	err := si.Store.ReadAll(ctx, func() { clear(copied); clear(importedPosts) }, func(name string, body []byte, hdr nats.Header) error {
		if k, kind := statsLine(body); kind == lineKeyed {
			copied[k] = true
		}
		if id := hdr.Get(HeaderPostID); id != "" && strings.HasPrefix(name, consumers.ImportToken) {
			importedPosts[id] = true
		}
		return nil
	})
	if err != nil {
		return rep, fmt.Errorf("the stats stream: %w", err)
	}
	// The channel, newest first, one page at a time: only the lines to
	// import are held, at most statsPerName per author (the newest; the
	// ones already imported count) and at most maxHeld bytes in all.
	var todo []candidate
	perUser := map[string]int{}
	held := 0
	cutoff := now.Add(-minAge)
	err = si.Source.EachChannelPost(ctx, si.Channel, time.Time{}, func(p mattermost.Post) error {
		if rep.Read >= maxRead {
			rep.Truncated = true
			return errEnough
		}
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
		case held+len(body) > maxHeld:
			rep.Read--
			rep.Truncated = true
			return errEnough
		}
		perUser[p.UserID]++
		held += len(body)
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
	var oldest, newest time.Time
	for _, c := range todo {
		u, known := users[c.userID]
		if !known || !u.IsBot || !agentNameRE.MatchString(u.Username) {
			rep.SkippedName++
			continue
		}
		if c.keyed && copied[c.key] { // the run stored it already
			rep.SkippedDuplicate++
			continue
		}
		hdr := map[string]string{
			HeaderSource:   SourceMattermost,
			HeaderOrigTime: c.at.Format(time.RFC3339Nano),
			HeaderPostID:   c.postID,
		}
		if err := si.Store.Put(ctx, u.Username, c.body, hdr); err != nil {
			return rep, fmt.Errorf("the stats stream took %d lines, then: %w", rep.Imported, err)
		}
		if c.keyed {
			copied[c.key] = true
		}
		rep.Imported++
		if !c.keyed {
			rep.ImportedWithoutSid++
		}
		if oldest.IsZero() || c.at.Before(oldest) {
			oldest = c.at
		}
		if c.at.After(newest) {
			newest = c.at
		}
	}
	if rep.Imported > 0 {
		rep.Oldest, rep.Newest = oldest.Format(time.RFC3339), newest.Format(time.RFC3339)
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
			"imported", rep.Imported)
		fail(w, http.StatusBadGateway, fmt.Sprintf("the stats import stopped after %d lines; run it again, it skips what is there", rep.Imported))
		return
	}
	h.logWrite(id, "stats import", si.Channel, rep)
	reply(w, http.StatusOK, rep)
}
