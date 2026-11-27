// Package testrealm is a Keycloak stand-in for tests, on the wire only: a
// real JWKS endpoint over httptest that serves the public half of an RSA key
// made in the test, and real access tokens signed with it by the JWT library.
package testrealm

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"math/big"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

// The bus's client and the approvers' group of the test realm.
const (
	Client = "agent-bus"
	Group  = "bus-approvers"
)

// Realm serves one signing key "k1".
type Realm struct {
	t   *testing.T
	srv *httptest.Server
	key *rsa.PrivateKey
}

// New starts a realm for the test.
func New(t *testing.T) *Realm {
	t.Helper()
	k, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	r := &Realm{t: t, key: k}
	b64 := base64.RawURLEncoding.EncodeToString
	r.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"keys": []map[string]string{{
			"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "k1",
			"n": b64(k.N.Bytes()), "e": b64(big.NewInt(int64(k.E)).Bytes()),
		}}})
	}))
	t.Cleanup(r.srv.Close)
	return r
}

// Config is the token check's configuration for this realm.
func (r *Realm) Config() token.Config {
	return token.Config{Issuer: r.srv.URL + "/realms/test", JWKSURL: r.srv.URL + "/certs",
		ClientID: Client, ApproverGroup: Group}
}

// Verifier is a token check against this realm and dir.
func (r *Realm) Verifier(dir token.Directory) *token.Verifier {
	r.t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	r.t.Cleanup(cancel)
	v, err := token.New(ctx, r.Config(), dir)
	if err != nil {
		r.t.Fatal(err)
	}
	return v
}

// Token is an access token of owner (its `sub` is "sub-<owner>"), edited by
// edit when not nil.
func (r *Realm) Token(owner string, edit func(jwt.MapClaims)) string {
	r.t.Helper()
	now := time.Now()
	c := jwt.MapClaims{
		"iss": r.srv.URL + "/realms/test", "aud": []string{Client, "account"}, "azp": Client,
		"typ": "Bearer", "sub": "sub-" + owner, "preferred_username": owner,
		"iat": now.Unix(), "exp": now.Add(5 * time.Minute).Unix(), "groups": []string{Group},
	}
	if edit != nil {
		edit(c)
	}
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, c)
	tok.Header["kid"] = "k1"
	s, err := tok.SignedString(r.key)
	if err != nil {
		r.t.Fatal(err)
	}
	return s
}

// Chat is a token.Directory and a channel directory: humans by name, and
// the channels each is a member of.
type Chat struct {
	Users    map[string]token.User
	Channels map[string][]string // username → channels
}

// NewChat has the given humans, each logging in over OpenID with
// "sub-<name>".
func NewChat(names ...string) *Chat {
	c := &Chat{Users: map[string]token.User{}, Channels: map[string][]string{}}
	for _, n := range names {
		c.Users[n] = token.User{Username: n, ID: ChatID(n), AuthKnown: true, AuthService: token.OpenIDService, AuthData: "sub-" + n}
	}
	return c
}

// User implements token.Directory.
func (c *Chat) User(_ context.Context, name string) (token.User, error) {
	if u, ok := c.Users[name]; ok {
		return u, nil
	}
	return token.User{}, token.ErrNoSuchUser
}

// UserByID implements callout.ChatAccounts: the human whose chat id is id.
func (c *Chat) UserByID(_ context.Context, id string) (token.User, error) {
	for _, u := range c.Users {
		if u.ID == id {
			return u, nil
		}
	}
	return token.User{}, token.ErrNoSuchUser
}

// Memberships implements callout.Channels.
func (c *Chat) Memberships(_ context.Context, name string, channels []string) (map[string]bool, error) {
	out := map[string]bool{}
	for _, want := range channels {
		for _, ch := range c.Channels[name] {
			if ch == want {
				out[want] = true
			}
		}
	}
	return out, nil
}

// ChatID is the chat id NewChat gives a human: 26 characters of [a-z0-9], as
// Mattermost's user ids are.
func ChatID(name string) string {
	h := sha256.Sum256([]byte(name))
	return hex.EncodeToString(h[:])[:26]
}
