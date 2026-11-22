// Package token checks the Keycloak access token an agent presents when it
// connects (ADR-014, point 6): signature and lifetime against the realm's
// JWKS, `aud` and `azp` for the bus's client, the owner from
// `preferred_username` bound to `sub`, that owner checked against the chat,
// and membership in the approvers' group.
//
// The package never logs and never puts the token into an error.
package token

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/MicahParks/keyfunc/v3"
	"github.com/golang-jwt/jwt/v5"
	"golang.org/x/time/rate"
)

// Why a token was refused. Every error from Verify wraps exactly one of them.
var (
	// ErrInvalid: malformed, unsigned, a refused signing method, a bad
	// signature, an unknown key, or a token that is not yet valid.
	ErrInvalid = errors.New("token invalid")
	// ErrExpired: `exp` has passed (beyond the leeway) or is missing.
	ErrExpired = errors.New("token expired")
	// ErrIssuer: `iss` is not the realm.
	ErrIssuer = errors.New("token from another issuer")
	// ErrAudience: `aud` lacks the bus's audience or `azp` is another client.
	ErrAudience = errors.New("token for another client")
	// ErrTokenType: not an access token (`typ` is not "Bearer").
	ErrTokenType = errors.New("not an access token")
	// ErrIdentity: `sub` or `preferred_username` is missing.
	ErrIdentity = errors.New("token without an identity")
	// ErrUnknownHuman: the chat has no account of that name.
	ErrUnknownHuman = errors.New("no chat account of that name")
	// ErrNotHuman: the chat account is a bot or deactivated.
	ErrNotHuman = errors.New("chat account is not an active human")
	// ErrSubjectMismatch: the chat account logs in over OpenID Connect with
	// another `sub`.
	ErrSubjectMismatch = errors.New("chat account belongs to another subject")
	// ErrDirectory: the chat could not be asked; try again later.
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
	// Expires is the token's `exp`.
	Expires time.Time
}

// Verifier checks tokens. It is safe for concurrent use.
type Verifier struct {
	cfg    Config
	dir    Directory
	keys   keyfunc.Keyfunc
	parser *jwt.Parser
}

// New fetches the realm's JWKS and returns a verifier. ctx ends the JWKS
// refresh in the background. A JWKS that cannot be fetched at the start is no
// error: the verifier tries again on the first token.
func New(ctx context.Context, cfg Config, dir Directory) (*Verifier, error) {
	if dir == nil {
		return nil, errors.New("token: no Directory")
	}
	cfg, err := cfg.withDefaults()
	if err != nil {
		return nil, fmt.Errorf("token: %w", err)
	}
	keys, err := keyfunc.NewDefaultOverrideCtx(ctx, []string{cfg.JWKSURL}, keyfunc.Override{
		RefreshInterval:   cfg.KeyRefresh,
		RefreshUnknownKID: rate.NewLimiter(rate.Every(cfg.UnknownKeyRefresh), 1),
		// A token with an unknown key waits for no refresh slot: it is refused
		// at once rather than holding the caller up to a minute.
		RateLimitWaitMax: time.Millisecond,
		HTTPTimeout:      10 * time.Second,
	})
	if err != nil {
		return nil, fmt.Errorf("token: JWKS: %w", err)
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
	claims := jwt.MapClaims{}
	if _, err := v.parser.ParseWithClaims(raw, claims, v.keys.KeyfuncCtx(ctx)); err != nil {
		return Identity{}, classify(err)
	}
	if typ, _ := claims["typ"].(string); typ != "Bearer" {
		return Identity{}, ErrTokenType
	}
	if azp, _ := claims["azp"].(string); azp != v.cfg.ClientID {
		return Identity{}, fmt.Errorf("%w: azp", ErrAudience)
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

// checkChat: the owner is an active human account in the chat, and, when that
// account logs in over OpenID Connect, the same `sub`. A chat that logs its
// humans in otherwise has no `sub` to compare; the name is the binding then.
func (v *Verifier) checkChat(ctx context.Context, owner, sub string) error {
	u, err := v.dir.User(ctx, owner)
	switch {
	case errors.Is(err, ErrNoSuchUser):
		return ErrUnknownHuman
	case err != nil:
		return fmt.Errorf("%w: %w", ErrDirectory, err)
	}
	if u.Username != owner {
		return ErrUnknownHuman
	}
	if u.Bot || u.Deactivated {
		return ErrNotHuman
	}
	if u.AuthService == OpenIDService && u.AuthData != sub {
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

// classify maps the JWT library's errors onto this package's.
func classify(err error) error {
	switch {
	case errors.Is(err, jwt.ErrTokenExpired), errors.Is(err, jwt.ErrTokenRequiredClaimMissing):
		return fmt.Errorf("%w: %w", ErrExpired, err)
	case errors.Is(err, jwt.ErrTokenInvalidIssuer):
		return ErrIssuer
	case errors.Is(err, jwt.ErrTokenInvalidAudience):
		return fmt.Errorf("%w: aud", ErrAudience)
	default:
		return fmt.Errorf("%w: %w", ErrInvalid, err)
	}
}
