package directory

import (
	"slices"
	"testing"
)

// An operator's anchor and channels (RT-6.43): the anchor only when the
// directory holds one, never made; the public channels and the member's.
func TestPersonsReadTheAnchorAndTheVisibleChannels(t *testing.T) {
	s, _ := open(t)
	p := Persons{Store: s}
	if a, ok, err := p.Anchor(ctx(t), "sub-ann"); err != nil || ok || a != "" {
		t.Fatalf("unbound: %q %v %v", a, ok, err)
	}
	if _, ok, _ := p.Anchor(ctx(t), "sub-ann"); ok {
		t.Fatal("the read made an anchor")
	}
	if _, err := s.Login(ctx(t), "sub-ann", "ann.k", "ann.k"); err != nil {
		t.Fatal(err)
	}
	if a, ok, err := p.Anchor(ctx(t), "sub-ann"); err != nil || !ok || a != "ann.k" {
		t.Fatalf("bound: %q %v %v", a, ok, err)
	}
	for _, c := range []Channel{
		{Name: "open", Owner: "sub-x", Public: true},
		{Name: "mine", Owner: "sub-x", Members: []string{"sub-ann"}},
		{Name: "theirs", Owner: "sub-x", Members: []string{"sub-x"}},
	} {
		if _, err := s.PutChannel(ctx(t), c, 0); err != nil {
			t.Fatal(err)
		}
	}
	got, err := p.Visible(ctx(t), "sub-ann")
	if err != nil || !slices.Equal(got, []string{"mine", "open"}) {
		t.Fatalf("visible %v, %v", got, err)
	}
}
