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

// The STATS history (RT-6.45): the STATS_ARCHIVE stream keeps a year of STATS
// lines on <archive>.<agent>. JetStream itself copies every STATS line into it
// (the stream's one source, with a subject transform); the login service's
// import adds the lines the stats channel held before (admin/statsimport.go).
// Only the login service's JetStream user writes it, and no agent has any
// right on its subjects (callout/perms.go grants <stats>.<agent> only). Its
// own size limit keeps a flood of STATS from pushing out the imported history
// any faster than the per-subject cap allows. The configuration of an
// existing archive is only checked, never updated, as for the other streams.

// Archive is the STATS_ARCHIVE stream as the login service uses it.
type Archive struct {
	m      *Manager
	prefix string

	mu    sync.Mutex
	above bool // the last fill check was at or over ArchiveWarnPercent
}

// Prefix is the archive's subject prefix.
func (a *Archive) Prefix() string { return a.prefix }

// archiveAgentRE: an agent's name as one archive subject token (the import
// takes the bot's username, which must fit).
var archiveAgentRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// EnsureArchive creates the STATS_ARCHIVE stream when it is missing (RT-6.45)
// and checks it when it exists: a stream that differs from the safe
// configuration (checkArchive) is refused with ErrUnsafeStream. prefix ""
// takes DefaultArchivePrefix; maxBytes <= 0 takes DefaultArchiveMaxBytes.
// Run it after EnsureStreams: its source is STATS.
func (m *Manager) EnsureArchive(ctx context.Context, prefix string, maxBytes int64) (*Archive, error) {
	if prefix == "" {
		prefix = DefaultArchivePrefix
	}
	if !prefixRE.MatchString(prefix) || prefix == DeliverPrefix {
		return nil, fmt.Errorf("consumers: archive prefix %q: letters, digits, - and _, not %q", prefix, DeliverPrefix)
	}
	if prefix == m.cfg.Bus || prefix == m.cfg.Presence || prefix == m.cfg.Stats {
		return nil, errors.New("consumers: the archive prefix must differ from the bus, the presence and the stats prefix")
	}
	cfg := ArchiveStreamJSON(prefix, m.cfg.Stats, maxBytes)
	for range 3 {
		var info struct {
			Config map[string]any `json:"config"`
		}
		err := m.api(ctx, "$JS.API.STREAM.INFO."+ArchiveStream, nil, &info)
		switch {
		case err == nil:
			if err := checkArchive(prefix, m.cfg.Stats, info.Config); err != nil {
				return nil, err
			}
			return &Archive{m: m, prefix: prefix}, nil
		case !isAPI(err, errStreamNotFound):
			return nil, streamErr(ArchiveStream, err)
		}
		err = m.api(ctx, "$JS.API.STREAM.CREATE."+ArchiveStream, cfg, nil)
		if err != nil && !isAPI(err, errStreamNameInUse) {
			return nil, streamErr(ArchiveStream, err)
		}
		if err == nil {
			m.log.Info("consumers", "event", "stream created", "stream", ArchiveStream)
		}
		// Read back, whatever the server kept is what gets checked.
	}
	return nil, fmt.Errorf("stream %s: neither found nor created", ArchiveStream)
}

// archiveInfo is the part of STREAM.INFO the archive reads.
type archiveInfo struct {
	Config struct {
		MaxBytes int64 `json:"max_bytes"`
	} `json:"config"`
	State struct {
		Msgs  uint64 `json:"messages"`
		Bytes uint64 `json:"bytes"`
	} `json:"state"`
	Sources []struct {
		Name  string    `json:"name"`
		Lag   uint64    `json:"lag"`
		Error *apiError `json:"error"`
	} `json:"sources"`
}

func (a *Archive) info(ctx context.Context) (archiveInfo, error) {
	var info archiveInfo
	if err := a.m.api(ctx, "$JS.API.STREAM.INFO."+ArchiveStream, nil, &info); err != nil {
		return info, streamErr(ArchiveStream, err)
	}
	return info, nil
}

// Fill is how full the archive is, in percent of its max_bytes (RT-6.45).
func (a *Archive) Fill(ctx context.Context) (int, error) {
	info, err := a.info(ctx)
	if err != nil {
		return 0, err
	}
	if info.Config.MaxBytes <= 0 {
		return 0, fmt.Errorf("stream %s: no max_bytes", ArchiveStream)
	}
	return int(info.State.Bytes * 100 / uint64(info.Config.MaxBytes)), nil
}

// CheckFill logs a warning once each time the archive's fill crosses
// ArchiveWarnPercent upwards, and a note when it is below again (RT-6.45):
// with the oldest discarded, a full archive drops the oldest history, the
// imported lines first.
func (a *Archive) CheckFill(ctx context.Context) {
	pct, err := a.Fill(ctx)
	if err != nil {
		a.m.log.Warn("stats archive: fill unknown", "reason", err.Error())
		return
	}
	a.mu.Lock()
	was := a.above
	a.above = pct >= ArchiveWarnPercent
	now := a.above
	a.mu.Unlock()
	switch {
	case now && !was:
		a.m.log.Warn(fmt.Sprintf("stats archive over %d %% full", ArchiveWarnPercent),
			"stream", ArchiveStream, "percent", pct,
			"remedy", "a larger max_bytes, made by hand (the service only checks an existing stream); the oldest lines go first")
	case !now && was:
		a.m.log.Info(fmt.Sprintf("stats archive below %d %% full again", ArchiveWarnPercent),
			"stream", ArchiveStream, "percent", pct)
	}
}

// WatchFill runs CheckFill now and then every interval until ctx ends.
func (a *Archive) WatchFill(ctx context.Context, every time.Duration) {
	go func() {
		for {
			cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
			a.CheckFill(cctx)
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
func (a *Archive) statsLast(ctx context.Context) (uint64, error) {
	var info struct {
		State struct {
			Msgs    uint64 `json:"messages"`
			LastSeq uint64 `json:"last_seq"`
		} `json:"state"`
	}
	if err := a.m.api(ctx, "$JS.API.STREAM.INFO."+StatsStream, nil, &info); err != nil {
		return 0, streamErr(StatsStream, err)
	}
	if info.State.Msgs == 0 {
		return 0, nil
	}
	return info.State.LastSeq, nil
}

// SourceSeq is the STATS sequence of an archived line JetStream copied from
// STATS, read from its Nats-Stream-Source header ("STATS[:<domain hash>]
// <seq> …"); false for a line that was not copied (an imported one).
func SourceSeq(hdr nats.Header) (uint64, bool) {
	f := strings.Fields(hdr.Get("Nats-Stream-Source"))
	if len(f) < 2 || (f[0] != StatsStream && !strings.HasPrefix(f[0], StatsStream+":")) {
		return 0, false
	}
	seq, err := strconv.ParseUint(f[1], 10, 64)
	return seq, err == nil
}

// ReadAll calls fn with the body of every line the archive holds, once the
// archive holds a copy of every STATS line stored before the call (RT-6.45),
// so the import's duplicate check sees each of them. A pass that finds the
// copy behind is thrown away (begin runs before each pass) and repeated
// until ctx ends. The source's lag in STREAM.INFO is no such proof: it reads
// 0 before the source has delivered anything.
func (a *Archive) ReadAll(ctx context.Context, begin func(), fn func(body []byte) error) error {
	want, err := a.statsLast(ctx)
	if err != nil {
		return err
	}
	for {
		begin()
		var got uint64
		err := a.Each(ctx, func(_ string, body []byte, hdr nats.Header) error {
			if seq, ok := SourceSeq(hdr); ok && seq > got {
				got = seq
			}
			return fn(body)
		})
		if err != nil || got >= want {
			return err
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("stream %s: its copy of STATS is behind (line %d of %d): %w", ArchiveStream, got, want, ctx.Err())
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// Each calls fn with every line the archive holds, oldest stored first,
// through an ephemeral push consumer without acknowledgements that it
// removes again (RT-6.45). It stops at the first error of fn.
func (a *Archive) Each(ctx context.Context, fn func(subject string, body []byte, hdr nats.Header) error) error {
	info, err := a.info(ctx)
	if err != nil || info.State.Msgs == 0 {
		return err
	}
	inbox := a.m.nc.NewInbox()
	sub, err := a.m.nc.SubscribeSync(inbox)
	if err != nil {
		return err
	}
	defer func() { _ = sub.Unsubscribe() }()
	_ = sub.SetPendingLimits(-1, -1)
	req := map[string]any{"stream_name": ArchiveStream, "config": map[string]any{
		"deliver_subject": inbox, "deliver_policy": "all", "ack_policy": "none",
		"filter_subject": a.prefix + ".>", "inactive_threshold": int64(time.Minute),
		"mem_storage": true, "num_replicas": 1,
	}}
	var ci struct {
		Name       string `json:"name"`
		NumPending uint64 `json:"num_pending"`
	}
	if err := a.m.api(ctx, "$JS.API.CONSUMER.CREATE."+ArchiveStream, req, &ci); err != nil {
		return fmt.Errorf("stream %s: reading it: %w", ArchiveStream, err)
	}
	defer func() {
		dctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		_ = a.m.api(dctx, "$JS.API.CONSUMER.DELETE."+ArchiveStream+"."+ci.Name, nil, nil)
	}()
	for left := ci.NumPending; left > 0; {
		mctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		msg, err := sub.NextMsgWithContext(mctx)
		cancel()
		if err != nil {
			return fmt.Errorf("stream %s: reading it: %w", ArchiveStream, err)
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

// ErrArchiveRefused: the archive refused an imported line.
var ErrArchiveRefused = errors.New("the archive refused the line")

// Put stores one imported line on <archive>.<agent> with its headers and waits
// for the archive's acknowledgement (RT-6.45). A line the archive takes as a
// duplicate (its Nats-Msg-Id within the duplicate window) counts as stored.
func (a *Archive) Put(ctx context.Context, agent string, body []byte, hdr map[string]string) error {
	if !archiveAgentRE.MatchString(agent) {
		return fmt.Errorf("%w: %q is no agent name", ErrArchiveRefused, agent)
	}
	msg := nats.NewMsg(a.prefix + "." + agent)
	msg.Data = body
	for k, v := range hdr {
		msg.Header.Set(k, v)
	}
	resp, err := a.m.nc.RequestMsgWithContext(ctx, msg)
	if err != nil {
		return fmt.Errorf("stream %s: %w", ArchiveStream, err)
	}
	var ack struct {
		Stream string    `json:"stream"`
		Error  *apiError `json:"error"`
	}
	if err := json.Unmarshal(resp.Data, &ack); err != nil {
		return fmt.Errorf("stream %s: no acknowledgement: %w", ArchiveStream, err)
	}
	if ack.Error != nil {
		return fmt.Errorf("%w: %w", ErrArchiveRefused, ack.Error)
	}
	if ack.Stream != ArchiveStream {
		return fmt.Errorf("%w: acknowledged by %q", ErrArchiveRefused, ack.Stream)
	}
	return nil
}
