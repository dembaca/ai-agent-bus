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

// RT-6.42 step 13, plan B piece 7: a post may name a Mattermost post as its
// root and a reaction the Mattermost post it reacts to, never beside a
// sequence, and only by a Mattermost id.
func TestDecodeAMattermostPostNamedByItsId(t *testing.T) {
	const id = "qqqqqqqqqqqqqqqqqqqqqqqqqq"
	m, err := Decode([]byte(`{"v":1,"kind":"post","text":"on it","root":null,"mm_root":"`+id+`"}`), "a2a.")
	if err != nil || m.MMRoot != id || m.Root != nil {
		t.Fatalf("%+v %v", m, err)
	}
	m, err = Decode([]byte(`{"v":1,"kind":"unreaction","ref":null,"emoji":"eyes","mm_ref":"`+id+`"}`), "a2a.")
	if err != nil || m.MMRef != id || m.Ref != nil || m.Kind != Unreaction {
		t.Fatalf("%+v %v", m, err)
	}
	for _, body := range []string{
		`{"v":1,"kind":"post","text":"x","root":3,"mm_root":"` + id + `"}`,
		`{"v":1,"kind":"post","text":"x","mm_root":"n1:home:3"}`,
		`{"v":1,"kind":"post","text":"x","mm_root":"QQQQQQQQQQQQQQQQQQQQQQQQQQ"}`,
		`{"v":1,"kind":"post","text":"x","mm_ref":"` + id + `"}`,
		`{"v":1,"kind":"reaction","emoji":"x","ref":3,"mm_ref":"` + id + `"}`,
		`{"v":1,"kind":"reaction","emoji":"x","mm_root":"` + id + `"}`,
		`{"v":1,"kind":"reaction","emoji":"x","mm_ref":""}`,
	} {
		if _, err := Decode([]byte(body), "a2a."); err == nil {
			t.Errorf("accepted %s", body)
		}
	}
}
