package token

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"strings"
	"time"
)

// Defaults and bounds of the configuration.
const (
	DefaultLeeway            = 30 * time.Second
	MaxLeeway                = 60 * time.Second
	DefaultApproverClaim     = "groups"
	DefaultUnknownKeyRefresh = time.Minute
	MinUnknownKeyRefresh     = time.Second
	DefaultKeyRefresh        = time.Hour
)

// asymmetric is every signing method a JWKS can serve a public key for.
// Symmetric methods (HS*) and "none" are never accepted: a public JWKS cannot
// hold a shared secret, and an HS256 token keyed with the public key is the
// classic algorithm-confusion attack.
var asymmetric = map[string]bool{
	"RS256": true, "RS384": true, "RS512": true,
	"PS256": true, "PS384": true, "PS512": true,
	"ES256": true, "ES384": true, "ES512": true,
	"EdDSA": true,
}

// Config is what the verifier needs to know about the Keycloak realm and the
// bus's client. Nothing in it is secret: the client is public (device flow)
// and the keys come from the realm's JWKS.
type Config struct {
	// Issuer is the realm's issuer URL, the token's `iss` exactly.
	Issuer string
	// JWKSURL is where the realm publishes its signing keys.
	JWKSURL string
	// ClientID is the bus's client: the token's `azp` must name it.
	ClientID string
	// Audience must be in the token's `aud`. Keycloak puts a public client
	// there only with an audience mapper on that client. Empty: ClientID.
	Audience string
	// Algorithms are the accepted signing methods. Empty: RS256.
	Algorithms []string
	// Leeway is the allowed clock skew for `exp`, `nbf` and `iat`, at most
	// MaxLeeway. Zero means DefaultLeeway; a negative value means none.
	Leeway time.Duration
	// ApproverClaim is the claim that lists the user's groups, filled by a
	// group mapper on the client. Empty: DefaultApproverClaim.
	ApproverClaim string
	// ApproverGroup is the group whose members may approve actions, as the
	// group mapper writes it (with or without the leading "/"). Empty: nobody
	// is an approver.
	ApproverGroup string
	// UnknownKeyRefresh is how often at most a token with an unknown key id
	// makes the verifier fetch the JWKS again. Zero: DefaultUnknownKeyRefresh.
	UnknownKeyRefresh time.Duration
	// KeyRefresh is how often the JWKS is fetched in any case. Zero:
	// DefaultKeyRefresh.
	KeyRefresh time.Duration
	// KeyFetchError, if set, hears of every JWKS fetch that failed, for the
	// caller to log. The package itself never logs. It is called from the
	// background refresh and from Verify at once, so it must be safe for
	// concurrent use.
	KeyFetchError func(error)
	// Binding decides whether `sub` may stand for the chat account of that
	// name, after the chat lookup. Nil: Open.
	Binding NameBinding
	// OtherClient is the other path's client (RT-6.43, ADR-015 addition
	// point 2): a token whose `aud` names it is refused, so a token that
	// names both the bus's client and the console's gets neither credential.
	// Empty: no such check.
	OtherClient string
}

// withDefaults fills the defaults in and checks every value. It returns the
// first invalid value as an error that names the field.
func (c Config) withDefaults() (Config, error) {
	if err := checkURL(c.Issuer); err != nil {
		return Config{}, fmt.Errorf("Issuer: %w", err)
	}
	if err := checkURL(c.JWKSURL); err != nil {
		return Config{}, fmt.Errorf("JWKSURL: %w", err)
	}
	if strings.TrimSpace(c.ClientID) == "" {
		return Config{}, fmt.Errorf("ClientID: must not be empty")
	}
	if c.Audience == "" {
		c.Audience = c.ClientID
	}
	if len(c.Algorithms) == 0 {
		c.Algorithms = []string{"RS256"}
	}
	for _, a := range c.Algorithms {
		if !asymmetric[a] {
			return Config{}, fmt.Errorf("Algorithms: %q is not an asymmetric signing method", a)
		}
	}
	switch {
	case c.Leeway == 0:
		c.Leeway = DefaultLeeway
	case c.Leeway < 0:
		c.Leeway = 0
	case c.Leeway > MaxLeeway:
		return Config{}, fmt.Errorf("Leeway: %s is more than %s", c.Leeway, MaxLeeway)
	}
	if c.ApproverClaim == "" {
		c.ApproverClaim = DefaultApproverClaim
	}
	switch {
	case c.UnknownKeyRefresh == 0:
		c.UnknownKeyRefresh = DefaultUnknownKeyRefresh
	case c.UnknownKeyRefresh < MinUnknownKeyRefresh:
		return Config{}, fmt.Errorf("UnknownKeyRefresh: %s is less than %s", c.UnknownKeyRefresh, MinUnknownKeyRefresh)
	}
	if c.Binding == nil {
		c.Binding = Open{}
	}
	if c.KeyRefresh <= 0 {
		c.KeyRefresh = DefaultKeyRefresh
	}
	return c, nil
}

// checkURL accepts https, and http only on a loopback host (tests, a local
// Keycloak).
func checkURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Host == "" {
		return fmt.Errorf("%q is not an absolute URL", raw)
	}
	if u.User != nil {
		// Credentials in a URL end up in error texts and logs.
		return errors.New("a URL with user information is refused")
	}
	switch u.Scheme {
	case "https":
		return nil
	case "http":
		host := u.Hostname()
		if host == "localhost" {
			return nil
		}
		if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
			return nil
		}
		return fmt.Errorf("%q: http only on a loopback host", raw)
	default:
		return fmt.Errorf("%q: scheme must be https", raw)
	}
}
