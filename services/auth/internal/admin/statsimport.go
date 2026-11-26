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

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/mattermost"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// The STATS import (RT-6.45): the lines the stats channel holds, copied into
// a stream of their own, STATS_IMPORT, each under its bot's name, marked as
// imported and with its original time. The archive (STATS_ARCHIVE) holds the
// copies of STATS only, so no agent's line can pass for an imported one, and
// no bot's line lands among an agent's. It can run again: a line already
// there is skipped, so a second run imports nothing, and a run that stopped
// is completed by the next. The line is stored byte for byte as the channel
// holds it; the strict decoder of whoever reads it applies the allow list.

// StatsSource is what the import reads from Mattermost (mattermost.Client).
type StatsSource interface {
	ChannelPosts(ctx context.Context, channelName string, since time.Time) ([]mattermost.Post, error)
	Users(ctx context.Context, ids []string) ([]mattermost.User, error)
}

// HistoryReader reads one stream of the STATS history (consumers.History).
type HistoryReader interface {
	// ReadAll calls fn with the name and the body of every line the stream
	// holds (for the archive once its copy of STATS has caught up); begin
	// runs before each pass over it.
	ReadAll(ctx context.Context, begin func(), fn func(name string, body []byte) error) error
}

// ImportStore is the import stream (consumers.History of STATS_IMPORT).
type ImportStore interface {
	HistoryReader
	// Put stores one line on the name's subject and waits for the stream's
	// acknowledgement; consumers.ErrDuplicate when the stream took it as a
	// duplicate.
	Put(ctx context.Context, name string, body []byte, hdr map[string]string) error
}

// StatsImport configures the import; a nil one in Config turns it off.
type StatsImport struct {
	Channel string // the stats channel (CB_MM_STATS_CHANNEL)
	Source  StatsSource
	Archive HistoryReader // STATS_ARCHIVE: the copies of STATS
	Store   ImportStore   // STATS_IMPORT
	// CatchUp bounds the wait for the archive's copy of STATS (default 30 s).
	CatchUp time.Duration
	// MinAge: a post younger than this is left for the next run, since its
	// line may still be on its way to STATS and the archive (default 2 min).
	MinAge time.Duration
}

// Headers of an imported line.
const (
	HeaderSource     = "Cb-Source"
	HeaderOrigTime   = "Cb-Orig-Time"
	SourceMattermost = "mattermost-import"
)

const (
	statsPrefix = "STATS "
	// StatsLineMax is the largest line the import takes, in bytes of its
	// JSON, as the MCP server's NATS publish (stats.py NATS_BODY_MAX).
	StatsLineMax = 8000
	// statsPerName is how many lines the import keeps per name, the import
	// stream's cap: the newest ones, so a second run stores the same set.
	statsPerName = consumers.HistoryMaxMsgsPerSubject
)

var (
	sidRE       = regexp.MustCompile(`^[0-9a-f]{12}$`)
	agentNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)
)

// StatsReport is what the import did. Every post read is counted once: as
// imported, as no STATS line, or under one reason it was skipped.
type StatsReport struct {
	Read             int    `json:"read"`
	Imported         int    `json:"imported"`
	NotStats         int    `json:"not_stats"`
	SkippedDuplicate int    `json:"skipped_duplicate"`
	SkippedInvalid   int    `json:"skipped_invalid"`
	SkippedTooBig    int    `json:"skipped_too_big"`
	SkippedName      int    `json:"skipped_name"`
	SkippedRecent    int    `json:"skipped_recent"`
	SkippedOverCap   int    `json:"skipped_over_cap"`
	Oldest           string `json:"oldest,omitempty"` // original time of the oldest imported line
	Newest           string `json:"newest,omitempty"`
}

type statsKey struct {
	sid  string
	part int64
}

type namedKey struct {
	name string
	statsKey
}

// statsLine reads a STATS line's JSON: an object with sid (12 lower-case hex
// digits) and an integer part. Nothing else of it is looked at.
func statsLine(body []byte) (statsKey, bool) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(body, &obj); err != nil || obj == nil {
		return statsKey{}, false
	}
	var k statsKey
	if err := json.Unmarshal(obj["sid"], &k.sid); err != nil || !sidRE.MatchString(k.sid) {
		return statsKey{}, false
	}
	var part json.Number
	d := json.NewDecoder(bytes.NewReader(obj["part"]))
	d.UseNumber()
	if err := d.Decode(&part); err != nil {
		return statsKey{}, false
	}
	n, err := part.Int64()
	if err != nil || strings.ContainsAny(part.String(), ".eE") {
		return statsKey{}, false
	}
	k.part = n
	return k, true
}

// candidate is a valid line of the channel.
type candidate struct {
	namedKey
	id   string
	at   time.Time
	body []byte
}

// importStats runs the import (RT-6.45) at the time now.
func importStats(ctx context.Context, si *StatsImport, now time.Time) (StatsReport, error) {
	var rep StatsReport
	// The channel first, the archive after: a line posted meanwhile is on
	// STATS too, and the archive's copy of it is then seen below.
	posts, err := si.Source.ChannelPosts(ctx, si.Channel, time.Time{})
	if err != nil {
		return rep, fmt.Errorf("the stats channel: %w", err)
	}
	rep.Read = len(posts)
	ids := map[string]bool{}
	for _, p := range posts {
		if strings.HasPrefix(p.Message, statsPrefix) {
			ids[p.UserID] = true
		}
	}
	users := map[string]mattermost.User{}
	if len(ids) > 0 {
		list := make([]string, 0, len(ids))
		for id := range ids {
			list = append(list, id)
		}
		sort.Strings(list)
		got, err := si.Source.Users(ctx, list)
		if err != nil {
			return rep, fmt.Errorf("the stats channel's authors: %w", err)
		}
		for _, u := range got {
			users[u.ID] = u
		}
	}
	wait, minAge := si.CatchUp, si.MinAge
	if wait <= 0 {
		wait = 30 * time.Second
	}
	if minAge <= 0 {
		minAge = 2 * time.Minute
	}
	// What the archive holds, by (sid, part) alone: the same line has the
	// agent's NATS name there and its bot's Mattermost name here.
	copied := map[statsKey]bool{}
	wctx, cancel := context.WithTimeout(ctx, wait)
	err = si.Archive.ReadAll(wctx, func() { clear(copied) }, func(_ string, body []byte) error {
		if k, ok := statsLine(body); ok {
			copied[k] = true
		}
		return nil
	})
	cancel()
	if err != nil {
		return rep, fmt.Errorf("the archive: %w", err)
	}
	// What the import stream holds, by (name, sid, part).
	imported := map[namedKey]bool{}
	err = si.Store.ReadAll(ctx, func() { clear(imported) }, func(name string, body []byte) error {
		if k, ok := statsLine(body); ok {
			imported[namedKey{name, k}] = true
		}
		return nil
	})
	if err != nil {
		return rep, fmt.Errorf("the import stream: %w", err)
	}
	byName := map[string][]candidate{}
	for _, p := range posts {
		if !strings.HasPrefix(p.Message, statsPrefix) {
			rep.NotStats++
			continue
		}
		if p.Created.After(now.Add(-minAge)) {
			rep.SkippedRecent++
			continue
		}
		body := []byte(p.Message[len(statsPrefix):])
		if len(body) > StatsLineMax {
			rep.SkippedTooBig++
			continue
		}
		k, ok := statsLine(body)
		if !ok {
			rep.SkippedInvalid++
			continue
		}
		u, known := users[p.UserID]
		if !known || !u.IsBot || !agentNameRE.MatchString(u.Username) {
			rep.SkippedName++
			continue
		}
		if copied[k] {
			rep.SkippedDuplicate++
			continue
		}
		byName[u.Username] = append(byName[u.Username], candidate{namedKey{u.Username, k}, p.ID, p.Created.UTC(), body})
	}
	// The newest lines per name up to the cap, so the import stream (which
	// drops a name's oldest above it) holds the same set after every run.
	var todo []candidate
	for _, cs := range byName {
		sort.SliceStable(cs, func(i, j int) bool { return newer(cs[i], cs[j]) })
		if len(cs) > statsPerName {
			rep.SkippedOverCap += len(cs) - statsPerName
			cs = cs[:statsPerName]
		}
		todo = append(todo, cs...)
	}
	sort.SliceStable(todo, func(i, j int) bool { return newer(todo[j], todo[i]) }) // oldest first
	var oldest, newest time.Time
	for _, c := range todo {
		if imported[c.namedKey] {
			rep.SkippedDuplicate++
			continue
		}
		hdr := map[string]string{
			HeaderSource:   SourceMattermost,
			HeaderOrigTime: c.at.Format(time.RFC3339Nano),
			"Nats-Msg-Id":  fmt.Sprintf("stats-import:%s:%s:%d", c.name, c.sid, c.part),
		}
		err := si.Store.Put(ctx, c.name, c.body, hdr)
		switch {
		case errors.Is(err, consumers.ErrDuplicate):
			rep.SkippedDuplicate++
			imported[c.namedKey] = true
			continue
		case err != nil:
			return rep, fmt.Errorf("the import stream took %d lines, then: %w", rep.Imported, err)
		}
		imported[c.namedKey] = true
		rep.Imported++
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

// newer orders by original time, newest first, then by post id.
func newer(a, b candidate) bool {
	if !a.at.Equal(b.at) {
		return a.at.After(b.at)
	}
	return a.id > b.id
}

// errStatsRunning: another import runs.
var errStatsRunning = errors.New("a stats import is running")

func (h *handler) runStatsImport(w http.ResponseWriter, r *http.Request, id token.Identity) {
	si := h.cfg.Stats
	if si == nil {
		fail(w, http.StatusNotFound, "the stats import is off (no stats channel, or no archive)")
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
