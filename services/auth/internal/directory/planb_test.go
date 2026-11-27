package directory

import (
	"errors"
	"slices"
	"testing"
)

// RT-6.39 step 5: an administrator switches plan B pieces on per channel; the
// directory keeps 1 to 13, sorted, each once, and refuses anything else.
func TestPlanBPiecesAreOneToThirteenEachOnce(t *testing.T) {
	s, _ := open(t)
	rev, err := s.PutChannel(ctx(t), Channel{Name: "home", PlanB: []int{13, 1, 6}}, 0)
	if err != nil {
		t.Fatal(err)
	}
	c, _, _, err := s.Channel(ctx(t), "home")
	if err != nil || !slices.Equal(c.PlanB, []int{1, 6, 13}) {
		t.Fatalf("stored %v, %v", c.PlanB, err)
	}
	for _, bad := range [][]int{{0}, {14}, {-1}, {3, 3}, {1, 2, 14}} {
		if _, err := s.PutChannel(ctx(t), Channel{Name: "home", PlanB: bad}, rev); !errors.Is(err, ErrBadPiece) {
			t.Errorf("%v: %v", bad, err)
		}
	}
	// None is off, and the record leaves the key out.
	if _, err := s.PutChannel(ctx(t), Channel{Name: "home", PlanB: []int{}}, rev); err != nil {
		t.Fatal(err)
	}
	e, err := s.kv.Get(ctx(t), channelPrefix+"home")
	if err != nil {
		t.Fatal(err)
	}
	if got := string(e.Value()); got != `{"name":"home","owner":"","public":false}` {
		t.Errorf("record %s", got)
	}
}

// What the login service and the bridge read: the stored list of each
// channel, none for an unknown channel; Pieces drops what is out of range or
// repeated, whoever wrote it into the bucket.
func TestPlanBReadsTheStoredListAndPiecesDropsTheInvalid(t *testing.T) {
	s, _ := open(t)
	if _, err := s.PutChannel(ctx(t), Channel{Name: "some", PlanB: []int{3, 1}}, 0); err != nil {
		t.Fatal(err)
	}
	if _, err := s.PutChannel(ctx(t), Channel{Name: "none"}, 0); err != nil {
		t.Fatal(err)
	}
	// Written around PutChannel, as by hand.
	if _, err := s.kv.Put(ctx(t), channelPrefix+"odd", []byte(`{"name":"odd","owner":"","public":false,"planb":[2,14,0,2]}`)); err != nil {
		t.Fatal(err)
	}
	got, err := s.PlanB(ctx(t), []string{"some", "none", "odd", "unknown", "Bad.Name"})
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(got["some"], []int{1, 3}) || got["none"] != nil || got["unknown"] != nil || got["Bad.Name"] != nil {
		t.Errorf("read %v", got)
	}
	valid, dropped := Pieces(got["odd"])
	if !slices.Equal(valid, []int{2}) || !slices.Equal(dropped, []int{14, 0, 2}) {
		t.Errorf("odd: valid %v dropped %v", valid, dropped)
	}
	if valid, dropped := Pieces(nil); valid != nil || dropped != nil {
		t.Errorf("nil: %v %v", valid, dropped)
	}
}

func TestAPlanBReadThatFailsIsAnError(t *testing.T) {
	s, nc := open(t)
	nc.Close()
	if _, err := s.PlanB(ctx(t), []string{"home"}); err == nil {
		t.Fatal("a failed read gave an answer")
	}
}
