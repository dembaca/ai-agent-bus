package admin

import (
	"encoding/json"
	"slices"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/directory"
)

// RT-6.39 step 5, RT-6.40: an administrator switches plan B pieces per
// channel in the channel's PUT; an update without the key keeps them, []
// turns every piece off, anything outside 1 to 13 or a repeat is refused; a
// channel's owner who is no administrator cannot.
func TestAnAdministratorSwitchesPlanBPiecesPerChannel(t *testing.T) {
	e := setup(t, Config{Burst: 100})
	const path = "/v1/directory/channels/town"
	pieces := func(body string) []int {
		t.Helper()
		var c directory.Channel
		if err := json.Unmarshal([]byte(body), &c); err != nil {
			t.Fatal(err)
		}
		return c.PlanB
	}
	code, body, h := e.do(t, "PUT", path, e.admin, `{"owner":"sub-carol","planb":[6,1]}`, "If-None-Match", "*")
	want(t, "create", code, 201, body)
	if got := pieces(body); !slices.Equal(got, []int{1, 6}) {
		t.Fatalf("created with %v", got)
	}
	code, body, h = e.do(t, "PUT", path, e.admin, `{"owner":"sub-carol","public":true}`, "If-Match", h.Get("ETag"))
	want(t, "update without planb", code, 200, body)
	if got := pieces(body); !slices.Equal(got, []int{1, 6}) {
		t.Fatalf("an update without the key changed them: %v", got)
	}
	for _, bad := range []string{`[0]`, `[14]`, `[2,2]`, `["1"]`, `[1.5]`} {
		code, body, _ = e.do(t, "PUT", path, e.admin, `{"owner":"sub-carol","planb":`+bad+`}`, "If-Match", h.Get("ETag"))
		if code != 400 {
			t.Errorf("%s: HTTP %d %s", bad, code, body)
		}
	}
	code, body, _ = e.do(t, "PUT", path, e.carol, `{"owner":"sub-carol","planb":[3]}`, "If-Match", h.Get("ETag"))
	if code != 403 {
		t.Errorf("the owner set switches: HTTP %d %s", code, body)
	}
	code, body, _ = e.do(t, "PUT", path, e.admin, `{"owner":"sub-carol","public":true,"planb":[]}`, "If-Match", h.Get("ETag"))
	want(t, "off", code, 200, body)
	if got := pieces(body); got != nil {
		t.Fatalf("[] left %v", got)
	}
	c, _, _, err := e.store.Channel(ctx(t), "town")
	if err != nil || c.PlanB != nil || !c.Public {
		t.Fatalf("stored %+v, %v", c, err)
	}
}
