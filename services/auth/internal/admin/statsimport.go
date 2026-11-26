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

	"github.com/dembaca/ai-agent-bus/services/auth/internal/mattermost"
	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// The STATS import (RT-6.45): the lines the stats channel holds, copied once
// into the STATS history (the STATS_ARCHIVE stream), each under its bot's
// name, marked as imported and with its original time. It can run again: a
// line whose (sid, part) the archive already holds is skipped, so a second
// run imports nothing, and a run that stopped is completed by the next. The
// line is stored byte for byte as the channel holds it; the console's strict
// decoder (its allow list) applies when it is read, not here.

// StatsSource is what the import reads from Mattermost (mattermost.Client).
type StatsSource interface {
	ChannelPosts(ctx context.Context, channelName string, since time.Time) ([]mattermost.Post, error)
	Users(ctx context.Context, ids []string) ([]mattermost.User, error)
}

// ArchiveStore is the STATS history (consumers.Archive).
type ArchiveStore interface {
	// ReadAll calls fn with every line the archive holds, once its copy of
	// STATS has caught up; begin runs before each pass over it.
	ReadAll(ctx context.Context, begin func(), fn func(body []byte) error) error
	// Put stores one line on the agent's archive subject and waits for the
	// archive's acknowledgement.
	Put(ctx context.Context, agent string, body []byte, hdr map[string]string) error
}

// StatsImport configures the import; a nil one in Config turns it off.
type StatsImport struct {
	Channel string // the stats channel (CB_MM_STATS_CHANNEL)
	Source  StatsSource
	Store   ArchiveStore
	// CatchUp bounds the wait for the archive's copy of STATS (default 30 s).
	CatchUp time.Duration
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
	// JSON: below the archive's 8 KiB per message, with room for headers.
	StatsLineMax = 8000
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
	Oldest           string `json:"oldest,omitempty"` // original time of the oldest imported line
	Newest           string `json:"newest,omitempty"`
}

type statsKey struct {
	sid  string
	part int64
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

// importStats runs the import (RT-6.45).
func importStats(ctx context.Context, si *StatsImport) (StatsReport, error) {
	var rep StatsReport
	// The channel first, the archive after: a line posted meanwhile is on
	// STATS too, and the archive's copy of it is then seen below.
	posts, err := si.Source.ChannelPosts(ctx, si.Channel, time.Time{})
	if err != nil {
		return rep, fmt.Errorf("the stats channel: %w", err)
	}
	rep.Read = len(posts)
	sort.SliceStable(posts, func(i, j int) bool {
		if !posts[i].Created.Equal(posts[j].Created) {
			return posts[i].Created.Before(posts[j].Created)
		}
		return posts[i].ID < posts[j].ID
	})
	ids := map[string]bool{}
	for _, p := range posts {
		if strings.HasPrefix(p.Message, statsPrefix) {
			ids[p.UserID] = true
		}
	}
	names := map[string]mattermost.User{}
	if len(ids) > 0 {
		list := make([]string, 0, len(ids))
		for id := range ids {
			list = append(list, id)
		}
		sort.Strings(list)
		users, err := si.Source.Users(ctx, list)
		if err != nil {
			return rep, fmt.Errorf("the stats channel's authors: %w", err)
		}
		for _, u := range users {
			names[u.ID] = u
		}
	}
	wait := si.CatchUp
	if wait <= 0 {
		wait = 30 * time.Second
	}
	held := map[statsKey]bool{}
	wctx, cancel := context.WithTimeout(ctx, wait)
	err = si.Store.ReadAll(wctx, func() { clear(held) }, func(body []byte) error {
		if k, ok := statsLine(body); ok {
			held[k] = true
		}
		return nil
	})
	cancel()
	if err != nil {
		return rep, fmt.Errorf("the archive: %w", err)
	}
	var oldest, newest time.Time
	for _, p := range posts {
		if !strings.HasPrefix(p.Message, statsPrefix) {
			rep.NotStats++
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
		u, known := names[p.UserID]
		name := u.Username
		if !known || !u.IsBot || !agentNameRE.MatchString(name) {
			rep.SkippedName++
			continue
		}
		if held[k] {
			rep.SkippedDuplicate++
			continue
		}
		at := p.Created.UTC()
		hdr := map[string]string{
			HeaderSource:   SourceMattermost,
			HeaderOrigTime: at.Format(time.RFC3339Nano),
			"Nats-Msg-Id":  fmt.Sprintf("stats-import:%s:%d", k.sid, k.part),
		}
		if err := si.Store.Put(ctx, name, body, hdr); err != nil {
			return rep, fmt.Errorf("the archive took %d lines, then: %w", rep.Imported, err)
		}
		held[k] = true
		rep.Imported++
		if oldest.IsZero() || at.Before(oldest) {
			oldest = at
		}
		if at.After(newest) {
			newest = at
		}
	}
	if rep.Imported > 0 {
		rep.Oldest, rep.Newest = oldest.Format(time.RFC3339), newest.Format(time.RFC3339)
	}
	return rep, nil
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
	rep, err := importStats(r.Context(), si)
	if err != nil {
		h.cfg.Log.Error("stats import stopped; it can be run again", "reason", err.Error(), "sub", id.Subject,
			"imported", rep.Imported)
		fail(w, http.StatusBadGateway, fmt.Sprintf("the stats import stopped after %d lines; run it again, it skips what is there", rep.Imported))
		return
	}
	h.logWrite(id, "stats import", si.Channel, rep)
	reply(w, http.StatusOK, rep)
}
