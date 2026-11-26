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
//	anchor.<hex>    the `sub` holding the anchor <hex> (hex-encoded value): one
//	                key per value, so no value is ever given twice, whether it
//	                came from a name or a `sub`
//	anchorof.<hex>  the anchor a `sub` (hex-encoded) claimed: found again at
//	                its next login even if its identity was never written
//	account.<product>.<hex>
//	                the `sub` whose account of that product (today "mattermost")
//	                has the name <hex> (hex-encoded): one key per name, so a
//	                chat name never resolves to two identities
//	meta.imported   written last by the one import from Mattermost; from then
//	                on the directory, not Mattermost, says who is a member
//
// Nothing here logs a token.
package directory

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
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
	anchorOfPrefix = "anchorof."
	accountPrefix  = "account."
	// movePrefix marks a name whose index key is being moved: the `sub` it
	// is taken from, until both records have followed.
	movePrefix  = "accountmove."
	importedKey = "meta.imported"
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
	// ErrAnchorShape: the `sub` cannot be an anchor (lower-cased, it is no
	// owner token: a symbol, too long).
	ErrAnchorShape = errors.New("directory: the sub cannot be an anchor")
	// ErrImportMissing: the configuration says the import has run
	// (AUTH_DIRECTORY_IMPORTED), but the directory has no mark of it: a lost
	// or replaced bucket, never a reason to fall back to the chat.
	ErrImportMissing = errors.New("directory: the import's mark is missing")
	// ErrUnsafeBucket: an existing bucket's stream differs from a safe one.
	ErrUnsafeBucket = errors.New("directory: bucket configuration is unsafe")
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
	// MovedFrom says, by product, which `sub` held this identity's account
	// name before it moved here (a name taken over by a verified login or by
	// an admin): for the operator to see, never a key.
	MovedFrom map[string]string `json:"moved_from,omitempty"`
}

// Store is the directory over a key-value bucket.
type Store struct {
	kv jetstream.KeyValue
	// expect: the configuration says the import has run; a missing mark is
	// an error, not "not yet".
	expect bool
}

// keyRE is one safe key token for a `sub`: what a KV key allows, minus the
// dot that would split it, and not empty.
var keyRE = regexp.MustCompile(`^[A-Za-z0-9_=-]{1,128}$`)

// channelRE is a channel's token on the bus, as the callout checks it
// (callout.tokenRE): lower case only, so one channel is one key.
var channelRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

// nameAnchorRE: what may be a name anchor. Before the import a login's
// owner is a Mattermost account of that name (token.Verifier's chat check),
// and the Mattermost server takes usernames of a letter and then up to 63 of
// [a-z0-9._-] (model.IsValidUsername; the 22 of the web form is not the
// server's rule). A UUID may have that shape too: names and subs are kept
// apart by the one index per value, not by their shape.
var nameAnchorRE = regexp.MustCompile(`^[a-z][a-z0-9._-]{0,63}$`)

// productRE: a chat product's name in an account key.
var productRE = regexp.MustCompile(`^[a-z]{1,16}$`)

// MaxTrusted is how many names of a host's trust list are resolved.
const MaxTrusted = 32

// ErrNoIdentity: the `sub` has no identity record.
var ErrNoIdentity = errors.New("directory: no such identity")

// maxSuffix bounds the tries for a `sub` anchor whose value someone holds
// already: <sub>, <sub>.1 … <sub>.<maxSuffix>, the `sub` shortened where the
// suffix would pass the 64 an owner token may have. Each squatter holds one
// name, the one it logs in with: locking an identity out takes nine
// identities made on purpose, and then the issue is refused (ErrAnchorTaken).
const maxSuffix = 8

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

func checkChannel(s string) error {
	if !channelRE.MatchString(s) {
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

// Open returns the store over the bucket. js must be the login service's
// own JetStream connection: the only one with a right on the bucket.
// Without `imported` it makes the bucket with Config when it is missing; with
// it (AUTH_DIRECTORY_IMPORTED: the operator ran the import) a missing bucket
// is an error, never made anew, and a missing mark refuses every answer
// (ErrImportMissing). An existing bucket is checked: no mirror, source or
// republish (which could hand its records to a subject an agent reads), file
// storage, a size limit, exactly its own subjects.
func Open(ctx context.Context, js jetstream.JetStream, bucket string, imported bool) (*Store, error) {
	cfg := Config(bucket)
	kv, err := js.KeyValue(ctx, cfg.Bucket)
	switch {
	case errors.Is(err, jetstream.ErrBucketNotFound) && imported:
		return nil, fmt.Errorf("directory bucket %s: missing although the import has run (AUTH_DIRECTORY_IMPORTED): %w",
			cfg.Bucket, ErrImportMissing)
	case errors.Is(err, jetstream.ErrBucketNotFound):
		kv, err = js.CreateKeyValue(ctx, cfg)
	}
	if err != nil {
		return nil, fmt.Errorf("directory bucket %s: %w", cfg.Bucket, err)
	}
	st, err := kv.Status(ctx)
	if err != nil {
		return nil, fmt.Errorf("directory bucket %s: %w", cfg.Bucket, err)
	}
	b, ok := st.(*jetstream.KeyValueBucketStatus)
	if !ok {
		return nil, fmt.Errorf("%w: %s: its stream cannot be read", ErrUnsafeBucket, cfg.Bucket)
	}
	if err := checkBucket(cfg.Bucket, b.StreamInfo()); err != nil {
		return nil, err
	}
	return &Store{kv: kv, expect: imported}, nil
}

func checkBucket(bucket string, info *jetstream.StreamInfo) error {
	if info == nil {
		return fmt.Errorf("%w: %s: no stream info", ErrUnsafeBucket, bucket)
	}
	c, bad := info.Config, []string{}
	if c.Mirror != nil || len(c.Sources) > 0 {
		bad = append(bad, "mirror or sources set")
	}
	if c.RePublish != nil {
		bad = append(bad, "republish set")
	}
	if c.Storage != jetstream.FileStorage {
		bad = append(bad, "not file storage")
	}
	if c.MaxBytes <= 0 {
		bad = append(bad, "no max_bytes")
	}
	if len(c.Subjects) != 1 || c.Subjects[0] != "$KV."+bucket+".>" {
		bad = append(bad, "subjects are not exactly $KV."+bucket+".>")
	}
	if len(bad) > 0 {
		return fmt.Errorf("%w: %s: %s", ErrUnsafeBucket, bucket, strings.Join(bad, ", "))
	}
	return nil
}

// New wraps a bucket that is already open (tests).
func New(kv jetstream.KeyValue, imported bool) *Store { return &Store{kv: kv, expect: imported} }

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
		if s.expect {
			return false, ErrImportMissing
		}
		return false, nil
	}
	return err == nil, err
}

// MarkImported is the import's last step. Written once (compare and set): a
// second one returns ErrConflict and leaves the first mark as it is.
func (s *Store) MarkImported(ctx context.Context, at time.Time) error {
	_, err := s.kv.Create(ctx, importedKey, []byte(at.UTC().Format(time.RFC3339)))
	if errors.Is(err, jetstream.ErrKeyExists) {
		return ErrConflict
	}
	return err
}

// Channel reads a channel by its token; false when there is none.
func (s *Store) Channel(ctx context.Context, name string) (Channel, uint64, bool, error) {
	var c Channel
	if err := checkChannel(name); err != nil {
		return c, 0, false, err
	}
	rev, ok, err := s.get(ctx, channelPrefix+name, &c)
	return c, rev, ok, err
}

// PutChannel writes a channel over revision rev (0: a new one).
func (s *Store) PutChannel(ctx context.Context, c Channel, rev uint64) (uint64, error) {
	if err := checkChannel(c.Name); err != nil {
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
		if checkChannel(name) != nil {
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
// choice: Anchors passes today's name, before and after the import (#86);
// "" gives the sub.
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

// claimAnchor reserves an anchor for sub and returns it. First the anchor
// sub claimed before, if any (anchorof.<sub>): a login whose identity was not
// written finds it again, before or after the import. Else `today` when it is
// given, has a username's shape and is free (or already sub's). Else sub's
// own lower-cased value, or with a suffix .1 … .8 when that value is held
// (by a name equal to it): one index key per value (anchor.<hex>) is created
// first, so no value is ever given twice, and a squatter that calls itself
// another identity's `sub` takes one value, never that identity's place.
func (s *Store) claimAnchor(ctx context.Context, sub, today string) (string, error) {
	of := anchorOfPrefix + hex.EncodeToString([]byte(sub))
	if e, err := s.kv.Get(ctx, of); err == nil {
		a := string(e.Value())
		held, err := s.kv.Get(ctx, anchorPrefix+hex.EncodeToString([]byte(a)))
		if !anchorRE.MatchString(a) || err != nil || string(held.Value()) != sub {
			return "", fmt.Errorf("%w: the anchor recorded for this sub is not its own", ErrAnchorTaken)
		}
		return a, nil
	} else if !errors.Is(err, jetstream.ErrKeyNotFound) {
		return "", err
	}
	try := func(a string) (bool, error) {
		key := anchorPrefix + hex.EncodeToString([]byte(a))
		_, err := s.kv.Create(ctx, key, []byte(sub))
		if err == nil {
			return true, nil
		}
		if !errors.Is(err, jetstream.ErrKeyExists) {
			return false, err
		}
		e, err := s.kv.Get(ctx, key)
		if err != nil {
			return false, err
		}
		return string(e.Value()) == sub, nil
	}
	remember := func(a string) (string, error) {
		if _, err := s.kv.Put(ctx, of, []byte(a)); err != nil {
			return "", err
		}
		return a, nil
	}
	if today != "" && nameAnchorRE.MatchString(today) {
		ok, err := try(today)
		if err != nil {
			return "", err
		}
		if ok {
			return remember(today)
		}
	}
	own := lower(sub)
	if !anchorRE.MatchString(own) {
		return "", fmt.Errorf("%w: %.64q", ErrAnchorShape, own)
	}
	for i := 0; i <= maxSuffix; i++ {
		a := own
		if i > 0 {
			suffix := fmt.Sprintf(".%d", i)
			base := own
			if len(base)+len(suffix) > 64 {
				base = base[:64-len(suffix)]
			}
			a = base + suffix
		}
		if !anchorRE.MatchString(a) {
			break
		}
		ok, err := try(a)
		if err != nil {
			return "", err
		}
		if ok {
			return remember(a)
		}
	}
	return "", fmt.Errorf("%w: %.64q", ErrAnchorTaken, own)
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

func accountKey(product, name string) (string, error) {
	if !productRE.MatchString(product) || name == "" || len(name) > 64 {
		return "", fmt.Errorf("%w: account %.16q %.64q", ErrBadKey, product, name)
	}
	return accountPrefix + product + "." + hex.EncodeToString([]byte(name)), nil
}

// SetAccount records that the identity `sub` has the chat account `name` of
// `product`. The index key is created first: a name another identity holds is
// ErrConflict, and the same identity's own is fine. The identity must exist
// (ErrNoIdentity); its record is written over its revision.
func (s *Store) SetAccount(ctx context.Context, sub, product, name string) error {
	if err := checkKey(sub); err != nil {
		return err
	}
	key, err := accountKey(product, name)
	if err != nil {
		return err
	}
	if _, err := s.kv.Create(ctx, key, []byte(sub)); errors.Is(err, jetstream.ErrKeyExists) {
		e, err := s.kv.Get(ctx, key)
		if err != nil {
			return err
		}
		if string(e.Value()) != sub {
			return ErrConflict
		}
	} else if err != nil {
		return err
	}
	for range 3 {
		id, rev, ok, err := s.Identity(ctx, sub)
		if err != nil {
			return err
		}
		if !ok {
			return ErrNoIdentity
		}
		if id.Accounts[product] == name {
			// No rewrite needed. A release could otherwise race this fast path, but none
			// runs while SetAccount does: the import is its only caller, it runs before the
			// import mark, and claims (and with them releases) only after it (#72).
			return nil
		}
		old := id.Accounts[product]
		if id.Accounts == nil {
			id.Accounts = map[string]string{}
		}
		id.Accounts[product] = name
		if _, err := s.put(ctx, identityPrefix+sub, id, rev); errors.Is(err, ErrConflict) {
			continue
		} else if err != nil {
			return err
		}
		if old != "" { // the name it left is free again
			if oldKey, err := accountKey(product, old); err == nil {
				if e, err := s.kv.Get(ctx, oldKey); err == nil && string(e.Value()) == sub {
					_ = s.kv.Delete(ctx, oldKey)
				}
			}
		}
		return nil
	}
	return ErrConflict
}

// ClaimAccount makes `name` the chat account of `product` of the identity
// `sub`, also when another identity holds that name: the index key is moved to
// `sub` and the old holder's record loses the name; the move says which `sub`
// it was taken from (From "" when it was free or already sub's). ONLY for a login
// whose chat account is proven to be the token's (token.Identity.ChatOpenID)
// or an admin: the import keeps SetAccount, which never takes a name over.
// The identity must exist (ErrNoIdentity).
//
// The safety is ByAccount's: it needs the index key and the identity's
// record to agree, so a write that stops half way leaves "not found" or the
// new holder, never the old one once the key moved, in any order of the
// records. The key moves first (compare and set, after a mark of the move),
// then the new holder's record, last the old holder's; a move that stops is
// finished from its mark by the next write of that name (settle).
func (s *Store) ClaimAccount(ctx context.Context, sub, product, name string) (AccountMove, error) {
	if err := checkKey(sub); err != nil {
		return AccountMove{}, err
	}
	key, err := accountKey(product, name)
	if err != nil {
		return AccountMove{}, err
	}
	if _, _, ok, err := s.Identity(ctx, sub); err != nil {
		return AccountMove{}, err
	} else if !ok {
		return AccountMove{}, ErrNoIdentity
	}
	released := ""
	for range 5 {
		_, err := s.kv.Create(ctx, key, []byte(sub))
		if err == nil {
			return s.settle(ctx, sub, product, name, "", released, true)
		}
		if !errors.Is(err, jetstream.ErrKeyExists) {
			return AccountMove{Released: released}, err
		}
		e, err := s.kv.Get(ctx, key)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			continue // deleted meanwhile: create it
		}
		if err != nil {
			return AccountMove{Released: released}, err
		}
		from := string(e.Value())
		if from == sub {
			return s.settle(ctx, sub, product, name, "", released, false)
		}
		r, err := s.beginMove(ctx, product, name, from, sub)
		if r != "" {
			released = r
		}
		if err != nil {
			return AccountMove{Released: released}, err
		}
		if err := accountStep("before key"); err != nil {
			return AccountMove{Released: released}, err
		}
		if _, err := s.kv.Update(ctx, key, []byte(sub), e.Revision()); err != nil {
			if isWrongSequence(err) {
				continue
			}
			return AccountMove{Released: released}, err
		}
		return s.settle(ctx, sub, product, name, from, released, true)
	}
	return AccountMove{Released: released}, ErrConflict
}

// AccountMove is what a write of the index did besides pointing a name at
// its holder. From: the `sub` the name moved from (also when the records did
// not all follow, so that the move is reported anyway). Released: an earlier
// holder whose unfinished move was finished by this write, by losing the
// name from its record.
type AccountMove struct {
	From     string
	Released string
}

// accountStep runs at the steps of a move; tests use it to stop a move half
// way or change the key meanwhile. Nothing in production.
var accountStep = func(string) error { return nil }

// SetAccountStepForTest replaces the hook at the steps of a move, for the
// tests of other packages; it returns the restore. Never in production.
func SetAccountStepForTest(f func(step string) error) (restore func()) {
	old := accountStep
	accountStep = f
	return func() { accountStep = old }
}

func moveKey(product, name string) string {
	return movePrefix + product + "." + hex.EncodeToString([]byte(name))
}

// openMark reads the mark of a move of `name`. It counts only while it
// describes an unfinished move: its `sub` is not `holder` (the key's) and
// its record still lists the name. Else it is stale: from is "", and mark is
// returned for the caller to remove.
func (s *Store) openMark(ctx context.Context, product, name, holder string) (from string, mark jetstream.KeyValueEntry, err error) {
	mark, err = s.kv.Get(ctx, moveKey(product, name))
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return "", nil, nil
	}
	if err != nil {
		return "", nil, err
	}
	m := string(mark.Value())
	if m == holder || checkKey(m) != nil {
		return "", mark, nil
	}
	id, _, ok, err := s.Identity(ctx, m)
	if err != nil {
		return "", nil, err
	}
	if !ok || id.Accounts[product] != name {
		return "", mark, nil
	}
	return m, mark, nil
}

// beginMove notes, before the index key moves away from `from` to `sub`,
// that it is taken from there. A move that stops after the key leaves the
// mark, and the next write of that name finishes it from there (settle). An
// unfinished move's mark is not lost to the new one: its old holder, who no
// longer has the key, loses the name first (released). Nothing is written
// into the record of `from`: only a login of `from` or an admin writes the
// name into it (a write from here could free a name `from` took meanwhile).
// When `sub` is that old holder taking its name back, nothing is released.
func (s *Store) beginMove(ctx context.Context, product, name, from, sub string) (released string, err error) {
	open, _, err := s.openMark(ctx, product, name, from)
	if err != nil {
		return "", err
	}
	if open != "" && open != sub {
		if err := s.releaseAccount(ctx, open, product, name); err != nil {
			return "", err
		}
		released = open
	}
	_, err = s.kv.Put(ctx, moveKey(product, name), []byte(from))
	return released, err
}

// settle follows a write of the index key that now points at `sub`: the
// records follow (finishAccount), and an unfinished move's mark is finished:
// its old holder counts as moved from when this write moved nothing, else it
// is released. The mark is removed once the records have followed; a stale
// one too.
func (s *Store) settle(ctx context.Context, sub, product, name, from, released string, changed bool) (AccountMove, error) {
	m := AccountMove{From: from, Released: released}
	open, mark, err := s.openMark(ctx, product, name, sub)
	if err != nil {
		return m, err
	}
	switch {
	case open == "":
	case m.From == "":
		m.From = open
	case open != m.From:
		if err := s.releaseAccount(ctx, open, product, name); err != nil {
			return m, err
		}
		m.Released = open
	}
	if err := s.finishAccount(ctx, sub, product, name, m.From, changed); err != nil {
		return m, err
	}
	if mark != nil {
		err := s.kv.Delete(ctx, moveKey(product, name), jetstream.LastRevision(mark.Revision()))
		if err != nil && !errors.Is(err, jetstream.ErrKeyNotFound) && !isWrongSequence(err) {
			return m, err
		}
	}
	return m, nil
}

// PutAccount is the admin's write of the index entry: rev 0 creates it
// (ErrConflict when the key exists), rev > 0 replaces it over exactly that
// revision (ErrConflict otherwise, ErrNotFound when there is none). The
// identity `sub` must exist (ErrNoIdentity). The move says where the name
// came from, when that was another `sub`.
func (s *Store) PutAccount(ctx context.Context, sub, product, name string, rev uint64) (AccountMove, error) {
	if err := checkKey(sub); err != nil {
		return AccountMove{}, err
	}
	key, err := accountKey(product, name)
	if err != nil {
		return AccountMove{}, err
	}
	if _, _, ok, err := s.Identity(ctx, sub); err != nil {
		return AccountMove{}, err
	} else if !ok {
		return AccountMove{}, ErrNoIdentity
	}
	if rev == 0 {
		if _, err := s.kv.Create(ctx, key, []byte(sub)); errors.Is(err, jetstream.ErrKeyExists) {
			return AccountMove{}, ErrConflict
		} else if err != nil {
			return AccountMove{}, err
		}
		return s.settle(ctx, sub, product, name, "", "", true)
	}
	e, err := s.kv.Get(ctx, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return AccountMove{}, ErrNotFound
	}
	if err != nil {
		return AccountMove{}, err
	}
	if e.Revision() != rev {
		return AccountMove{}, ErrConflict
	}
	from, released := "", ""
	if old := string(e.Value()); old != sub {
		from = old
		if released, err = s.beginMove(ctx, product, name, old, sub); err != nil {
			return AccountMove{Released: released}, err
		}
	}
	if err := accountStep("before key"); err != nil {
		return AccountMove{Released: released}, err
	}
	if _, err := s.kv.Update(ctx, key, []byte(sub), rev); err != nil {
		if isWrongSequence(err) {
			return AccountMove{Released: released}, ErrConflict
		}
		return AccountMove{Released: released}, err
	}
	return s.settle(ctx, sub, product, name, from, released, true)
}

// finishAccount is what follows a write of the index key: the new holder's
// record gets the name (and movedFrom), the name it had before is freed, and
// the old holder's record loses the name.
func (s *Store) finishAccount(ctx context.Context, sub, product, name, movedFrom string, changed bool) error {
	if err := accountStep("key moved"); err != nil {
		return err
	}
	if err := s.holdAccount(ctx, sub, product, name, movedFrom, changed); err != nil {
		return err
	}
	if err := accountStep("holder written"); err != nil {
		return err
	}
	if movedFrom == "" {
		return nil
	}
	return s.releaseAccount(ctx, movedFrom, product, name)
}

// holdAccount writes `name` into the record of `sub`. After a key this call
// changed (changed) it always writes, also when the record lists the name
// already: a release that read the record before must fail over its
// revision (releaseAccount).
func (s *Store) holdAccount(ctx context.Context, sub, product, name, movedFrom string, changed bool) error {
	for range 3 {
		id, rev, ok, err := s.Identity(ctx, sub)
		if err != nil {
			return err
		}
		if !ok {
			return ErrNoIdentity
		}
		old := id.Accounts[product]
		if !changed && old == name && (movedFrom == "" || id.MovedFrom[product] == movedFrom) {
			return nil
		}
		if !changed && old != name {
			// The key of `name` is this identity's and did not change in this call, but the
			// record names another: a login of the same identity with that other name may be
			// about to free the key of `name` over the revision it read. Touch the key first,
			// so that delete fails; recreate it when it is already gone (#72).
			if err := s.touchAccount(ctx, sub, product, name); err != nil {
				return err
			}
		}
		// The key of the name it leaves is read before the record is written, and freed
		// over exactly that revision: a login of this identity that takes that name back
		// meanwhile touches the key after reading this record's new name, so after this
		// read, and the delete fails (#72).
		var oldKey string
		var oldRev uint64
		if old != "" && old != name {
			if k, err := accountKey(product, old); err == nil {
				if e, err := s.kv.Get(ctx, k); err == nil && string(e.Value()) == sub {
					oldKey, oldRev = k, e.Revision()
				}
			}
		}
		if id.Accounts == nil {
			id.Accounts = map[string]string{}
		}
		id.Accounts[product] = name
		switch {
		case movedFrom != "":
			if id.MovedFrom == nil {
				id.MovedFrom = map[string]string{}
			}
			id.MovedFrom[product] = movedFrom
		case old != name:
			delete(id.MovedFrom, product) // it told of the name it left
		}
		if _, err := s.put(ctx, identityPrefix+sub, id, rev); errors.Is(err, ErrConflict) {
			continue
		} else if err != nil {
			return err
		}
		if err := accountStep("holder put"); err != nil {
			return err
		}
		if !changed && old != name {
			// Another identity may have taken the key between the touch and the record:
			// then this record would name a key it does not hold. Take the name back out
			// of the record and fail the login; the next one moves the name back if
			// Mattermost still says it is this identity's. A record never names a key
			// another identity holds.
			if e, err := s.kv.Get(ctx, s.mustKey(product, name)); err != nil || string(e.Value()) != sub {
				return errors.Join(err, s.releaseAccount(ctx, sub, product, name), ErrConflict)
			}
		}
		if oldKey != "" { // the name it left is free again
			_ = accountStep("free old")
			_ = s.kv.Delete(ctx, oldKey, jetstream.LastRevision(oldRev))
		}
		return nil
	}
	return ErrConflict
}

// mustKey is accountKey for a name already checked by the caller.
func (s *Store) mustKey(product, name string) string {
	k, _ := accountKey(product, name)
	return k
}

// touchAccount gives the key of `name`, held by `sub`, a new revision (or
// creates it again for `sub` when it is gone), so that a delete over an
// older revision fails. ErrConflict when another identity holds it now.
func (s *Store) touchAccount(ctx context.Context, sub, product, name string) error {
	key, err := accountKey(product, name)
	if err != nil {
		return err
	}
	for range 5 {
		e, err := s.kv.Get(ctx, key)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			if _, err := s.kv.Create(ctx, key, []byte(sub)); errors.Is(err, jetstream.ErrKeyExists) {
				continue
			} else if err != nil {
				return err
			}
			return nil
		}
		if err != nil {
			return err
		}
		if string(e.Value()) != sub {
			return ErrConflict
		}
		if _, err := s.kv.Update(ctx, key, []byte(sub), e.Revision()); err != nil {
			if isWrongSequence(err) || errors.Is(err, jetstream.ErrKeyNotFound) {
				continue
			}
			return err
		}
		return nil
	}
	return ErrConflict
}

// releaseAccount removes `name` from the record of `sub`, if it still lists
// it and does not hold its key. A `sub` without a record has nothing to lose.
//
// The record is read before the key, and written over the revision read: a
// `sub` that takes the key meanwhile writes its record after its key (hold
// always writes after a key it changed), so either the key read here shows
// it and nothing is released, or its record write comes later and the one
// here fails over the stale revision and looks again.
func (s *Store) releaseAccount(ctx context.Context, sub, product, name string) error {
	if checkKey(sub) != nil {
		return nil
	}
	key, err := accountKey(product, name)
	if err != nil {
		return err
	}
	for range 3 {
		id, rev, ok, err := s.Identity(ctx, sub)
		if err != nil {
			return err
		}
		if !ok || id.Accounts[product] != name {
			return nil
		}
		if e, err := s.kv.Get(ctx, key); err == nil && string(e.Value()) == sub {
			return nil // it holds the key (again): the name is its own
		} else if err != nil && !errors.Is(err, jetstream.ErrKeyNotFound) {
			return err
		}
		if err := accountStep("release checked"); err != nil {
			return err
		}
		delete(id.Accounts, product)
		delete(id.MovedFrom, product)
		if _, err := s.put(ctx, identityPrefix+sub, id, rev); errors.Is(err, ErrConflict) {
			continue
		} else if err != nil {
			return err
		}
		return nil
	}
	return ErrConflict
}

// Account reads the index entry of `name`: the `sub` it points at, the key's
// revision, and where the name came from when it moved ("" otherwise). ok is
// false when there is no key.
func (s *Store) Account(ctx context.Context, product, name string) (sub string, rev uint64, movedFrom string, ok bool, err error) {
	key, err := accountKey(product, name)
	if err != nil {
		return "", 0, "", false, err
	}
	e, err := s.kv.Get(ctx, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return "", 0, "", false, nil
	}
	if err != nil {
		return "", 0, "", false, err
	}
	sub = string(e.Value())
	if checkKey(sub) == nil {
		if id, _, found, err := s.Identity(ctx, sub); err != nil {
			return "", 0, "", false, err
		} else if found && id.Accounts[product] == name {
			movedFrom = id.MovedFrom[product]
		}
	}
	return sub, e.Revision(), movedFrom, true, nil
}

// DropAccount deletes the index entry of `name` over exactly the revision
// rev the admin read (ErrNotFound when there is none, ErrConflict when it
// changed), then removes the name from the record of the identity it
// pointed at, if that lists it. It returns that `sub`.
func (s *Store) DropAccount(ctx context.Context, product, name string, rev uint64) (sub, released string, err error) {
	key, err := accountKey(product, name)
	if err != nil {
		return "", "", err
	}
	e, err := s.kv.Get(ctx, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return "", "", ErrNotFound
	}
	if err != nil {
		return "", "", err
	}
	if e.Revision() != rev {
		return "", "", ErrConflict
	}
	err = s.kv.Delete(ctx, key, jetstream.LastRevision(rev))
	switch {
	case err == nil:
	case errors.Is(err, jetstream.ErrKeyNotFound):
		return "", "", ErrNotFound
	case isWrongSequence(err):
		return "", "", ErrConflict
	default:
		return "", "", err
	}
	sub = string(e.Value())
	// An unfinished move's old holder loses the name too: no one holds it now.
	open, _, err := s.openMark(ctx, product, name, sub)
	if err != nil {
		return sub, "", err
	}
	if open != "" {
		if err := s.releaseAccount(ctx, open, product, name); err != nil {
			return sub, "", err
		}
		released = open
	}
	if err := s.kv.Delete(ctx, moveKey(product, name)); err != nil && !errors.Is(err, jetstream.ErrKeyNotFound) {
		return sub, released, err
	}
	return sub, released, s.releaseAccount(ctx, sub, product, name)
}

// ByAccount finds the identity that has the chat account `name` of `product`;
// false when none has.
func (s *Store) ByAccount(ctx context.Context, product, name string) (Identity, bool, error) {
	key, err := accountKey(product, name)
	if err != nil {
		return Identity{}, false, err
	}
	e, err := s.kv.Get(ctx, key)
	if errors.Is(err, jetstream.ErrKeyNotFound) {
		return Identity{}, false, nil
	}
	if err != nil {
		return Identity{}, false, err
	}
	id, _, ok, err := s.Identity(ctx, string(e.Value()))
	if err != nil || !ok || id.Accounts[product] != name {
		return Identity{}, false, err
	}
	return id, true, nil
}

// Trusted is one human a host trusts: the anchor of their identity and the
// name the host gave.
type Trusted struct {
	Anchor string `json:"anchor"`
	Name   string `json:"name"`
}

// ResolveTrusted resolves the names of a host's trust list (MM_TRUSTED_HUMANS).
// At most MaxTrusted names are looked at; more, a name that is no lower-case
// username and, after the import, a name no identity has set cut=true and are
// left out, never guessed. Before the import (imported false) the name is the
// anchor, as it is for every identity then, and the store is not read. A read
// that fails is an error: no partial answer.
func (s *Store) ResolveTrusted(ctx context.Context, names []string, imported bool) ([]Trusted, bool, error) {
	cut := false
	if len(names) > MaxTrusted {
		names, cut = names[:MaxTrusted], true
	}
	out := make([]Trusted, 0, len(names))
	for _, n := range names {
		if !nameAnchorRE.MatchString(n) {
			cut = true
			continue
		}
		if !imported {
			out = append(out, Trusted{Anchor: n, Name: n})
			continue
		}
		id, ok, err := s.ByAccount(ctx, "mattermost", n)
		if err != nil {
			return nil, false, err
		}
		if !ok {
			cut = true
			continue
		}
		out = append(out, Trusted{Anchor: id.Anchor, Name: n})
	}
	return out, cut, nil
}

// Trust gives the callout a host's resolved trust list: before the import by
// name, after it by the accounts the import recorded.
type Trust struct{ Store *Store }

// Resolve is ResolveTrusted with the import's state read from the store.
func (t Trust) Resolve(ctx context.Context, names []string) ([]Trusted, bool, error) {
	imported, err := t.Store.Imported(ctx)
	if err != nil {
		return nil, false, err
	}
	return t.Store.ResolveTrusted(ctx, names, imported)
}
