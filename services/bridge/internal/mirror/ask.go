package mirror

// Plan B piece 6 (RT-6.11, ADR-014 phase 5): an ask to a human that the
// agent sent over NATS. With the piece switched on for the home, the bridge
// posts its card and keeps the one mention of the person it asks; with the
// piece off, an ask is mirrored like any other post (its mentions defused,
// no card), as before.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/message"
)

// AskPiece is plan B's piece number of asks to humans and their answers.
const AskPiece = 6

// MaxCard bounds the characters of an ask's card, all its texts together:
// the same bound as a post's text.
const MaxCard = MaxPost

// maxCardFields bounds the fields of one card; an ask has at most eight.
const maxCardFields = 20

// personTTL is how long a lookup of a person's username holds.
const personTTL = 5 * time.Minute

// MentionsPerHour bounds the mentions the bridge keeps for one sender in an
// hour, asks and reminders together: an agent asks one human at a time, and
// a reminder comes hours apart (RT-6.19); past it the mention is plain.
const MentionsPerHour = 20

// The props of an ask over NATS, after the props prefix: the card and the
// alternatives as JSON text, and the username its text mentions.
const (
	askCard    = "ask_card"
	askAnswers = "ask_answers"
	askTo      = "ask_to"
	askKind    = "a2a_kind" // the bus's note, flat: the post's kind
)

// cardColors are the colours an ask's card may have: red for a blocking
// ask, amber for any other, grey for the debug card after it (RT-6.31).
var cardColors = map[string]bool{"#d24b4e": true, "#e8a317": true, "#8b8b8b": true}

var usernameRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// Switches says which plan B pieces are on in a channel (presence.Switches).
type Switches interface {
	On(piece int, channel string) bool
}

// People gives the Mattermost user id of a username's account when it is
// a person's: it exists, is active and is no bot; "" otherwise, not an
// error. An error is a failed read (mm.Client.Person).
type People interface {
	Person(ctx context.Context, username string) (string, error)
}

// Members, when People has it, says whether an account is a member of a
// channel. Only for the log: a kept mention of a person outside the home is
// logged, never changed (the owner's decision on #281, an open point).
type Members interface {
	Member(ctx context.Context, channelID, userID string) (bool, error)
}

// logOutsider logs a kept mention of a person who is no member of the home's
// channel, or whose membership could not be read; what is posted stays as is.
func (m *Mirror) logOutsider(ctx context.Context, channelID, sender, keep string) {
	mem, ok := m.People.(Members)
	if keep == "" || !ok {
		return
	}
	id, _ := m.person(ctx, keep) // cached by ask
	if id == "" {
		return
	}
	in, err := mem.Member(ctx, channelID, id)
	switch {
	case err != nil:
		m.Log.Info("bridge: an ask's mention is kept; the person's membership was not read",
			"home", m.Home, "agent", sender, "error", err)
	case !in:
		m.Log.Warn("bridge: an ask mentions a person who is no member of the home",
			"home", m.Home, "agent", sender, "person", keep)
	}
}

// Directory names the person the login service's account record binds a
// Mattermost account to ("" for none): the people an ask may mention
// (ADR-014 phase 5, piece 6; inbound.Accounts).
type Directory interface {
	Anchor(userID string) string
}

type personSeen struct {
	id string
	at time.Time
}

// personCache remembers People's answers for personTTL.
type personCache struct {
	mu   sync.Mutex
	seen map[string]personSeen
}

func (c *personCache) get(name string, now time.Time) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	s, ok := c.seen[name]
	if !ok || now.Sub(s.at) >= personTTL {
		return "", false
	}
	return s.id, true
}

func (c *personCache) put(name, id string, now time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.seen == nil {
		c.seen = map[string]personSeen{}
	}
	if len(c.seen) > 1000 {
		c.seen = map[string]personSeen{}
	}
	c.seen[name] = personSeen{id, now}
}

// mentionLimit counts the mentions kept per sender in the last hour.
type mentionLimit struct {
	mu   sync.Mutex
	kept map[string][]keptMention
}

// keptMention is one unit spent at at, for the message id: a retry of the same
// message spends nothing more (#287).
type keptMention struct {
	at time.Time
	id string
}

// take says whether sender may have the mention of message id kept now, and
// counts it once: the same message again, as a retry after a failed create is,
// is let through without a second unit.
func (l *mentionLimit) take(sender, id string, now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.kept == nil {
		l.kept = map[string][]keptMention{}
	}
	recent := l.kept[sender][:0]
	for _, t := range l.kept[sender] {
		if now.Sub(t.at) < time.Hour {
			recent = append(recent, t)
		}
	}
	for _, t := range recent {
		if t.id == id {
			l.kept[sender] = recent
			return true
		}
	}
	if len(recent) >= MentionsPerHour {
		l.kept[sender] = recent
		return false
	}
	l.kept[sender] = append(recent, keptMention{now, id})
	return true
}

// asksOn says whether piece 6 is on for this home.
func (m *Mirror) asksOn() bool {
	return m.Switches != nil && m.Switches.On(AskPiece, m.Home)
}

// askKind says whether a post is an ask (kind ask, top-level) or its
// reminder (kind reminder, in a thread, also a Mattermost one named by
// `mm_root` with piece 7 on: an ask asked before the switch, target.go): ""
// for anything else, also for a reminder without a root, which would mention
// someone outside any ask.
func (m *Mirror) askKind(msg message.Msg) string {
	k, _ := msg.Props[m.Prefix+askKind].(string)
	switch {
	case k == "ask" && msg.Root == nil && msg.MMRoot == "",
		k == "reminder" && (msg.Root != nil || msg.MMRoot != ""):
		return k
	}
	return ""
}

// withoutAsk is props without the ask's own keys, which the bridge reads
// itself and never passes through.
func (m *Mirror) withoutAsk(props map[string]any) map[string]any {
	out := make(map[string]any, len(props))
	for k, v := range props {
		switch strings.TrimPrefix(k, m.Prefix) {
		case askCard, askAnswers, askTo:
			if strings.HasPrefix(k, m.Prefix) {
				continue
			}
		}
		out[k] = v
	}
	return out
}

// ask reads what the bridge does for an ask with piece 6 on: the one
// username whose mention it keeps ("" for none: everything is defused) and
// the card to post as the copy's attachments (nil for none). The username
// counts only when the first line of the text mentions it, it names no
// group (channel, all, here), Mattermost says it is an active person's
// account, the login service's record binds that account to a person (the
// directory) and the sender is within MentionsPerHour; a card that breaks a
// rule is left out and logged, the post goes without it. An error is a
// lookup that failed for a while (a 5xx, a 429, the network): the reader
// tries the message again; any other refusal counts as no person.
func (m *Mirror) ask(ctx context.Context, sender, msgID string, msg message.Msg) (string, []any, error) {
	text, props := msg.Text, msg.Props
	kind := m.askKind(msg)
	if kind == "" {
		return "", nil, nil
	}
	var cards []any
	if kind == "ask" {
		if raw, ok := props[m.Prefix+askCard].(string); ok {
			got, err := Card(raw)
			if err != nil {
				m.Log.Warn("bridge: an ask's card is left out", "home", m.Home, "error", err)
			} else {
				cards = got
			}
		}
	}
	name, _ := props[m.Prefix+askTo].(string)
	name = strings.ToLower(name)
	if !usernameRE.MatchString(name) || name == "channel" || name == "all" || name == "here" {
		return "", cards, nil
	}
	if _, ok := mentionAt(firstLine(text), name); !ok {
		return "", cards, nil
	}
	id, err := m.person(ctx, name)
	if err != nil {
		if transient(err) {
			return "", nil, err
		}
		m.Log.Warn("bridge: an ask's mention is plain: the username was refused", "home", m.Home,
			"agent", sender, "error", err)
		id = ""
	}
	if id == "" {
		return "", cards, nil
	}
	if m.Directory == nil || m.Directory.Anchor(id) == "" {
		m.Log.Info("bridge: an ask's mention is plain: no account record binds the person",
			"home", m.Home, "agent", sender)
		return "", cards, nil
	}
	if !m.mentions.take(sender, msgID, time.Now()) {
		m.Log.Warn("bridge: an ask's mention is plain: the sender's mentions this hour are used up",
			"home", m.Home, "agent", sender, "limit", MentionsPerHour)
		return "", cards, nil
	}
	return name, cards, nil
}

// transient says whether a failed lookup may succeed when tried again: a
// 5xx, a 429 or no answer at all (the network, a timeout).
func transient(err error) bool {
	var st interface{ HTTPStatus() int }
	if !errors.As(err, &st) {
		return true
	}
	s := st.HTTPStatus()
	return s >= 500 || s == 429
}

func (m *Mirror) person(ctx context.Context, name string) (string, error) {
	if m.People == nil {
		return "", nil
	}
	now := time.Now()
	if id, ok := m.people.get(name, now); ok {
		return id, nil
	}
	id, err := m.People.Person(ctx, name)
	if err != nil {
		return "", err
	}
	m.people.put(name, id, now)
	return id, nil
}

func firstLine(text string) string {
	if i := strings.IndexByte(text, '\n'); i >= 0 {
		return text[:i]
	}
	return text
}

func nameByte(b byte) bool {
	return b >= 'a' && b <= 'z' || b >= 'A' && b <= 'Z' || b >= '0' && b <= '9' || b == '.' || b == '_' || b == '-'
}

// mentionAt is the byte index of the first "@name" in text that ends where
// the name ends (case does not matter), and whether there is one.
func mentionAt(text, name string) (int, bool) {
	for i := 0; i < len(text); i++ {
		if text[i] != '@' || i+1+len(name) > len(text) {
			continue
		}
		if !strings.EqualFold(text[i+1:i+1+len(name)], name) {
			continue
		}
		if end := i + 1 + len(name); end < len(text) && nameByte(text[end]) {
			continue
		}
		return i, true
	}
	return 0, false
}

// DefuseMentionsExcept is DefuseMentions but for the first mention of keep
// on the text's first line, which stays a mention; "" keeps none.
func DefuseMentionsExcept(text, keep string) string {
	if keep == "" {
		return DefuseMentions(text)
	}
	i, ok := mentionAt(firstLine(text), keep)
	if !ok {
		return DefuseMentions(text)
	}
	end := i + 1 + len(keep)
	return DefuseMentions(text[:i]) + text[i:end] + DefuseMentions(text[end:])
}

type cardField struct {
	Title string `json:"title"`
	Value string `json:"value"`
	Short bool   `json:"short"`
}

type card struct {
	Color    string      `json:"color"`
	Fallback string      `json:"fallback"`
	Title    string      `json:"title"`
	Fields   []cardField `json:"fields"`
}

// Card reads an ask's card from its JSON text: one or two cards (the ask's
// and a debug card), each with a colour of cardColors, a fallback, an
// optional title and at most maxCardFields fields of a title, a value and
// short; no other key. Every text is neutralised as a post's is (no
// mention, no sender mark), and all of them together hold at most MaxCard
// characters. The result is the attachments prop of the copy.
func Card(raw string) ([]any, error) {
	if !utf8.ValidString(raw) {
		return nil, errors.New("the card is not valid UTF-8")
	}
	dec := json.NewDecoder(bytes.NewReader([]byte(raw)))
	dec.DisallowUnknownFields()
	var cards []card
	if err := dec.Decode(&cards); err != nil {
		return nil, errors.New("the card is not a list of cards")
	}
	if dec.More() {
		return nil, errors.New("the card has text after it")
	}
	if len(cards) == 0 || len(cards) > 2 {
		return nil, errors.New("one or two cards")
	}
	total := 0
	clean := func(s string) string {
		total += utf8.RuneCountInString(s)
		return DefuseMentions(unmark(s))
	}
	out := make([]any, 0, len(cards))
	for _, c := range cards {
		if !cardColors[c.Color] {
			return nil, errors.New("a card's colour is not an ask's")
		}
		if len(c.Fields) > maxCardFields {
			return nil, errors.New("too many fields")
		}
		fields := make([]any, 0, len(c.Fields))
		for _, f := range c.Fields {
			fields = append(fields, map[string]any{"title": clean(f.Title), "value": clean(f.Value), "short": f.Short})
		}
		a := map[string]any{"color": c.Color, "fallback": clean(c.Fallback), "fields": fields}
		if c.Title != "" {
			a["title"] = clean(c.Title)
		}
		out = append(out, a)
	}
	if total > MaxCard {
		return nil, errors.New("the card is too long")
	}
	return out, nil
}
