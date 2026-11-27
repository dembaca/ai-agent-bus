// Package mattermost asks a Mattermost server about humans for the login
// service: the account behind a name (token.Directory) and the channels it
// is a member of (callout.Channels). It only reads.
package mattermost

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

const (
	maxAnswerBytes = 1 << 20
	// How long a channel's id is kept, and how often the token's right to
	// see how accounts log in is asked again.
	cacheFor   = time.Minute
	recheckFor = time.Minute
)

// Client reads users and channel memberships with one access token.
//
// Mattermost shows auth_service and auth_data only to a caller with the right
// to manage the system (a system admin's token). The client asks /users/me
// for that right at start, again every minute and after every 401 or 403;
// while the token lacks it, every user comes back with AuthKnown false, which
// the token check refuses rather than reading the blanks as a password
// account.
type Client struct {
	base  *url.URL
	team  string
	token string
	http  *http.Client
	now   func() time.Time

	mu        sync.Mutex
	authSeen  bool
	checkedAt time.Time
	recheck   bool
	channels  map[string]cached
}

type cached struct {
	id string
	at time.Time
}

// New returns a client for the server at baseURL (https, or http on a
// loopback host), the team that holds the bus's channels and an access
// token. ctx bounds the check of the token's rights.
func New(ctx context.Context, baseURL, team, accessToken string) (*Client, error) {
	u, err := url.Parse(strings.TrimRight(baseURL, "/"))
	if err != nil || u.Host == "" || u.User != nil || !secure(u) {
		return nil, errors.New("mattermost: the URL must be https (http only on a loopback host), without user information")
	}
	if team == "" || accessToken == "" {
		return nil, errors.New("mattermost: team and access token must be set")
	}
	c := &Client{base: u, team: team, token: accessToken, now: time.Now, channels: map[string]cached{},
		http: &http.Client{
			Timeout:       10 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirect refused") },
		}}
	if err := c.checkAuth(ctx); err != nil {
		return nil, fmt.Errorf("mattermost: the access token: %w", err)
	}
	return c, nil
}

func secure(u *url.URL) bool {
	switch u.Scheme {
	case "https":
		return true
	case "http":
		h := u.Hostname()
		ip := net.ParseIP(h)
		return h == "localhost" || (ip != nil && ip.IsLoopback())
	}
	return false
}

// AuthVisible says whether this token lets the client see how accounts log
// in, as last asked.
func (c *Client) AuthVisible() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.authSeen
}

func (c *Client) checkAuth(ctx context.Context) error {
	var me struct {
		Roles string `json:"roles"`
	}
	err := c.get(ctx, "/api/v4/users/me", &me)
	c.mu.Lock()
	defer c.mu.Unlock()
	if err != nil {
		// not an answer: checkedAt stays, and the next lookup asks again
		c.authSeen, c.recheck = false, true
		return err
	}
	c.checkedAt, c.recheck = c.now(), false
	c.authSeen = hasRole(me.Roles, "system_admin")
	return nil
}

// ErrTooMany: a list is longer than this client reads (maxItems).
var ErrTooMany = errors.New("mattermost: list too long")

// authState is authVisible with the reason: a check that failed (a
// network error, a 5xx) is an error, not "cannot see".
func (c *Client) authState(ctx context.Context) (bool, error) {
	c.mu.Lock()
	stale := c.recheck || c.now().Sub(c.checkedAt) > recheckFor
	c.mu.Unlock()
	if stale {
		if err := c.checkAuth(ctx); err != nil {
			return false, err
		}
	}
	return c.AuthVisible(), nil
}

// authVisible asks again when the last answer is old or a request was
// refused since.
func (c *Client) authVisible(ctx context.Context) bool {
	c.mu.Lock()
	stale := c.recheck || c.now().Sub(c.checkedAt) > recheckFor
	c.mu.Unlock()
	if stale {
		_ = c.checkAuth(ctx)
	}
	return c.AuthVisible()
}

type user struct {
	ID          string `json:"id"`
	Username    string `json:"username"`
	IsBot       bool   `json:"is_bot"`
	DeleteAt    int64  `json:"delete_at"`
	Roles       string `json:"roles"`
	AuthService string `json:"auth_service"`
	AuthData    string `json:"auth_data"`
}

func (c *Client) user(ctx context.Context, username string) (user, error) {
	var u user
	err := c.get(ctx, "/api/v4/users/username/"+url.PathEscape(username), &u)
	if errors.Is(err, errNotFound) {
		return user{}, token.ErrNoSuchUser
	}
	return u, err
}

// User implements token.Directory.
func (c *Client) User(ctx context.Context, username string) (token.User, error) {
	seen := c.authVisible(ctx)
	u, err := c.user(ctx, username)
	if err != nil {
		return token.User{}, err
	}
	return u.asToken(seen), nil
}

// UserByID reads an account by Mattermost's user id (callout.ChatAccounts,
// the sweep of the bindings, RT-6.42 step 3); token.ErrNoSuchUser for one
// that is gone.
func (c *Client) UserByID(ctx context.Context, id string) (token.User, error) {
	seen := c.authVisible(ctx)
	var u user
	err := c.get(ctx, "/api/v4/users/"+url.PathEscape(id), &u)
	if errors.Is(err, errNotFound) {
		return token.User{}, token.ErrNoSuchUser
	}
	if err != nil {
		return token.User{}, err
	}
	return u.asToken(seen), nil
}

// asToken is the account as the verifier sees it; auth_service and auth_data
// only when the token can see them (seen).
func (u user) asToken(seen bool) token.User {
	tu := token.User{
		Username:    u.Username,
		ID:          u.ID,
		Bot:         u.IsBot,
		Guest:       hasRole(u.Roles, "system_guest"),
		Deactivated: u.DeleteAt != 0,
		AuthKnown:   seen,
	}
	if seen {
		tu.AuthService, tu.AuthData = u.AuthService, u.AuthData
	}
	return tu
}

// Memberships says of which of the team's channels of these names the human
// is a member, with one user lookup. An unknown user or channel is no
// membership; an error means the answer is unknown.
func (c *Client) Memberships(ctx context.Context, username string, channels []string) (map[string]bool, error) {
	out := map[string]bool{}
	u, err := c.user(ctx, username)
	if errors.Is(err, token.ErrNoSuchUser) {
		return out, nil
	}
	if err != nil {
		return nil, err
	}
	// One lookup per channel, all at once: a host's channels cost one round
	// trip, not one per channel, within the request's deadline.
	var (
		wg    sync.WaitGroup
		mu    sync.Mutex
		first error
	)
	for _, name := range channels {
		wg.Go(func() {
			member, err := c.member(ctx, name, u.ID)
			mu.Lock()
			defer mu.Unlock()
			switch {
			case err != nil && first == nil:
				first = fmt.Errorf("channel %q: %w", name, err)
			case err == nil:
				out[name] = member
			}
		})
	}
	wg.Wait()
	if first != nil {
		return nil, first
	}
	return out, nil
}

// member: an unknown channel or a missing membership is false; an error means
// the answer is unknown.
func (c *Client) member(ctx context.Context, channel, userID string) (bool, error) {
	id, err := c.channelID(ctx, channel)
	if errors.Is(err, errNotFound) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var m struct {
		UserID string `json:"user_id"`
	}
	err = c.get(ctx, "/api/v4/channels/"+url.PathEscape(id)+"/members/"+url.PathEscape(userID), &m)
	switch {
	case errors.Is(err, errNotFound):
		return false, nil
	case err != nil:
		return false, err
	}
	return m.UserID == userID, nil
}

func (c *Client) channelID(ctx context.Context, name string) (string, error) {
	c.mu.Lock()
	hit, ok := c.channels[name]
	c.mu.Unlock()
	if ok && c.now().Sub(hit.at) < cacheFor {
		return hit.id, nil
	}
	var ch struct {
		ID string `json:"id"`
	}
	if err := c.get(ctx, "/api/v4/teams/name/"+url.PathEscape(c.team)+"/channels/name/"+url.PathEscape(name), &ch); err != nil {
		return "", err
	}
	c.mu.Lock()
	c.channels[name] = cached{id: ch.ID, at: c.now()}
	c.mu.Unlock()
	return ch.ID, nil
}

var errNotFound = errors.New("not found")

func (c *Client) get(ctx context.Context, path string, into any) error {
	return c.do(ctx, http.MethodGet, path, nil, into, maxAnswerBytes)
}

func (c *Client) post(ctx context.Context, path string, body, into any) error {
	b, err := json.Marshal(body)
	if err != nil {
		return err
	}
	return c.do(ctx, http.MethodPost, path, b, into, maxAnswerBytes)
}

// do sends one request and decodes at most limit bytes of its answer.
func (c *Client) do(ctx context.Context, method, path string, body []byte, into any, limit int64) error {
	var rd io.Reader
	if body != nil {
		rd = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base.String()+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusOK:
		return json.NewDecoder(io.LimitReader(resp.Body, limit)).Decode(into)
	case http.StatusNotFound:
		return errNotFound
	case http.StatusUnauthorized, http.StatusForbidden:
		c.mu.Lock()
		c.recheck = true
		c.mu.Unlock()
	}
	return fmt.Errorf("HTTP %d", resp.StatusCode)
}

func hasRole(roles, role string) bool {
	return slices.Contains(strings.Fields(roles), role)
}
