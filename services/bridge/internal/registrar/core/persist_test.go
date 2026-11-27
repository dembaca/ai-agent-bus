package core

import (
	"errors"
	"testing"
	"time"
)

// fakePersister keeps the whole-state JSON in a map, as the bridge's bucket
// does, and can refuse every save.
type fakePersister struct {
	data map[string][]byte
	fail bool
}

func newFakePersister() *fakePersister {
	return &fakePersister{data: map[string][]byte{}}
}

func (f *fakePersister) Load(name string) ([]byte, error) { return f.data[name], nil }

func (f *fakePersister) Save(name string, data []byte) error {
	if f.fail {
		return errors.New("the bucket refuses")
	}
	f.data[name] = data
	return nil
}

// Issue #151 step 3: the three stores keep their state as one value under
// modes, mandates and debug, and a new store finds it again.
func TestTheStoresKeepTheirStateInAPersister(t *testing.T) {
	p := newFakePersister()
	modes, err := OpenModeStoreWith(p, "modes")
	if err != nil {
		t.Fatal(err)
	}
	mandates, err := OpenMandateStoreWith(p, "mandates")
	if err != nil {
		t.Fatal(err)
	}
	debug, err := OpenDebugStoreWith(p, "debug")
	if err != nil {
		t.Fatal(err)
	}
	at := time.Unix(1800000000, 0)
	if err := modes.Set(ChannelMode{ChannelID: chanTeam, Mode: ModeCoordinator, SetBy: "alice",
		SetByID: hAlice.ID, SetAt: at}); err != nil {
		t.Fatal(err)
	}
	if err := mandates.Set(ChannelMandate{ChannelID: chanTeam, HomeID: chanPriv, Home: "proj",
		SetBy: "alice", SetByID: hAlice.ID, SetAt: at, Until: "2027-01-20", Note: "ship it"}); err != nil {
		t.Fatal(err)
	}
	if err := debug.Set(ChannelDebug{ChannelID: chanTeam, Categories: []string{"wake"},
		SetBy: "alice", SetByID: hAlice.ID, SetAt: at, Until: at.Add(time.Hour)}); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"modes", "mandates", "debug"} {
		if p.data[key] == nil {
			t.Errorf("nothing under %s: %v", key, p.data)
		}
	}

	reopened, err := OpenModeStoreWith(p, "modes")
	if err != nil {
		t.Fatal(err)
	}
	if m, ok := reopened.Get(chanTeam); !ok || m.Mode != ModeCoordinator || m.SetByID != hAlice.ID {
		t.Fatalf("mode after reopen: %+v %v", m, ok)
	}
	rema, err := OpenMandateStoreWith(p, "mandates")
	if err != nil {
		t.Fatal(err)
	}
	if m, ok := rema.Get(chanTeam); !ok || m.HomeID != chanPriv || m.Note != "ship it" {
		t.Fatalf("mandate after reopen: %+v %v", m, ok)
	}
	rede, err := OpenDebugStoreWith(p, "debug")
	if err != nil {
		t.Fatal(err)
	}
	if d, ok := rede.Get(chanTeam); !ok || len(d.Categories) != 1 || d.Categories[0] != "wake" {
		t.Fatalf("debug after reopen: %+v %v", d, ok)
	}
}

// A save that fails rolls the store back, as the file stores do.
func TestASaveFailureRollsBack(t *testing.T) {
	p := newFakePersister()
	modes, _ := OpenModeStoreWith(p, "modes")
	mandates, _ := OpenMandateStoreWith(p, "mandates")
	debug, _ := OpenDebugStoreWith(p, "debug")
	at := time.Unix(1800000000, 0)
	old := ChannelMode{ChannelID: chanTeam, Mode: ModeDirect, SetBy: "alice", SetByID: hAlice.ID, SetAt: at}
	if err := modes.Set(old); err != nil {
		t.Fatal(err)
	}
	mand := ChannelMandate{ChannelID: chanTeam, HomeID: chanPriv, Home: "proj", SetBy: "alice", SetByID: hAlice.ID, SetAt: at}
	if err := mandates.Set(mand); err != nil {
		t.Fatal(err)
	}
	if err := debug.Set(ChannelDebug{ChannelID: chanTeam, Categories: []string{"wake"}, SetBy: "alice",
		SetByID: hAlice.ID, SetAt: at, Until: at.Add(time.Hour)}); err != nil {
		t.Fatal(err)
	}
	p.fail = true
	if err := modes.Set(ChannelMode{ChannelID: chanTeam, Mode: ModeCoordinator}); err == nil {
		t.Fatal("a save that fails was no error")
	}
	if m, _ := modes.Get(chanTeam); m.Mode != ModeDirect {
		t.Fatalf("the mode was not rolled back: %+v", m)
	}
	if err := mandates.Delete(chanTeam); err == nil {
		t.Fatal("a delete whose save fails was no error")
	}
	if _, ok := mandates.Get(chanTeam); !ok {
		t.Fatal("the mandate was not rolled back")
	}
	if _, _, err := debug.Update(chanTeam, func(ChannelDebug, bool) (ChannelDebug, bool) {
		return ChannelDebug{}, false
	}); err == nil {
		t.Fatal("a debug update whose save fails was no error")
	}
	if _, ok := debug.Get(chanTeam); !ok {
		t.Fatal("the debug setting was not rolled back")
	}
}
