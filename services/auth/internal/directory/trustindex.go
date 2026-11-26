package directory

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The trust index (RT-6.43 step 3, ADR-016 point 4): which operators may see
// an agent's channels because the agent trusts them directly or is theirs.
// The login service writes one record per agent at each credential it issues,
// from what it verified and attested itself in that same issue: the owner's
// anchor from the directory, the host's trust list resolved to anchors as the
// attestation names them (RT-6.41), and the channels it granted, each one the
// owner is a member of. Nothing an agent publishes about itself goes in. A
// record counts until the credential it was written with expires; the next
// issue for the same agent replaces it, so a trust the host dropped is gone
// with the next credential at the latest.
//
// Key:
//
//	agenttrust.<hex>  the record of the agent named <hex> (hex-encoded)
const agentTrustPrefix = "agenttrust."

// AgentTrust is an agent's record in the trust index.
type AgentTrust struct {
	// Agent is the agent's name, as in its credential.
	Agent string `json:"agent"`
	// Owner is the anchor of the agent's owner.
	Owner string `json:"owner"`
	// Trusted are the anchors the attestation names as trusted.
	Trusted []string `json:"trusted"`
	// Channels are the channels granted in that credential: the home and
	// the guest channels, never the lobby.
	Channels []string `json:"channels"`
	// Exp is the credential's expiry (Unix seconds): after it the record
	// counts for nothing.
	Exp int64 `json:"exp"`
}

func agentTrustKey(agent string) string {
	return agentTrustPrefix + hex.EncodeToString([]byte(agent))
}

// opens says whether the record, unexpired at now, opens its channels to the
// person with anchor: the agent is theirs or trusts them directly.
func (r AgentTrust) opens(anchor string, now time.Time) bool {
	return anchor != "" && r.Exp > now.Unix() && (r.Owner == anchor || slices.Contains(r.Trusted, anchor))
}

// PutAgentTrust writes an agent's record, replacing the one before. An owner
// or a trusted anchor that is no anchor (anchorRE) refuses the record; channels
// that are no channel token are left out.
func (s *Store) PutAgentTrust(ctx context.Context, r AgentTrust) error {
	if r.Agent == "" || len(r.Agent) > 128 || !anchorRE.MatchString(r.Owner) {
		return fmt.Errorf("%w: agent %.64q owner %.64q", ErrBadKey, r.Agent, r.Owner)
	}
	for _, a := range r.Trusted {
		if !anchorRE.MatchString(a) {
			return fmt.Errorf("%w: trusted anchor %.64q", ErrBadKey, a)
		}
	}
	var chs []string
	for _, c := range r.Channels {
		if checkChannel(c) == nil {
			chs = append(chs, c)
		}
	}
	r.Channels = chs
	if r.Trusted == nil {
		r.Trusted = []string{}
	}
	// The later issue's record wins; there is nothing to merge.
	b, err := json.Marshal(r)
	if err != nil {
		return err
	}
	_, err = s.kv.Put(ctx, agentTrustKey(r.Agent), b)
	return err
}

// errUnreadable: a trust record is there but is no record. It opens
// nothing, and the sweep purges it.
var errUnreadable = errors.New("directory: unreadable trust record")

// agentTrust reads the record at key with its revision; false when there is
// none. A value that is no record is errUnreadable, with its revision; any
// other error is the bucket's.
func (s *Store) agentTrust(ctx context.Context, key string) (AgentTrust, uint64, bool, error) {
	var r AgentTrust
	e, err := s.kv.Get(ctx, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return r, 0, false, nil
	}
	if err != nil {
		return r, 0, false, err
	}
	if err := json.Unmarshal(e.Value(), &r); err != nil {
		return AgentTrust{}, e.Revision(), true, errUnreadable
	}
	return r, e.Revision(), true, nil
}

// TrustOpened lists the channels of the unexpired records that open to the
// person with anchor (AgentTrust.opens). A record that cannot be read opens
// nothing: its key is in unreadable, never its content. A failed listing or
// read of the bucket is an error: no partial answer.
func (s *Store) TrustOpened(ctx context.Context, anchor string, now time.Time) (channels, unreadable []string, err error) {
	if anchor == "" {
		return nil, nil, nil
	}
	keys, err := s.keysWith(ctx, agentTrustPrefix)
	if err != nil {
		return nil, nil, err
	}
	var out []string
	for _, k := range keys {
		r, _, ok, err := s.agentTrust(ctx, k)
		if errors.Is(err, errUnreadable) {
			unreadable = append(unreadable, k)
			continue
		}
		if err != nil {
			return nil, nil, err
		}
		if ok && r.opens(anchor, now) {
			for _, c := range r.Channels {
				if checkChannel(c) == nil {
					out = append(out, c)
				}
			}
		}
	}
	slices.Sort(out)
	return slices.Compact(out), unreadable, nil
}

// PruneAgentTrust purges the records expired before now and those that
// cannot be read, their history with them, so the names of ended agents
// leave one marker each, not their records; a record written again
// meanwhile stays (the purge names the revision it read).
func (s *Store) PruneAgentTrust(ctx context.Context, now time.Time) error {
	keys, err := s.keysWith(ctx, agentTrustPrefix)
	if err != nil {
		return err
	}
	for _, k := range keys {
		r, rev, ok, err := s.agentTrust(ctx, k)
		if err != nil && !errors.Is(err, errUnreadable) {
			return err
		}
		if !ok || (err == nil && r.Exp > now.Unix()) {
			continue
		}
		if err := s.purgeAt(ctx, k, rev); err != nil {
			return err
		}
	}
	return nil
}

// purgeAt purges key if it is still at revision rev; a key written since
// stays, and that is no error.
func (s *Store) purgeAt(ctx context.Context, key string, rev uint64) error {
	err := s.kv.Purge(ctx, key, jetstream.LastRevision(rev))
	if err != nil && !isWrongSequence(err) {
		return err
	}
	return nil
}

// SweepEvery is how often TrustIndex.Sweep runs; sweepTimeout bounds one
// round.
const (
	SweepEvery   = 5 * time.Minute
	sweepTimeout = time.Minute
)

// TrustIndex is callout.TrustIndex over the directory: Index writes an
// agent's record at each issue, and nothing else, within the login; Sweep,
// in the background, purges the expired records so the names of ended
// agents do not fill the bucket.
type TrustIndex struct {
	Store *Store
	// Now is the clock; nil is time.Now.
	Now func() time.Time
	// Every is the sweep's interval; 0 is SweepEvery.
	Every time.Duration
	// Log gets a failed sweep; nil is slog.Default().
	Log *slog.Logger
}

// Index implements callout.TrustIndex.
func (t *TrustIndex) Index(ctx context.Context, r AgentTrust) error {
	return t.Store.PutAgentTrust(ctx, r)
}

// Sweep purges the expired records every Every until ctx ends; each round has its own timeout. A failed round
// only leaves expired records, which count for nothing; the next takes them.
func (t *TrustIndex) Sweep(ctx context.Context) {
	every := t.Every
	if every <= 0 {
		every = SweepEvery
	}
	log := t.Log
	if log == nil {
		log = slog.Default()
	}
	tick := time.NewTicker(every)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		if err := t.sweepOnce(ctx); err != nil && ctx.Err() == nil {
			log.Warn("trust index sweep failed", "reason", err.Error())
		}
	}
}

func (t *TrustIndex) sweepOnce(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, sweepTimeout)
	defer cancel()
	now := time.Now()
	if t.Now != nil {
		now = t.Now()
	}
	return t.Store.PruneAgentTrust(ctx, now)
}
