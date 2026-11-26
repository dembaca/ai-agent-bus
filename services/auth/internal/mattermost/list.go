package mattermost

import (
	"context"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const (
	pageSize = 200
	maxItems = 10000
)

// Channel is a channel of the team: Type "O" is public, "P" private.
type Channel struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Type      string `json:"type"`
	CreatorID string `json:"creator_id"`
}

// User is an account as the one-time import reads it.
type User struct {
	ID          string
	Username    string
	IsBot       bool
	Deactivated bool
	Guest       bool // role system_guest
	AuthService string
	AuthData    string
	// AuthKnown: the token could see how accounts log in; when false,
	// AuthService and AuthData are left empty.
	AuthKnown bool
}

// paged reads path page by page until a short page, at most maxItems items.
func paged[T any](ctx context.Context, c *Client, path string) ([]T, error) {
	var all []T
	for page := 0; ; page++ {
		var got []T
		sep := "?"
		if strings.Contains(path, "?") {
			sep = "&"
		}
		if err := c.get(ctx, path+sep+"page="+strconv.Itoa(page)+"&per_page="+strconv.Itoa(pageSize), &got); err != nil {
			return nil, err
		}
		all = append(all, got...)
		if len(all) > maxItems {
			return nil, fmt.Errorf("%w: more than %d items", ErrTooMany, maxItems)
		}
		if len(got) < pageSize {
			return all, nil
		}
	}
}

func (c *Client) teamID(ctx context.Context) (string, error) {
	var t struct {
		ID string `json:"id"`
	}
	if err := c.get(ctx, "/api/v4/teams/name/"+url.PathEscape(c.team), &t); err != nil {
		return "", fmt.Errorf("team %q: %w", c.team, err)
	}
	return t.ID, nil
}

// TeamChannels lists the team's public and private channels, once each.
func (c *Client) TeamChannels(ctx context.Context) ([]Channel, error) {
	id, err := c.teamID(ctx)
	if err != nil {
		return nil, err
	}
	base := "/api/v4/teams/" + url.PathEscape(id) + "/channels"
	var out []Channel
	seen := map[string]bool{}
	for _, p := range []string{base, base + "/private"} {
		chs, err := paged[Channel](ctx, c, p)
		if err != nil {
			return nil, err
		}
		for _, ch := range chs {
			if !seen[ch.ID] {
				seen[ch.ID] = true
				out = append(out, ch)
			}
		}
	}
	return out, nil
}

// ChannelMemberIDs lists the user ids of a channel's members.
func (c *Client) ChannelMemberIDs(ctx context.Context, channelID string) ([]string, error) {
	type member struct {
		UserID string `json:"user_id"`
	}
	ms, err := paged[member](ctx, c, "/api/v4/channels/"+url.PathEscape(channelID)+"/members")
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(ms))
	for _, m := range ms {
		ids = append(ids, m.UserID)
	}
	return ids, nil
}

// Post is a channel's post as the STATS import reads it (RT-6.45).
type Post struct {
	ID      string
	Created time.Time // create_at, milliseconds on the server
	UserID  string
	Message string
}

const (
	// postsPage and postsAnswerMax: a post's message is at most 16383
	// characters (64 KiB in UTF-8), so a page of 30 is at most about 2 MiB of
	// messages; the answer limit leaves room for the posts' metadata.
	postsPage      = 30
	postsAnswerMax = 8 << 20
	maxPosts       = 100000
)

// ChannelPosts reads the posts of the team's channel by name, created at or
// after since (the zero time: all), newest page first, each post once.
// System posts are left out.
func (c *Client) ChannelPosts(ctx context.Context, channelName string, since time.Time) ([]Post, error) {
	id, err := c.channelID(ctx, channelName)
	if err != nil {
		return nil, fmt.Errorf("channel %q: %w", channelName, err)
	}
	type post struct {
		ID       string `json:"id"`
		CreateAt int64  `json:"create_at"`
		DeleteAt int64  `json:"delete_at"`
		UserID   string `json:"user_id"`
		Type     string `json:"type"`
		Message  string `json:"message"`
	}
	var out []Post
	seen := map[string]bool{}
	for page := 0; ; page++ {
		var got struct {
			Order []string        `json:"order"`
			Posts map[string]post `json:"posts"`
		}
		path := "/api/v4/channels/" + url.PathEscape(id) + "/posts?page=" + strconv.Itoa(page) + "&per_page=" + strconv.Itoa(postsPage)
		if err := c.do(ctx, http.MethodGet, path, nil, &got, postsAnswerMax); err != nil {
			return nil, fmt.Errorf("posts of %q: %w", channelName, err)
		}
		older := false
		for _, pid := range got.Order {
			p, ok := got.Posts[pid]
			if !ok || seen[pid] {
				continue
			}
			seen[pid] = true
			at := time.UnixMilli(p.CreateAt).UTC()
			if at.Before(since) {
				older = true
				continue
			}
			if p.Type != "" || p.DeleteAt != 0 {
				continue
			}
			out = append(out, Post{ID: p.ID, Created: at, UserID: p.UserID, Message: p.Message})
		}
		if len(seen) > maxPosts {
			return nil, fmt.Errorf("%w: more than %d posts", ErrTooMany, maxPosts)
		}
		if len(got.Order) < postsPage || older {
			return out, nil
		}
	}
}

// Users reads accounts by id (in batches of pageSize).
func (c *Client) Users(ctx context.Context, ids []string) ([]User, error) {
	var out []User
	seen, err := c.authState(ctx)
	if err != nil {
		return nil, fmt.Errorf("whether the token sees how accounts log in: %w", err)
	}
	for len(ids) > 0 {
		n := min(len(ids), pageSize)
		var got []user
		if err := c.post(ctx, "/api/v4/users/ids", ids[:n], &got); err != nil {
			return nil, err
		}
		for _, u := range got {
			nu := User{ID: u.ID, Username: u.Username, IsBot: u.IsBot, Deactivated: u.DeleteAt != 0,
				Guest: hasRole(u.Roles, "system_guest"), AuthKnown: seen}
			if seen {
				nu.AuthService, nu.AuthData = u.AuthService, u.AuthData
			}
			out = append(out, nu)
		}
		ids = ids[n:]
	}
	return out, nil
}
