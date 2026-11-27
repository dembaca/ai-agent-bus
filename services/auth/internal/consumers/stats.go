package consumers

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

// The STATS history (RT-6.45): the STATS stream itself keeps a year of lines
// (StatsMaxAge), and the stats channel's older lines are imported into it.
// The service brings an existing stream's limits to the current ones at its
// start (Update), watches its fill, and the import (admin/statsimport.go)
// reads it and publishes on <stats>.import-<bot> with the service's own
// JetStream user: no agent may publish there.

// ImportToken starts the subject token of an imported line:
// <stats>.import-<bot>. No agent's name can start with it (the callout
// refuses a name prefix that does), so no agent may publish an imported line
// and no imported line lands on an agent's subject. Readers (AGENT_RE in
// natsbus.py) take it as one more name.
const ImportToken = "import-"

// botRE: a Mattermost bot's username as it follows ImportToken.
var botRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// Stats is the STATS stream as the history uses it.
type Stats struct {
	m *Manager

	mu    sync.Mutex
	above bool // the last fill check was at or over StatsWarnPercent
}

// StatsWarnPercent is the fill the service warns about.
const StatsWarnPercent = 80

// Stats returns the STATS stream's history side.
func (m *Manager) Stats() *Stats { return &Stats{m: m} }

func (s *Stats) maxBytes() int64 {
	if s.m.cfg.StatsMaxBytes > 0 {
		return s.m.cfg.StatsMaxBytes
	}
	return DefaultStatsMaxBytes
}

// statsLimits are the limits Update brings an existing STATS stream to: its
// age, its size and its cap per subject; nothing else of it is changed.
func (s *Stats) limits() map[string]int64 {
	return map[string]int64{"max_age": int64(StatsMaxAge), "max_bytes": s.maxBytes(),
		"max_msgs_per_subject": StatsMaxMsgsPerSubject}
}

// Update brings the limits of the existing STATS stream to the current ones
// (RT-6.45): an older login service made it with 30 days. Only max_age,
// max_msgs_per_subject and a max_bytes below the service's are changed, in
// place (a larger max_bytes, an operator's room for more, is kept: shrinking
// would drop history); a stream that
// fails checkStream is refused before and after, never updated. Idempotent:
// a stream with the current limits is left as it is. Run it after
// EnsureStreams.
func (s *Stats) Update(ctx context.Context) error {
	var info struct {
		Config map[string]any `json:"config"`
	}
	if err := s.m.api(ctx, "$JS.API.STREAM.INFO."+StatsStream, nil, &info); err != nil {
		return streamErr(StatsStream, err)
	}
	subject := s.m.cfg.Stats + ".>"
	if err := checkStream(StatsStream, subject, info.Config); err != nil {
		return err
	}
	want := s.limits()
	var old []any
	for k, v := range want {
		if have, _ := info.Config[k].(float64); have != float64(v) && !(k == "max_bytes" && have > float64(v)) {
			old = append(old, k, info.Config[k])
			info.Config[k] = v
		}
	}
	if len(old) == 0 {
		return nil
	}
	var out struct {
		Config map[string]any `json:"config"`
	}
	if err := s.m.api(ctx, "$JS.API.STREAM.UPDATE."+StatsStream, info.Config, &out); err != nil {
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, nats.ErrTimeout) || errors.Is(err, nats.ErrNoResponders) {
			return fmt.Errorf("stream %s: no answer from JetStream (%w): the login service's JetStream user needs publish on $JS.API.STREAM.UPDATE.%s",
				StatsStream, err, StatsStream)
		}
		return fmt.Errorf("stream %s: update: %w", StatsStream, err)
	}
	if err := checkStream(StatsStream, subject, out.Config); err != nil {
		return err
	}
	for k, v := range want {
		if have, _ := out.Config[k].(float64); have != float64(v) && !(k == "max_bytes" && have > float64(v)) {
			return fmt.Errorf("stream %s: %s is %v after the update, want %d", StatsStream, k, out.Config[k], v)
		}
	}
	s.m.log.Info("consumers: stream limits updated", "stream", StatsStream, "was", fmt.Sprint(old...))
	return nil
}

// statsInfo is the part of STREAM.INFO the fill reads.
type statsInfo struct {
	Config struct {
		MaxBytes int64 `json:"max_bytes"`
	} `json:"config"`
	State struct {
		Msgs  uint64 `json:"messages"`
		Bytes uint64 `json:"bytes"`
	} `json:"state"`
}

func (s *Stats) info(ctx context.Context) (statsInfo, error) {
	var info statsInfo
	if err := s.m.api(ctx, "$JS.API.STREAM.INFO."+StatsStream, nil, &info); err != nil {
		return info, streamErr(StatsStream, err)
	}
	return info, nil
}

// Fill is how full STATS is, in percent of its max_bytes.
func (s *Stats) Fill(ctx context.Context) (int, error) {
	info, err := s.info(ctx)
	if err != nil {
		return 0, err
	}
	if info.Config.MaxBytes <= 0 {
		return 100, nil
	}
	return int(info.State.Bytes * 100 / uint64(info.Config.MaxBytes)), nil
}

// CheckFill logs a warning once each time the fill of STATS crosses
// StatsWarnPercent upwards, and a note when it is below again (RT-6.45): a
// full stream drops its oldest lines, the imported history first.
func (s *Stats) CheckFill(ctx context.Context) {
	pct, err := s.Fill(ctx)
	if err != nil {
		s.m.log.Warn("stats stream: fill unknown", "reason", err.Error())
		return
	}
	s.mu.Lock()
	was := s.above
	s.above = pct >= StatsWarnPercent
	now := s.above
	s.mu.Unlock()
	switch {
	case now && !was:
		s.m.log.Warn(fmt.Sprintf("stats stream over %d %% full", StatsWarnPercent), "stream", StatsStream, "percent", pct,
			"remedy", "a larger max_bytes, set on the stream by hand: the service keeps a larger one")
	case !now && was:
		s.m.log.Info(fmt.Sprintf("stats stream below %d %% full again", StatsWarnPercent), "stream", StatsStream, "percent", pct)
	}
}

// WatchFill runs CheckFill now and then every interval until ctx ends.
func (s *Stats) WatchFill(ctx context.Context, every time.Duration) {
	go func() {
		for {
			cctx, cancel := context.WithTimeout(ctx, 10*time.Second)
			s.CheckFill(cctx)
			cancel()
			select {
			case <-ctx.Done():
				return
			case <-time.After(every):
			}
		}
	}()
}

// ReadAll calls fn with the name (the subject's last token), the body and the
// headers of every line STATS holds, oldest stored first, through an
// ephemeral push consumer without acknowledgements that it removes again
// ($JS.API.CONSUMER.CREATE.STATS and $JS.API.CONSUMER.DELETE.STATS.<name>,
// rights the operators' consumers need anyway). begin runs before the pass.
// It stops at the first error of fn.
func (s *Stats) ReadAll(ctx context.Context, begin func(), fn func(name string, body []byte, hdr nats.Header) error) error {
	begin()
	info, err := s.info(ctx)
	if err != nil || info.State.Msgs == 0 {
		return err
	}
	prefix := s.m.cfg.Stats + "."
	inbox := s.m.nc.NewInbox()
	sub, err := s.m.nc.SubscribeSync(inbox)
	if err != nil {
		return err
	}
	defer func() { _ = sub.Unsubscribe() }()
	_ = sub.SetPendingLimits(-1, -1)
	req := map[string]any{"stream_name": StatsStream, "config": map[string]any{
		"deliver_subject": inbox, "deliver_policy": "all", "ack_policy": "none",
		"filter_subject": prefix + ">", "inactive_threshold": int64(time.Minute),
		"mem_storage": true, "num_replicas": 1,
	}}
	var ci struct {
		Name       string `json:"name"`
		NumPending uint64 `json:"num_pending"`
	}
	if err := s.m.api(ctx, "$JS.API.CONSUMER.CREATE."+StatsStream, req, &ci); err != nil {
		return fmt.Errorf("stream %s: reading it: %w", StatsStream, err)
	}
	defer func() {
		dctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		_ = s.m.api(dctx, "$JS.API.CONSUMER.DELETE."+StatsStream+"."+ci.Name, nil, nil)
	}()
	for left := ci.NumPending; left > 0; {
		mctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		msg, err := sub.NextMsgWithContext(mctx)
		cancel()
		if err != nil {
			return fmt.Errorf("stream %s: reading it: %w", StatsStream, err)
		}
		md, err := msg.Metadata()
		if err != nil {
			continue // no stream message (a status)
		}
		if err := fn(strings.TrimPrefix(msg.Subject, prefix), msg.Data, msg.Header); err != nil {
			return err
		}
		left = md.NumPending
	}
	return nil
}

// ErrNoBot: the name is no bot's username that may follow ImportToken.
var ErrNoBot = errors.New("not a bot's username")

// Put publishes one imported line on <stats>.import-<bot> with its headers
// (RT-6.45) and waits until the server has it. STATS has no_ack: the stream
// says nothing back, so the import's own check of what STATS holds, at the
// next run, is what finds a line again.
func (s *Stats) Put(ctx context.Context, bot string, body []byte, hdr map[string]string) error {
	if !botRE.MatchString(bot) {
		return fmt.Errorf("%w: %q", ErrNoBot, bot)
	}
	msg := nats.NewMsg(s.m.cfg.Stats + "." + ImportToken + bot)
	msg.Data = body
	for k, v := range hdr {
		msg.Header.Set(k, v)
	}
	if err := s.m.nc.PublishMsg(msg); err != nil {
		return fmt.Errorf("stream %s: %w", StatsStream, err)
	}
	fctx, cancel := context.WithTimeout(ctx, 10*time.Second) // a flush wants a deadline
	defer cancel()
	return s.m.nc.FlushWithContext(fctx)
}
