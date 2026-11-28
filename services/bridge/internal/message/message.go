// Package message is the bus message format of the MCP server (RT-6.36): one
// JSON body per NATS message on `<prefix>.ch.<channel>.<sender>`.
//
// The format is a contract with natsbus.py (`decode`/`_check`): version 1, a
// kind of post, reaction or unreaction; a post carries its text (which may be
// empty) and optionally the stream sequence of its thread's root, a reaction
// the sequence it reacts to and one emoji. With plan B piece 7 (RT-6.42 step
// 11) a post may name a Mattermost post as its root (`mm_root`) and a reaction
// the Mattermost post it reacts to (`mm_ref`) instead of a sequence. Decoding is as strict as the Python
// side: no invalid UTF-8, no duplicate keys, props with the bus prefix only and
// scalar values. Anything else is refused, never guessed.
package message

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"unicode/utf8"
)

// Kind is what a message does.
type Kind string

const (
	Post       Kind = "post"
	Reaction   Kind = "reaction"
	Unreaction Kind = "unreaction"
)

// MaxBody is the largest message the BUS stream carries (the Python side's
// MAX_BODY): a bigger one is refused here rather than mirrored.
const MaxBody = 65536

// Msg is one decoded message.
type Msg struct {
	Kind  Kind
	Text  string
	Root  *uint64 // post: the stream sequence of the thread's root
	Ref   *uint64 // reaction: the stream sequence of the post
	Emoji string  // reaction
	Props map[string]any
	// Plan B piece 7 (RT-6.42 step 12): a Mattermost post named by its id
	// instead of a sequence, never beside Root or Ref.
	MMRoot string // post: the Mattermost post it replies to
	MMRef  string // reaction: the Mattermost post it reacts to
}

// mmPost is a Mattermost post id: 26 characters of [a-z0-9].
func mmPost(s string) bool {
	if len(s) != 26 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9') {
			return false
		}
	}
	return true
}

// Decode reads one message body; prefix is the bus props prefix (MM_PROPS_PREFIX).
func Decode(body []byte, prefix string) (Msg, error) {
	switch {
	case len(body) == 0:
		return Msg{}, errors.New("not a bus message: empty")
	case len(body) > MaxBody:
		return Msg{}, fmt.Errorf("not a bus message: %d bytes, more than %d", len(body), MaxBody)
	case !utf8.Valid(body):
		return Msg{}, errors.New("not a bus message: not valid UTF-8")
	case prefix == "":
		return Msg{}, errors.New("not a bus message: no props prefix")
	}
	if err := noDuplicateKeys(body); err != nil {
		return Msg{}, fmt.Errorf("not a bus message: %w", err)
	}
	var raw struct {
		V      *int           `json:"v"`
		Kind   string         `json:"kind"`
		Text   string         `json:"text"`
		Root   *uint64        `json:"root"`
		Ref    *uint64        `json:"ref"`
		Emoji  string         `json:"emoji"`
		Props  map[string]any `json:"props"`
		MMRoot *string        `json:"mm_root"`
		MMRef  *string        `json:"mm_ref"`
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	if err := dec.Decode(&raw); err != nil {
		return Msg{}, fmt.Errorf("not a bus message: %w", err)
	}
	if raw.V == nil || *raw.V != 1 {
		return Msg{}, errors.New("not format v1")
	}
	m := Msg{Text: raw.Text, Props: raw.Props}
	if raw.MMRoot != nil && (Kind(raw.Kind) != Post || raw.Root != nil || !mmPost(*raw.MMRoot)) {
		return Msg{}, errors.New("mm_root is a Mattermost post id, on a post without root")
	}
	if raw.MMRef != nil && (Kind(raw.Kind) == Post || raw.Ref != nil || !mmPost(*raw.MMRef)) {
		return Msg{}, errors.New("mm_ref is a Mattermost post id, on a reaction without ref")
	}
	if raw.MMRoot != nil {
		m.MMRoot = *raw.MMRoot
	}
	if raw.MMRef != nil {
		m.MMRef = *raw.MMRef
	}
	if m.Props == nil {
		m.Props = map[string]any{}
	}
	for k, v := range m.Props {
		if !strings.HasPrefix(k, prefix) {
			return Msg{}, fmt.Errorf("prop %q does not start with the bus prefix %q", k, prefix)
		}
		switch v.(type) {
		case string, bool, json.Number:
		default:
			return Msg{}, fmt.Errorf("prop %q is no string or simple value", k)
		}
	}
	switch Kind(raw.Kind) {
	case Post:
		m.Kind = Post
		if raw.Ref != nil || raw.Emoji != "" {
			return Msg{}, errors.New("a post has no ref or emoji")
		}
		m.Root = raw.Root
		if raw.Root != nil && *raw.Root == 0 {
			return Msg{}, errors.New("a root is a stream sequence above 0")
		}
	case Reaction, Unreaction:
		m.Kind = Kind(raw.Kind)
		if raw.Text != "" || raw.Root != nil {
			return Msg{}, errors.New("a reaction has no text or root")
		}
		if (raw.Ref == nil || *raw.Ref == 0) && m.MMRef == "" {
			return Msg{}, errors.New("a reaction needs ref")
		}
		if raw.Emoji == "" || len(raw.Emoji) > 64 || strings.ContainsAny(raw.Emoji, "\r\n") {
			return Msg{}, errors.New("an emoji is one line of at most 64 characters")
		}
		m.Ref, m.Emoji = raw.Ref, raw.Emoji
	default:
		return Msg{}, fmt.Errorf("unknown kind %q", raw.Kind)
	}
	return m, nil
}

// noDuplicateKeys refuses a JSON body with the same key twice in one object,
// as the Python reader does: a second value could hide the first.
func noDuplicateKeys(body []byte) error {
	type frame struct {
		object, expectKey bool
		keys              map[string]bool
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	stack := []*frame{}
	for {
		tok, err := dec.Token()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		top := func() *frame {
			if len(stack) == 0 {
				return nil
			}
			return stack[len(stack)-1]
		}
		switch v := tok.(type) {
		case json.Delim:
			switch v {
			case '{':
				stack = append(stack, &frame{object: true, expectKey: true, keys: map[string]bool{}})
			case '[':
				stack = append(stack, &frame{})
			case '}', ']':
				if len(stack) == 0 {
					return errors.New("unbalanced JSON")
				}
				stack = stack[:len(stack)-1]
				if f := top(); f != nil && f.object {
					f.expectKey = true
				}
			}
		case string:
			if f := top(); f != nil && f.object && f.expectKey {
				if f.keys[v] {
					return fmt.Errorf("duplicate key %q", v)
				}
				f.keys[v] = true
				f.expectKey = false
			} else if f != nil && f.object {
				f.expectKey = true
			}
		default:
			if f := top(); f != nil && f.object {
				f.expectKey = true
			}
		}
	}
}

// Sender is the last token of the subject, which is the agent's name; the
// message body never names its sender.
func Sender(subject string) string {
	for i := len(subject) - 1; i >= 0; i-- {
		if subject[i] == '.' {
			return subject[i+1:]
		}
	}
	return subject
}
