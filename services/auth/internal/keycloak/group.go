// Package keycloak reads the members of one group from Keycloak's admin REST
// API, for the login service's refresh of the approvers' account records
// (RT-6.42 step 3, #266). It logs in with a confidential client of its own
// (the client credentials grant) and only reads: the group by its path and
// its members, never another group, a user's details or anything to write.
// What that client may do is set in Keycloak (services/auth/README.md);
// this package asks for nothing more. Nothing here logs the secret or a
// token.
package keycloak

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Timeout bounds each request to Keycloak.
const Timeout = 10 * time.Second

// maxBody bounds an answer read from Keycloak: a member list of the cap
// (callout.MaxApprovers) in brief form is far below it.
const maxBody = 1 << 20

// jwksSuffix is the end of a realm's JWKS URL in Keycloak.
const jwksSuffix = "/protocol/openid-connect/certs"

// ErrGroup: Keycloak has no group of the configured path, or answered with
// another one.
var ErrGroup = errors.New("keycloak: no such group")

// Member is an enabled user of the group, as Keycloak lists it.
type Member struct {
	// ID is the user's id in Keycloak: the `sub` of the user's tokens.
	ID string
	// Username is the user's name in Keycloak, which its tokens carry as
	// `preferred_username`.
	Username string
}

// Group reads one group's members. It is safe for concurrent use.
type Group struct {
	tokenURL, adminURL string
	path               string
	clientID, secret   string
	hc                 *http.Client
}

// New is the group `group` (its path, with or without the leading "/"; a
// name without one is a top-level group) of the realm whose JWKS URL is
// jwksURL (CB_AUTH_KEYCLOAK_JWKS_URL, the address the service already reaches
// Keycloak at): the token and admin URLs are taken from it. The client logs
// in with clientID and secret. hc nil: a client with Timeout.
func New(jwksURL, clientID, secret, group string, hc *http.Client) (*Group, error) {
	u, err := url.Parse(jwksURL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, fmt.Errorf("keycloak: the JWKS URL %q is no absolute URL", jwksURL)
	}
	if err := secure(u); err != nil {
		return nil, err
	}
	realmPath, ok := strings.CutSuffix(u.Path, jwksSuffix)
	prefix, realm, found := strings.Cut(realmPath, "/realms/")
	if !ok || !found || realm == "" || strings.Contains(realm, "/") {
		return nil, fmt.Errorf("keycloak: the JWKS URL %q does not end in /realms/<realm>%s", jwksURL, jwksSuffix)
	}
	if strings.TrimSpace(clientID) == "" || strings.TrimSpace(secret) == "" {
		return nil, errors.New("keycloak: the client id and its secret must be set")
	}
	path := "/" + strings.TrimPrefix(group, "/")
	for _, seg := range strings.Split(path[1:], "/") {
		if seg == "" {
			return nil, fmt.Errorf("keycloak: the group %q has an empty part", group)
		}
	}
	if hc == nil {
		hc = &http.Client{Timeout: Timeout}
	}
	// No redirect is followed: the secret and the token go to the
	// configured address only.
	noRedirect := *hc
	noRedirect.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	base := u.Scheme + "://" + u.Host + prefix
	return &Group{
		tokenURL: base + "/realms/" + realm + "/protocol/openid-connect/token",
		adminURL: base + "/admin/realms/" + realm,
		path:     path, clientID: clientID, secret: secret, hc: &noRedirect,
	}, nil
}

// secure accepts https, and http only on a loopback host (tests, a local
// Keycloak): the client's secret crosses this connection.
func secure(u *url.URL) error {
	switch u.Scheme {
	case "https":
		return nil
	case "http":
		host := u.Hostname()
		if ip := net.ParseIP(host); host == "localhost" || (ip != nil && ip.IsLoopback()) {
			return nil
		}
	}
	return fmt.Errorf("keycloak: %s:// to %s would send the client's secret unencrypted", u.Scheme, u.Hostname())
}

// Members lists the group's enabled members, at most max of them; cut says
// that Keycloak has more. Each call logs in anew (one token per call, never
// kept). An error is an unknown answer, never an empty group.
func (g *Group) Members(ctx context.Context, max int) ([]Member, bool, error) {
	if max < 1 {
		return nil, false, errors.New("keycloak: max must be at least 1")
	}
	tok, err := g.token(ctx)
	if err != nil {
		return nil, false, err
	}
	var grp struct {
		ID   string `json:"id"`
		Path string `json:"path"`
	}
	segs := strings.Split(g.path[1:], "/")
	for i, s := range segs {
		segs[i] = url.PathEscape(s)
	}
	if err := g.get(ctx, tok, "/group-by-path/"+strings.Join(segs, "/"), &grp); err != nil {
		return nil, false, err
	}
	if grp.ID == "" || grp.Path != g.path {
		return nil, false, fmt.Errorf("%w: %s", ErrGroup, g.path)
	}
	var users []struct {
		ID       string `json:"id"`
		Username string `json:"username"`
		Enabled  bool   `json:"enabled"`
	}
	// One more than max tells a cut list from a full one.
	q := url.Values{"first": {"0"}, "max": {strconv.Itoa(max + 1)}, "briefRepresentation": {"true"}}
	if err := g.get(ctx, tok, "/groups/"+url.PathEscape(grp.ID)+"/members?"+q.Encode(), &users); err != nil {
		return nil, false, err
	}
	cut := len(users) > max
	if cut {
		users = users[:max]
	}
	out := make([]Member, 0, len(users))
	for _, u := range users {
		if u.Enabled && u.ID != "" && u.Username != "" {
			out = append(out, Member{ID: u.ID, Username: u.Username})
		}
	}
	return out, cut, nil
}

// token logs the client in (client credentials grant).
func (g *Group) token(ctx context.Context) (string, error) {
	form := url.Values{"grant_type": {"client_credentials"}, "client_id": {g.clientID}, "client_secret": {g.secret}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, g.tokenURL, strings.NewReader(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	var got struct {
		AccessToken string `json:"access_token"`
		TokenType   string `json:"token_type"`
	}
	if err := g.do(req, "the client's login", &got); err != nil {
		return "", err
	}
	if got.AccessToken == "" || !strings.EqualFold(got.TokenType, "bearer") {
		return "", errors.New("keycloak: the client's login gave no bearer token")
	}
	return got.AccessToken, nil
}

func (g *Group) get(ctx context.Context, tok, path string, v any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, g.adminURL+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+tok)
	req.Header.Set("Accept", "application/json")
	what := "the group"
	if strings.Contains(path, "/members") {
		what = "the group's members"
	}
	err = g.do(req, what, v)
	var st statusError
	if errors.As(err, &st) && st.code == http.StatusNotFound && what == "the group" {
		return fmt.Errorf("%w: %s", ErrGroup, g.path)
	}
	return err
}

// statusError is an answer other than 200; the body is not kept (it may
// echo what was sent).
type statusError struct {
	what string
	code int
}

func (e statusError) Error() string {
	return fmt.Sprintf("keycloak: %s: status %d", e.what, e.code)
}

// do sends req and decodes a 200 answer into v (a redirect is an answer
// other than 200).
func (g *Group) do(req *http.Request, what string, v any) error {
	resp, err := g.hc.Do(req)
	if err != nil {
		var ue *url.Error
		if errors.As(err, &ue) {
			err = ue.Err // the URL is known; the error without it
		}
		return fmt.Errorf("keycloak: %s: %w", what, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxBody))
		return statusError{what, resp.StatusCode}
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, maxBody+1))
	if err != nil {
		return fmt.Errorf("keycloak: %s: %w", what, err)
	}
	if len(b) > maxBody {
		return fmt.Errorf("keycloak: %s: the answer is larger than %d bytes", what, maxBody)
	}
	if err := json.Unmarshal(b, v); err != nil {
		return fmt.Errorf("keycloak: %s: not the expected JSON", what)
	}
	return nil
}
