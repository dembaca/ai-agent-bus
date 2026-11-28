package mm

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"
	"time"
)

const botID = "abcdefghijklmnopqrstuvwxyz"

type seen struct {
	method, path, auth string
	body               map[string]any
}

// statusServer answers every request with code and body, and records them.
func statusServer(t *testing.T, code int, answer string) (*Client, *[]seen) {
	t.Helper()
	var got []seen
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s := seen{method: r.Method, path: r.URL.Path, auth: r.Header.Get("Authorization")}
		if raw, _ := io.ReadAll(r.Body); len(raw) > 0 {
			if err := json.Unmarshal(raw, &s.body); err != nil {
				t.Errorf("body %q: %v", raw, err)
			}
		}
		got = append(got, s)
		w.WriteHeader(code)
		_, _ = io.WriteString(w, answer)
	}))
	t.Cleanup(srv.Close)
	return New(srv.URL, "tok").As(botID), &got
}

func check(t *testing.T, got []seen, method, path string, body map[string]any) {
	t.Helper()
	if len(got) != 1 {
		t.Fatalf("got %d requests", len(got))
	}
	g := got[0]
	if g.method != method || g.path != path || g.auth != "Bearer tok" || !reflect.DeepEqual(g.body, body) {
		t.Fatalf("got %+v, want %s %s %v", g, method, path, body)
	}
}

func TestSetStatus(t *testing.T) {
	c, got := statusServer(t, 200, `{}`)
	if err := c.SetStatus(context.Background(), "away"); err != nil {
		t.Fatal(err)
	}
	check(t, *got, "PUT", "/api/v4/users/"+botID+"/status", map[string]any{"user_id": botID, "status": "away"})
}

func TestSetCustomStatus(t *testing.T) {
	c, got := statusServer(t, 200, `{}`)
	end := time.Date(2026, 11, 27, 13, 30, 0, 0, time.FixedZone("x", 3600))
	if err := c.SetCustomStatus(context.Background(), "zzz", "idle · ~home", end); err != nil {
		t.Fatal(err)
	}
	check(t, *got, "PUT", "/api/v4/users/me/status/custom", map[string]any{
		"emoji": "zzz", "text": "idle · ~home", "duration": "date_and_time", "expires_at": "2026-11-27T12:30:00Z"})
}

func TestClearCustomStatus(t *testing.T) {
	c, got := statusServer(t, 200, `{}`)
	if err := c.ClearCustomStatus(context.Background()); err != nil {
		t.Fatal(err)
	}
	check(t, *got, "DELETE", "/api/v4/users/me/status/custom", nil)
}

func TestChannelHeader(t *testing.T) {
	c, got := statusServer(t, 200, `{"id":"c1","header":"people's text"}`)
	h, err := c.ChannelHeader(context.Background(), "c1")
	if err != nil || h != "people's text" {
		t.Fatalf("got %q, %v", h, err)
	}
	check(t, *got, "GET", "/api/v4/channels/c1", nil)
}

func TestSetChannelHeader(t *testing.T) {
	c, got := statusServer(t, 200, `{}`)
	if err := c.SetChannelHeader(context.Background(), "c1", "new header"); err != nil {
		t.Fatal(err)
	}
	check(t, *got, "PUT", "/api/v4/channels/c1/patch", map[string]any{"header": "new header"})
}

func TestStatusCallsReturnAnErrorOn404(t *testing.T) {
	c, _ := statusServer(t, 404, `{"message":"not found"}`)
	ctx := context.Background()
	if c.SetStatus(ctx, "online") == nil || c.SetCustomStatus(ctx, "zzz", "x", time.Now()) == nil ||
		c.ClearCustomStatus(ctx) == nil || c.SetChannelHeader(ctx, "c1", "h") == nil {
		t.Fatal("a 404 gave no error")
	}
	if _, err := c.ChannelHeader(ctx, "c1"); err == nil {
		t.Fatal("a 404 gave no error")
	}
}
