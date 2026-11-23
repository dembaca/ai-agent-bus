// Package directory is the bus's channel directory on NATS (ADR-016 point 2, RT-6.39):
// the channels with their owner, whether they are public, their members as
// Keycloak identities and a binding per chat; and per identity its fixed
// anchor and the name it shows. It lives in one key-value bucket in the
// tenant account that only the login service writes; agents have no right on
// $KV or $JS (ADR-014 point 16), so they neither read nor write it.
//
// Keys:
//
//	ch.<token>      a channel, by its token on the bus (the subject token)
//	id.<sub>        an identity, by its Keycloak `sub`
//	anchor.<value>  the `sub` that holds an anchor: no value is given twice
//	meta.imported   written last by the one import from Mattermost; from then
//	                on the directory, not Mattermost, says who is a member
//
// Nothing here logs a token.
package directory

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// DefaultBucket is the bucket's name when the configuration names none.
const DefaultBucket = "BUS_DIRECTORY"

// Key prefixes.
const (
	channelPrefix  = "ch."
	identityPrefix = "id."
	anchorPrefix   = "anchor."
	importedKey    = "meta.imported"
)

// MaxMembers bounds a channel's member list (the record must fit one value).
const MaxMembers = 1000

// Errors.
var (
	// ErrBadKey: a channel token, `sub` or anchor that is no safe key token.
	ErrBadKey = errors.New("directory: not a valid key")
	// ErrConflict: the record changed since it was read; read again and retry.
	ErrConflict = errors.New("directory: changed meanwhile")
	// ErrAnchorTaken: another identity holds that anchor.
	ErrAnchorTaken = errors.New("directory: anchor held by another identity")
)

// Channel is a channel on the bus.
type Channel struct {
	// Name is the channel's token on the bus (`<bus>.ch.<name>.<sender>`). It
	// never changes when a chat renames its own channel.
	Name string `json:"name"`
	// Owner is the `sub` of the identity that keeps the member list.
	Owner string `json:"owner"`
	// Public: every identity of the tenant may read it (decision 4).
	Public bool `json:"public"`
	// Members are identities by `sub`.
	Members []string `json:"members,omitempty"`
	// Bindings name the channel in each chat, by product ("mattermost": its
	// channel name there); a channel without one lives on NATS only.
	Bindings map[string]string `json:"bindings,omitempty"`
}

// Identity is a human on the bus: a Keycloak identity.
type Identity struct {
	Sub string `json:"sub"`
	// Anchor is fixed for good (decisions 7 and 9): the owner token of the
	// identity's agents' presence subjects and the starting value of their
	// names' hash. Today's name for an identity known before the import, its
	// `sub` for one that is new after it.
	Anchor string `json:"anchor"`
	// Display is the preferred_username of the last login: shown, never a key.
	Display string `json:"display"`
	// Accounts are the identity's chat accounts by product ("mattermost":
	// its username there).
	Accounts map[string]string `json:"accounts,omitempty"`
}

// Store is the directory over a key-value bucket.
type Store struct {
	kv jetstream.KeyValue
}

// keyRE is one safe key token: what a KV key allows, minus the dot that
// would split it, and not empty.
var keyRE = regexp.MustCompile(`^[A-Za-z0-9_=-][A-Za-z0-9_=~-]{0,127}$`)

// anchorRE: an anchor is what the callout hashes into an agent's name and
// writes, with "." as "~", as the owner token of its presence subject
// (callout.ownerToken): so a lower-case username as Keycloak and Mattermost
// give it today, or a `sub`. Exactly callout's ownerRE.
var anchorRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

func checkKey(s string) error {
	if !keyRE.MatchString(s) {
		return fmt.Errorf("%w: %.64q", ErrBadKey, s)
	}
	return nil
}

// Config returns the bucket's configuration: file storage, a short history
// for an operator to see the last changes, no expiry.
func Config(bucket string) jetstream.KeyValueConfig {
	if bucket == "" {
		bucket = DefaultBucket
	}
	return jetstream.KeyValueConfig{
		Bucket:       bucket,
		Description:  "ai-agent-bus channel directory (ADR-016); written only by the login service",
		History:      5,
		Storage:      jetstream.FileStorage,
		MaxValueSize: 256 << 10,
		MaxBytes:     64 << 20,
	}
}

// Open returns the store over the bucket, made with Config when it is missing
// (an existing one is taken as it is: the rights for an update are not
// granted). js must be the login service's own JetStream connection: the
// only one with a right on the bucket.
func Open(ctx context.Context, js jetstream.JetStream, bucket string) (*Store, error) {
	cfg := Config(bucket)
	kv, err := js.KeyValue(ctx, cfg.Bucket)
	if errors.Is(err, jetstream.ErrBucketNotFound) {
		kv, err = js.CreateKeyValue(ctx, cfg)
	}
	if err != nil {
		return nil, fmt.Errorf("directory bucket %s: %w", cfg.Bucket, err)
	}
	return &Store{kv: kv}, nil
}

// New wraps a bucket that is already open (tests).
func New(kv jetstream.KeyValue) *Store { return &Store{kv: kv} }

func (s *Store) get(ctx context.Context, key string, v any) (uint64, bool, error) {
	e, err := s.kv.Get(ctx, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return 0, false, nil
	}
	if err != nil {
		return 0, false, err
	}
	if err := json.Unmarshal(e.Value(), v); err != nil {
		return 0, false, fmt.Errorf("directory %s: %w", key, err)
	}
	return e.Revision(), true, nil
}

// put writes v at key: created when rev is 0 (refused if it exists), else
// only over that revision. ErrConflict when someone was faster.
func (s *Store) put(ctx context.Context, key string, v any, rev uint64) (uint64, error) {
	b, err := json.Marshal(v)
	if err != nil {
		return 0, err
	}
	if rev == 0 {
		n, err := s.kv.Create(ctx, key, b)
		if errors.Is(err, jetstream.ErrKeyExists) {
			return 0, ErrConflict
		}
		return n, err
	}
	n, err := s.kv.Update(ctx, key, b, rev)
	if err != nil && isWrongSequence(err) {
		return 0, ErrConflict
	}
	return n, err
}

func isWrongSequence(err error) bool {
	var apiErr *jetstream.APIError
	return errors.As(err, &apiErr) && apiErr.ErrorCode == jetstream.JSErrCodeStreamWrongLastSequence
}

// Imported says whether the import from Mattermost has run: from then on the
// directory decides membership (decision 2).
func (s *Store) Imported(ctx context.Context) (bool, error) {
	_, err := s.kv.Get(ctx, importedKey)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return false, nil
	}
	return err == nil, err
}

// MarkImported is the import's last step.
func (s *Store) MarkImported(ctx context.Context, at time.Time) error {
	_, err := s.kv.Put(ctx, importedKey, []byte(at.UTC().Format(time.RFC3339)))
	return err
}

// Channel reads a channel by its token; false when there is none.
func (s *Store) Channel(ctx context.Context, name string) (Channel, uint64, bool, error) {
	var c Channel
	if err := checkKey(name); err != nil {
		return c, 0, false, err
	}
	rev, ok, err := s.get(ctx, channelPrefix+name, &c)
	return c, rev, ok, err
}

// PutChannel writes a channel over revision rev (0: a new one).
func (s *Store) PutChannel(ctx context.Context, c Channel, rev uint64) (uint64, error) {
	if err := checkKey(c.Name); err != nil {
		return 0, err
	}
	if c.Owner != "" {
		if err := checkKey(c.Owner); err != nil {
			return 0, err
		}
	}
	if len(c.Members) > MaxMembers {
		return 0, fmt.Errorf("directory: more than %d members", MaxMembers)
	}
	for _, m := range c.Members {
		if err := checkKey(m); err != nil {
			return 0, err
		}
	}
	return s.put(ctx, channelPrefix+c.Name, c, rev)
}

// Memberships says of which channels the identity `sub` is a reader: a
// member, or the channel is public. A channel the directory does not know is
// no; a read that fails is an error, never a no (unknown is not "no").
func (s *Store) Memberships(ctx context.Context, sub string, channels []string) (map[string]bool, error) {
	if err := checkKey(sub); err != nil {
		return nil, err
	}
	out := make(map[string]bool, len(channels))
	for _, name := range channels {
		if checkKey(name) != nil {
			out[name] = false
			continue
		}
		c, _, ok, err := s.Channel(ctx, name)
		if err != nil {
			return nil, err
		}
		out[name] = ok && (c.Public || slices.Contains(c.Members, sub))
	}
	return out, nil
}

// Identity reads an identity by `sub`; false when there is none.
func (s *Store) Identity(ctx context.Context, sub string) (Identity, uint64, bool, error) {
	var id Identity
	if err := checkKey(sub); err != nil {
		return id, 0, false, err
	}
	rev, ok, err := s.get(ctx, identityPrefix+sub, &id)
	return id, rev, ok, err
}

// Login records a login of `sub` as `display` (its preferred_username) and
// returns the identity's anchor. A known identity keeps its anchor and takes
// the new display name. A new one gets `today` as its anchor when that is
// given and free, else its `sub` (lower-cased); the anchor index is written
// first, so two logins never share an anchor. `today` is the caller's
// choice: today's name before the import, "" after it (decisions 7 and 9).
func (s *Store) Login(ctx context.Context, sub, display, today string) (Identity, error) {
	for attempt := 0; attempt < 3; attempt++ {
		id, rev, ok, err := s.Identity(ctx, sub)
		if err != nil {
			return Identity{}, err
		}
		if ok {
			if id.Display == display {
				return id, nil
			}
			id.Display = display
			if _, err := s.put(ctx, identityPrefix+sub, id, rev); errors.Is(err, ErrConflict) {
				continue
			} else if err != nil {
				return Identity{}, err
			}
			return id, nil
		}
		anchor, err := s.claimAnchor(ctx, sub, today)
		if err != nil {
			return Identity{}, err
		}
		id = Identity{Sub: sub, Anchor: anchor, Display: display}
		if _, err := s.put(ctx, identityPrefix+sub, id, 0); errors.Is(err, ErrConflict) {
			continue // a parallel login of the same sub created it: read that one
		} else if err != nil {
			return Identity{}, err
		}
		return id, nil
	}
	return Identity{}, ErrConflict
}

// claimAnchor reserves `today` for sub if it is a valid anchor and free (or
// already sub's), else sub's own lower-cased value.
func (s *Store) claimAnchor(ctx context.Context, sub, today string) (string, error) {
	try := func(a string) (bool, error) {
		if !anchorRE.MatchString(a) {
			return false, nil
		}
		_, err := s.kv.Create(ctx, anchorPrefix+a, []byte(sub))
		if err == nil {
			return true, nil
		}
		if !errors.Is(err, jetstream.ErrKeyExists) {
			return false, err
		}
		e, err := s.kv.Get(ctx, anchorPrefix+a)
		if err != nil {
			return false, err
		}
		return string(e.Value()) == sub, nil
	}
	if today != "" {
		if ok, err := try(today); err != nil || ok {
			return today, err
		}
	}
	own := lower(sub)
	ok, err := try(own)
	if err != nil {
		return "", err
	}
	if !ok {
		return "", fmt.Errorf("%w: %.64q", ErrAnchorTaken, own)
	}
	return own, nil
}

func lower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 'a' - 'A'
		}
	}
	return string(b)
}
