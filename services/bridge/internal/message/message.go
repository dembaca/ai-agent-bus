// Package message is the bus message format of the MCP server (RT-6.36): one
// JSON body per NATS message on `<prefix>.ch.<channel>.<sender>`.
//
// The format is a contract with natsbus.py (`Message.decode`): version 1, a
// kind of post, reaction or unreaction; a post carries its text and optionally
// the stream sequence of its thread's root, a reaction the sequence it reacts
// to and an emoji. Anything else is refused, never guessed.
package message

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"unicode/utf8"
)

// Kind is what a message does.
type Kind string

const (
	Post       Kind = "post"
	Reaction   Kind = "reaction"
	Unreaction Kind = "unreaction"
)

// MaxText bounds a mirrored text: the PRESENCE stream's own limit for records
// is 4 KiB; a bus post is bounded by the BUS stream at 64 KiB, and Mattermost
// takes 16 383 characters. Posts above the Mattermost limit are cut by the
// mirror, not here.
const MaxText = 65536

// Msg is one decoded message.
type Msg struct {
	Kind  Kind
	Text  string
	Root  *uint64 // post: the stream sequence of the thread's root
	Ref   *uint64 // reaction: the stream sequence of the post
	Emoji string  // reaction
	Props map[string]any
}

// Decode reads one message body.
func Decode(body []byte) (Msg, error) {
	var raw struct {
		V     *int           `json:"v"`
		Kind  string         `json:"kind"`
		Text  string         `json:"text"`
		Root  *uint64        `json:"root"`
		Ref   *uint64        `json:"ref"`
		Emoji string         `json:"emoji"`
		Props map[string]any `json:"props"`
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
	switch Kind(raw.Kind) {
	case Post:
		m.Kind = Post
		if raw.Ref != nil || raw.Emoji != "" {
			return Msg{}, errors.New("a post has no ref or emoji")
		}
		if m.Text == "" || len(m.Text) > MaxText || !utf8.ValidString(m.Text) {
			return Msg{}, errors.New("a post needs text")
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
		if raw.Ref == nil || *raw.Ref == 0 {
			return Msg{}, errors.New("a reaction needs ref")
		}
		if raw.Emoji == "" || len(raw.Emoji) > 64 || !utf8.ValidString(raw.Emoji) {
			return Msg{}, errors.New("a reaction needs an emoji")
		}
		m.Ref, m.Emoji = raw.Ref, raw.Emoji
	default:
		return Msg{}, fmt.Errorf("unknown kind %q", raw.Kind)
	}
	if m.Props == nil {
		m.Props = map[string]any{}
	}
	return m, nil
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
