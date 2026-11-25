package token

import (
	"context"
	"slices"

	"github.com/golang-jwt/jwt/v5"
)

// The console's path (RT-6.43, the addition of 2026-11-25 to ADR-015): a
// person's access token from the console's own Keycloak client, checked
// against the realm like an agent's, but without the chat: a person's
// credential is named by the directory's anchor, never by a chat account.

// Client is the token's `azp`, read WITHOUT checking anything: it only
// chooses which path checks the token in full, an agent's or a person's.
// Malformed or without `azp`: "".
func Client(raw string) string {
	if len(raw) > MaxTokenBytes {
		return ""
	}
	claims := jwt.MapClaims{}
	if _, _, err := jwt.NewParser().ParseUnverified(raw, claims); err != nil {
		return ""
	}
	azp, _ := claims["azp"].(string)
	return azp
}

// VerifyRealm checks raw as Verify does (signature by the realm's JWKS,
// issuer, `exp`, `aud` naming Config.Audience, `azp` = Config.ClientID, an
// access token, no `aud` naming one of Config.OtherClients) and returns the identity
// with its `sub` and groups. It asks no chat: Owner is the token's
// preferred_username, for the log only, and ChatOpenID stays false.
func (v *Verifier) VerifyRealm(ctx context.Context, raw string) (Identity, error) {
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
	if azp, _ := claims["azp"].(string); azp != v.cfg.ClientID || v.namesOther(claims) {
		return Identity{}, ErrAudience
	}
	sub, _ := claims["sub"].(string)
	if sub == "" {
		return Identity{}, ErrIdentity
	}
	name, _ := claims["preferred_username"].(string)
	exp, _ := claims.GetExpirationTime() // required and checked by the parser
	return Identity{Owner: name, Subject: sub, Groups: v.groups(claims), Expires: exp.Time, ID: jti(claims)}, nil
}

// namesOther: the token's `aud` names the other path's client.
func (v *Verifier) namesOther(claims jwt.MapClaims) bool {
	aud, _ := claims.GetAudience()
	for _, o := range v.cfg.OtherClients {
		if o != "" && slices.Contains(aud, o) {
			return true
		}
	}
	return false
}
