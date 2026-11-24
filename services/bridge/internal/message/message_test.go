package message

import "testing"

func TestDecodeAPostAndAReaction(t *testing.T) {
	m, err := Decode([]byte(`{"v":1,"kind":"post","text":"hello","props":{"a2a.kind":"TASK"}}`), "a2a.")
	if err != nil || m.Kind != Post || m.Text != "hello" || m.Root != nil {
		t.Fatalf("%+v %v", m, err)
	}
	root := uint64(7)
	m, err = Decode([]byte(`{"v":1,"kind":"post","text":"reply","root":7}`), "a2a.")
	if err != nil || m.Root == nil || *m.Root != root {
		t.Fatalf("%+v %v", m, err)
	}
	m, err = Decode([]byte(`{"v":1,"kind":"reaction","ref":3,"emoji":"thumbsup"}`), "a2a.")
	if err != nil || m.Ref == nil || *m.Ref != 3 || m.Emoji != "thumbsup" {
		t.Fatalf("%+v %v", m, err)
	}
}

func TestDecodeRefusesWhatIsNoMessage(t *testing.T) {
	for _, body := range []string{``, `{}`, `{"v":2,"kind":"post","text":"x"}`, `{"v":1,"kind":"nope"}`,
		`{"v":1,"kind":"post","text":"x","ref":1}`,
		`{"v":1,"kind":"reaction","emoji":"thumbsup"}`, `{"v":1,"kind":"reaction","ref":0,"emoji":"x"}`,
		`{"v":1,"kind":"reaction","ref":1}`, `{"v":1,"kind":"post","text":"x","root":0}`,
		`{"v":1,"v":1,"kind":"post","text":"x"}`,             // duplicate key
		`{"v":1,"kind":"post","text":"x","props":{"k":"v"}}`, // prop without the bus prefix
		`{"v":1,"kind":"post","text":"x","props":{"a2a.k":["not","simple"]}}`,
	} {
		if _, err := Decode([]byte(body), "a2a."); err == nil {
			t.Errorf("accepted %q", body)
		}
	}
	// empty text is a post on the Python side too
	if m, err := Decode([]byte(`{"v":1,"kind":"post","text":""}`), "a2a."); err != nil || m.Text != "" {
		t.Fatalf("empty text: %v", err)
	}
	// invalid UTF-8 is refused, not replaced
	if _, err := Decode([]byte("{\"v\":1,\"kind\":\"post\",\"text\":\"\xff\"}"), "a2a."); err == nil {
		t.Fatal("invalid UTF-8 accepted")
	}
}

func TestSenderIsTheLastToken(t *testing.T) {
	if got := Sender("bus.ch.home.agent-x"); got != "agent-x" {
		t.Fatalf("%q", got)
	}
}
