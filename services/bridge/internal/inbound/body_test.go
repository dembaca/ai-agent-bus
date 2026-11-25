package inbound

import (
	"encoding/json"
	"strings"
	"testing"
	"unicode/utf8"
)

// RT-6.42 step 2: a text over 16 000 runes is cut there, and further until
// the body fits the stream's 64 KiB, and marked cut.
func TestALongTextIsCutToFitAndMarked(t *testing.T) {
	short, err := EncodePost(PostBody{Text: "hi", Name: "alice", MM: strings.Repeat("a", 26)})
	if err != nil || strings.Contains(string(short), "cut") {
		t.Fatalf("%s %v", short, err)
	}
	for _, text := range []string{strings.Repeat("ä", 20000), strings.Repeat("\u0001", 16000), strings.Repeat("😀", 16001)} {
		raw, err := EncodePost(PostBody{Text: text, Name: "alice", MM: strings.Repeat("a", 26)})
		if err != nil {
			t.Fatal(err)
		}
		var body PostBody
		if err := json.Unmarshal(raw, &body); err != nil {
			t.Fatal(err)
		}
		if len(raw) > MaxBody || !body.Cut || utf8.RuneCountInString(body.Text) > MaxText || body.Text == "" {
			t.Fatalf("%d bytes, cut %v, %d runes", len(raw), body.Cut, utf8.RuneCountInString(body.Text))
		}
	}
}

// RT-6.42 step 4: the sender token is the anchor with "." as "~", "-" when
// the account is bound to nobody.
func TestTheSenderToken(t *testing.T) {
	if SenderToken("") != "-" || SenderToken("a.b.c") != "a~b~c" {
		t.Fatal(SenderToken(""), SenderToken("a.b.c"))
	}
}

// RT-6.42 steps 3 and 10: an account record decodes strictly — format v1,
// exactly its fields, no unknown or duplicate key, nothing empty; a JSON
// answer that landed on the subject is none.
func TestAnAccountRecordDecodesStrictly(t *testing.T) {
	id := strings.Repeat("a", 26)
	good := `{"v":1,"account":"` + id + `","anchor":"alice","name":"alice","approver":true,"token_iat":1,"iat":2,"exp":3}`
	rec, err := DecodeAccount([]byte(good))
	if err != nil || rec.Anchor != "alice" || !rec.Approver || rec.Exp != 3 {
		t.Fatalf("%+v %v", rec, err)
	}
	if rec, err := DecodeAccount([]byte(strings.Replace(good, `"alice","name"`, `null,"name"`, 1))); err != nil || rec.Anchor != "" {
		t.Fatalf("anchor null: %+v %v", rec, err)
	}
	for name, bad := range map[string]string{
		"empty":      ``,
		"not v1":     strings.Replace(good, `"v":1`, `"v":2`, 1),
		"unknown":    strings.Replace(good, `"exp":3`, `"exp":3,"props":{}`, 1),
		"duplicate":  strings.Replace(good, `"exp":3`, `"exp":3,"anchor":"bob"`, 1),
		"missing":    strings.Replace(good, `,"exp":3`, ``, 1),
		"null name":  strings.Replace(good, `"name":"alice"`, `"name":null`, 1),
		"bad anchor": strings.Replace(good, `"anchor":"alice"`, `"anchor":"Alice *"`, 1),
		"bad id":     strings.Replace(good, id, "x", 1),
		"trailing":   good + `{}`,
		"js answer":  `{"type":"io.nats.jetstream.api.v1.pub_ack","stream":"PRESENCE","seq":4}`,
	} {
		if _, err := DecodeAccount([]byte(bad)); err == nil {
			t.Errorf("%s decoded: %s", name, bad)
		}
	}
}
