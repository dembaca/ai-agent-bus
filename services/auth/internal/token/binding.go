package token

import (
	"context"
	"errors"
)

// ErrBinding: the name binding refuses this pairing of chat account and
// `sub`.
var ErrBinding = errors.New("name binding refused")

// NameBinding decides whether the token's `sub` may stand for the chat
// account of that name. Verify calls it after the chat lookup, with the
// account as the chat reported it. Which binding a tenant uses is the owner's
// decision; a store that pins the first `sub` seen for a name fits here too.
type NameBinding interface {
	Check(ctx context.Context, sub string, u User) error
}

// Open binds by name alone for an account that does not log in over OpenID
// Connect (the check of `sub` against an OpenID account's auth_data is done
// before, always). It is the state of ADR-014 until the owner decides.
type Open struct{}

// Check lets every pairing pass.
func (Open) Check(context.Context, string, User) error { return nil }

// OpenIDOnly accepts only chat accounts that log in over OpenID Connect, so
// that `sub` binds every owner.
type OpenIDOnly struct{}

// Check refuses an account whose auth_service is not OpenIDService.
func (OpenIDOnly) Check(_ context.Context, _ string, u User) error {
	if u.AuthService != OpenIDService {
		return ErrBinding
	}
	return nil
}
