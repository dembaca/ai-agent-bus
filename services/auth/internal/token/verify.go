// Package token checks the Keycloak access token an agent presents when it
// connects (ADR-014, point 6): signature and lifetime against the realm's
// JWKS, `aud` and `azp` for the bus's client, the owner from
// `preferred_username` bound to `sub`, that owner checked against the chat,
// and membership in the approvers' group.
//
// The package never logs; errors carry only the reason, never the token, a
// key id from it or the chat's own error text. Failed JWKS fetches go to
// Config.KeyFetchError.
package token

import (
	"context"
	"errors"
	"time"

	"github.com/MicahParks/keyfunc/v3"
	"github.com/golang-jwt/jwt/v5"
)

// MaxTokenBytes bounds a token before it is parsed. Keycloak's access tokens
// are a few KiB; group lists make them grow, not to this.
const MaxTokenBytes = 16 << 10

// Why a token was refused. Verify returns exactly one of them.
var (
	// ErrInvalid: too long, malformed, unsigned, a refused signing method, a
	// bad signature, no or an unknown key id, or not yet valid.
	ErrInvalid = errors.New("token invalid")
	// ErrExpired: `exp` has passed (beyond the leeway) or is missing.
	ErrExpired = errors.New("token expired")
	// ErrIssuer: `iss` is not the realm, or missing.
	ErrIssuer = errors.New("token from another issuer")
	// ErrAudience: `aud` lacks the bus's audience, or `azp` is another client.
	ErrAudience = errors.New("token for another client")
	// ErrTokenType: not an access token (`typ` is not "Bearer").
	ErrTokenType = errors.New("not an access token")
	// ErrIdentity: `sub` or `preferred_username` is missing.
	ErrIdentity = errors.New("token without an identity")
	// ErrUnknownHuman: the chat has no account of exactly that name.
	ErrUnknownHuman = errors.New("no chat account of that name")
	// ErrNotHuman: the chat account is a bot or deactivated.
	ErrNotHuman = errors.New("chat account is not an active human")
	// ErrGuest: the chat account is a guest, and guests own no agents.
	ErrGuest = errors.New("chat account is a guest")
	// ErrSubjectMismatch: the chat account logs in over OpenID Connect with
	// another `sub`.
	ErrSubjectMismatch = errors.New("chat account belongs to another subject")
	// ErrDirectory: the chat could not be asked, or did not say how the
	// account logs in; try again later.
	ErrDirectory = errors.New("chat lookup failed")
)

// Identity is the human a valid token stands for.
type Identity struct {
	// Owner is `preferred_username`, which is the chat username.
	Owner string
	// Subject is the token's `sub`.
	Subject string
	// Approver is true when the token lists the approvers' group. It is the
	// upper bound only: the session's trusted list selects within it.
	Approver bool
	// Expires is the token's `exp`. This service checks a token only up to
	// then; it learns of no revocation before it.
	Expires time.Time
}

// Verifier checks tokens. It is safe for concurrent use.
type Verifier struct {
	cfg    Config
	dir    Directory
	keys   keyfunc.Keyfunc
	parser *jwt.Parser
}

// New returns a verifier; ctx ends its JWKS refresh in the background.
func New(ctx context.Context, cfg Config, dir Directory) (*Verifier, error) {
	if dir == nil {
		return nil, errors.New("token: no Directory")
	}
	cfg, err := cfg.withDefaults()
	if err != nil {
		return nil, errors.Join(errors.New("token"), err)
	}
	keys, err := newKeys(ctx, cfg)
	if err != nil {
		return nil, errors.Join(errors.New("token: JWKS"), err)
	}
	parser := jwt.NewParser(
		jwt.WithValidMethods(cfg.Algorithms),
		jwt.WithIssuer(cfg.Issuer),
		jwt.WithAudience(cfg.Audience),
		jwt.WithExpirationRequired(),
		jwt.WithIssuedAt(),
		jwt.WithLeeway(cfg.Leeway),
	)
	return &Verifier{cfg: cfg, dir: dir, keys: keys, parser: parser}, nil
}

// Verify checks raw and returns the human it stands for.
func (v *Verifier) Verify(ctx context.Context, raw string) (Identity, error) {
	if len(raw) > MaxTokenBytes {
		return Identity{}, ErrInvalid
	}
	claims := jwt.MapClaims{}
	if _, err := v.parser.ParseWithClaims(raw, claims, v.keyfunc(ctx)); err != nil {
		return Identity{}, classify(err, claims)
	}
	if typ, _ := claims["typ"].(string); typ != "Bearer" {
		return Identity{}, ErrTokenType
	}
	if azp, _ := claims["azp"].(string); azp != v.cfg.ClientID {
		return Identity{}, ErrAudience
	}
	sub, _ := claims["sub"].(string)
	owner, _ := claims["preferred_username"].(string)
	if sub == "" || owner == "" {
		return Identity{}, ErrIdentity
	}
	if err := v.checkChat(ctx, owner, sub); err != nil {
		return Identity{}, err
	}
	exp, _ := claims.GetExpirationTime() // required and checked by the parser
	return Identity{
		Owner:    owner,
		Subject:  sub,
		Approver: v.inApproverGroup(claims),
		Expires:  exp.Time,
	}, nil
}

// keyfunc demands a key id: without one, the JWT library would try every key
// of the realm.
func (v *Verifier) keyfunc(ctx context.Context) jwt.Keyfunc {
	keys := v.keys.KeyfuncCtx(ctx)
	return func(t *jwt.Token) (any, error) {
		if kid, _ := t.Header["kid"].(string); kid == "" {
			return nil, ErrInvalid
		}
		return keys(t)
	}
}

// checkChat: the owner is an active human account of exactly that name, not a
// guest, and, when that account logs in over OpenID Connect, with the same
// `sub`. A chat that logs its humans in otherwise has no `sub` to compare; the
// name alone binds then.
func (v *Verifier) checkChat(ctx context.Context, owner, sub string) error {
	u, err := v.dir.User(ctx, owner)
	switch {
	case errors.Is(err, ErrNoSuchUser):
		return ErrUnknownHuman
	case err != nil:
		return ErrDirectory
	}
	switch {
	case u.Username != owner:
		return ErrUnknownHuman
	case u.Bot || u.Deactivated:
		return ErrNotHuman
	case u.Guest:
		return ErrGuest
	case !u.AuthKnown:
		return ErrDirectory
	case u.AuthService == OpenIDService && u.AuthData != sub:
		return ErrSubjectMismatch
	}
	return nil
}

func (v *Verifier) inApproverGroup(claims jwt.MapClaims) bool {
	if v.cfg.ApproverGroup == "" {
		return false
	}
	groups, _ := claims[v.cfg.ApproverClaim].([]any)
	for _, g := range groups {
		if s, ok := g.(string); ok && s == v.cfg.ApproverGroup {
			return true
		}
	}
	return false
}

// classify maps the JWT library's error onto this package's reasons. The
// library reports every missing required claim alike, so the claims it
// decoded tell which one.
func classify(err error, claims jwt.MapClaims) error {
	switch {
	case errors.Is(err, jwt.ErrTokenRequiredClaimMissing):
		switch {
		case empty(claims["exp"]):
			return ErrExpired
		case empty(claims["iss"]):
			return ErrIssuer
		case empty(claims["aud"]):
			return ErrAudience
		}
		return ErrInvalid
	case errors.Is(err, jwt.ErrTokenExpired):
		return ErrExpired
	case errors.Is(err, jwt.ErrTokenInvalidIssuer):
		return ErrIssuer
	case errors.Is(err, jwt.ErrTokenInvalidAudience):
		return ErrAudience
	default:
		return ErrInvalid
	}
}

// empty: a claim that is missing, null, "" or [] (the library takes each for
// missing).
func empty(v any) bool {
	switch x := v.(type) {
	case nil:
		return true
	case string:
		return x == ""
	case []any:
		return len(x) == 0
	}
	return false
}
