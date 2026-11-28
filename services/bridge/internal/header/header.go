// Package header writes the bus line of a home's channel header for the
// home's crown holder (ADR-014 phase 5, plan B piece 9; RT-6.6 step 7). In a
// home where piece 9 is on, the holder's session sends the line on
// <req>.header.<agent>; the bridge writes it with that agent's own bot
// (RT-6.44) only while the claims say the sender holds the home's crown
// (RT-6.38), else it refuses. It replaces only the bus line and keeps what
// people wrote, as the MCP server does for a home not over NATS
// (lease.with_bus_header).
package header

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"
	"unicode"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/presence"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/requests"
)

// Piece is plan B's piece for the header line (ADR-014 phase 5).
const Piece = 9

// HeaderMax is Mattermost's header limit, in characters.
const HeaderMax = 1024

// The bus part of a header (RT-6.6 step 7), as the MCP server writes it
// (lease.HEADER_MARK, HEADER_RULE, BUS_HEADER).
const (
	HeaderMark = "\U0001F68C agent bus"
	headerRule = "plain post → \U0001F451 routes it · @agent → that agent · \U0001F64B = a question for you"
	BusHeader  = HeaderMark + " · " + headerRule
	crown      = "\U0001F451"
)

// ruleHead is the rule's first 12 characters: a rule's line after the mark's
// (a header from before 0.65.4) belongs to the bus part.
var ruleHead = string([]rune(headerRule)[:12])

// busPart finds the bus part of a header (lease._BUS_PART).
var busPart = regexp.MustCompile(`\n*` + regexp.QuoteMeta(HeaderMark) + `[^\n]*(?:\n` +
	regexp.QuoteMeta(ruleHead) + `[^\n]*)?`)

// lineRE is every line the MCP server writes (lease.bus_line): the bus header,
// then the ask mode when it is not direct (RT-6.25), then the mandate that
// holds, with its end (RT-6.26 step 3). Nothing else is written: a crown
// holder cannot put its own words into the header.
var lineRE = regexp.MustCompile(`^` + regexp.QuoteMeta(BusHeader) +
	`(?: · asks → ` + crown + ` first)?` +
	`(?: · tasks here from ` + crown + ` of ~[a-z0-9][a-z0-9_-]{0,63}(?: ends (\d{4}-\d\d-\d\d \d\d:\d\d)Z)?)?$`)

// CheckLine says whether line is one the MCP server writes (lineRE), its
// mandate's end a real time.
func CheckLine(line string) bool {
	m := lineRE.FindStringSubmatch(line)
	if m == nil {
		return false
	}
	if m[1] != "" {
		if _, err := time.Parse("2006-01-02 15:04", m[1]); err != nil {
			return false
		}
	}
	return true
}

// Bot is what the bridge reads and writes of a channel with an agent's own bot
// (mm.Client).
type Bot interface {
	ChannelHeader(ctx context.Context, channelID string) (string, error)
	SetChannelHeader(ctx context.Context, channelID, header string) error
}

// Bots gives an agent's own bot, joined to home, or nil while it has none.
type Bots interface {
	Bot(ctx context.Context, name, home string) Bot
}

// Switches are plan B's switches per channel (presence.Switches).
type Switches interface {
	On(piece int, channel string) bool
}

// Writer is the request scaffold's piece for kind "header".
type Writer struct {
	Homes     []string // the homes the bridge serves
	ChannelID func(ctx context.Context, home string) (string, error)
	Claims    *presence.Claims
	Presence  presence.PresenceOf
	Switches  Switches
	Bots      Bots
	// Member says whether an agent is a member of a channel; nil checks no
	// membership (the claims alone), until the bridge reads memberships.
	Member func(channel, name string) bool
	Window time.Duration // presence freshness; 90 s when 0
}

// Payload is a header request's payload: the home and the line.
type Payload struct {
	Channel string `json:"channel"`
	Line    string `json:"line"`
}

// Result is the answer's result: whether the header changed.
type Result struct {
	V       int  `json:"v"`
	Changed bool `json:"changed"`
}

// Piece answers one header request of agent (the subject's last token).
func (w *Writer) Piece(ctx context.Context, agent string, req requests.Request) (requests.Reply, error) {
	var p Payload
	dec := json.NewDecoder(bytes.NewReader(req.Payload))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil || dec.More() {
		return requests.Reply{}, errors.New(`a header request's payload is {"channel": <home>, "line": <bus line>}`)
	}
	if !slices.Contains(w.Homes, p.Channel) {
		return requests.Reply{}, fmt.Errorf("the bridge serves no home %q", p.Channel)
	}
	if !w.Switches.On(Piece, p.Channel) {
		return requests.Reply{}, fmt.Errorf("plan B piece 9 is off in ~%s: the header stays the session's", p.Channel)
	}
	if !CheckLine(p.Line) {
		return requests.Reply{}, errors.New("not a bus line of the header (RT-6.6 step 7)")
	}
	if !w.Claims.Read() {
		return requests.Reply{}, errors.New("the crown claims are not read yet; try again")
	}
	window := w.Window
	if window == 0 {
		window = 90 * time.Second
	}
	if holder := w.Claims.Holder(p.Channel, w.Presence, window, w.Member); holder != agent {
		return requests.Reply{}, fmt.Errorf("not the crown holder of ~%s by the claims (RT-6.38)", p.Channel)
	}
	cid, err := w.ChannelID(ctx, p.Channel)
	if err != nil {
		return requests.Reply{}, fmt.Errorf("the home's channel: %w", err)
	}
	bot := w.Bots.Bot(ctx, agent, p.Channel)
	if bot == nil {
		return requests.Reply{}, errors.New("the agent has no bot of its own now; try again")
	}
	current, err := bot.ChannelHeader(ctx, cid)
	if err != nil {
		return requests.Reply{}, fmt.Errorf("the header was not read: %w", err)
	}
	next := WithBusHeader(current, p.Line)
	changed := next != current
	if changed {
		if err := bot.SetChannelHeader(ctx, cid, next); err != nil {
			return requests.Reply{}, fmt.Errorf("the header was not written: %w", err)
		}
	}
	out, err := json.Marshal(Result{V: 1, Changed: changed})
	if err != nil {
		return requests.Reply{}, err
	}
	return requests.Reply{Result: out, Part: 1, Last: true}, nil
}

// WithBusHeader is header with its bus part replaced by bus, or bus appended
// (lease.with_bus_header). What people wrote stays; past HeaderMax it is cut,
// never the bus part.
func WithBusHeader(header, bus string) string {
	before, after := header, ""
	if loc := busPart.FindStringIndex(header); loc != nil {
		before, after = header[:loc[0]], header[loc[1]:]
	}
	before = strings.TrimRightFunc(before, unicode.IsSpace)
	before = cut(before, HeaderMax-runes(bus)-runes(after)-2)
	sep := ""
	if before != "" {
		sep = "\n\n"
	}
	after = cut(after, HeaderMax-runes(before)-runes(sep)-runes(bus))
	out := []rune(before + sep + bus + after)
	if len(out) > HeaderMax {
		out = out[:HeaderMax]
	}
	return string(out)
}

func runes(s string) int { return len([]rune(s)) }

func cut(text string, room int) string {
	r := []rune(text)
	if len(r) <= room {
		return text
	}
	if room > 1 {
		return strings.TrimRightFunc(string(r[:room-1]), unicode.IsSpace) + "…"
	}
	return ""
}
