// Package consumers is the login service's consumer management (ADR-014
// point 16, RT-6.35): the streams with their safe settings, and the push
// consumers through which alone an agent reads, made, kept and removed in
// step with its credential.
package consumers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
)

// Metadata keys of the consumers: agent and owner on all of them; on the
// presence consumer also the channels of the credential (comma separated)
// and the server's time of the last login.
const (
	metaAgent    = "agent"
	metaOwner    = "owner"
	metaChannels = "channels"
	metaLogin    = "login"
)

// ExpiryTolerance is how much older than MaxHistory the stream's first
// message may be before a history start falls back to a start by time.
const ExpiryTolerance = time.Minute

// DeliverPrefix starts the subjects push consumers deliver to.
const DeliverPrefix = "deliver"

// Defaults of Config.
const (
	DefaultInactiveThreshold = time.Hour
	DefaultBrakeInterval     = 10 * time.Second
	byeTimeout               = 10 * time.Second
	sweepTimeout             = 30 * time.Second
	maxIgnoredLogged         = 16
	// maxScanned bounds the fallback that looks for an agent's consumers
	// by listing the names of a stream.
	maxScanned = 10000
)

// Config of a Manager.
type Config struct {
	Bus      string // subject prefix of channels and direct messages
	Presence string // subject prefix of presence and crown claims
	// Sizes of the streams in bytes; 0 takes the defaults.
	BusMaxBytes, PresenceMaxBytes int64
	// InactiveThreshold: JetStream removes a push consumer nobody listens
	// to after this long; 0 takes an hour.
	InactiveThreshold time.Duration
	// BrakeInterval is how often WatchBrake looks; 0 takes 10 s.
	BrakeInterval time.Duration
	Log           *slog.Logger
	Now           func() time.Time // the clock; nil: time.Now
	// BeforeBye is a hook for tests: WatchByes calls it with the agent after
	// it read a bye and before it removes anything.
	BeforeBye func(agent string)
	// BeforeRemove is a hook for tests: Reconcile calls it before it removes
	// a consumer that is no longer wanted; an error ends the reconcile there.
	BeforeRemove func(consumer string) error
	// BeforeRecord is a hook for tests: Reconcile calls it before it records
	// only the new channels; an error ends the reconcile there.
	BeforeRecord func(agent string) error
}

// SinceDM is the key of Reconcile's since map for the direct-message
// consumer. It cannot collide with a channel name (channelRE).
const SinceDM = "@dm"

var (
	agentRE   = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`)
	channelRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)
	prefixRE  = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
)

// Manager makes and removes the agents' consumers.
type Manager struct {
	nc    *nats.Conn
	js    nats.JetStreamContext
	cfg   Config
	log   *slog.Logger
	lmu   sync.Mutex
	locks map[string]*agentLock // only agents somebody holds or waits for
}

type agentLock struct {
	mu   sync.Mutex
	refs int
}

// New returns a Manager on nc, a connection in the tenant account with
// JetStream API rights.
func New(nc *nats.Conn, cfg Config) (*Manager, error) {
	for what, v := range map[string]string{"bus prefix": cfg.Bus, "presence prefix": cfg.Presence} {
		if !prefixRE.MatchString(v) || v == DeliverPrefix {
			return nil, fmt.Errorf("consumers: %s %q: letters, digits, - and _, not %q", what, v, DeliverPrefix)
		}
	}
	if cfg.Bus == cfg.Presence {
		return nil, errors.New("consumers: bus and presence prefix must differ")
	}
	if cfg.InactiveThreshold <= 0 {
		cfg.InactiveThreshold = DefaultInactiveThreshold
	}
	if cfg.BrakeInterval <= 0 {
		cfg.BrakeInterval = DefaultBrakeInterval
	}
	if cfg.Now == nil {
		cfg.Now = time.Now
	}
	log := cfg.Log
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	js, err := nc.JetStream()
	if err != nil {
		return nil, fmt.Errorf("consumers: %w", err)
	}
	return &Manager{nc: nc, js: js, cfg: cfg, log: log, locks: map[string]*agentLock{}}, nil
}

// apiError is an error answer of the JetStream API.
type apiError struct {
	Code    int    `json:"code"`
	ErrCode int    `json:"err_code"`
	Desc    string `json:"description"`
}

func (e *apiError) Error() string {
	return fmt.Sprintf("jetstream %d/%d: %s", e.Code, e.ErrCode, e.Desc)
}

const (
	errStreamNameInUse  = 10058
	errStreamNotFound   = 10059
	errConsumerNotFound = 10014
)

// api calls a JetStream API subject with a JSON request and decodes the
// answer into out.
func (m *Manager) api(ctx context.Context, subject string, req, out any) error {
	var data []byte
	switch r := req.(type) {
	case nil:
	case string:
		data = []byte(r)
	default:
		b, err := json.Marshal(r)
		if err != nil {
			return err
		}
		data = b
	}
	msg, err := m.nc.RequestWithContext(ctx, subject, data)
	if err != nil {
		return err
	}
	var e struct {
		Error *apiError `json:"error"`
	}
	if err := json.Unmarshal(msg.Data, &e); err != nil {
		return err
	}
	if e.Error != nil {
		return e.Error
	}
	if out != nil {
		return json.Unmarshal(msg.Data, out)
	}
	return nil
}

func isAPI(err error, code int) bool {
	var e *apiError
	return errors.As(err, &e) && e.ErrCode == code
}

// EnsureStreams creates the streams BUS and PRESENCE when they are missing.
// A stream that exists is read back and refused with ErrUnsafeStream when a
// safety setting differs.
func (m *Manager) EnsureStreams(ctx context.Context) error {
	for _, s := range []struct{ name, cfg, subject string }{
		{BusStream, BusStreamJSON(m.cfg.Bus, m.cfg.BusMaxBytes), m.cfg.Bus + ".>"},
		{PresenceStream, PresenceStreamJSON(m.cfg.Presence, m.cfg.PresenceMaxBytes), m.cfg.Presence + ".>"},
	} {
		if err := m.ensure(ctx, s.name, s.cfg, s.subject); err != nil {
			return err
		}
	}
	return nil
}

func (m *Manager) ensure(ctx context.Context, name, cfg, subject string) error {
	for range 3 {
		var info struct {
			Config map[string]any `json:"config"`
		}
		err := m.api(ctx, "$JS.API.STREAM.INFO."+name, nil, &info)
		switch {
		case err == nil:
			return checkStream(name, subject, info.Config)
		case !isAPI(err, errStreamNotFound):
			return fmt.Errorf("stream %s: %w", name, err)
		}
		err = m.api(ctx, "$JS.API.STREAM.CREATE."+name, cfg, nil)
		if err != nil && !isAPI(err, errStreamNameInUse) {
			return fmt.Errorf("stream %s: %w", name, err)
		}
		if err == nil {
			m.log.Info("consumers", "event", "stream created", "stream", name)
		}
		// Read back, whatever the server kept is what gets checked.
	}
	return fmt.Errorf("stream %s: neither found nor created", name)
}

// spec is one consumer an agent should have.
type spec struct {
	stream, name, deliver string
	channel               string // "": not a channel consumer
	dm                    bool   // the direct-message consumer (since key SinceDM)
	presence              bool
	filters               []string
}

func (m *Manager) desired(agent string, channels []string) []spec {
	var out []spec
	filters := []string{m.cfg.Presence + ".*"}
	for _, ch := range channels {
		out = append(out, spec{stream: BusStream, name: agent + "_ch_" + ch, channel: ch,
			deliver: DeliverPrefix + "." + agent + ".ch." + ch, filters: []string{m.cfg.Bus + ".ch." + ch + ".*"}})
		filters = append(filters, m.cfg.Presence+".crown."+ch+".*")
	}
	out = append(out,
		spec{stream: BusStream, name: agent + "_dm", dm: true, deliver: DeliverPrefix + "." + agent + ".dm",
			filters: []string{m.cfg.Bus + ".dm." + agent + ".*"}},
		spec{stream: PresenceStream, name: agent + "_presence", presence: true,
			deliver: DeliverPrefix + "." + agent + ".presence", filters: filters})
	return out
}

func (m *Manager) config(w spec, agent, owner string, channels []string) *nats.ConsumerConfig {
	c := &nats.ConsumerConfig{
		Durable: w.name, DeliverSubject: w.deliver, AckPolicy: nats.AckNonePolicy,
		DeliverPolicy: nats.DeliverNewPolicy, InactiveThreshold: m.cfg.InactiveThreshold,
		Metadata: map[string]string{metaAgent: agent, metaOwner: owner},
	}
	if w.presence {
		c.Metadata[metaChannels] = strings.Join(channels, ",")
	}
	if len(w.filters) == 1 {
		c.FilterSubject = w.filters[0]
	} else {
		c.FilterSubjects = w.filters
	}
	if w.presence {
		c.DeliverPolicy = nats.DeliverLastPerSubjectPolicy
	}
	return c
}

func filtersOf(c nats.ConsumerConfig) []string {
	set := map[string]bool{}
	for _, f := range c.FilterSubjects {
		set[f] = true
	}
	if c.FilterSubject != "" {
		set[c.FilterSubject] = true
	}
	f := make([]string, 0, len(set))
	for k := range set {
		f = append(f, k)
	}
	sort.Strings(f)
	return f
}

// same says whether an existing consumer has the configuration wanted (its
// start position aside, which only matters for the presence consumer).
func same(have, want nats.ConsumerConfig) bool {
	h, w := filtersOf(have), filtersOf(want)
	if len(h) != len(w) {
		return false
	}
	for i := range h {
		if h[i] != w[i] {
			return false
		}
	}
	return have.DeliverSubject == want.DeliverSubject && have.AckPolicy == nats.AckNonePolicy &&
		!have.FlowControl && have.Heartbeat == 0 && have.DeliverGroup == "" &&
		have.InactiveThreshold == want.InactiveThreshold &&
		have.Metadata[metaAgent] == want.Metadata[metaAgent] && have.Metadata[metaOwner] == want.Metadata[metaOwner] &&
		have.Metadata[metaChannels] == want.Metadata[metaChannels] &&
		(want.DeliverPolicy != nats.DeliverLastPerSubjectPolicy || have.DeliverPolicy == want.DeliverPolicy)
}

// lock takes the agent's lock and returns the unlock. The entry of an agent
// nobody holds or waits for is dropped, so the map does not grow.
func (m *Manager) lock(agent string) func() {
	m.lmu.Lock()
	e := m.locks[agent]
	if e == nil {
		e = &agentLock{}
		m.locks[agent] = e
	}
	e.refs++
	m.lmu.Unlock()
	e.mu.Lock()
	return func() {
		e.mu.Unlock()
		m.lmu.Lock()
		if e.refs--; e.refs == 0 {
			delete(m.locks, agent)
		}
		m.lmu.Unlock()
	}
}

// Reconcile makes the push consumers of an agent so that they match its
// credential: one per channel, one for direct messages, one for presence.
// A consumer with the right configuration is kept with its position; one of
// a channel no longer in channels is deleted; the presence consumer is
// updated when its filters differ. since maps a channel to the stream
// sequence to start from (the agent's last seen); it counts only for a channel
// in channels (or the key SinceDM, for the direct-message consumer), and never reaches back further than MaxHistory. It returns
// when everything is done or ctx ends.
func (m *Manager) Reconcile(ctx context.Context, agent, owner string, channels []string, since map[string]uint64) error {
	if !agentRE.MatchString(agent) {
		return fmt.Errorf("consumers: invalid agent name")
	}
	seen := map[string]bool{}
	var chans []string
	for _, ch := range channels {
		if !channelRE.MatchString(ch) {
			return fmt.Errorf("consumers: invalid channel name %.64q", ch)
		}
		if !seen[ch] {
			seen[ch] = true
			chans = append(chans, ch)
		}
	}
	sort.Strings(chans)
	starts := map[string]uint64{}
	ignored := 0
	for ch, seq := range since {
		switch {
		case seq == 0:
		case ch == SinceDM || seen[ch]:
			starts[ch] = seq
		default:
			if ignored++; ignored <= maxIgnoredLogged {
				m.log.Info("consumers", "event", "since ignored", "agent", agent, "channel", fmt.Sprintf("%.64q", ch),
					"reason", "not a channel of the credential")
			}
		}
	}

	defer m.lock(agent)()
	want := m.desired(agent, chans)
	wanted := map[string]bool{}
	for _, w := range want {
		wanted[w.stream+"/"+w.name] = true
	}
	// What the agent had before: the presence consumer says which channels.
	pres, err := m.info(ctx, PresenceStream, agent+"_presence")
	if err != nil {
		return err
	}
	if pres != nil && pres.Config.Metadata[metaAgent] != agent {
		return fmt.Errorf("consumers: %s belongs to another agent", pres.Name)
	}
	stamp := ""
	if pres != nil && !pres.Ts.IsZero() {
		stamp = pres.Ts.UTC().Format(time.RFC3339Nano) // the server's time of this login
	}
	prev := m.previous(ctx, agent, pres)
	// First record the old and the new channels together, then make and
	// remove, then record only the new ones: a step that fails in between
	// leaves every channel consumer that may exist in the record, so a later
	// reconcile or bye finds it.
	both := append([]string(nil), chans...)
	inBoth := map[string]bool{}
	for _, ch := range chans {
		inBoth[ch] = true
	}
	for _, c := range prev {
		if ch, ok := strings.CutPrefix(c.name, agent+"_ch_"); ok && c.stream == BusStream && !inBoth[ch] {
			inBoth[ch] = true
			both = append(both, ch)
		}
	}
	sort.Strings(both)
	presence := want[len(want)-1]
	if err := m.ensureConsumer(ctx, presence, agent, owner, both, starts, stamp); err != nil {
		return err
	}
	var jobs []func() error
	for _, c := range prev {
		if wanted[c.stream+"/"+c.name] {
			continue
		}
		jobs = append(jobs, func() error {
			if m.cfg.BeforeRemove != nil {
				if err := m.cfg.BeforeRemove(c.name); err != nil {
					return err
				}
			}
			if ok, err := m.deleteOwned(ctx, c.stream, c.name, agent, time.Time{}); err != nil || !ok {
				return err
			}
			m.log.Info("consumers", "event", "removed", "agent", agent, "consumer", c.name, "reason", "no longer in the credential")
			return nil
		})
	}
	// The consumers are independent, and each call waits for the server to
	// write to disk: do them at once, a login has little time.
	if err := parallel(jobs); err != nil {
		return err
	}
	jobs = nil
	for _, w := range want[:len(want)-1] {
		jobs = append(jobs, func() error {
			return m.ensureConsumer(ctx, w, agent, owner, chans, starts, stamp)
		})
	}
	if err := parallel(jobs); err != nil {
		return err
	}
	if m.cfg.BeforeRecord != nil {
		if err := m.cfg.BeforeRecord(agent); err != nil {
			return err
		}
	}
	return m.ensureConsumer(ctx, presence, agent, owner, chans, starts, stamp)
}

type named struct{ stream, name string }

// previous returns the consumers the agent may have from before: the
// channel consumers its presence consumer records, or, when that is missing
// or records nothing, the ones found by a bounded scan of the names. Callers
// check each one's metadata.
func (m *Manager) previous(ctx context.Context, agent string, pres *consumerInfo) []named {
	if pres != nil {
		if chs, ok := pres.Config.Metadata[metaChannels]; ok {
			var out []named
			for _, ch := range splitChannels(chs) {
				out = append(out, named{BusStream, agent + "_ch_" + ch})
			}
			return append(out, named{BusStream, agent + "_dm"}, named{PresenceStream, agent + "_presence"})
		}
	}
	return m.scan(ctx, agent)
}

func splitChannels(s string) []string {
	var out []string
	for _, ch := range strings.Split(s, ",") {
		if channelRE.MatchString(ch) {
			out = append(out, ch)
		}
	}
	return out
}

// scan is the fallback: the consumers of both streams whose name starts with
// the agent's (candidates only: they are checked by their metadata), looking
// at no more than maxScanned names of a stream.
func (m *Manager) scan(ctx context.Context, agent string) []named {
	var out []named
	for _, st := range []string{BusStream, PresenceStream} {
		names, truncated, err := m.names(ctx, st, maxScanned)
		if err != nil {
			m.log.Warn("consumers", "event", "scan failed", "stream", st, "reason", err.Error())
			continue
		}
		if truncated {
			m.log.Warn("consumers", "event", "scan stopped", "stream", st, "limit", maxScanned)
		}
		for _, n := range names {
			if strings.HasPrefix(n, agent+"_") {
				out = append(out, named{st, n})
			}
		}
	}
	return out
}

// deleteOwned deletes the consumer when its metadata says it is the agent's
// and, with a non-zero before, when it was made before then. It says whether
// it deleted.
func (m *Manager) deleteOwned(ctx context.Context, stream, name, agent string, before time.Time) (bool, error) {
	ci, err := m.info(ctx, stream, name)
	if err != nil || ci == nil {
		return false, err
	}
	if ci.Config.Metadata[metaAgent] != agent || (!before.IsZero() && !ci.Created.Before(before)) {
		return false, nil
	}
	return true, m.delete(ctx, stream, name)
}

// consumerInfo is a consumer with the server's time of the answer.
type consumerInfo struct {
	nats.ConsumerInfo
	Ts time.Time `json:"ts"`
}

// info reads one consumer; nil when there is none.
func (m *Manager) info(ctx context.Context, stream, name string) (*consumerInfo, error) {
	var ci consumerInfo
	err := m.api(ctx, "$JS.API.CONSUMER.INFO."+stream+"."+name, nil, &ci)
	switch {
	case isAPI(err, errConsumerNotFound):
		return nil, nil
	case err != nil:
		return nil, fmt.Errorf("consumer %s: %w", name, err)
	}
	return &ci, nil
}

func (m *Manager) ensureConsumer(ctx context.Context, w spec, agent, owner string, chans []string, starts map[string]uint64, stamp string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	cfg := m.config(w, agent, owner, chans)
	key := w.channel
	if w.dm {
		key = SinceDM
	}
	seq, restart := starts[key]
	restart = restart && key != ""
	ci, err := m.info(ctx, w.stream, w.name)
	if err != nil {
		return err
	}
	if ci != nil {
		if ci.Config.Metadata[metaAgent] != agent {
			return fmt.Errorf("consumer %s belongs to another agent", w.name)
		}
		if w.presence && !restart {
			// Kept or updated, with the time of this login: a bye the server
			// stamped before it must not remove the consumer.
			if stamp != "" {
				cfg.Metadata[metaLogin] = stamp
			}
			if _, err := m.js.UpdateConsumer(w.stream, cfg, nats.Context(ctx)); err == nil {
				if !same(ci.Config, *cfg) {
					m.log.Info("consumers", "event", "updated", "agent", agent, "consumer", w.name)
				}
				return nil
			}
		} else if !restart && same(ci.Config, *cfg) {
			return nil
		}
		if err := m.delete(ctx, w.stream, w.name); err != nil {
			return err
		}
	}
	if restart {
		if err := m.start(ctx, cfg, seq); err != nil {
			return err
		}
	}
	if _, err := m.js.AddConsumer(w.stream, cfg, nats.Context(ctx)); err != nil {
		return fmt.Errorf("consumer %s: %w", w.name, err)
	}
	return nil
}

// parallel runs the jobs at once and returns the first error.
func parallel(jobs []func() error) error {
	errs := make([]error, len(jobs))
	var wg sync.WaitGroup
	for i, j := range jobs {
		wg.Add(1)
		go func() { defer wg.Done(); errs[i] = j() }()
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			return err
		}
	}
	return nil
}

// start makes cfg start after the stream sequence last (the last one the
// agent has seen), at last+1, but not further back than MaxHistory. Only the
// stream's info is read, not a message (the service's JetStream user has no
// right on MSG.GET): when the stream's first message is younger than now
// minus MaxHistory, every message is, and the start is by sequence; when it
// is older (BUS keeps at most MaxHistory, checked at start, so only while
// the server has not yet expired it), the start is by time at now minus
// MaxHistory, which may replay a little more than asked but never older.
func (m *Manager) start(ctx context.Context, cfg *nats.ConsumerConfig, last uint64) error {
	si, err := m.js.StreamInfo(BusStream, nats.Context(ctx))
	if err != nil {
		return fmt.Errorf("stream %s: %w", BusStream, err)
	}
	if last >= si.State.LastSeq {
		return nil // nothing after it yet (or a sequence past the last): from the next message on
	}
	seq := last + 1
	if seq < si.State.FirstSeq {
		seq = si.State.FirstSeq
	}
	cfg.DeliverPolicy, cfg.OptStartSeq = nats.DeliverByStartSequencePolicy, seq
	// The server expires old messages only every few hundred ms, and the two
	// clocks may differ a little: only a first message clearly older than the
	// limit makes the start by time (which replays more than asked).
	if cutoff := m.cfg.Now().Add(-MaxHistory); si.State.FirstTime.Before(cutoff.Add(-ExpiryTolerance)) {
		cfg.DeliverPolicy, cfg.OptStartSeq, cfg.OptStartTime = nats.DeliverByStartTimePolicy, 0, &cutoff
	}
	return nil
}

// names lists the consumer names of a stream, at most limit of them;
// truncated says there were more.
func (m *Manager) names(ctx context.Context, stream string, limit int) (all []string, truncated bool, err error) {
	for {
		var r struct {
			Total     int      `json:"total"`
			Consumers []string `json:"consumers"`
		}
		if err := m.api(ctx, "$JS.API.CONSUMER.NAMES."+stream, map[string]int{"offset": len(all)}, &r); err != nil {
			return nil, false, fmt.Errorf("consumers of %s: %w", stream, err)
		}
		all = append(all, r.Consumers...)
		if len(all) >= limit && len(all) < r.Total {
			return all, true, nil
		}
		if len(r.Consumers) == 0 || len(all) >= r.Total {
			return all, false, nil
		}
	}
}

// list returns the consumers of a stream with their configuration.
func (m *Manager) list(ctx context.Context, stream string) ([]*nats.ConsumerInfo, error) {
	var all []*nats.ConsumerInfo
	for {
		var r struct {
			Total     int                  `json:"total"`
			Consumers []*nats.ConsumerInfo `json:"consumers"`
		}
		if err := m.api(ctx, "$JS.API.CONSUMER.LIST."+stream, map[string]int{"offset": len(all)}, &r); err != nil {
			return nil, fmt.Errorf("consumers of %s: %w", stream, err)
		}
		all = append(all, r.Consumers...)
		if len(r.Consumers) == 0 || len(all) >= r.Total {
			return all, nil
		}
	}
}

func (m *Manager) delete(ctx context.Context, stream, name string) error {
	if err := m.js.DeleteConsumer(stream, name, nats.Context(ctx)); err != nil && !errors.Is(err, nats.ErrConsumerNotFound) {
		return fmt.Errorf("delete consumer %s: %w", name, err)
	}
	return nil
}

// Remove deletes all consumers of an agent (sign-off).
func (m *Manager) Remove(ctx context.Context, agent string) error {
	return m.RemoveBefore(ctx, agent, time.Time{})
}

// RemoveBefore deletes the consumers of an agent made before the given time
// (the server's time of its bye; zero: all of them). A bye handled late, after
// the same agent logged in again, so leaves the new login's consumers alone:
// the presence consumer carries the server's time of the last login, and a
// login at or after the bye ends it. The consumers are found by their names,
// from the channels the presence consumer records, and by their metadata, not
// by a prefix.
func (m *Manager) RemoveBefore(ctx context.Context, agent string, before time.Time) error {
	if !agentRE.MatchString(agent) {
		return fmt.Errorf("consumers: invalid agent name")
	}
	defer m.lock(agent)()
	pres, err := m.info(ctx, PresenceStream, agent+"_presence")
	if err != nil {
		return err
	}
	if pres != nil && !before.IsZero() && pres.Config.Metadata[metaAgent] == agent {
		if t, err := time.Parse(time.RFC3339Nano, pres.Config.Metadata[metaLogin]); err == nil && !t.Before(before) {
			m.log.Info("consumers", "event", "bye ignored", "agent", agent, "reason", "logged in again since")
			return nil
		}
	}
	n := 0
	for _, c := range m.previous(ctx, agent, pres) {
		ok, err := m.deleteOwned(ctx, c.stream, c.name, agent, before)
		if err != nil {
			return err
		}
		if ok {
			n++
		}
	}
	m.log.Info("consumers", "event", "removed", "agent", agent, "count", n, "reason", "sign-off")
	return nil
}

// RemoveOwner deletes the consumers of every agent of an owner.
func (m *Manager) RemoveOwner(ctx context.Context, owner string) error {
	return m.sweep(ctx, func(o string) bool { return strings.EqualFold(o, owner) }, "owner removed")
}

// BrakeCheck is the emergency brake (callout.Brake): Check returns an error
// when the brake holds owner.
type BrakeCheck interface{ Check(owner string) error }

// SweepBrake removes the consumers of every owner the brake holds, or of
// all when it holds everyone.
func (m *Manager) SweepBrake(ctx context.Context, b BrakeCheck) error {
	return m.sweep(ctx, func(o string) bool { return b.Check(o) != nil }, "emergency brake")
}

// sweep deletes the consumers this service made (they carry the agent and
// owner in their metadata) whose owner is chosen.
func (m *Manager) sweep(ctx context.Context, choose func(owner string) bool, reason string) error {
	var firstErr error
	for _, st := range []string{BusStream, PresenceStream} {
		cis, err := m.list(ctx, st)
		if err != nil {
			return err
		}
		for _, ci := range cis {
			agent, owner := ci.Config.Metadata["agent"], ci.Config.Metadata["owner"]
			if agent == "" || !choose(owner) {
				continue
			}
			if err := m.delete(ctx, st, ci.Name); err != nil {
				if firstErr == nil {
					firstErr = err
				}
				continue
			}
			m.log.Info("consumers", "event", "removed", "agent", agent, "owner", owner, "consumer", ci.Name, "reason", reason)
		}
	}
	return firstErr
}

// WatchBrake looks at the brake every BrakeInterval until ctx ends and
// removes the consumers of the owners it holds. It returns at once.
func (m *Manager) WatchBrake(ctx context.Context, b BrakeCheck) {
	go func() {
		t := time.NewTicker(m.cfg.BrakeInterval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				cctx, cancel := context.WithTimeout(ctx, sweepTimeout)
				if err := m.SweepBrake(cctx, b); err != nil && ctx.Err() == nil {
					m.log.Warn("consumers", "event", "brake sweep failed", "reason", err.Error())
				}
				cancel()
			}
		}
	}()
}

// WatchByes subscribes, with an ordered consumer of the service's own, to
// the presence subjects and removes an agent's consumers when it posts
// {"status":"bye"} on its own presence subject. The sender is the last token
// of the subject only, which NATS has checked against the credential. It
// returns once subscribed; the subscription ends with ctx.
func (m *Manager) WatchByes(ctx context.Context) error {
	sub, err := m.js.Subscribe(m.cfg.Presence+".*", func(msg *nats.Msg) { m.bye(ctx, msg) },
		nats.BindStream(PresenceStream), nats.OrderedConsumer(), nats.DeliverNew())
	if err != nil {
		return fmt.Errorf("consumers: watching byes: %w", err)
	}
	go func() { <-ctx.Done(); _ = sub.Unsubscribe() }()
	return nil
}

// bye handles one presence message: a bye removes the agent's consumers made
// before the server's time of it; without that time it removes nothing.
func (m *Manager) bye(ctx context.Context, msg *nats.Msg) {
	agent := msg.Subject[strings.LastIndexByte(msg.Subject, '.')+1:]
	var body struct {
		Status string `json:"status"`
	}
	if json.Unmarshal(msg.Data, &body) != nil || body.Status != "bye" || !agentRE.MatchString(agent) {
		return
	}
	if m.cfg.BeforeBye != nil {
		m.cfg.BeforeBye(agent)
	}
	md, err := msg.Metadata()
	if err != nil {
		m.log.Warn("consumers", "event", "bye ignored", "agent", agent, "reason", "no JetStream metadata")
		return
	}
	at := md.Timestamp // the server's time of the bye
	cctx, cancel := context.WithTimeout(ctx, byeTimeout)
	defer cancel()
	if err := m.RemoveBefore(cctx, agent, at); err != nil && ctx.Err() == nil {
		m.log.Warn("consumers", "event", "bye not handled", "agent", agent, "reason", err.Error())
	}
}
