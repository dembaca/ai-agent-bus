package directory

import (
	"context"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"time"
)

// bindingPrefix keys, per Mattermost account (by its user id), the binding
// the login service proved at a login (RT-6.42 step 3, the owner's decision
// of 2026-11-27): the identity and its anchor, how the account logs in and
// the value its auth_data equalled, and the account record last published
// for it. The login service publishes that record again from here, so a
// binding proven once does not age out of PRESENCE; no agent and not the
// bridge reads the directory.
const bindingPrefix = "binding.mattermost."

// BindingMaxAge is how long a binding holds after the login that proved it
// (Binding.At): the login service's hourly check unbinds an older one as it
// does a changed account, and the next proven login renews it. So a person
// disabled in Keycloak whose Mattermost account stays active stops being
// bound within it; an approver logs in at least once in that time.
const BindingMaxAge = 30 * 24 * time.Hour

// chatIDRE is a Mattermost user id: 26 characters of [a-z0-9].
var chatIDRE = regexp.MustCompile(`^[a-z0-9]{26}$`)

// Binding is one proven binding of a Mattermost account to an identity.
type Binding struct {
	// Account is Mattermost's user id of the account.
	Account string `json:"account"`
	// Sub is the identity's `sub`, Anchor its anchor.
	Sub    string `json:"sub"`
	Anchor string `json:"anchor"`
	// AuthService is how the account logged in when it was proven
	// ("openid" or "ldap"), AuthData the value its auth_data equalled: the
	// token's `sub`, or the token's LDAP id.
	AuthService string `json:"auth_service"`
	AuthData    string `json:"auth_data"`
	// Record is the account record last published for the account, as
	// published (callout.AccountRecord).
	Record json.RawMessage `json:"record"`
	// At is when the binding was proven, in Unix seconds: at the newest
	// proven login, so each login renews it. It holds BindingMaxAge from then.
	At int64 `json:"at"`
}

// bindingSkew is how far ahead of now a binding's At may lie (clocks).
const bindingSkew = 5 * time.Minute

// Expired says whether the binding is older than BindingMaxAge at now, or
// was proven more than bindingSkew ahead of now: a time that never was
// cannot start the 30 days.
func (b Binding) Expired(now time.Time) bool {
	age := now.Sub(time.Unix(b.At, 0))
	return age > BindingMaxAge || age < -bindingSkew
}

func bindingKey(account string) (string, error) {
	if !chatIDRE.MatchString(account) {
		return "", ErrBadKey
	}
	return bindingPrefix + account, nil
}

// PutBinding keeps b, replacing what the account had. The caller orders
// the writes of one identity (the login service's lock per `sub`).
func (s *Store) PutBinding(ctx context.Context, b Binding) error {
	key, err := bindingKey(b.Account)
	if err != nil {
		return err
	}
	if err := checkKey(b.Sub); err != nil {
		return err
	}
	if b.Anchor == "" || b.AuthService == "" || b.AuthData == "" || len(b.Record) == 0 {
		return errors.New("directory: an incomplete binding")
	}
	v, err := json.Marshal(b)
	if err != nil {
		return err
	}
	_, err = s.kv.Put(ctx, key, v)
	return err
}

// Binding is the account's binding and its revision; ok false when it has
// none.
func (s *Store) Binding(ctx context.Context, account string) (Binding, uint64, bool, error) {
	key, err := bindingKey(account)
	if err != nil {
		return Binding{}, 0, false, err
	}
	var b Binding
	rev, ok, err := s.get(ctx, key, &b)
	if err != nil || !ok {
		return Binding{}, 0, false, err
	}
	return b, rev, true, nil
}

// BindingAccounts lists the accounts that have a binding.
func (s *Store) BindingAccounts(ctx context.Context) ([]string, error) {
	keys, err := s.keysWith(ctx, bindingPrefix)
	if err != nil {
		return nil, err
	}
	out := make([]string, 0, len(keys))
	for _, k := range keys {
		out = append(out, strings.TrimPrefix(k, bindingPrefix))
	}
	return out, nil
}

// DropBinding removes the account's binding, its history with it, if it is
// still at revision rev: one written again meanwhile stays.
func (s *Store) DropBinding(ctx context.Context, account string, rev uint64) error {
	key, err := bindingKey(account)
	if err != nil {
		return err
	}
	return s.purgeAt(ctx, key, rev)
}

// PutBinding, Binding, BindingAccounts and DropBinding make Anchors the
// login service's store of the bindings (callout.BindingStore).
func (a Anchors) PutBinding(ctx context.Context, b Binding) error { return a.Store.PutBinding(ctx, b) }

func (a Anchors) Binding(ctx context.Context, account string) (Binding, uint64, bool, error) {
	return a.Store.Binding(ctx, account)
}

func (a Anchors) BindingAccounts(ctx context.Context) ([]string, error) {
	return a.Store.BindingAccounts(ctx)
}

func (a Anchors) DropBinding(ctx context.Context, account string, rev uint64) error {
	return a.Store.DropBinding(ctx, account, rev)
}
