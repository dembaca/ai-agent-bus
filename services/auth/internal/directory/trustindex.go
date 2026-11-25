package directory

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"slices"
	"sync"
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

// PutAgentTrust writes an agent's record, replacing the one before. Channels
// that are no channel token are left out.
func (s *Store) PutAgentTrust(ctx context.Context, r AgentTrust) error {
	if r.Agent == "" || len(r.Agent) > 128 || r.Owner == "" {
		return fmt.Errorf("%w: agent %.64q owner %.64q", ErrBadKey, r.Agent, r.Owner)
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

func (s *Store) agentTrust(ctx context.Context, key string) (AgentTrust, uint64, bool, error) {
	var r AgentTrust
	rev, ok, err := s.get(ctx, key, &r)
	return r, rev, ok, err
}

// TrustOpened lists the channels of the unexpired records that open to the
// person with anchor (AgentTrust.opens). A record that cannot be read is an
// error: no partial answer.
func (s *Store) TrustOpened(ctx context.Context, anchor string, now time.Time) ([]string, error) {
	if anchor == "" {
		return nil, nil
	}
	keys, err := s.keysWith(ctx, agentTrustPrefix)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, k := range keys {
		r, _, ok, err := s.agentTrust(ctx, k)
		if err != nil {
			return nil, err
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
	return slices.Compact(out), nil
}

// PruneAgentTrust deletes the records expired before now; a record written
// again meanwhile stays (the delete names the revision it read).
func (s *Store) PruneAgentTrust(ctx context.Context, now time.Time) error {
	keys, err := s.keysWith(ctx, agentTrustPrefix)
	if err != nil {
		return err
	}
	for _, k := range keys {
		r, rev, ok, err := s.agentTrust(ctx, k)
		if err != nil {
			return err
		}
		if !ok || r.Exp > now.Unix() {
			continue
		}
		err = s.kv.Delete(ctx, k, jetstream.LastRevision(rev))
		if err != nil && !isWrongSequence(err) {
			return err
		}
	}
	return nil
}

// PruneEvery is how often TrustIndex deletes expired records.
const PruneEvery = 5 * time.Minute

// TrustIndex is callout.TrustIndex over the directory: it writes an agent's
// record at each issue and, at most every PruneEvery, deletes the expired
// ones, so the names of ended agents do not fill the bucket.
type TrustIndex struct {
	Store *Store
	// Now is the clock; nil is time.Now.
	Now func() time.Time

	mu        sync.Mutex
	lastPrune time.Time
}

// Index implements callout.TrustIndex.
func (t *TrustIndex) Index(ctx context.Context, r AgentTrust) error {
	if err := t.Store.PutAgentTrust(ctx, r); err != nil {
		return err
	}
	now := time.Now()
	if t.Now != nil {
		now = t.Now()
	}
	t.mu.Lock()
	due := now.Sub(t.lastPrune) >= PruneEvery
	if due {
		t.lastPrune = now
	}
	t.mu.Unlock()
	if due {
		// A failed sweep only leaves expired records, which count for
		// nothing; the next sweep takes them.
		_ = t.Store.PruneAgentTrust(ctx, now)
	}
	return nil
}
