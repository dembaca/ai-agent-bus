package inbound

import (
	"bytes"
	"encoding/json"
	"regexp"
	"strings"
	"unicode/utf8"
)

// MaxText is where a person's text is cut (RT-6.42 step 2), as the mirror cuts
// the other way; MaxBody is the BUS stream's message size the body must fit.
const (
	MaxText = 16000
	MaxBody = 64 << 10
)

// mmID is a Mattermost id: 26 characters of [a-z0-9].
var mmID = regexp.MustCompile(`^[a-z0-9]{26}$`)

// emojiRE is a Mattermost emoji name.
var emojiRE = regexp.MustCompile(`^[a-zA-Z0-9_+-]{1,64}$`)

// PostBody is a person's post, format v1 (docs/protocol.md, point 18): no
// props, nothing that names the author — the subject's last token does.
type PostBody struct {
	V      int    `json:"v"`
	Kind   string `json:"kind"`
	Text   string `json:"text"`
	Name   string `json:"name"`
	MM     string `json:"mm"`
	MMRoot string `json:"mm_root,omitempty"`
	Root   uint64 `json:"root,omitempty"`
	Files  int    `json:"files"`
	Cut    bool   `json:"cut,omitempty"`
}

// ReactionBody is a person's reaction or unreaction, format v1.
type ReactionBody struct {
	V     int    `json:"v"`
	Kind  string `json:"kind"`
	Emoji string `json:"emoji"`
	Name  string `json:"name"`
	MM    string `json:"mm"`
	Ref   uint64 `json:"ref,omitempty"`
}

// marshal writes JSON without HTML escapes: a "<" stays one byte, so the cut
// counts what the stream stores.
func marshal(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	return bytes.TrimRight(buf.Bytes(), "\n"), nil
}

// EncodePost cuts the text at MaxText runes, then further until the body fits
// MaxBody, and marks a cut.
func EncodePost(b PostBody) ([]byte, error) {
	b.V, b.Kind = 1, "post"
	if !utf8.ValidString(b.Text) {
		b.Text = strings.ToValidUTF8(b.Text, "�")
	}
	runes := []rune(b.Text)
	if len(runes) > MaxText {
		runes, b.Cut = runes[:MaxText], true
		b.Text = string(runes)
	}
	for {
		raw, err := marshal(b)
		if err != nil {
			return nil, err
		}
		over := len(raw) - MaxBody
		if over <= 0 {
			return raw, nil
		}
		// an escaped rune takes at most six bytes ("\u0001")
		drop := max(over/6, 1)
		if drop >= len(runes) {
			runes = nil
		} else {
			runes = runes[:len(runes)-drop]
		}
		b.Text, b.Cut = string(runes), true
		if len(runes) == 0 {
			if raw, err := marshal(b); err != nil || len(raw) <= MaxBody {
				return raw, err
			}
			return nil, errTooLarge
		}
	}
}

type tooLarge struct{}

func (tooLarge) Error() string { return "inbound: the body does not fit without its text" }

var errTooLarge error = tooLarge{}

// EncodeReaction writes a reaction or unreaction.
func EncodeReaction(b ReactionBody, added bool) ([]byte, error) {
	b.V, b.Kind = 1, "unreaction"
	if added {
		b.Kind = "reaction"
	}
	return marshal(b)
}

// SenderToken is the subject's last token for an anchor: "." written as "~"
// as in the presence subject, and "-" for an account bound to nobody.
func SenderToken(anchor string) string {
	if anchor == "" {
		return "-"
	}
	return strings.ReplaceAll(anchor, ".", "~")
}
