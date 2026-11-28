package mm

import (
	"context"
	"net/url"
	"time"
)

// What a bot sets of itself for plan B piece 9 (ADR-014 phase 5): its
// presence, its custom status and, as a home's crown holder, the bus line of
// the home's header. Each goes with the token of the client it is called on,
// the agent's own bot (RT-6.44), never an admin's.

// SetStatus sets the bot's own presence: online, away, dnd or offline.
func (c *Client) SetStatus(ctx context.Context, status string) error {
	uid, err := c.Me(ctx)
	if err != nil {
		return err
	}
	return refuse(c.do(ctx, "PUT", "/api/v4/users/"+url.PathEscape(uid)+"/status",
		map[string]any{"user_id": uid, "status": status}, nil))
}

// SetCustomStatus sets the bot's own custom status, until expires.
func (c *Client) SetCustomStatus(ctx context.Context, emoji, text string, expires time.Time) error {
	return refuse(c.do(ctx, "PUT", "/api/v4/users/me/status/custom", map[string]any{
		"emoji": emoji, "text": text, "duration": "date_and_time",
		"expires_at": expires.UTC().Format(time.RFC3339)}, nil))
}

// ClearCustomStatus removes the bot's own custom status.
func (c *Client) ClearCustomStatus(ctx context.Context) error {
	return refuse(c.do(ctx, "DELETE", "/api/v4/users/me/status/custom", nil, nil))
}

// ChannelHeader reads a channel's header as it is now: a person may have
// changed it.
func (c *Client) ChannelHeader(ctx context.Context, channelID string) (string, error) {
	var ch struct {
		Header string `json:"header"`
	}
	if err := c.do(ctx, "GET", "/api/v4/channels/"+url.PathEscape(channelID), nil, &ch); err != nil {
		return "", refuse(err)
	}
	return ch.Header, nil
}

// SetChannelHeader sets a channel's header; Mattermost posts a system message
// for every change.
func (c *Client) SetChannelHeader(ctx context.Context, channelID, header string) error {
	return refuse(c.do(ctx, "PUT", "/api/v4/channels/"+url.PathEscape(channelID)+"/patch",
		map[string]any{"header": header}, nil))
}
