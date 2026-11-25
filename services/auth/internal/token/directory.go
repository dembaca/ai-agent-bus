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
	// ID is the chat's own id of the account (Mattermost's user id), what the
	// account record is keyed by (RT-6.42).
	ID string
	// Bot is true for a bot account: a bot never stands for a human.
	Bot bool
	// Guest is true for a guest account: a guest owns no agents.
	Guest bool
	// Deactivated is true for an account that may no longer log in.
	Deactivated bool
	// AuthKnown is true when the lookup could see how the account logs in.
	// Mattermost blanks auth_service and auth_data for a caller without the
	// right to read them (a system admin's token can): an answer with
	// AuthKnown false is refused, never taken for a password account.
	AuthKnown bool
	// AuthService is how the account logs in ("" for a password,
	// OpenIDService over OpenID Connect, others for LDAP, SAML …).
	AuthService string
	// AuthData is the identity provider's id for the account, for
	// OpenIDService its `sub`.
	AuthData string
}

// Directory looks a human up in the chat by username. The Mattermost
// implementation arrives with the wiring in cmd/authd; it needs a token that
// Mattermost shows auth_service and auth_data to. ErrNoSuchUser for an
// unknown name, any other error for a lookup that failed.
type Directory interface {
	User(ctx context.Context, username string) (User, error)
}
