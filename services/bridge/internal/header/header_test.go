package header

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
)

const (
	crownMark = "\U0001F451"
	today     = "\U0001F68C agent bus · plain post → \U0001F451 routes it · @agent → that agent · \U0001F64B = a question for you"
	endDate   = "2026-12-01 00:00Z"
)

func TestBusHeaderText(t *testing.T) {
	if BusHeader != today {
		t.Fatalf("got %q", BusHeader)
	}
}

func oldBus(name, at string) string {
	return "\U0001F68C agent bus · \U0001F451 @" + name + " since " + at + "\n" +
		"plain post → \U0001F451 routes it · @agent → that agent · \U0001F64B = a question for you"
}

func TestWithBusHeaderLimitAndSeparator(t *testing.T) {
	out := WithBusHeader(strings.Repeat("x", 2000), BusHeader)
	if len([]rune(out)) != HeaderMax || !strings.HasSuffix(out, BusHeader) || !strings.Contains(out, "…\n\n") {
		t.Fatalf("got %d runes", len([]rune(out)))
	}
	if got := WithBusHeader("", BusHeader); got != BusHeader {
		t.Fatalf("got %q", got)
	}
	if got := WithBusHeader("a\n"+BusHeader, BusHeader); got != "a\n\n"+BusHeader {
		t.Fatalf("got %q", got)
	}
}

func TestWithBusHeaderReplacesEveryOlderForm(t *testing.T) {
	next := BusHeader + " · asks → " + crownMark + " first · tasks here from " + crownMark + " of ~hub ends " + endDate
	for _, old := range []string{
		today,
		oldBus("agent-old", "22:40"),
		today + " · asks → " + crownMark + " first",
		today + " · tasks here from " + crownMark + " of ~hub ends 2026-11-20 00:00Z",
		today + " · asks → " + crownMark + " first · tasks here from " + crownMark + " of ~x ends 2026-01-01 00:00Z",
	} {
		if got := WithBusHeader("Release on Fridays\n\n"+old+"\nmore notes", next); got != "Release on Fridays\n\n"+next+"\nmore notes" {
			t.Errorf("old %q: got %q", old, got)
		}
	}
	if got := WithBusHeader("Notes\n\n"+next, today); got != "Notes\n\n"+today {
		t.Fatalf("got %q", got)
	}
}

func TestTheLongestLineStaysWithinTheLimit(t *testing.T) {
	line := BusHeader + " · asks → " + crownMark + " first · tasks here from " + crownMark + " of ~" +
		strings.Repeat("h", 64) + " ends " + endDate
	out := WithBusHeader(strings.Repeat("x", 2000), line)
	if len([]rune(out)) != HeaderMax || !strings.HasSuffix(out, line) || !strings.Contains(out, "…\n\n") {
		t.Fatalf("got %d runes", len([]rune(out)))
	}
}

type fakeBot struct {
	header        string
	reads, writes int
	calls         int
}

func (b *fakeBot) ChannelHeader(context.Context, string) (string, error) {
	b.calls++
	b.reads++
	return b.header, nil
}

func (b *fakeBot) SetChannelHeader(_ context.Context, _, h string) error {
	b.calls++
	b.writes++
	b.header = h
	return nil
}

type fakeBots struct{ bot *fakeBot }

func (f fakeBots) Bot(context.Context, string, string) Bot {
	if f.bot == nil {
		return nil
	}
	return f.bot
}

type fakeSwitches map[string]bool

func (s fakeSwitches) On(piece int, channel string) bool { return piece == Piece && s[channel] }

var now = time.Date(2026, 11, 27, 12, 0, 0, 0, time.UTC)

type env struct {
	w      *Writer
	bot    *fakeBot
	claims *presence.Claims
	sw     fakeSwitches
}

func newEnv(t *testing.T) *env {
	t.Helper()
	e := &env{bot: &fakeBot{}, sw: fakeSwitches{"home": true}}
	e.claims = &presence.Claims{Clock: func() time.Time { return now }}
	e.claims.Begin()
	e.claims.Observed("home", "agent-a", 2, now.Add(-time.Hour))
	e.claims.Seed(now)
	e.claims.Commit()
	snap := presence.New()
	snap.Observed("agent-a", "o", "", presence.Online, now.Add(-5*time.Second))
	e.w = &Writer{Homes: []string{"home"}, Claims: e.claims, Presence: snap, Switches: e.sw,
		Bots: fakeBots{e.bot},
		ChannelID: func(context.Context, string) (string, error) {
			return strings.Repeat("c", 26), nil
		}}
	return e
}

func req(t *testing.T, payload any) requests.Request {
	t.Helper()
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	return requests.Request{V: 1, Request: "abcdefghijklmnop", Kind: "header", Payload: raw}
}

func line(l string) map[string]any { return map[string]any{"channel": "home", "line": l} }

func TestAcceptedLines(t *testing.T) {
	for _, l := range []string{
		BusHeader,
		BusHeader + " · asks → " + crownMark + " first",
		BusHeader + " · tasks here from " + crownMark + " of ~home ends 2026-11-27 12:00Z",
		BusHeader + " · asks → " + crownMark + " first · tasks here from " + crownMark + " of ~home ends 2026-11-27 12:00Z",
		BusHeader + " · tasks here from " + crownMark + " of ~home",
	} {
		e := newEnv(t)
		reps, err := e.w.Piece(context.Background(), "agent-a", req(t, line(l)))
		var rep requests.Reply
		if len(reps) > 0 {
			rep = reps[0]
		}
		if err != nil {
			t.Errorf("%q: %v", l, err)
			continue
		}
		if string(rep.Result) != `{"v":1,"changed":true}` || e.bot.header != l {
			t.Errorf("%q: result %s header %q", l, rep.Result, e.bot.header)
		}
	}
}

func TestRefusals(t *testing.T) {
	type tc struct {
		name    string
		payload any
		agent   string
		change  func(e *env)
	}
	for _, c := range []tc{
		{name: "unknown field", payload: map[string]any{"channel": "home", "line": BusHeader, "extra": 1}},
		{name: "home not served", payload: map[string]any{"channel": "elsewhere", "line": BusHeader}},
		{name: "switch off", payload: line(BusHeader), change: func(e *env) { e.sw["home"] = false }},
		{name: "other words", payload: line(BusHeader + " · and more")},
		{name: "newline", payload: line(BusHeader + "\nsecond line")},
		{name: "a trailing newline", payload: line(BusHeader + "\n")},
		{name: "an uppercase home", payload: line(BusHeader + " · tasks here from " + crownMark + " of ~Home")},
		{name: "a date that is none", payload: line(BusHeader + " · tasks here from " + crownMark +
			" of ~home ends 2026-13-45 25:61Z")},
		{name: "the holder's presence stale", payload: line(BusHeader), change: func(e *env) {
			e.w.Presence.(*presence.Snapshot).Observed("agent-a", "o", "", presence.Online, now.Add(-2*time.Minute))
		}},
		{name: "another served home without a claim", payload: map[string]any{"channel": "other", "line": BusHeader},
			change: func(e *env) {
				e.w.Homes = append(e.w.Homes, "other")
				e.sw["other"] = true
			}},
		{name: "claims not read", payload: line(BusHeader), change: func(e *env) {
			e.w.Claims = &presence.Claims{Clock: func() time.Time { return now }}
		}},
		{name: "not the holder", payload: line(BusHeader), change: func(e *env) {
			e.claims.Observed("home", "agent-b", 1, now.Add(-2*time.Hour))
			e.w.Presence.(*presence.Snapshot).Observed("agent-b", "o", "", presence.Online, now)
		}},
		{name: "no bot", payload: line(BusHeader), change: func(e *env) { e.w.Bots = fakeBots{} }},
		{name: "channel lookup fails", payload: line(BusHeader), change: func(e *env) {
			e.w.ChannelID = func(context.Context, string) (string, error) { return "", errors.New("x") }
		}},
	} {
		e := newEnv(t)
		if c.change != nil {
			c.change(e)
		}
		reps, err := e.w.Piece(context.Background(), "agent-a", req(t, c.payload))
		var rep requests.Reply
		if len(reps) > 0 {
			rep = reps[0]
		}
		if err == nil || len(rep.Result) != 0 {
			t.Errorf("%s: accepted: %s", c.name, rep.Result)
		}
		if e.bot.calls != 0 {
			t.Errorf("%s: the bot was called %d times", c.name, e.bot.calls)
		}
	}
}

func TestTheHeaderKeepsPeoplesTextAndIsWrittenOnce(t *testing.T) {
	e := newEnv(t)
	e.bot.header = "people wrote this"
	l := BusHeader + " · asks → " + crownMark + " first"
	reps, err := e.w.Piece(context.Background(), "agent-a", req(t, line(l)))
	var rep requests.Reply
	if len(reps) > 0 {
		rep = reps[0]
	}
	if err != nil {
		t.Fatal(err)
	}
	if want := WithBusHeader("people wrote this", l); e.bot.header != want || !strings.HasPrefix(want, "people wrote this\n\n") {
		t.Fatalf("header %q", e.bot.header)
	}
	if string(rep.Result) != `{"v":1,"changed":true}` {
		t.Fatalf("reply %+v", rep)
	}
	reps, err = e.w.Piece(context.Background(), "agent-a", req(t, line(l)))
	if err != nil {
		t.Fatal(err)
	}
	rep = reps[0]
	if string(rep.Result) != `{"v":1,"changed":false}` || e.bot.writes != 1 {
		t.Fatalf("result %s, writes %d", rep.Result, e.bot.writes)
	}
}

func TestTrailingJSONAfterThePayloadIsRefused(t *testing.T) {
	e := newEnv(t)
	raw, err := json.Marshal(line(BusHeader))
	if err != nil {
		t.Fatal(err)
	}
	r := requests.Request{V: 1, Request: "abcdefghijklmnop", Kind: "header",
		Payload: append(raw, []byte(` {"channel":"home"}`)...)}
	if _, err := e.w.Piece(context.Background(), "agent-a", r); err == nil || e.bot.calls != 0 {
		t.Fatalf("accepted a payload with trailing JSON (err %v, calls %d)", err, e.bot.calls)
	}
}

// TestTheSharedVectorWithPython reads the lines and headers the MCP server's
// lease.bus_line and lease.with_bus_header make (testdata/bus_lines.json, which
// mcp/tests/test_planb_piece9.py checks against the Python side): every line
// is one the bridge writes, and the header comes out the same.
func TestTheSharedVectorWithPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/bus_lines.json")
	if err != nil {
		t.Fatal(err)
	}
	var v struct {
		BusHeader string `json:"bus_header"`
		Lines     []struct {
			Line string `json:"line"`
		} `json:"lines"`
		Headers []struct {
			Header, Bus, Want string
		} `json:"headers"`
	}
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	if v.BusHeader != BusHeader || len(v.Lines) == 0 || len(v.Headers) == 0 {
		t.Fatalf("the vector: %q, %d lines, %d headers", v.BusHeader, len(v.Lines), len(v.Headers))
	}
	for _, l := range v.Lines {
		if !CheckLine(l.Line) {
			t.Errorf("a line Python writes is refused: %q", l.Line)
		}
	}
	for _, h := range v.Headers {
		if got := WithBusHeader(h.Header, h.Bus); got != h.Want {
			t.Errorf("WithBusHeader(%.40q…) = %.80q…, Python %.80q…", h.Header, got, h.Want)
		}
	}
}
