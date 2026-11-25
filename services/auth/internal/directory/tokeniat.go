package directory

import (
	"context"
	"errors"
)

// tokenIatPrefix keys, per identity (by `sub`), the issue time of the newest
// Keycloak token whose account record the login service wrote (RT-6.42,
// ADR-016 step 2, point 8).
const tokenIatPrefix = "tokiat."

// tokenIat is the value of a tokiat.<sub> key.
type tokenIat struct {
	Iat      int64 `json:"iat"`
	Approver bool  `json:"approver"`
}

// maxTokenIatTries bounds the compare-and-set loop against a writer that is
// faster every time.
const maxTokenIatTries = 8

// NewestToken says whether a token issued at iat (Unix seconds), with or
// without the approvers' group, may write the identity's account record: true
// when no token of the identity was kept or when iat is not older than the
// newest kept; it then keeps iat if it is newer. Two tokens of the same second
// are ordered by a tie-break that fails closed: the one without the group
// wins, so the stored flag can go from true to false at an equal iat and never
// back (an equal iat with the same flag renews the record). false: an older
// token, or one of the same second that would raise the flag, whose record must not replace the newer one,
// so an older but still valid token replayed from another host cannot flip
// the approver flag back after a removal from the group. The guard lives in
// the directory, not in the record on NATS, so a record erased on NATS does
// not reset it (RT-6.42 step 3).
func (s *Store) NewestToken(ctx context.Context, sub string, iat int64, approver bool) (bool, error) {
	if err := checkKey(sub); err != nil {
		return false, err
	}
	key := tokenIatPrefix + sub
	for range maxTokenIatTries {
		var have tokenIat
		rev, found, err := s.get(ctx, key, &have)
		if err != nil {
			return false, err
		}
		switch {
		case found && iat < have.Iat:
			return false, nil
		case found && iat == have.Iat && approver && !have.Approver:
			return false, nil
		case found && iat == have.Iat && approver == have.Approver:
			return true, nil
		}
		if _, err := s.put(ctx, key, tokenIat{Iat: iat, Approver: approver}, rev); err == nil {
			return true, nil
		} else if !errors.Is(err, ErrConflict) {
			return false, err
		}
	}
	return false, ErrConflict
}

// NewestToken makes Anchors the callout's guard of the account record
// (callout.TokenGuard).
func (a Anchors) NewestToken(ctx context.Context, sub string, iat int64, approver bool) (bool, error) {
	return a.Store.NewestToken(ctx, sub, iat, approver)
}
