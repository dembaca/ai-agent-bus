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
// (no acknowledgement of a publish, so no message id is set or answered) and
// a duplicate window of 100 ms, so the check of what STATS holds is the only
// duplicate check, and a re-read after the run confirms what arrived. The
// line is stored byte for byte as the channel holds it; the strict decoder
// of whoever reads it applies the allow list. Lines from before 0.91.0 have
// no sid and part: they are imported too, known by their post's id.

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
	// room STATS has left (Room, read again right before publishing), so the
	// import never pushes STATS's own lines out, and by MaxHeld (default
	// 64 MiB, the memory one run takes). At either bound the reading ends
	// there: the newest posts are read first, the older ones wait, and the
	// report says truncated and why.
	MaxLines int
	MaxHeld  int
	// ConfirmWait is how long the import waits before it reads STATS again
	// when fewer lines than it published are there (default 500 ms).
	ConfirmWait time.Duration
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
	Read                int `json:"read"`
	Published           int `json:"published"`
	PublishedWithoutSid int `json:"published_without_sid"` // of Published: lines from before 0.91.0
	// Confirmed: of Published, found in STATS when read again after the run
	// (once more after ConfirmWait when fewer). It can still be lower when
	// STATS is slow to show the last lines; the next run publishes what is
	// missing.
	Confirmed       int `json:"confirmed"`
	NotStats        int `json:"not_stats"`
	SkippedInStats  int `json:"skipped_in_stats"` // an agent published the same (sid, part) on STATS
	SkippedImported int `json:"skipped_imported"` // imported already, by an earlier run or this one
	SkippedInvalid  int `json:"skipped_invalid"`
	SkippedTooBig   int `json:"skipped_too_big"`
	SkippedName     int `json:"skipped_name"`
	SkippedRecent   int `json:"skipped_recent"`
	SkippedOverCap  int `json:"skipped_over_cap"`
	// SkippedRoom: held to publish, but STATS had less room right before
	// publishing than when the run began; the next run takes them.
	SkippedRoom int    `json:"skipped_room"`
	Truncated   string `json:"truncated,omitempty"`    // older posts were not read: "lines" or "room"
	KnownBefore string `json:"known_before,omitempty"` // older posts were gone through by an earlier complete run and not read again
	Oldest      string `json:"oldest,omitempty"`       // original time of the oldest published line
	Newest      string `json:"newest,omitempty"`
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

func (c candidate) size() int64 { return int64(len(c.body) + lineOverhead) }

// errEnough ends the channel's reading at a bound.
var errEnough = errors.New("enough read")

// inStats is what STATS holds: the (sid, part) of the agents' lines, the
// (sid, part) of the imported lines, and the imported lines' post ids, read
// only on the import's own subjects (no agent can publish there, so no agent
// can hide a post that way).
type inStats struct {
	agents, imported map[statsKey]bool
	posts            map[string]bool
}

func readStats(ctx context.Context, store StatsStore) (inStats, error) {
	h := inStats{map[statsKey]bool{}, map[statsKey]bool{}, map[string]bool{}}
	err := store.ReadAll(ctx, func() { clear(h.agents); clear(h.imported); clear(h.posts) }, func(name string, body []byte, hdr nats.Header) error {
		imported := strings.HasPrefix(name, importPrefix)
		if k, kind := statsLine(body); kind == lineKeyed {
			if imported {
				h.imported[k] = true
			} else {
				h.agents[k] = true
			}
		}
		if id := hdr.Get(HeaderPostID); id != "" && imported {
			h.posts[id] = true
		}
		return nil
	})
	return h, err
}

// importStats runs the import (RT-6.45) at the time now (this host's clock,
// for the 2-minute skip only). known is the create_at of the newest post an
// earlier complete run of this process went through, every older one with
// it (zero: none); done is the same for this run, zero when it was cut short
// (truncated, failed, not all confirmed) or went through no post.
func importStats(ctx context.Context, si *StatsImport, now, known time.Time) (rep StatsReport, done time.Time, err error) {
	minAge, maxLines, maxHeld, wait := si.MinAge, si.MaxLines, si.MaxHeld, si.ConfirmWait
	if minAge <= 0 {
		minAge = 2 * time.Minute
	}
	if maxLines <= 0 {
		maxLines = defaultMaxLines
	}
	if maxHeld <= 0 {
		maxHeld = int(consumers.DefaultStatsMaxBytes)
	}
	if wait <= 0 {
		wait = 500 * time.Millisecond
	}
	room, err := si.Store.Room(ctx)
	if err != nil {
		return rep, done, fmt.Errorf("the stats stream: %w", err)
	}
	limit := min(int64(maxHeld), room)
	// STATS first: only posts older than now-minAge are taken, and their
	// lines were on STATS by then. By (sid, part): the same line has the
	// agent's NATS name on STATS and its bot's Mattermost name in the
	// channel, so names cannot be compared.
	held, err := readStats(ctx, si.Store)
	if err != nil {
		return rep, done, fmt.Errorf("the stats stream: %w", err)
	}
	// The channel, newest first, one page at a time: only the lines to
	// publish are held, at most statsPerName per author (the newest; the
	// ones already imported count), at most maxLines lines and limit bytes.
	// Once a post is older than the newest post an earlier complete run went
	// through, so are all after it: the reading stops there. Both are
	// Mattermost's create_at, never this host's clock. Its newer posts were
	// all read, so the newest per author are counted as before.
	var todo []candidate
	perUser := map[string]int{}
	var used int64
	cutoff := now.Add(-minAge) // the 2-minute skip: this host's clock against create_at
	var through time.Time      // the newest post at or before cutoff: every older one is gone through
	err = si.Source.EachChannelPost(ctx, si.Channel, time.Time{}, func(p mattermost.Post) error {
		if !known.IsZero() && p.Created.Before(known) {
			rep.KnownBefore = known.UTC().Format(time.RFC3339)
			return errEnough
		}
		rep.Read++
		if !p.Created.After(cutoff) && p.Created.After(through) {
			through = p.Created
		}
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
		case held.posts[p.ID] && perUser[p.UserID] < statsPerName: // STATS holds it on the import's subject
			perUser[p.UserID]++
			rep.SkippedImported++
			return nil
		case held.posts[p.ID]: // held, but older than the newest of its author: STATS drops it next
			rep.SkippedOverCap++
			return nil
		case kind == lineKeyed && held.agents[k]: // an agent published it
			rep.SkippedInStats++
			return nil
		case kind == lineKeyed && held.imported[k]: // an earlier run, from another post of the same line
			rep.SkippedImported++
			return nil
		case perUser[p.UserID] >= statsPerName:
			rep.SkippedOverCap++
			return nil
		case len(todo) >= maxLines:
			rep.Read--
			rep.Truncated = truncatedLines
			return errEnough
		}
		c := candidate{k, kind == lineKeyed, p.ID, p.UserID, p.Created.UTC(), []byte(body)}
		if used+c.size() > limit {
			rep.Read--
			rep.Truncated = truncatedRoom
			return errEnough
		}
		perUser[p.UserID]++
		used += c.size()
		todo = append(todo, c)
		return nil
	})
	if err != nil && !errors.Is(err, errEnough) {
		return rep, done, fmt.Errorf("the stats channel: %w", err)
	}
	users, err := authors(ctx, si.Source, todo)
	if err != nil {
		return rep, done, err
	}
	// STATS kept growing while the channel was read: its room again, and
	// the oldest held lines dropped until the rest fit. todo is newest
	// first here (the channel's order).
	if room, err = si.Store.Room(ctx); err != nil {
		return rep, done, fmt.Errorf("the stats stream: %w", err)
	}
	for used > room && len(todo) > 0 {
		used -= todo[len(todo)-1].size()
		todo = todo[:len(todo)-1]
		rep.SkippedRoom++
		rep.Truncated = truncatedRoom
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
		if c.keyed && held.imported[c.key] { // the run published it already
			rep.SkippedImported++
			continue
		}
		hdr := map[string]string{
			HeaderSource:   SourceMattermost,
			HeaderOrigTime: c.at.Format(time.RFC3339Nano),
			HeaderPostID:   c.postID,
		}
		if err := si.Store.Put(ctx, u.Username, c.body, hdr); err != nil {
			return rep, done, fmt.Errorf("the stats stream took %d lines, then: %w", rep.Published, err)
		}
		if c.keyed {
			held.imported[c.key] = true
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
		// arrived, once more after a short wait when fewer did. A line that
		// did not is published again by the next run.
		for try := 0; try < 2; try++ {
			if try > 0 {
				select {
				case <-ctx.Done():
					return rep, done, ctx.Err()
				case <-time.After(wait):
				}
			}
			after, err := readStats(ctx, si.Store)
			if err != nil {
				return rep, done, fmt.Errorf("the stats stream, read again: %w", err)
			}
			rep.Confirmed = 0
			for id := range published {
				if after.posts[id] {
					rep.Confirmed++
				}
			}
			if rep.Confirmed == rep.Published {
				break
			}
		}
	}
	if rep.Truncated == "" && rep.Confirmed == rep.Published {
		done = through
	}
	return rep, done, nil
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
	rep, done, err := importStats(r.Context(), si, h.cfg.Now(), h.statsDone)
	if err != nil {
		h.cfg.Log.Error("stats import stopped; it can be run again", "reason", err.Error(), "sub", id.Subject,
			"published", rep.Published)
		fail(w, http.StatusBadGateway, fmt.Sprintf("the stats import stopped after %d lines; run it again, it skips what is there", rep.Published))
		return
	}
	// A run that went through every post up to its newest one older than two
	// minutes lets the next one stop at posts older than that; one cut short leaves the mark as it was, so the next run
	// reads on past it.
	if !done.IsZero() {
		h.statsDone = done
	}
	h.logWrite(id, "stats import", si.Channel, rep)
	reply(w, http.StatusOK, rep)
}
