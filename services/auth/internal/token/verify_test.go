package token

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

const (
	client   = "agent-bus"
	group    = "/bus-approvers"
	owner    = "alice"
	aliceSub = "6f1c2a3e-0000-4000-8000-000000000001"
)

// realm is a Keycloak stand-in on the wire only: a real JWKS endpoint over
// httptest, serving the public halves of keys made in the test. Tokens are
// real JWTs signed with those keys by the JWT library itself.
type realm struct {
	t       *testing.T
	srv     *httptest.Server
	mu      sync.Mutex
	keys    map[string]*rsa.PrivateKey // kid → key, what the JWKS serves
	fetches atomic.Int32
}

func newRealm(t *testing.T) *realm {
	t.Helper()
	r := &realm{t: t, keys: map[string]*rsa.PrivateKey{}}
	r.addKey("k1")
	r.srv = httptest.NewServer(http.HandlerFunc(r.serveJWKS))
	t.Cleanup(r.srv.Close)
	return r
}

func newKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	k, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return k
}

func (r *realm) addKey(kid string) *rsa.PrivateKey {
	k := newKey(r.t)
	r.mu.Lock()
	defer r.mu.Unlock()
	r.keys[kid] = k
	return k
}

func (r *realm) key(kid string) *rsa.PrivateKey {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.keys[kid]
}

func (r *realm) serveJWKS(w http.ResponseWriter, _ *http.Request) {
	r.fetches.Add(1)
	r.mu.Lock()
	defer r.mu.Unlock()
	b64 := base64.RawURLEncoding.EncodeToString
	var keys []map[string]string
	for kid, k := range r.keys {
		keys = append(keys, map[string]string{
			"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
			"n": b64(k.N.Bytes()), "e": b64(big.NewInt(int64(k.E)).Bytes()),
		})
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"keys": keys})
}

func (r *realm) issuer() string { return r.srv.URL + "/realms/lab" }

// claims are those of a Keycloak access token for the bus's client with an
// audience mapper and a group mapper.
func (r *realm) claims() jwt.MapClaims {
	now := time.Now()
	return jwt.MapClaims{
		"iss": r.issuer(), "aud": []string{client, "account"}, "azp": client,
		"typ": "Bearer", "sub": aliceSub, "preferred_username": owner,
		"email": "someone-else@example.org",
		"iat":   now.Unix(), "exp": now.Add(5 * time.Minute).Unix(),
		"groups": []string{"/staff", group},
	}
}

func sign(t *testing.T, claims jwt.MapClaims, kid string, key *rsa.PrivateKey) string {
	t.Helper()
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	tok.Header["kid"] = kid
	s, err := tok.SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func (r *realm) token(edit func(jwt.MapClaims)) string {
	c := r.claims()
	if edit != nil {
		edit(c)
	}
	return sign(r.t, c, "k1", r.key("k1"))
}

// chat is the Directory: a map of accounts, as Mattermost would answer.
type chat map[string]User

func (c chat) User(_ context.Context, name string) (User, error) {
	if u, ok := c[name]; ok {
		return u, nil
	}
	return User{}, ErrNoSuchUser
}

type brokenChat struct{}

func (brokenChat) User(context.Context, string) (User, error) {
	return User{}, errors.New("connection refused")
}

func aliceOverOpenID() chat {
	return chat{owner: {Username: owner, AuthService: OpenIDService, AuthData: aliceSub}}
}

func (r *realm) config() Config {
	return Config{Issuer: r.issuer(), JWKSURL: r.srv.URL + "/certs", ClientID: client, ApproverGroup: group}
}

func verifier(t *testing.T, r *realm, dir Directory, edit func(*Config)) *Verifier {
	t.Helper()
	cfg := r.config()
	if edit != nil {
		edit(&cfg)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	v, err := New(ctx, cfg, dir)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func TestAValidTokenNamesItsOwnerAndApprover(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, aliceOverOpenID(), nil)
	id, err := v.Verify(context.Background(), r.token(nil))
	if err != nil {
		t.Fatal(err)
	}
	if id.Owner != owner || id.Subject != aliceSub || !id.Approver {
		t.Fatalf("identity %+v", id)
	}
	if time.Until(id.Expires) <= 0 {
		t.Fatalf("expires %s", id.Expires)
	}
}

func TestTheApproversGroupNeverGatesTheStart(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, aliceOverOpenID(), nil)
	for name, edit := range map[string]func(jwt.MapClaims){
		"other groups": func(c jwt.MapClaims) { c["groups"] = []string{"/staff"} },
		"no groups":    func(c jwt.MapClaims) { delete(c, "groups") },
		"not a list":   func(c jwt.MapClaims) { c["groups"] = group },
	} {
		id, err := v.Verify(context.Background(), r.token(edit))
		if err != nil || id.Approver {
			t.Errorf("%s: %+v, %v", name, id, err)
		}
	}
	// Without a configured group nobody is an approver.
	v = verifier(t, r, aliceOverOpenID(), func(c *Config) { c.ApproverGroup = "" })
	if id, err := v.Verify(context.Background(), r.token(nil)); err != nil || id.Approver {
		t.Fatalf("%+v, %v", id, err)
	}
	// The claim's name is configurable.
	v = verifier(t, r, aliceOverOpenID(), func(c *Config) { c.ApproverClaim = "bus_groups" })
	tok := r.token(func(c jwt.MapClaims) { c["bus_groups"] = []string{group}; delete(c, "groups") })
	if id, err := v.Verify(context.Background(), tok); err != nil || !id.Approver {
		t.Fatalf("%+v, %v", id, err)
	}
}

func TestRefusedTokens(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, aliceOverOpenID(), nil)
	stranger := newKey(t)
	cases := []struct {
		name string
		tok  func() string
		want error
	}{
		{"aud without the bus", func() string {
			return r.token(func(c jwt.MapClaims) { c["aud"] = "account" })
		}, ErrAudience},
		{"azp another client", func() string {
			return r.token(func(c jwt.MapClaims) { c["azp"] = "other-client" })
		}, ErrAudience},
		{"azp missing", func() string {
			return r.token(func(c jwt.MapClaims) { delete(c, "azp") })
		}, ErrAudience},
		{"expired beyond the leeway", func() string {
			return r.token(func(c jwt.MapClaims) { c["exp"] = time.Now().Add(-31 * time.Second).Unix() })
		}, ErrExpired},
		{"no exp", func() string {
			return r.token(func(c jwt.MapClaims) { delete(c, "exp") })
		}, ErrExpired},
		{"not yet valid", func() string {
			return r.token(func(c jwt.MapClaims) { c["nbf"] = time.Now().Add(time.Minute).Unix() })
		}, ErrInvalid},
		{"issued in the future", func() string {
			return r.token(func(c jwt.MapClaims) { c["iat"] = time.Now().Add(time.Minute).Unix() })
		}, ErrInvalid},
		{"another issuer", func() string {
			return r.token(func(c jwt.MapClaims) { c["iss"] = r.srv.URL + "/realms/other" })
		}, ErrIssuer},
		{"signed with a stranger's key under a known kid", func() string {
			return sign(t, r.claims(), "k1", stranger)
		}, ErrInvalid},
		{"signed with a key the realm does not publish", func() string {
			return sign(t, r.claims(), "k9", stranger)
		}, ErrInvalid},
		{"alg none", func() string {
			tok := jwt.NewWithClaims(jwt.SigningMethodNone, r.claims())
			tok.Header["kid"] = "k1"
			s, err := tok.SignedString(jwt.UnsafeAllowNoneSignatureType)
			if err != nil {
				t.Fatal(err)
			}
			return s
		}, ErrInvalid},
		{"HS256 keyed with the public key", func() string {
			tok := jwt.NewWithClaims(jwt.SigningMethodHS256, r.claims())
			tok.Header["kid"] = "k1"
			s, err := tok.SignedString(r.key("k1").N.Bytes())
			if err != nil {
				t.Fatal(err)
			}
			return s
		}, ErrInvalid},
		{"refresh token", func() string {
			return r.token(func(c jwt.MapClaims) { c["typ"] = "Refresh" })
		}, ErrTokenType},
		{"ID token", func() string {
			return r.token(func(c jwt.MapClaims) { c["typ"] = "ID"; c["aud"] = client })
		}, ErrTokenType},
		{"no sub", func() string {
			return r.token(func(c jwt.MapClaims) { delete(c, "sub") })
		}, ErrIdentity},
		{"no preferred_username, only an email", func() string {
			return r.token(func(c jwt.MapClaims) { delete(c, "preferred_username") })
		}, ErrIdentity},
		{"not a JWT", func() string { return "not.a.jwt" }, ErrInvalid},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			id, err := v.Verify(context.Background(), tc.tok())
			if !errors.Is(err, tc.want) {
				t.Fatalf("got %+v, %v; want %v", id, err, tc.want)
			}
		})
	}
}

func TestTheLeewayCoversSmallClockSkew(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, aliceOverOpenID(), nil)
	tok := r.token(func(c jwt.MapClaims) {
		c["exp"] = time.Now().Add(-20 * time.Second).Unix()
		c["nbf"] = time.Now().Add(20 * time.Second).Unix()
		c["iat"] = time.Now().Add(20 * time.Second).Unix()
	})
	if _, err := v.Verify(context.Background(), tok); err != nil {
		t.Fatalf("within 30 s: %v", err)
	}
	strict := verifier(t, r, aliceOverOpenID(), func(c *Config) { c.Leeway = -1 })
	if _, err := strict.Verify(context.Background(), tok); !errors.Is(err, ErrExpired) {
		t.Fatalf("without leeway: %v", err)
	}
}

func TestTheOwnerIsCheckedAgainstTheChat(t *testing.T) {
	r := newRealm(t)
	cases := []struct {
		name string
		dir  Directory
		want error
	}{
		{"chat logs in over OpenID with the same sub", aliceOverOpenID(), nil},
		{"chat logs in with a password: the name binds", chat{owner: {Username: owner}}, nil},
		{"chat logs in over LDAP: the name binds", chat{owner: {Username: owner, AuthService: "ldap", AuthData: "uid=alice"}}, nil},
		{"chat logs in over OpenID with another sub",
			chat{owner: {Username: owner, AuthService: OpenIDService, AuthData: "another-sub"}}, ErrSubjectMismatch},
		{"chat logs in over OpenID without a sub",
			chat{owner: {Username: owner, AuthService: OpenIDService}}, ErrSubjectMismatch},
		{"no such account", chat{"bob": {Username: "bob"}}, ErrUnknownHuman},
		{"a directory answering with another name", chat{owner: {Username: "mallory"}}, ErrUnknownHuman},
		{"a bot of that name", chat{owner: {Username: owner, Bot: true}}, ErrNotHuman},
		{"a deactivated account", chat{owner: {Username: owner, Deactivated: true}}, ErrNotHuman},
		{"the chat cannot be asked", brokenChat{}, ErrDirectory},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			v := verifier(t, r, tc.dir, nil)
			_, err := v.Verify(context.Background(), r.token(nil))
			if tc.want == nil && err != nil || tc.want != nil && !errors.Is(err, tc.want) {
				t.Fatalf("got %v; want %v", err, tc.want)
			}
		})
	}
}

func TestAKeyRotationIsPickedUp(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, aliceOverOpenID(), nil)
	if _, err := v.Verify(context.Background(), r.token(nil)); err != nil {
		t.Fatal(err)
	}
	before := r.fetches.Load()
	k2 := r.addKey("k2")
	if _, err := v.Verify(context.Background(), sign(t, r.claims(), "k2", k2)); err != nil {
		t.Fatalf("new key: %v", err)
	}
	if r.fetches.Load() != before+1 {
		t.Fatalf("fetches %d, want %d", r.fetches.Load(), before+1)
	}
	// Unknown keys right after do not hammer the realm: refused at once,
	// without another fetch.
	stranger := newKey(t)
	start := time.Now()
	for range 5 {
		if _, err := v.Verify(context.Background(), sign(t, r.claims(), "k9", stranger)); !errors.Is(err, ErrInvalid) {
			t.Fatalf("unknown key: %v", err)
		}
	}
	if r.fetches.Load() != before+1 {
		t.Fatalf("fetches %d after unknown keys, want %d", r.fetches.Load(), before+1)
	}
	if time.Since(start) > 2*time.Second {
		t.Fatalf("unknown keys took %s", time.Since(start))
	}
}

func TestErrorsNeverCarryTheToken(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, aliceOverOpenID(), nil)
	for _, tok := range []string{
		r.token(func(c jwt.MapClaims) { c["aud"] = "account" }),
		r.token(func(c jwt.MapClaims) { c["exp"] = time.Now().Add(-time.Hour).Unix() }),
		sign(t, r.claims(), "k1", newKey(t)),
	} {
		_, err := v.Verify(context.Background(), tok)
		if err == nil {
			t.Fatal("accepted")
		}
		for _, part := range []string{tok, tok[:40]} {
			if strings.Contains(err.Error(), part) {
				t.Fatalf("error %q carries the token", err)
			}
		}
	}
}

func TestConfig(t *testing.T) {
	ok := Config{Issuer: "https://kc.example/realms/lab", JWKSURL: "https://kc.example/certs", ClientID: client}
	c, err := ok.withDefaults()
	if err != nil {
		t.Fatal(err)
	}
	if c.Audience != client || c.Leeway != DefaultLeeway || c.ApproverClaim != "groups" ||
		len(c.Algorithms) != 1 || c.Algorithms[0] != "RS256" {
		t.Fatalf("defaults %+v", c)
	}
	for name, edit := range map[string]func(*Config){
		"leeway above the cap":     func(c *Config) { c.Leeway = 61 * time.Second },
		"alg none":                 func(c *Config) { c.Algorithms = []string{"none"} },
		"symmetric alg":            func(c *Config) { c.Algorithms = []string{"RS256", "HS256"} },
		"no client":                func(c *Config) { c.ClientID = " " },
		"http issuer off loopback": func(c *Config) { c.Issuer = "http://kc.example/realms/lab" },
		"relative JWKS URL":        func(c *Config) { c.JWKSURL = "/certs" },
		"refresh too often":        func(c *Config) { c.UnknownKeyRefresh = time.Millisecond },
	} {
		bad := ok
		edit(&bad)
		if _, err := bad.withDefaults(); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := New(context.Background(), ok, nil); err == nil {
		t.Error("no Directory: accepted")
	}
}
