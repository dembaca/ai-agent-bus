package mirror

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/message"
)

// RT-6.11, plan B piece 6: the bridge posts an ask the agent sent over NATS
// with its card and the one mention of the person it asks.

type fakeSwitches map[int]bool

func (f fakeSwitches) On(piece int, channel string) bool { return channel == "home" && f[piece] }

type fakePeople struct {
	people map[string]bool
	asked  []string
	err    error
}

func (f *fakePeople) Person(_ context.Context, name string) (bool, error) {
	f.asked = append(f.asked, name)
	if f.err != nil {
		return false, f.err
	}
	return f.people[name], nil
}

const askCardJSON = `[{"color":"#e8a317","fallback":"dembaca: Merge #42?","title":"Merge #42?",` +
	`"fields":[{"title":"From","value":"@agent-a","short":true},{"title":"Decision","value":"merge it, @all","short":false},` +
	`{"title":"Answer","value":"👍 = go · 👎 = no-go · or reply in this thread","short":false}]}]`

func askMsg(text string, extra map[string]any) message.Msg {
	props := map[string]any{"x_a2a_kind": "ask", "x_ask_to": "dembaca", "x_ask_card": askCardJSON,
		"x_ask_answers": `["go","no-go"]`}
	for k, v := range extra {
		props[k] = v
	}
	return message.Msg{Kind: message.Post, Text: text, Props: props}
}

func askMirror(t *testing.T, on bool) (*Mirror, *fakeSink, *fakePeople) {
	t.Helper()
	m, sink, _ := newMirror(t)
	m.Prefix = "x_"
	m.Switches = fakeSwitches{AskPiece: on}
	people := &fakePeople{people: map[string]bool{"dembaca": true}}
	m.People = people
	return m, sink, people
}

func TestWithPiece6OnAnAskKeepsItsOneMentionAndPostsItsCard(t *testing.T) {
	m, sink, people := askMirror(t, true)
	if err := m.Handle(context.Background(), channel, "agent-a", 7, askMsg("❓ @dembaca and @bob, @channel", nil)); err != nil {
		t.Fatal(err)
	}
	got := sink.posts[0]
	if !strings.Contains(got.text, "❓ @dembaca and @​bob, @​channel") {
		t.Fatalf("the ask's own mention only: %q", got.text)
	}
	cards, ok := got.props["attachments"].([]any)
	if !ok || len(cards) != 1 {
		t.Fatalf("no card: %+v", got.props)
	}
	c := cards[0].(map[string]any)
	if c["title"] != "Merge #42?" || c["color"] != "#e8a317" {
		t.Fatalf("card: %+v", c)
	}
	fields := c["fields"].([]any)
	if v := fields[1].(map[string]any)["value"]; v != "merge it, @​all" {
		t.Fatalf("a mention in the card would notify: %q", v)
	}
	for _, k := range []string{"x_ask_card", "x_ask_to", "x_ask_answers"} {
		if _, ok := got.props[k]; ok {
			t.Fatalf("%s reached Mattermost: %+v", k, got.props)
		}
	}
	if got.props["x_nats_id"] != "n1:home:7" || got.props["x_a2a_kind"] != "ask" {
		t.Fatalf("props: %+v", got.props)
	}
	// a second ask to the same person: the lookup is remembered
	if err := m.Handle(context.Background(), channel, "agent-a", 8, askMsg("❓ @dembaca", nil)); err != nil {
		t.Fatal(err)
	}
	if len(people.asked) != 1 {
		t.Fatalf("looked up again: %v", people.asked)
	}
}

func TestWithPiece6OffAnAskIsMirroredAsBefore(t *testing.T) {
	m, sink, people := askMirror(t, false)
	if err := m.Handle(context.Background(), channel, "agent-a", 7, askMsg("❓ @dembaca", nil)); err != nil {
		t.Fatal(err)
	}
	got := sink.posts[0]
	if !strings.Contains(got.text, "@​dembaca") {
		t.Fatalf("off: every mention is defused: %q", got.text)
	}
	if _, ok := got.props["attachments"]; ok {
		t.Fatalf("off: no card: %+v", got.props)
	}
	if len(people.asked) != 0 {
		t.Fatalf("off: no lookup: %v", people.asked)
	}
	// and the same with no switches at all
	m.Switches = nil
	if err := m.Handle(context.Background(), channel, "agent-a", 8, askMsg("❓ @dembaca", nil)); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(sink.posts[1].text, "@​dembaca") {
		t.Fatalf("no switches: defused: %q", sink.posts[1].text)
	}
}

func TestAnAsksMentionNeedsAPersonNamedOnItsFirstLine(t *testing.T) {
	cases := []struct {
		name  string
		text  string
		props map[string]any
	}{
		{"a bot", "❓ @agent-c", map[string]any{"x_ask_to": "agent-c"}},
		{"a group", "❓ @channel", map[string]any{"x_ask_to": "channel"}},
		{"not on the first line", "❓ ask\n@dembaca", nil},
		{"a longer name", "❓ @dembaca2", nil},
		{"not an ask", "@dembaca hi", map[string]any{"x_a2a_kind": "TASK"}},
		{"no username", "❓ @dembaca", map[string]any{"x_ask_to": "Not A Name!"}},
	}
	for _, tc := range cases {
		m, sink, _ := askMirror(t, true)
		if err := m.Handle(context.Background(), channel, "agent-a", 7, askMsg(tc.text, tc.props)); err != nil {
			t.Fatal(tc.name, err)
		}
		if strings.Contains(strings.ReplaceAll(sink.posts[0].text, "@​", ""), "@") {
			t.Fatalf("%s: a mention kept: %q", tc.name, sink.posts[0].text)
		}
	}
	// a reminder in the ask's thread keeps its mention, and has no card
	m, sink, _ := askMirror(t, true)
	if err := m.Handle(context.Background(), channel, "agent-a", 9, askMsg("⏰ @dembaca — reminder", map[string]any{"x_a2a_kind": "reminder"})); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(sink.posts[0].text, "⏰ @dembaca —") {
		t.Fatalf("reminder: %q", sink.posts[0].text)
	}
	if _, ok := sink.posts[0].props["attachments"]; ok {
		t.Fatalf("a reminder has no card: %+v", sink.posts[0].props)
	}
}

func TestAFailedLookupIsTriedAgainNotPostedWithout(t *testing.T) {
	m, sink, people := askMirror(t, true)
	people.err = errors.New("mattermost down")
	if err := m.Handle(context.Background(), channel, "agent-a", 7, askMsg("❓ @dembaca", nil)); err == nil {
		t.Fatal("a failed lookup must fail the message, so the reader retries it")
	}
	if len(sink.posts) != 0 {
		t.Fatalf("posted without the mention: %+v", sink.posts)
	}
}

func TestACardThatBreaksARuleIsLeftOut(t *testing.T) {
	for _, bad := range []string{
		`not json`,
		`[]`,
		`[{"color":"#000000","fallback":"x","fields":[]}]`,
		`[{"color":"#e8a317","fallback":"x","fields":[],"author_icon":"http://x"}]`,
		`[{"color":"#e8a317","fallback":"x","fields":[{"title":"t","value":"v","short":false,"x":1}]}]`,
		`[{"color":"#e8a317","fallback":"x","fields":[]},{"color":"#e8a317","fallback":"x","fields":[]},{"color":"#e8a317","fallback":"x","fields":[]}]`,
		`[{"color":"#e8a317","fallback":"` + strings.Repeat("a", MaxCard+1) + `","fields":[]}]`,
		`[{"color":"#e8a317","fallback":"x","fields":[]}] trailing`,
	} {
		if _, err := Card(bad); err == nil {
			t.Errorf("accepted: %.80s", bad)
		}
		m, sink, _ := askMirror(t, true)
		if err := m.Handle(context.Background(), channel, "agent-a", 7, askMsg("❓ @dembaca", map[string]any{"x_ask_card": bad})); err != nil {
			t.Fatal(err)
		}
		if _, ok := sink.posts[0].props["attachments"]; ok || !strings.Contains(sink.posts[0].text, "❓ @dembaca") {
			t.Fatalf("a bad card: %+v %q", sink.posts[0].props, sink.posts[0].text)
		}
	}
	// a debug card after the ask's own is kept
	got, err := Card(`[{"color":"#d24b4e","fallback":"a","fields":[]},{"color":"#8b8b8b","fallback":"MCP debug","title":"debug","fields":[]}]`)
	if err != nil || len(got) != 2 {
		t.Fatalf("debug card: %v %v", got, err)
	}
}

func TestTheOwnBotKeepsTheAsksMentionToo(t *testing.T) {
	m, _, _ := askMirror(t, true)
	own := &fakeSink{}
	m.Bots = &fakeBots{own: map[string]*fakeSink{"agent-a": own}}
	if err := m.Handle(context.Background(), channel, "agent-a", 7, askMsg("❓ @dembaca, see @bob", nil)); err != nil {
		t.Fatal(err)
	}
	if len(own.posts) != 1 || own.posts[0].text != "❓ @dembaca, see @​bob" {
		t.Fatalf("own bot: %+v", own.posts)
	}
	if _, ok := own.posts[0].props["attachments"]; !ok {
		t.Fatalf("own bot: no card: %+v", own.posts[0].props)
	}
}
