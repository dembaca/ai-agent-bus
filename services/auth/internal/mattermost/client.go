// Package mattermost asks a Mattermost server about humans for the login
// service: the account behind a name (token.Directory) and whether it is a
// member of a channel (callout.Channels). It only reads.
package mattermost

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/token"
)

const maxAnswerBytes = 1 << 20

// Client reads users and channel memberships with one access token.
//
// Mattermost shows auth_service and auth_data only to a caller with the right
// to manage the system (a system admin's token). New asks once, via
// /users/me, whether this token has it; without it every user comes back with
// AuthKnown false, which the token check refuses rather than reading the
// blanks as a password account.
type Client struct {
	base     *url.URL
	team     string
	token    string
	http     *http.Client
	authSeen bool
}

// New returns a client for the server at baseURL, the team that holds the
// bus's channels and an access token. ctx bounds the check of the token's
// rights.
func New(ctx context.Context, baseURL, team, accessToken string) (*Client, error) {
	u, err := url.Parse(strings.TrimRight(baseURL, "/"))
	if err != nil || (u.Scheme != "https" && u.Scheme != "http") || u.Host == "" || u.User != nil {
		return nil, errors.New("mattermost: the URL must be an absolute http(s) URL without user information")
	}
	if team == "" || accessToken == "" {
		return nil, errors.New("mattermost: team and access token must be set")
	}
	c := &Client{base: u, team: team, token: accessToken, http: &http.Client{
		Timeout:       10 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return errors.New("redirect refused") },
	}}
	var me struct {
		Roles string `json:"roles"`
	}
	if err := c.get(ctx, "/api/v4/users/me", &me); err != nil {
		return nil, fmt.Errorf("mattermost: the access token: %w", err)
	}
	c.authSeen = hasRole(me.Roles, "system_admin")
	return c, nil
}

// AuthVisible says whether this token lets the client see how accounts log
// in; the caller logs it at start.
func (c *Client) AuthVisible() bool { return c.authSeen }

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
	u, err := c.user(ctx, username)
	if err != nil {
		return token.User{}, err
	}
	tu := token.User{
		Username:    u.Username,
		Bot:         u.IsBot,
		Guest:       hasRole(u.Roles, "system_guest"),
		Deactivated: u.DeleteAt != 0,
		AuthKnown:   c.authSeen,
	}
	if c.authSeen {
		tu.AuthService, tu.AuthData = u.AuthService, u.AuthData
	}
	return tu, nil
}

// Member says whether the human of that name is a member of the team's
// channel of that name. An unknown channel or user is no membership.
func (c *Client) Member(ctx context.Context, channel, username string) (bool, error) {
	u, err := c.user(ctx, username)
	if errors.Is(err, token.ErrNoSuchUser) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var ch struct {
		ID string `json:"id"`
	}
	err = c.get(ctx, "/api/v4/teams/name/"+url.PathEscape(c.team)+"/channels/name/"+url.PathEscape(channel), &ch)
	if errors.Is(err, errNotFound) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var m struct {
		UserID string `json:"user_id"`
	}
	err = c.get(ctx, "/api/v4/channels/"+url.PathEscape(ch.ID)+"/members/"+url.PathEscape(u.ID), &m)
	if errors.Is(err, errNotFound) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return m.UserID == u.ID, nil
}

var errNotFound = errors.New("not found")

func (c *Client) get(ctx context.Context, path string, into any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.base.String()+path, nil)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return errNotFound
	case resp.StatusCode != http.StatusOK:
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(resp.Body, maxAnswerBytes)).Decode(into)
}

func hasRole(roles, role string) bool {
	for _, r := range strings.Fields(roles) {
		if r == role {
			return true
		}
	}
	return false
}
