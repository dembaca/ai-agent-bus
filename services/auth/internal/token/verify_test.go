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
	group    = "bus-approvers"
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
	noAlg   bool                       // serve the keys without "alg", as some realms do
	use     string                     // "use" of the served keys: "sig" unless set, "-" for none
	delay   time.Duration              // answer time of the JWKS endpoint
	down    atomic.Bool                // answer 503
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
	delay, noAlg, use := r.delay, r.noAlg, r.use
	b64 := base64.RawURLEncoding.EncodeToString
	var keys []map[string]string
	for kid, k := range r.keys {
		jwk := map[string]string{
			"kty": "RSA", "use": "sig", "kid": kid,
			"n": b64(k.N.Bytes()), "e": b64(big.NewInt(int64(k.E)).Bytes()),
		}
		if !noAlg {
			jwk["alg"] = "RS256"
		}
		switch use {
		case "":
		case "-":
			delete(jwk, "use")
		default:
			jwk["use"] = use
		}
		keys = append(keys, jwk)
	}
	r.mu.Unlock()
	time.Sleep(delay)
	if r.down.Load() {
		http.Error(w, "unavailable", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"keys": keys})
}

func (r *realm) issuer() string { return r.srv.URL + "/realms/lab" }

// claims are those of a Keycloak access token for the bus's client with an
// audience mapper and a group mapper (short group names).
func (r *realm) claims() jwt.MapClaims {
	now := time.Now()
	return jwt.MapClaims{
		"iss": r.issuer(), "aud": []string{client, "account"}, "azp": client,
		"typ": "Bearer", "sub": aliceSub, "preferred_username": owner,
		"email": "someone-else@example.org",
		"iat":   now.Unix(), "exp": now.Add(5 * time.Minute).Unix(),
		"groups": []string{"staff", group},
	}
}

func signWith(t *testing.T, method jwt.SigningMethod, claims jwt.MapClaims, kid string, key *rsa.PrivateKey) string {
	t.Helper()
	tok := jwt.NewWithClaims(method, claims)
	if kid != "" {
		tok.Header["kid"] = kid
	}
	s, err := tok.SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func sign(t *testing.T, claims jwt.MapClaims, kid string, key *rsa.PrivateKey) string {
	t.Helper()
	return signWith(t, jwt.SigningMethodRS256, claims, kid, key)
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

// caseless answers like a chat that matches names regardless of case.
type caseless chat

func (c caseless) User(ctx context.Context, name string) (User, error) {
	return chat(c).User(ctx, strings.ToLower(name))
}

type brokenChat struct{}

func (brokenChat) User(context.Context, string) (User, error) {
	return User{}, errors.New("dial tcp 10.0.0.1:8065: connection refused")
}

func human(edit func(*User)) chat {
	u := User{Username: owner, AuthKnown: true, AuthService: OpenIDService, AuthData: aliceSub}
	if edit != nil {
		edit(&u)
	}
	return chat{owner: u}
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
	v := verifier(t, r, human(nil), nil)
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
	// aud as a single string, as Keycloak writes it with one audience.
	if _, err := v.Verify(context.Background(), r.token(func(c jwt.MapClaims) { c["aud"] = client })); err != nil {
		t.Fatalf("aud as a string: %v", err)
	}
}

func TestTheApproversGroupNeverGatesTheStart(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, human(nil), nil)
	for name, edit := range map[string]func(jwt.MapClaims){
		"other groups":     func(c jwt.MapClaims) { c["groups"] = []string{"staff"} },
		"no groups":        func(c jwt.MapClaims) { delete(c, "groups") },
		"not a list":       func(c jwt.MapClaims) { c["groups"] = group },
		"the group's path": func(c jwt.MapClaims) { c["groups"] = []string{"/" + group} },
	} {
		id, err := v.Verify(context.Background(), r.token(edit))
		if err != nil || id.Approver {
			t.Errorf("%s: %+v, %v", name, id, err)
		}
	}
	// Without a configured group nobody is an approver.
	v = verifier(t, r, human(nil), func(c *Config) { c.ApproverGroup = "" })
	if id, err := v.Verify(context.Background(), r.token(nil)); err != nil || id.Approver {
		t.Fatalf("%+v, %v", id, err)
	}
	// The claim's name is configurable.
	v = verifier(t, r, human(nil), func(c *Config) { c.ApproverClaim = "bus_groups" })
	tok := r.token(func(c jwt.MapClaims) { c["bus_groups"] = []string{group}; delete(c, "groups") })
	if id, err := v.Verify(context.Background(), tok); err != nil || !id.Approver {
		t.Fatalf("%+v, %v", id, err)
	}
}

func TestRefusedTokens(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, human(nil), nil)
	stranger := newKey(t)
	edited := func(edit func(jwt.MapClaims)) func() string {
		return func() string { return r.token(edit) }
	}
	cases := []struct {
		name string
		tok  func() string
		want error
	}{
		{"aud without the bus", edited(func(c jwt.MapClaims) { c["aud"] = "account" }), ErrAudience},
		{"no aud", edited(func(c jwt.MapClaims) { delete(c, "aud") }), ErrAudience},
		{"azp another client", edited(func(c jwt.MapClaims) { c["azp"] = "other-client" }), ErrAudience},
		{"no azp", edited(func(c jwt.MapClaims) { delete(c, "azp") }), ErrAudience},
		{"expired beyond the leeway", edited(func(c jwt.MapClaims) { c["exp"] = time.Now().Add(-31 * time.Second).Unix() }), ErrExpired},
		{"no exp", edited(func(c jwt.MapClaims) { delete(c, "exp") }), ErrExpired},
		{"not yet valid", edited(func(c jwt.MapClaims) { c["nbf"] = time.Now().Add(time.Minute).Unix() }), ErrInvalid},
		{"issued in the future", edited(func(c jwt.MapClaims) { c["iat"] = time.Now().Add(time.Minute).Unix() }), ErrInvalid},
		{"another issuer", edited(func(c jwt.MapClaims) { c["iss"] = r.srv.URL + "/realms/other" }), ErrIssuer},
		{"the issuer with a trailing slash", edited(func(c jwt.MapClaims) { c["iss"] = r.issuer() + "/" }), ErrIssuer},
		{"no iss", edited(func(c jwt.MapClaims) { delete(c, "iss") }), ErrIssuer},
		{"iss empty", edited(func(c jwt.MapClaims) { c["iss"] = "" }), ErrIssuer},
		{"aud an empty list", edited(func(c jwt.MapClaims) { c["aud"] = []string{} }), ErrAudience},
		{"aud empty", edited(func(c jwt.MapClaims) { c["aud"] = "" }), ErrAudience},
		{"a stranger's key under a known kid", func() string { return sign(t, r.claims(), "k1", stranger) }, ErrInvalid},
		{"a key the realm does not publish", func() string { return sign(t, r.claims(), "k9", stranger) }, ErrInvalid},
		{"no kid", func() string { return sign(t, r.claims(), "", r.key("k1")) }, ErrInvalid},
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
		{"refresh token", edited(func(c jwt.MapClaims) { c["typ"] = "Refresh" }), ErrTokenType},
		{"ID token", edited(func(c jwt.MapClaims) { c["typ"] = "ID"; c["aud"] = client }), ErrTokenType},
		{"no typ", edited(func(c jwt.MapClaims) { delete(c, "typ") }), ErrTokenType},
		{"typ in lower case", edited(func(c jwt.MapClaims) { c["typ"] = "bearer" }), ErrTokenType},
		{"no sub", edited(func(c jwt.MapClaims) { delete(c, "sub") }), ErrIdentity},
		{"no preferred_username, only an email", edited(func(c jwt.MapClaims) { delete(c, "preferred_username") }), ErrIdentity},
		{"not a JWT", func() string { return "not.a.jwt" }, ErrInvalid},
		{"too long", func() string { return r.token(func(c jwt.MapClaims) { c["pad"] = strings.Repeat("x", MaxTokenBytes) }) }, ErrInvalid},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			id, err := v.Verify(context.Background(), tc.tok())
			if err != tc.want {
				t.Fatalf("got %+v, %v; want exactly %v", id, err, tc.want)
			}
		})
	}
}

// Without "alg" in the JWKS only the configured methods guard the signing
// method: a token signed with the realm's own key under RS384 or PS256 is
// refused when only RS256 is configured, and accepted when it is configured.
func TestOnlyTheConfiguredMethodsPass(t *testing.T) {
	r := newRealm(t)
	r.noAlg = true
	v := verifier(t, r, human(nil), nil)
	for _, m := range []jwt.SigningMethod{jwt.SigningMethodRS384, jwt.SigningMethodPS256} {
		tok := signWith(t, m, r.claims(), "k1", r.key("k1"))
		if _, err := v.Verify(context.Background(), tok); err != ErrInvalid {
			t.Errorf("%s: %v", m.Alg(), err)
		}
	}
	both := verifier(t, r, human(nil), func(c *Config) { c.Algorithms = []string{"RS256", "PS256"} })
	if _, err := both.Verify(context.Background(), signWith(t, jwt.SigningMethodPS256, r.claims(), "k1", r.key("k1"))); err != nil {
		t.Fatalf("PS256 configured: %v", err)
	}
}

// Only signing keys count: a key for encryption, or one that says nothing of
// its use, is never taken, even without "alg" in the JWKS.
func TestOnlySigningKeysCount(t *testing.T) {
	for _, use := range []string{"enc", "-"} {
		r := newRealm(t)
		r.noAlg, r.use = true, use
		v := verifier(t, r, human(nil), nil)
		if _, err := v.Verify(context.Background(), r.token(nil)); err != ErrInvalid {
			t.Errorf("use %q: %v", use, err)
		}
	}
}

func TestTheLeewayCoversSmallClockSkew(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, human(nil), nil)
	tok := r.token(func(c jwt.MapClaims) {
		c["exp"] = time.Now().Add(-20 * time.Second).Unix()
		c["nbf"] = time.Now().Add(20 * time.Second).Unix()
		c["iat"] = time.Now().Add(20 * time.Second).Unix()
	})
	if _, err := v.Verify(context.Background(), tok); err != nil {
		t.Fatalf("within 30 s: %v", err)
	}
	strict := verifier(t, r, human(nil), func(c *Config) { c.Leeway = -1 })
	if _, err := strict.Verify(context.Background(), tok); err != ErrExpired {
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
		{"chat logs in over OpenID with the same sub", human(nil), nil},
		{"chat logs in with a password: the name binds", human(func(u *User) { u.AuthService, u.AuthData = "", "" }), nil},
		{"chat logs in over LDAP: the name binds", human(func(u *User) { u.AuthService, u.AuthData = "ldap", "uid=alice" }), nil},
		{"chat logs in over OpenID with another sub", human(func(u *User) { u.AuthData = "another-sub" }), ErrSubjectMismatch},
		{"chat logs in over OpenID without a sub", human(func(u *User) { u.AuthData = "" }), ErrSubjectMismatch},
		{"the lookup could not see how the account logs in",
			human(func(u *User) { u.AuthKnown, u.AuthService, u.AuthData = false, "", "" }), ErrDirectory},
		{"no such account", chat{"bob": {Username: "bob", AuthKnown: true}}, ErrUnknownHuman},
		{"a directory answering with another name", human(func(u *User) { u.Username = "mallory" }), ErrUnknownHuman},
		{"a bot of that name", human(func(u *User) { u.Bot = true }), ErrNotHuman},
		{"a deactivated account", human(func(u *User) { u.Deactivated = true }), ErrNotHuman},
		{"a guest account", human(func(u *User) { u.Guest = true }), ErrGuest},
		{"the chat cannot be asked", brokenChat{}, ErrDirectory},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			v := verifier(t, r, tc.dir, nil)
			_, err := v.Verify(context.Background(), r.token(nil))
			if err != tc.want {
				t.Fatalf("got %v; want exactly %v", err, tc.want)
			}
		})
	}
	// A name in other case is another name, even for a chat that ignores case.
	v := verifier(t, r, caseless(human(nil)), nil)
	if _, err := v.Verify(context.Background(), r.token(func(c jwt.MapClaims) { c["preferred_username"] = "Alice" })); err != ErrUnknownHuman {
		t.Fatalf("Alice: %v", err)
	}
}

func TestAKeyRotationIsPickedUpFromASlowRealm(t *testing.T) {
	r := newRealm(t)
	r.delay = 50 * time.Millisecond
	v := verifier(t, r, human(nil), nil)
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
		if _, err := v.Verify(context.Background(), sign(t, r.claims(), "k9", stranger)); err != ErrInvalid {
			t.Fatalf("unknown key: %v", err)
		}
	}
	if r.fetches.Load() != before+1 {
		t.Fatalf("fetches %d after unknown keys, want %d", r.fetches.Load(), before+1)
	}
	if time.Since(start) > time.Second {
		t.Fatalf("unknown keys took %s", time.Since(start))
	}
}

func TestARealmDownAtTheStartIsFetchedOnTheFirstToken(t *testing.T) {
	r := newRealm(t)
	r.delay = 50 * time.Millisecond
	r.down.Store(true)
	var failed atomic.Int32
	v := verifier(t, r, human(nil), func(c *Config) { c.KeyFetchError = func(error) { failed.Add(1) } })
	if failed.Load() == 0 {
		t.Fatal("the failed first fetch was not reported")
	}
	r.down.Store(false)
	if _, err := v.Verify(context.Background(), r.token(nil)); err != nil {
		t.Fatalf("first token after the realm came back: %v", err)
	}
}

func TestTheJWKSFetchFollowsNoRedirectAndReadsNoMegabytes(t *testing.T) {
	r := newRealm(t)
	moved := httptest.NewServer(http.RedirectHandler(r.srv.URL+"/certs", http.StatusFound))
	t.Cleanup(moved.Close)
	huge := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"keys":[],"pad":"` + strings.Repeat("x", maxJWKSBytes) + `"}`))
	}))
	t.Cleanup(huge.Close)
	for name, url := range map[string]string{"redirect": moved.URL, "oversize": huge.URL} {
		var errs []error
		var mu sync.Mutex
		v := verifier(t, r, human(nil), func(c *Config) {
			c.JWKSURL = url
			c.KeyFetchError = func(err error) { mu.Lock(); errs = append(errs, err); mu.Unlock() }
		})
		if _, err := v.Verify(context.Background(), r.token(nil)); err != ErrInvalid {
			t.Errorf("%s: %v", name, err)
		}
		mu.Lock()
		if len(errs) == 0 {
			t.Errorf("%s: no fetch error reported", name)
		}
		mu.Unlock()
	}
}

func TestErrorsCarryNoTokenNoKeyIdAndNoChatError(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, human(nil), nil)
	kid := "kid-chosen-by-the-attacker"
	for _, tok := range []string{
		r.token(func(c jwt.MapClaims) { c["aud"] = "account" }),
		r.token(func(c jwt.MapClaims) { c["exp"] = time.Now().Add(-time.Hour).Unix() }),
		sign(t, r.claims(), kid, newKey(t)),
	} {
		_, err := v.Verify(context.Background(), tok)
		if err == nil {
			t.Fatal("accepted")
		}
		for _, part := range []string{tok[:40], kid} {
			if strings.Contains(err.Error(), part) {
				t.Fatalf("error %q carries %q", err, part)
			}
		}
	}
	v = verifier(t, r, brokenChat{}, nil)
	if _, err := v.Verify(context.Background(), r.token(nil)); err == nil || strings.Contains(err.Error(), "10.0.0.1") {
		t.Fatalf("chat error: %v", err)
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
		"JWKS URL with user info":  func(c *Config) { c.JWKSURL = "https://user:pw@kc.example/certs" },
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
