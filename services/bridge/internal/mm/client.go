// Package mm is the bridge's Mattermost client: one bot, the pilot's account,
// posts what came over NATS into a home and reacts there. It never reads posts
// for the mirror (one way, NATS to Mattermost).
package mm

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
)

// StatusError is Mattermost's answer to one request. Create, React and Unreact
// turn a message-specific refusal (400, 404, 413) into mirror.Refused; every
// other failure (401/403: a revoked token or a bot outside the channel, 429,
// 5xx, a network error) is transient and must be retried, never skipped.
type StatusError struct {
	Status int
	Err    error
}

func (e *StatusError) Error() string { return e.Err.Error() }
func (e *StatusError) Unwrap() error { return e.Err }

// refuse maps a message-specific refusal, else nil.
func refuse(err error) error {
	var se *StatusError
	if errors.As(err, &se) {
		switch se.Status {
		case http.StatusBadRequest, http.StatusNotFound, http.StatusRequestEntityTooLarge:
			return &mirror.Refused{Status: se.Status, Err: se.Err}
		}
	}
	return err
}

// Client is a Mattermost bot's HTTP client.
type Client struct {
	base  string
	token string
	http  *http.Client
	me    string // the bot's user id, resolved once
}

// New returns a client for baseURL (no trailing slash) with a bot token.
func New(baseURL, token string) *Client {
	return &Client{base: strings.TrimRight(baseURL, "/"), token: token,
		http: &http.Client{Timeout: 20 * time.Second}}
}

// Me resolves the bot's own user id, which a reaction needs.
func (c *Client) Me(ctx context.Context) (string, error) {
	if c.me != "" {
		return c.me, nil
	}
	var u struct {
		ID string `json:"id"`
	}
	if err := c.do(ctx, "GET", "/api/v4/users/me", nil, &u); err != nil {
		return "", err
	}
	if u.ID == "" {
		return "", errors.New("mattermost: no user id")
	}
	c.me = u.ID
	return c.me, nil
}

// ChannelIDByName resolves a channel in a team by its name.
func (c *Client) ChannelIDByName(ctx context.Context, team, name string) (string, error) {
	var ch struct {
		ID string `json:"id"`
	}
	path := "/api/v4/teams/name/" + url.PathEscape(team) + "/channels/name/" + url.PathEscape(name)
	if err := c.do(ctx, "GET", path, nil, &ch); err != nil {
		return "", fmt.Errorf("mattermost: channel %q in team %q: %w", name, team, err)
	}
	if ch.ID == "" {
		return "", fmt.Errorf("mattermost: no channel %q in team %q", name, team)
	}
	return ch.ID, nil
}

// User is one of a home's members, as the roster needs it.
type User struct {
	ID       string
	Username string
	Status   string // the custom status text, where the ⇄tag sits
}

// Members lists a channel's members: the bots' usernames and status texts.
func (c *Client) Members(ctx context.Context, channelID string) ([]User, error) {
	var raw []struct {
		ID       string `json:"id"`
		Username string `json:"username"`
		Props    struct {
			Custom string `json:"customStatus"`
		} `json:"props"`
	}
	path := "/api/v4/users?in_channel=" + url.QueryEscape(channelID) + "&per_page=200"
	if err := c.do(ctx, "GET", path, nil, &raw); err != nil {
		return nil, err
	}
	out := make([]User, 0, len(raw))
	for _, u := range raw {
		status := ""
		if u.Props.Custom != "" {
			var st struct {
				Text string `json:"text"`
			}
			if json.Unmarshal([]byte(u.Props.Custom), &st) == nil {
				status = st.Text
			}
		}
		out = append(out, User{ID: u.ID, Username: u.Username, Status: status})
	}
	return out, nil
}

// Create posts text in channelID, as a reply to rootID when that is set.
func (c *Client) Create(ctx context.Context, channelID, text, rootID string,
	props map[string]any) (string, error) {
	body := map[string]any{"channel_id": channelID, "message": text}
	if rootID != "" {
		body["root_id"] = rootID
	}
	if len(props) > 0 {
		body["props"] = props
	}
	var p struct {
		ID string `json:"id"`
	}
	if err := c.do(ctx, "POST", "/api/v4/posts", body, &p); err != nil {
		return "", refuse(err)
	}
	if p.ID == "" {
		return "", errors.New("mattermost: a post without an id")
	}
	return p.ID, nil
}

// React adds emoji to a post as the bridge's bot.
func (c *Client) React(ctx context.Context, postID, emoji string) error {
	uid, err := c.Me(ctx)
	if err != nil {
		return err
	}
	return refuse(c.do(ctx, "POST", "/api/v4/reactions",
		map[string]any{"user_id": uid, "post_id": postID, "emoji_name": emoji}, nil))
}

// Unreact removes the bridge's own reaction.
func (c *Client) Unreact(ctx context.Context, postID, emoji string) error {
	uid, err := c.Me(ctx)
	if err != nil {
		return err
	}
	return refuse(c.do(ctx, "DELETE", "/api/v4/users/"+url.PathEscape(uid)+"/posts/"+
		url.PathEscape(postID)+"/reactions/"+url.PathEscape(emoji), nil, nil))
}

func (c *Client) do(ctx context.Context, method, path string, in, out any) error {
	var body io.Reader
	if in != nil {
		raw, err := json.Marshal(in)
		if err != nil {
			return err
		}
		body = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return &StatusError{Status: resp.StatusCode,
			Err: fmt.Errorf("mattermost %s %s: %s: %s", method, path, resp.Status,
				strings.TrimSpace(string(msg)))}
	}
	if out == nil {
		return nil
	}
	return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(out)
}
