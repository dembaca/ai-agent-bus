// Package mm is the bridge's Mattermost client: one bot, the pilot's account,
// posts what came over NATS into a home and reacts there. The mirror never
// reads posts (one way, NATS to Mattermost); the history read does, with the
// agent's own bot.
package mm

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/url"
	"slices"
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
	// Set by GetUser only. Deactivated and Guest bind an account to nobody;
	// AuthService and AuthData (how it logs in, "openid" or "ldap", and the
	// identity provider's id) are what the binding's proof is compared with
	// (RT-6.42 step 3). Mattermost shows the two only to a system admin's
	// token; for another both are empty, and no account is bound.
	Deactivated, Guest    bool
	AuthService, AuthData string
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
// Mattermost may say it — and what binds it to a person: whether it is
// active and no guest, and how it logs in.
func (c *Client) GetUser(ctx context.Context, userID string) (User, error) {
	var raw struct {
		ID          string `json:"id"`
		Username    string `json:"username"`
		IsBot       bool   `json:"is_bot"`
		DeleteAt    int64  `json:"delete_at"`
		Roles       string `json:"roles"`
		AuthService string `json:"auth_service"`
		AuthData    string `json:"auth_data"`
	}
	path := "/api/v4/users/" + url.PathEscape(userID)
	if err := c.do(ctx, "GET", path, nil, &raw); err != nil {
		return User{}, fmt.Errorf("mattermost: user %q: %w", userID, err)
	}
	if raw.ID == "" {
		return User{}, fmt.Errorf("mattermost: no user %q: %w", userID, errNoUser)
	}
	return User{ID: raw.ID, Username: raw.Username, IsBot: raw.IsBot, Deactivated: raw.DeleteAt != 0,
		Guest: slices.Contains(strings.Fields(raw.Roles), "system_guest"), AuthService: raw.AuthService,
		AuthData: raw.AuthData}, nil
}

// errNoUser is GetUser's answer for a body that names no user.
var errNoUser = errors.New("no user in the answer")

// notDecodable says err is a real decode error (the body is not the JSON asked
// for), not a read that was cut or failed: that one is the network's and is
// retried.
func notDecodable(err error) bool {
	var syn *json.SyntaxError
	var typ *json.UnmarshalTypeError
	return errors.As(err, &syn) || errors.As(err, &typ)
}

// Person is the user id of username's account when it is a person's: it
// exists, is not deactivated and is no bot (Mattermost's is_bot, as
// GetUser); "" otherwise, not an error. Plan B piece 6 (RT-6.11) keeps an
// ask's mention only of such an account (mirror.People).
func (c *Client) Person(ctx context.Context, username string) (string, error) {
	var raw []struct {
		ID       string `json:"id"`
		Username string `json:"username"`
		IsBot    bool   `json:"is_bot"`
		DeleteAt int64  `json:"delete_at"`
	}
	if err := c.do(ctx, "POST", "/api/v4/users/usernames", []string{username}, &raw); err != nil {
		// A refusal keeps its status and a request that got no answer, or whose
		// answer was cut or failed to read, is retried; only an answer that does
		// not decode counts as no person (404), not as a failure to retry for
		// ever (#287).
		if notDecodable(err) {
			err = &StatusError{Status: http.StatusNotFound, Err: err}
		}
		return "", fmt.Errorf("mattermost: username %q: %w", username, err)
	}
	for _, u := range raw {
		if strings.EqualFold(u.Username, username) && !u.IsBot && u.DeleteAt == 0 {
			return u.ID, nil
		}
	}
	return "", nil
}

// Member says whether userID is a member of channelID: a 404 is no member,
// any other failure an error (plan B piece 6 logs it only, mirror.Members).
func (c *Client) Member(ctx context.Context, channelID, userID string) (bool, error) {
	err := c.do(ctx, "GET", "/api/v4/channels/"+url.PathEscape(channelID)+"/members/"+url.PathEscape(userID), nil, nil)
	var st *StatusError
	if errors.As(err, &st) && st.Status == http.StatusNotFound {
		return false, nil
	}
	return err == nil, err
}

// Target reads one post by id and its author, for plan B piece 7 (RT-6.42
// step 13, mirror.Targets): the bridge checks it before an agent's reply or
// reaction naming it is written. A refusal keeps its status (StatusError) and
// a request that got no answer, or whose body was cut or failed mid-read, is
// retried; an answer that does not decode, or names no post or author, counts
// as no such post (404), so it is skipped rather than retried for ever.
func (c *Client) Target(ctx context.Context, postID string) (mirror.Target, error) {
	var raw postJSON
	body, err := c.request(ctx, "GET", "/api/v4/posts/"+url.PathEscape(postID), nil)
	if err != nil {
		return mirror.Target{}, fmt.Errorf("mattermost: post %q: %w", postID, err)
	}
	err = decodeBody(body, &raw)
	body.Close()
	if err != nil && !notDecodable(err) {
		// a cut or failed read is no answer about the post: retried
		return mirror.Target{}, fmt.Errorf("mattermost: post %q: %w", postID, err)
	}
	if err != nil || raw.ID == "" || raw.UserID == "" {
		return mirror.Target{}, &StatusError{Status: http.StatusNotFound,
			Err: fmt.Errorf("mattermost: post %q: no post in the answer", postID)}
	}
	u, err := c.GetUser(ctx, raw.UserID)
	if err != nil {
		// A refusal keeps its status and the network (no answer, a timeout, a cut
		// or failed read) is retried; an answer that does not decode, or names no
		// user, is none (404), so it is skipped rather than retried for ever.
		if notDecodable(err) || errors.Is(err, errNoUser) {
			err = &StatusError{Status: http.StatusNotFound, Err: err}
		}
		return mirror.Target{}, err
	}
	_, hook := raw.Props["from_webhook"]
	_, override := raw.Props["override_username"]
	return mirror.Target{ID: raw.ID, ChannelID: raw.ChannelID, RootID: raw.RootID, Type: raw.Type,
		DeleteAt: raw.DeleteAt, Webhook: hook || override, AuthorName: u.Username, AuthorBot: u.IsBot}, nil
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

// postList is Mattermost's PostList on the wire: the order and the posts by
// id, as PostsSince and ThreadPage read it; has_next says a thread page is not
// the thread's end.
type postList struct {
	Order   []string            `json:"order"`
	Posts   map[string]postJSON `json:"posts"`
	HasNext bool                `json:"has_next"`
}

// post is the wire post as the reads carry it: the metadata, the props and
// the files as Mattermost sent them, the file count from its file_ids.
func (raw postJSON) post() Post {
	var reactions []Reaction
	if raw.Metadata != nil {
		for _, re := range raw.Metadata.Reactions {
			reactions = append(reactions, Reaction{UserID: re.UserID,
				EmojiName: re.EmojiName, CreateAt: re.CreateAt})
		}
	}
	return Post{
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
	}
}

// posts gives the list's posts sorted by create_at ascending, the metadata,
// the props and the files as Mattermost sent them.
func (l postList) posts() []Post {
	out := make([]Post, 0, len(l.Order))
	for _, id := range l.Order {
		raw, ok := l.Posts[id]
		if !ok {
			continue
		}
		out = append(out, raw.post())
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].CreateAt < out[j].CreateAt })
	return out
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
	var list postList
	path := "/api/v4/channels/" + url.PathEscape(channelID) +
		"/posts?since=" + strconv.FormatInt(since, 10)
	if err := c.doLimit(ctx, "GET", path, nil, &list, catchUpCap); err != nil {
		return nil, fmt.Errorf("mattermost: channel %q: %w", channelID, err)
	}
	out := list.posts()
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

// PageSize is one page of a channel read: Mattermost's own cap for a page.
const PageSize = 200

// maxReadPages bounds one channel read: 100 pages of 200 posts.
const maxReadPages = 100

// MaxReadPosts is the most posts one channel read walks. A window with more is
// refused, never answered in part: an answer cut at the newest side would lose
// the posts between.
const MaxReadPosts = PageSize * maxReadPages

// ErrTooManyPosts says a channel read walked its page bound without reaching
// the since it was given: the caller asks from a later since.
var ErrTooManyPosts = errors.New("mattermost: the channel has more posts in that time than one read walks")

// ChannelPage reads one page of a channel's posts, newest first, page counted
// from 0, at most perPage posts (PageSize at most). The posts come sorted by
// create_at ascending, with the same fields PostsSince carries. It filters
// nothing: the history read walks the pages and decides (PostsFrom).
func (c *Client) ChannelPage(ctx context.Context, channelID string, page, perPage int) ([]Post, error) {
	if perPage <= 0 || perPage > PageSize {
		perPage = PageSize
	}
	if page < 0 {
		page = 0
	}
	var list postList
	path := "/api/v4/channels/" + url.PathEscape(channelID) + "/posts?page=" + strconv.Itoa(page) +
		"&per_page=" + strconv.Itoa(perPage)
	if err := c.doLimit(ctx, "GET", path, nil, &list, catchUpCap); err != nil {
		return nil, fmt.Errorf("mattermost: channel %q: %w", channelID, err)
	}
	return list.posts(), nil
}

// PostsFrom reads a channel's posts created at or after since, oldest first,
// at most limit of them; more says posts at or after since remain past those.
// A run of posts with one create_at is carried whole, over the limit, so the
// caller's cursor (the last post's create_at plus one, exclusive) never splits
// a run and walks it twice. It pages with
// page/per_page from the newest post back, never with `since` alone: that
// answer is cut at a thousand posts in no set order (sqlstore getPostsSince:
// no paging, LIMIT 1000), so a busy channel lost its newest. The walk ends at
// the first page that holds a post before since, or at the channel's oldest
// post; only the oldest limit posts from since on are kept meanwhile (each page
// is newer than the one after it), so the answer's cursor walks forward. A
// window with more posts than MaxReadPosts is ErrTooManyPosts.
func (c *Client) PostsFrom(ctx context.Context, channelID string, since int64, limit int) ([]Post, bool, error) {
	if limit <= 0 {
		limit = PageSize
	}
	var (
		oldest []Post // ascending, at most limit: the oldest posts from since on
		more   bool
	)
	for page := 0; page < maxReadPages; page++ {
		posts, err := c.ChannelPage(ctx, channelID, page, PageSize)
		if err != nil {
			return nil, false, err
		}
		reached := len(posts) < PageSize
		keep := make([]Post, 0, len(posts))
		for _, post := range posts { // ascending within the page
			if post.CreateAt < since {
				reached = true
				continue // the page's older part: nothing of the window
			}
			keep = append(keep, post)
		}
		if len(keep) > 0 {
			oldest = append(keep, oldest...) // the page is older than what came before
			if len(oldest) > limit {
				// Never cut inside a run of equal create_at: the cursor goes on
				// from the last post read, and a run a cut split would be read
				// again from the same create_at for ever (Fable's review of #270).
				end := limit
				for end < len(oldest) && oldest[end].CreateAt == oldest[limit-1].CreateAt {
					end++
				}
				oldest, more = oldest[:end], true
			}
		}
		if reached {
			return oldest, more, nil
		}
	}
	return nil, false, fmt.Errorf("%w: %d pages of %d", ErrTooManyPosts, maxReadPages, PageSize)
}

// ThreadCap is the most posts one thread page asks for: Mattermost's own page
// cap for a post list.
const ThreadCap = 200

// ThreadPage reads one page of a post's thread: the root and its replies,
// oldest first, at most perPage posts (ThreadCap at most). fromPost and
// fromCreateAt (both or neither) go on from the last post of an earlier page,
// the posts newer than that one; hasNext says the thread goes on past the page.
// The fields are PostsSince's: metadata and the reactions it holds, the props,
// the files, the create, update, edit and delete marks. It never filters: the
// caller decides what of the thread it answers.
func (c *Client) ThreadPage(ctx context.Context, postID, fromPost string, fromCreateAt int64,
	perPage int) ([]Post, bool, error) {
	if perPage <= 0 || perPage > ThreadCap {
		perPage = ThreadCap
	}
	var list postList
	path := "/api/v4/posts/" + url.PathEscape(postID) + "/thread?perPage=" + strconv.Itoa(perPage) +
		"&direction=down"
	if fromPost != "" {
		path += "&fromPost=" + url.PathEscape(fromPost) +
			"&fromCreateAt=" + strconv.FormatInt(fromCreateAt, 10)
	}
	if err := c.doLimit(ctx, "GET", path, nil, &list, catchUpCap); err != nil {
		return nil, false, fmt.Errorf("mattermost: thread of %q: %w", postID, err)
	}
	return list.posts(), list.HasNext, nil
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

// FileInfo is what Mattermost keeps about one uploaded file (RT-6.23), the
// fields the reads use: its name, extension and mime type, its size in bytes,
// an image's bounds, the post it is attached to, who uploaded it — Mattermost
// makes the uploader the post's author — and whether a preview image was
// made. extension is lower case without the dot; width and height are an
// image's, 0 else.
type FileInfo struct {
	ID              string `json:"id"`
	Name            string `json:"name"`
	Extension       string `json:"extension"`
	Size            int64  `json:"size"`
	MimeType        string `json:"mime_type"`
	Width           int    `json:"width"`
	Height          int    `json:"height"`
	PostID          string `json:"post_id"`
	UserID          string `json:"user_id"`
	HasPreviewImage bool   `json:"has_preview_image"`
}

// UploadFile uploads one file to a channel (POST /api/v4/files, multipart:
// the channel_id and files fields), as the client's own bot, and answers what
// Mattermost kept about it: the upload answer's first file_infos entry, the
// shape the create-post answer carries in a post's metadata too. The caller
// has checked the bytes (plan B piece 8 re-implements the MCP side's attach
// rules); Mattermost's own MaxFileSize refuses on top, as the status it
// answers.
func (c *Client) UploadFile(ctx context.Context, channelID, name string, data []byte) (FileInfo, error) {
	var body bytes.Buffer
	w := multipart.NewWriter(&body)
	if err := w.WriteField("channel_id", channelID); err != nil {
		return FileInfo{}, err
	}
	file, err := w.CreateFormFile("files", name)
	if err != nil {
		return FileInfo{}, err
	}
	if _, err := file.Write(data); err != nil {
		return FileInfo{}, err
	}
	if err := w.Close(); err != nil {
		return FileInfo{}, err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", c.base+"/api/v4/files", &body)
	if err != nil {
		return FileInfo{}, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", w.FormDataContentType())
	resp, err := c.http.Do(req)
	if err != nil {
		return FileInfo{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return FileInfo{}, &StatusError{Status: resp.StatusCode,
			Err: fmt.Errorf("mattermost POST /api/v4/files: %s: %s", resp.Status,
				strings.TrimSpace(string(msg)))}
	}
	var out struct {
		FileInfos []FileInfo `json:"file_infos"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&out); err != nil {
		return FileInfo{}, err
	}
	if len(out.FileInfos) == 0 || out.FileInfos[0].ID == "" {
		return FileInfo{}, errors.New("mattermost: a file upload without a file info")
	}
	return out.FileInfos[0], nil
}

// FileInfo reads what Mattermost keeps about one file
// (GET /api/v4/files/{id}/info): what a download answers from.
func (c *Client) FileInfo(ctx context.Context, fileID string) (FileInfo, error) {
	var info FileInfo
	path := "/api/v4/files/" + url.PathEscape(fileID) + "/info"
	if err := c.do(ctx, "GET", path, nil, &info); err != nil {
		return FileInfo{}, fmt.Errorf("mattermost: file %q: %w", fileID, err)
	}
	if info.ID == "" {
		return FileInfo{}, fmt.Errorf("mattermost: no file %q", fileID)
	}
	return info, nil
}

// maxFileBytes is the ceiling of what one file read may ask: an image the
// piece serves is at most 5 MB (its preview, else the original) and a text
// file 80 KB, so a limit past the cap is taken as the cap, never more.
const maxFileBytes = 32 << 20

// FileBytes reads the head of a file's bytes: at most limit of the body, its
// preview image when it has one and the caller asks
// (GET /api/v4/files/{id}/preview), else the file itself
// (GET /api/v4/files/{id}). A limit of zero or less, or past maxFileBytes,
// reads maxFileBytes.
func (c *Client) FileBytes(ctx context.Context, fileID string, preview bool, limit int64) ([]byte, error) {
	if limit <= 0 || limit > maxFileBytes {
		limit = maxFileBytes
	}
	path := "/api/v4/files/" + url.PathEscape(fileID)
	if preview {
		path += "/preview"
	}
	req, err := http.NewRequestWithContext(ctx, "GET", c.base+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, &StatusError{Status: resp.StatusCode,
			Err: fmt.Errorf("mattermost GET %s: %s: %s", path, resp.Status,
				strings.TrimSpace(string(msg)))}
	}
	return io.ReadAll(io.LimitReader(resp.Body, limit))
}

// PostByID reads one post (GET /api/v4/posts/{id}): its channel, author,
// message and clock fields — the fields the file read needs of the post a
// file is attached to.
func (c *Client) PostByID(ctx context.Context, postID string) (Post, error) {
	var raw postJSON
	path := "/api/v4/posts/" + url.PathEscape(postID)
	if err := c.do(ctx, "GET", path, nil, &raw); err != nil {
		return Post{}, fmt.Errorf("mattermost: post %q: %w", postID, err)
	}
	if raw.ID == "" {
		return Post{}, fmt.Errorf("mattermost: no post %q", postID)
	}
	return raw.post(), nil
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
	return decodeBody(body, out)
}

// decodeBody reads the whole body first and decodes it after, so a read that
// is cut or fails (an error of the network) is told from a body that is no
// JSON of the shape asked for (a *json.SyntaxError or *json.UnmarshalTypeError).
func decodeBody(body io.Reader, out any) error {
	data, err := io.ReadAll(io.LimitReader(body, 1<<20))
	if err != nil {
		return err
	}
	return json.Unmarshal(data, out)
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
