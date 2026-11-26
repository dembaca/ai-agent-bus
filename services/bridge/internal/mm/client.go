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
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/mirror"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/netloc"
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

// HTTPStatus is the answer's status: the mirror tells a revoked token (401,
// 403) from a transient failure by it without importing this package.
func (e *StatusError) HTTPStatus() int { return e.Status }

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
		http: &http.Client{Timeout: 20 * time.Second, CheckRedirect: netloc.NoRedirect}}
}

// As sets the bot's user id when the caller knows it (an agent's own bot,
// RT-6.44), so Me makes no call and the client is safe to share.
func (c *Client) As(userID string) *Client {
	c.me = userID
	return c
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

// ServerTime is Mattermost's clock in ms, from the Date header of a ping
// (whole seconds, so never ahead of it): the catch-up's watermark is on
// Mattermost's clock, since a post's create_at is (RT-6.42 step 1).
func (c *Client) ServerTime(ctx context.Context) (int64, error) {
	req, err := http.NewRequestWithContext(ctx, "GET", c.base+"/api/v4/system/ping", nil)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return 0, &StatusError{Status: resp.StatusCode,
			Err: fmt.Errorf("mattermost GET /api/v4/system/ping: %s", resp.Status)}
	}
	at, err := http.ParseTime(resp.Header.Get("Date"))
	if err != nil {
		return 0, fmt.Errorf("mattermost: the ping's Date header %q: %w", resp.Header.Get("Date"), err)
	}
	return at.UnixMilli(), nil
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
	IsBot    bool   // Mattermost's is_bot: whether an account is a bot's
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

// GetUser reads one user by id: its username and whether it is a bot
// account — the mark that decides whose post is a bot's, and only
// Mattermost may say it.
func (c *Client) GetUser(ctx context.Context, userID string) (User, error) {
	var raw struct {
		ID       string `json:"id"`
		Username string `json:"username"`
		IsBot    bool   `json:"is_bot"`
	}
	path := "/api/v4/users/" + url.PathEscape(userID)
	if err := c.do(ctx, "GET", path, nil, &raw); err != nil {
		return User{}, fmt.Errorf("mattermost: user %q: %w", userID, err)
	}
	if raw.ID == "" {
		return User{}, fmt.Errorf("mattermost: no user %q", userID)
	}
	return User{ID: raw.ID, Username: raw.Username, IsBot: raw.IsBot}, nil
}

// Post is one of a channel's posts, as the catch-up after a WebSocket
// reconnect needs it (mmws.Reconnected): who wrote it and where, its
// thread, Mattermost's clock fields, the text — and the fields the drop
// rules read: the type and props, the files the post carries and its
// delete mark (a post created and deleted inside the gap comes back
// from `since`).
type Post struct {
	ID        string
	UserID    string
	ChannelID string
	RootID    string
	Type      string         // Mattermost's post type (a join, a custom one)
	Props     map[string]any // the post's props: fromusername, attachments
	FileCount int            // its `file_ids` length (model/post.go's field)
	CreateAt  int64          // ms
	UpdateAt  int64          // ms
	EditAt    int64          // ms, 0 when never edited
	DeleteAt  int64          // ms, past 0: deleted
	Message   string
	// Reactions are the post's current reactions, from its metadata: the
	// catch-up compares them with what it carried (RT-6.42 step 1).
	// HasMetadata says the answer held a metadata object at all; without
	// one, no reaction list is known and nothing may be read as removed.
	Reactions   []Reaction
	HasMetadata bool
}

// Reaction is one of a post's current reactions.
type Reaction struct {
	UserID    string
	EmojiName string
	CreateAt  int64 // ms
}

// postJSON is a post as Mattermost writes it on the wire.
type postJSON struct {
	ID        string         `json:"id"`
	UserID    string         `json:"user_id"`
	ChannelID string         `json:"channel_id"`
	RootID    string         `json:"root_id"`
	Type      string         `json:"type"`
	Props     map[string]any `json:"props"`
	FileIDs   []string       `json:"file_ids"`
	CreateAt  int64          `json:"create_at"`
	UpdateAt  int64          `json:"update_at"`
	EditAt    int64          `json:"edit_at"`
	DeleteAt  int64          `json:"delete_at"`
	Message   string         `json:"message"`
	Metadata  *struct {
		Reactions []struct {
			UserID    string `json:"user_id"`
			EmojiName string `json:"emoji_name"`
			CreateAt  int64  `json:"create_at"`
		} `json:"reactions"`
	} `json:"metadata"`
}

// PostsSince reads a channel's posts from since on, for the catch-up
// after a WebSocket drop: mmws.Reconnected's hint goes here. Mattermost's
// `since` answers by update_at alone (sqlstore getPostsSince: no paging,
// LIMIT 1000), so a post edited or deleted after the bound comes back
// too, its old create_at kept — and an older post that only collected
// a reaction counts toward the limit, so a full read may have left
// posts behind it: that is logged as the gap it is. This method never
// filters, and UpdateAt, EditAt, DeleteAt, the type, the props and the
// files are carried because the caller decides: an edited or
// deleted post is never carried again. The posts come sorted by
// create_at ascending.
func (c *Client) PostsSince(ctx context.Context, channelID string, since int64) ([]Post, error) {
	var list struct {
		Order []string            `json:"order"`
		Posts map[string]postJSON `json:"posts"`
	}
	path := "/api/v4/channels/" + url.PathEscape(channelID) +
		"/posts?since=" + strconv.FormatInt(since, 10)
	if err := c.doLimit(ctx, "GET", path, nil, &list, catchUpCap); err != nil {
		return nil, fmt.Errorf("mattermost: channel %q: %w", channelID, err)
	}
	out := make([]Post, 0, len(list.Order))
	for _, id := range list.Order {
		if raw, ok := list.Posts[id]; ok {
			var reactions []Reaction
			if raw.Metadata != nil {
				for _, re := range raw.Metadata.Reactions {
					reactions = append(reactions, Reaction{UserID: re.UserID,
						EmojiName: re.EmojiName, CreateAt: re.CreateAt})
				}
			}
			out = append(out, Post{
				Reactions:   reactions,
				HasMetadata: raw.Metadata != nil,
				ID:          raw.ID,
				UserID:      raw.UserID,
				ChannelID:   raw.ChannelID,
				RootID:      raw.RootID,
				Type:        raw.Type,
				Props:       raw.Props,
				FileCount:   len(raw.FileIDs),
				CreateAt:    raw.CreateAt,
				UpdateAt:    raw.UpdateAt,
				EditAt:      raw.EditAt,
				DeleteAt:    raw.DeleteAt,
				Message:     raw.Message,
			})
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].CreateAt < out[j].CreateAt })
	if len(out) >= maxSincePosts {
		// The read cannot say whether it was cut at the limit, and the
		// server's LIMIT has no ORDER BY, so which posts sat behind the
		// cap is unknowable from here — only that there may be some.
		// This is a log, not a signal: the caller reads the warning with
		// everything else it knows (a gap that big is its own error row).
		slog.Warn("mattermost: the since read returned its thousand",
			"channel", channelID)
	}
	return out, nil
}

// maxSincePosts is Mattermost's own page cap for getPostsSince: the read
// is silently cut there, so the count names the cut.
const maxSincePosts = 1000

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

// catchUpCap is what PostsSince may read: `since` answers up to a
// thousand posts in one body, far past the cap a single object needs.
const catchUpCap = 32 << 20 // 32 MiB

// ErrTooLarge says a response ran past the cap its method reads: the
// catch-up names its own bound, so the caller can fall back to pages
// instead of retrying a body that cannot fit.
var ErrTooLarge = errors.New("mattermost: the response is larger than this read takes")

// doLimit is do with the body's cap named: PostsSince needs far more
// than a single object, and its refusal says itself (ErrTooLarge) so the
// caller can page instead of retrying a body that cannot fit.
func (c *Client) doLimit(ctx context.Context, method, path string, in, out any,
	limit int64) error {
	body, err := c.request(ctx, method, path, in)
	if err != nil {
		return err
	}
	defer body.Close()
	raw, rerr := io.ReadAll(io.LimitReader(body, limit+1))
	if rerr != nil {
		return rerr
	}
	if int64(len(raw)) > limit {
		return fmt.Errorf("mattermost %s %s: %w", method, path, ErrTooLarge)
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(raw, out)
}

func (c *Client) do(ctx context.Context, method, path string, in, out any) error {
	body, err := c.request(ctx, method, path, in)
	if err != nil {
		return err
	}
	defer body.Close()
	if out == nil {
		return nil
	}
	return json.NewDecoder(io.LimitReader(body, 1<<20)).Decode(out)
}

// request is do and doLimit's shared half: one request sent, its status
// checked, the body left open for whichever cap reads it.
func (c *Client) request(ctx context.Context, method, path string, in any) (io.ReadCloser, error) {
	var body io.Reader
	if in != nil {
		raw, err := json.Marshal(in)
		if err != nil {
			return nil, err
		}
		body = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		resp.Body.Close()
		return nil, &StatusError{Status: resp.StatusCode,
			Err: fmt.Errorf("mattermost %s %s: %s: %s", method, path, resp.Status,
				strings.TrimSpace(string(msg)))}
	}
	return resp.Body, nil
}
