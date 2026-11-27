package directory

// RT-6.42 step 3 (#266, the owner's decision of 2026-11-27): a binding proven
// at a login is kept in the directory, durably, so it is proven once.

import (
	"encoding/json"
	"errors"
	"slices"
	"testing"
	"time"

	"github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

const boundID = "abcdefghijklmnopqrstuvwxyz"

func aBinding() Binding {
	return Binding{Account: boundID, Sub: "sub-alice", Anchor: "alice", AuthService: "ldap", AuthData: "8d1f",
		Record: json.RawMessage(`{"v":1}`), At: 7}
}

func TestABindingIsKeptListedAndDroppedAtItsRevision(t *testing.T) {
	s, _ := open(t)
	if _, _, ok, err := s.Binding(ctx(t), boundID); ok || err != nil {
		t.Fatalf("before: %v %v", ok, err)
	}
	if err := s.PutBinding(ctx(t), aBinding()); err != nil {
		t.Fatal(err)
	}
	b, rev, ok, err := s.Binding(ctx(t), boundID)
	if err != nil || !ok || b.Sub != "sub-alice" || b.Anchor != "alice" || b.AuthService != "ldap" ||
		b.AuthData != "8d1f" || string(b.Record) != `{"v":1}` || b.At != 7 {
		t.Fatalf("%+v %v %v", b, ok, err)
	}
	if got, err := s.BindingAccounts(ctx(t)); err != nil || !slices.Equal(got, []string{boundID}) {
		t.Fatalf("accounts %v %v", got, err)
	}
	// a binding written again since it was read stays
	again := aBinding()
	again.At = 8
	if err := s.PutBinding(ctx(t), again); err != nil {
		t.Fatal(err)
	}
	if err := s.DropBinding(ctx(t), boundID, rev); err != nil {
		t.Fatal(err)
	}
	b, rev, ok, err = s.Binding(ctx(t), boundID)
	if err != nil || !ok || b.At != 8 {
		t.Fatalf("a newer binding went: %+v %v %v", b, ok, err)
	}
	if err := s.DropBinding(ctx(t), boundID, rev); err != nil {
		t.Fatal(err)
	}
	if _, _, ok, err := s.Binding(ctx(t), boundID); ok || err != nil {
		t.Fatalf("after the drop: %v %v", ok, err)
	}
	if got, err := s.BindingAccounts(ctx(t)); err != nil || len(got) != 0 {
		t.Fatalf("accounts after the drop %v %v", got, err)
	}
}

func TestAnIncompleteBindingOrABadKeyIsRefused(t *testing.T) {
	s, _ := open(t)
	for name, edit := range map[string]func(*Binding){
		"no Mattermost id": func(b *Binding) { b.Account = "alice" },
		"a bad sub":        func(b *Binding) { b.Sub = "a.b" },
		"no anchor":        func(b *Binding) { b.Anchor = "" },
		"no auth_service":  func(b *Binding) { b.AuthService = "" },
		"no auth_data":     func(b *Binding) { b.AuthData = "" },
		"no record":        func(b *Binding) { b.Record = nil },
	} {
		b := aBinding()
		edit(&b)
		if err := s.PutBinding(ctx(t), b); err == nil {
			t.Errorf("%s: kept", name)
		}
	}
	if _, _, _, err := s.Binding(ctx(t), "x.>"); !errors.Is(err, ErrBadKey) {
		t.Errorf("a bad key: %v", err)
	}
}

// The binding is in the directory's bucket, on file: it is there after the
// NATS server and the store start again.
func TestABindingOutlivesARestartOfTheServer(t *testing.T) {
	dir := t.TempDir()
	start := func() (*server.Server, *Store) {
		t.Helper()
		srv, err := server.NewServer(&server.Options{Host: "127.0.0.1", Port: -1, JetStream: true,
			StoreDir: dir, NoLog: true, NoSigs: true})
		if err != nil {
			t.Fatal(err)
		}
		go srv.Start()
		if !srv.ReadyForConnections(5 * time.Second) {
			t.Fatal("nats-server not ready")
		}
		nc, err := nats.Connect(srv.ClientURL())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(nc.Close)
		js, err := jetstream.New(nc)
		if err != nil {
			t.Fatal(err)
		}
		s, err := Open(ctx(t), js, "", false)
		if err != nil {
			t.Fatal(err)
		}
		return srv, s
	}
	srv, s := start()
	if err := s.PutBinding(ctx(t), aBinding()); err != nil {
		t.Fatal(err)
	}
	srv.Shutdown()
	srv.WaitForShutdown()
	srv, s = start()
	t.Cleanup(srv.Shutdown)
	b, _, ok, err := s.Binding(ctx(t), boundID)
	if err != nil || !ok || b.Anchor != "alice" || b.AuthData != "8d1f" {
		t.Fatalf("after the restart: %+v %v %v", b, ok, err)
	}
}
