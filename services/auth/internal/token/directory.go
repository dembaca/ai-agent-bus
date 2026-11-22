package token

import (
	"context"
	"errors"
)

// OpenIDService is Mattermost's auth_service for an account that logs in over
// OpenID Connect; its auth_data is then the identity provider's `sub`.
const OpenIDService = "openid"

// ErrNoSuchUser is what a Directory returns when the chat has no account of
// that name.
var ErrNoSuchUser = errors.New("no such user")

// User is what the verifier needs to know about a chat account.
type User struct {
	Username string
	// Bot is true for a bot account: a bot never stands for a human.
	Bot bool
	// Deactivated is true for an account that may no longer log in.
	Deactivated bool
	// AuthService is how the account logs in ("" for a password,
	// OpenIDService over OpenID Connect, others for LDAP, SAML …).
	AuthService string
	// AuthData is the identity provider's id for the account, for
	// OpenIDService its `sub`.
	AuthData string
}

// Directory looks a human up in the chat by username. The Mattermost
// implementation arrives with the wiring in cmd/authd; ErrNoSuchUser for an
// unknown name, any other error for a lookup that failed.
type Directory interface {
	User(ctx context.Context, username string) (User, error)
}
