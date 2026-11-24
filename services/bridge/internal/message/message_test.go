package message

import "testing"

func TestDecodeAPostAndAReaction(t *testing.T) {
	m, err := Decode([]byte(`{"v":1,"kind":"post","text":"hello","props":{"a2a":{"kind":"TASK"}}}`))
	if err != nil || m.Kind != Post || m.Text != "hello" || m.Root != nil {
		t.Fatalf("%+v %v", m, err)
	}
	root := uint64(7)
	m, err = Decode([]byte(`{"v":1,"kind":"post","text":"reply","root":7}`))
	if err != nil || m.Root == nil || *m.Root != root {
		t.Fatalf("%+v %v", m, err)
	}
	m, err = Decode([]byte(`{"v":1,"kind":"reaction","ref":3,"emoji":"thumbsup"}`))
	if err != nil || m.Ref == nil || *m.Ref != 3 || m.Emoji != "thumbsup" {
		t.Fatalf("%+v %v", m, err)
	}
}

func TestDecodeRefusesWhatIsNoMessage(t *testing.T) {
	for _, body := range []string{``, `{}`, `{"v":2,"kind":"post","text":"x"}`, `{"v":1,"kind":"nope"}`,
		`{"v":1,"kind":"post"}`, `{"v":1,"kind":"post","text":"x","ref":1}`,
		`{"v":1,"kind":"reaction","emoji":"thumbsup"}`, `{"v":1,"kind":"reaction","ref":0,"emoji":"x"}`,
		`{"v":1,"kind":"reaction","ref":1}`, `{"v":1,"kind":"post","text":"x","root":0}`,
	} {
		if _, err := Decode([]byte(body)); err == nil {
			t.Errorf("accepted %q", body)
		}
	}
}

func TestSenderIsTheLastToken(t *testing.T) {
	if got := Sender("bus.ch.home.agent-x"); got != "agent-x" {
		t.Fatalf("%q", got)
	}
}
