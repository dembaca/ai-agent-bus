package consumers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

// The STATS history (RT-6.45), two streams the login service keeps:
//
//   - STATS_ARCHIVE keeps a year of STATS lines on <archive>.<agent>.
//     JetStream itself copies every STATS line into it (the stream's one
//     source, with a subject transform); nobody publishes on it, so a line
//     there is exactly what an agent published on STATS.
//   - STATS_IMPORT keeps the lines the login service's import read from the
//     stats channel (admin/statsimport.go) on stats-import.<bot>, marked as
//     imported; only the login service publishes on it.
//
// No agent has any right on either (callout/perms.go grants <stats>.<agent>
// only), so neither an agent's own headers nor its name can make a line look
// imported, and an imported bot's name never lands among the copies. Their
// own limits keep a flood of STATS from reaching the imported lines. The
// configuration of an existing stream is only checked, never updated, as for
// the other streams.

// History is one of the two streams of the STATS history.
type History struct {
	m      *Manager
	stream string
	prefix string

	mu     sync.Mutex
	above  bool // the last fill check was at or over ArchiveWarnPercent
	behind int  // consecutive checks that found the copy of STATS behind
	warned bool // the copy's trouble was logged
}

// Stream is the history's stream name.
func (h *History) Stream() string { return h.stream }

// Prefix is the history's subject prefix.
func (h *History) Prefix() string { return h.prefix }

// historyNameRE: a name as one subject token of the history (the import
// takes the bot's username, which must fit).
var historyNameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// EnsureArchive creates the STATS_ARCHIVE stream when it is missing (RT-6.45)
// and checks it when it exists: a stream that differs from the safe
// configuration (checkArchive) is refused with ErrUnsafeStream. prefix ""
// takes DefaultArchivePrefix; maxBytes <= 0 takes DefaultArchiveMaxBytes.
// Run it after EnsureStreams: its source is STATS.
func (m *Manager) EnsureArchive(ctx context.Context, prefix string, maxBytes int64) (*History, error) {
	if prefix == "" {
		prefix = DefaultArchivePrefix
	}
	if !prefixRE.MatchString(prefix) || prefix == DeliverPrefix {
		return nil, fmt.Errorf("consumers: archive prefix %q: letters, digits, - and _, not %q", prefix, DeliverPrefix)
	}
	if prefix == m.cfg.Bus || prefix == m.cfg.Presence || prefix == m.cfg.Stats || prefix == ImportPrefix {
		return nil, fmt.Errorf("consumers: the archive prefix must differ from the bus, the presence, the stats and the import prefix (%s)", ImportPrefix)
	}
	err := m.ensureHistory(ctx, ArchiveStream, ArchiveStreamJSON(prefix, m.cfg.Stats, maxBytes),
		func(got map[string]any) error { return checkArchive(prefix, m.cfg.Stats, got) })
	if err != nil {
		return nil, err
	}
	return &History{m: m, stream: ArchiveStream, prefix: prefix}, nil
}

// EnsureImport creates the STATS_IMPORT stream when it is missing (RT-6.45)
// and checks it when it exists (checkImport). maxBytes <= 0 takes
// DefaultImportMaxBytes.
func (m *Manager) EnsureImport(ctx context.Context, maxBytes int64) (*History, error) {
	if ImportPrefix == m.cfg.Bus || ImportPrefix == m.cfg.Presence || ImportPrefix == m.cfg.Stats {
		return nil, fmt.Errorf("consumers: %q is the import stream's prefix: no other prefix may take it", ImportPrefix)
	}
	if err := m.ensureHistory(ctx, ImportStream, ImportStreamJSON(maxBytes), checkImport); err != nil {
		return nil, err
	}
	return &History{m: m, stream: ImportStream, prefix: ImportPrefix}, nil
}

// ensureHistory is the manager's ensure with the history's own check.
func (m *Manager) ensureHistory(ctx context.Context, name, cfg string, check func(map[string]any) error) error {
	for range 3 {
		var info struct {
			Config map[string]any `json:"config"`
		}
		err := m.api(ctx, "$JS.API.STREAM.INFO."+name, nil, &info)
		switch {
		case err == nil:
			return check(info.Config)
		case !isAPI(err, errStreamNotFound):
			return streamErr(name, err)
		}
		err = m.api(ctx, "$JS.API.STREAM.CREATE."+name, cfg, nil)
		if err != nil && !isAPI(err, errStreamNameInUse) {
			return streamErr(name, err)
		}
		if err == nil {
			m.log.Info("consumers", "event", "stream created", "stream", name)
		}
		// Read back, whatever the server kept is what gets checked.
	}
	return fmt.Errorf("stream %s: neither found nor created", name)
}

// sourceInfo is a source's state in STREAM.INFO.
type sourceInfo struct {
	Name  string    `json:"name"`
	Lag   uint64    `json:"lag"`
	Error *apiError `json:"error"`
}

// historyInfo is the part of STREAM.INFO the history reads.
type historyInfo struct {
	Config struct {
		MaxBytes int64 `json:"max_bytes"`
	} `json:"config"`
	State struct {
		Msgs  uint64 `json:"messages"`
		Bytes uint64 `json:"bytes"`
	} `json:"state"`
	Sources []sourceInfo `json:"sources"`
}

func (h *History) info(ctx context.Context) (historyInfo, error) {
	var info historyInfo
	if err := h.m.api(ctx, "$JS.API.STREAM.INFO."+h.stream, nil, &info); err != nil {
		return info, streamErr(h.stream, err)
	}
	return info, nil
}

func fill(info historyInfo) int {
	if info.Config.MaxBytes <= 0 {
		return 100
	}
	return int(info.State.Bytes * 100 / uint64(info.Config.MaxBytes))
}

// Fill is how full the stream is, in percent of its max_bytes (RT-6.45).
func (h *History) Fill(ctx context.Context) (int, error) {
	info, err := h.info(ctx)
	if err != nil {
		return 0, err
	}
	return fill(info), nil
}

// CheckFill logs a warning once each time the stream's fill crosses
// ArchiveWarnPercent upwards, and a note when it is below again (RT-6.45):
// the full archive drops its oldest lines, the full import stream refuses
// new ones. For the archive it also checks the copy of STATS (checkSource).
func (h *History) CheckFill(ctx context.Context) {
	info, err := h.info(ctx)
	if err != nil {
		h.m.log.Warn("stats history: fill unknown", "stream", h.stream, "reason", err.Error())
		return
	}
	pct := fill(info)
	h.mu.Lock()
	was := h.above
	h.above = pct >= ArchiveWarnPercent
	now := h.above
	h.mu.Unlock()
	switch {
	case now && !was:
		h.m.log.Warn(fmt.Sprintf("stats history over %d %% full", ArchiveWarnPercent),
			"stream", h.stream, "percent", pct,
			"remedy", "a larger max_bytes, made by hand (the service only checks an existing stream)")
	case !now && was:
		h.m.log.Info(fmt.Sprintf("stats history below %d %% full again", ArchiveWarnPercent),
			"stream", h.stream, "percent", pct)
	}
	if h.stream == ArchiveStream {
		h.checkSource(info.Sources)
	}
}

// checkSource warns when the archive's copy of STATS fails (the source
// reports an error, or there is none) or has been behind at two checks in a
// row (an hour apart: a lag at one check is a copy in progress), once per
// episode, and notes when it is well again.
func (h *History) checkSource(srcs []sourceInfo) {
	h.mu.Lock()
	defer h.mu.Unlock()
	var trouble string
	switch {
	case len(srcs) != 1:
		trouble = "no source"
	case srcs[0].Error != nil:
		trouble = srcs[0].Error.Error()
	case srcs[0].Lag > 0:
		h.behind++
		if h.behind >= 2 {
			trouble = fmt.Sprintf("%d lines behind", srcs[0].Lag)
		}
	default:
		h.behind = 0
	}
	switch {
	case trouble != "" && !h.warned:
		h.warned = true
		h.m.log.Warn("stats archive: the copy of STATS is failing", "stream", h.stream, "reason", trouble)
	case trouble == "" && h.warned && h.behind == 0:
		h.warned = false
		h.m.log.Info("stats archive: the copy of STATS is well again", "stream", h.stream)
	}
}

// WatchFill runs CheckFill now and then every interval until ctx ends.
func (h *History) WatchFill(ctx context.Context, every time.Duration) {
	go func() {
		for {
			cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
			h.CheckFill(cctx)
			cancel()
			select {
			case <-ctx.Done():
				return
			case <-time.After(every):
			}
		}
	}()
}

// statsLast is the sequence of the newest line STATS holds; 0 when it holds
// none.
func (h *History) statsLast(ctx context.Context) (uint64, error) {
	var info struct {
		State struct {
			Msgs    uint64 `json:"messages"`
			LastSeq uint64 `json:"last_seq"`
		} `json:"state"`
	}
	if err := h.m.api(ctx, "$JS.API.STREAM.INFO."+StatsStream, nil, &info); err != nil {
		return 0, streamErr(StatsStream, err)
	}
	if info.State.Msgs == 0 {
		return 0, nil
	}
	return info.State.LastSeq, nil
}

// SourceSeq is the STATS sequence of an archived line JetStream copied from
// STATS, read from its Nats-Stream-Source header ("STATS[:<domain hash>]
// <seq> …"); false for a message without it.
func SourceSeq(hdr nats.Header) (uint64, bool) {
	f := strings.Fields(hdr.Get("Nats-Stream-Source"))
	if len(f) < 2 || (f[0] != StatsStream && !strings.HasPrefix(f[0], StatsStream+":")) {
		return 0, false
	}
	seq, err := strconv.ParseUint(f[1], 10, 64)
	return seq, err == nil
}

// ReadAll calls fn with the name (the subject's last token), the body and the
// headers of every line the stream holds. For the archive it does so once the archive
// holds a copy of every STATS line stored before the call (RT-6.45), so the
// import's duplicate check sees each of them: a pass that finds the copy
// behind is thrown away (begin runs before each pass) and repeated until ctx
// ends. The source's lag in STREAM.INFO is no such proof: it reads 0 before
// the source has delivered anything.
func (h *History) ReadAll(ctx context.Context, begin func(), fn func(name string, body []byte, hdr nats.Header) error) error {
	var want uint64
	if h.stream == ArchiveStream {
		var err error
		if want, err = h.statsLast(ctx); err != nil {
			return err
		}
	}
	for {
		begin()
		var got uint64
		err := h.Each(ctx, func(subject string, body []byte, hdr nats.Header) error {
			if seq, ok := SourceSeq(hdr); ok && seq > got {
				got = seq
			}
			return fn(strings.TrimPrefix(subject, h.prefix+"."), body, hdr)
		})
		if err != nil || got >= want {
			return err
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("stream %s: its copy of STATS is behind (line %d of %d): %w", h.stream, got, want, ctx.Err())
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// Each calls fn with every message the stream holds, oldest stored first,
// through an ephemeral push consumer without acknowledgements that it removes
// again (RT-6.45): $JS.API.CONSUMER.CREATE.<stream> and
// $JS.API.CONSUMER.DELETE.<stream>.<name>, nothing else. It stops at the
// first error of fn.
func (h *History) Each(ctx context.Context, fn func(subject string, body []byte, hdr nats.Header) error) error {
	info, err := h.info(ctx)
	if err != nil || info.State.Msgs == 0 {
		return err
	}
	inbox := h.m.nc.NewInbox()
	sub, err := h.m.nc.SubscribeSync(inbox)
	if err != nil {
		return err
	}
	defer func() { _ = sub.Unsubscribe() }()
	_ = sub.SetPendingLimits(-1, -1)
	req := map[string]any{"stream_name": h.stream, "config": map[string]any{
		"deliver_subject": inbox, "deliver_policy": "all", "ack_policy": "none",
		"filter_subject": h.prefix + ".>", "inactive_threshold": int64(time.Minute),
		"mem_storage": true, "num_replicas": 1,
	}}
	var ci struct {
		Name       string `json:"name"`
		NumPending uint64 `json:"num_pending"`
	}
	if err := h.m.api(ctx, "$JS.API.CONSUMER.CREATE."+h.stream, req, &ci); err != nil {
		return fmt.Errorf("stream %s: reading it: %w", h.stream, err)
	}
	defer func() {
		dctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		_ = h.m.api(dctx, "$JS.API.CONSUMER.DELETE."+h.stream+"."+ci.Name, nil, nil)
	}()
	for left := ci.NumPending; left > 0; {
		mctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		msg, err := sub.NextMsgWithContext(mctx)
		cancel()
		if err != nil {
			return fmt.Errorf("stream %s: reading it: %w", h.stream, err)
		}
		md, err := msg.Metadata()
		if err != nil {
			continue // no stream message (a status)
		}
		if err := fn(msg.Subject, msg.Data, msg.Header); err != nil {
			return err
		}
		left = md.NumPending
	}
	return nil
}

// ErrRefused: the stream refused a line.
var ErrRefused = errors.New("the stream refused the line")

// ErrDuplicate: the stream took the line as a duplicate of one stored within
// its duplicate window (its Nats-Msg-Id) and stored nothing.
var ErrDuplicate = errors.New("the stream already holds the line")

// Put stores one line on <prefix>.<name> with its headers and waits for the
// stream's acknowledgement (RT-6.45; the import stream only, the archive
// does not acknowledge and no one may publish on it).
func (h *History) Put(ctx context.Context, name string, body []byte, hdr map[string]string) error {
	if !historyNameRE.MatchString(name) {
		return fmt.Errorf("%w: %q is no name", ErrRefused, name)
	}
	msg := nats.NewMsg(h.prefix + "." + name)
	msg.Data = body
	for k, v := range hdr {
		msg.Header.Set(k, v)
	}
	resp, err := h.m.nc.RequestMsgWithContext(ctx, msg)
	if err != nil {
		return fmt.Errorf("stream %s: %w", h.stream, err)
	}
	var ack struct {
		Stream    string    `json:"stream"`
		Duplicate bool      `json:"duplicate"`
		Error     *apiError `json:"error"`
	}
	if err := json.Unmarshal(resp.Data, &ack); err != nil {
		return fmt.Errorf("stream %s: no acknowledgement: %w", h.stream, err)
	}
	switch {
	case ack.Error != nil:
		return fmt.Errorf("%w: %w", ErrRefused, ack.Error)
	case ack.Stream != h.stream:
		return fmt.Errorf("%w: acknowledged by %q", ErrRefused, ack.Stream)
	case ack.Duplicate:
		return ErrDuplicate
	}
	return nil
}
