package signing

import (
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The bridge's key and token files are secrets: a group- or world-readable one
// is refused by name, and a 0600 seed file opens its attestor.
func TestTheKeyAndTokenFilesMustBePrivate(t *testing.T) {
	dir := t.TempDir()
	write := func(name, v string, mode os.FileMode) string {
		t.Helper()
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(v), mode); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(p, mode); err != nil {
			t.Fatal(err)
		}
		return p
	}
	seed := make([]byte, 32)
	for i := range seed {
		seed[i] = byte(i)
	}
	key := write("key", "\n  "+base64.RawURLEncoding.EncodeToString(seed)+"=\n", 0o600)
	a, err := Open(key)
	if err != nil || a.Kid() != "Vkdap1RjR0wChd9d" || a.Key().X != "A6EHv_POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg" {
		t.Fatalf("key: %v %v", a, err)
	}
	for _, mode := range []os.FileMode{0o644, 0o640, 0o604, 0o660} {
		open := write("open", base64.RawURLEncoding.EncodeToString(seed), mode)
		if _, err := Open(open); err == nil || !strings.Contains(err.Error(), open) || !strings.Contains(err.Error(), "0600") {
			t.Errorf("%04o: %v", mode, err)
		}
	}
	if _, err := Open(filepath.Join(dir, "missing")); err == nil {
		t.Fatal("a missing key file")
	}
	if _, err := Open(write("junk", "not-a-seed\n", 0o600)); err == nil ||
		!strings.Contains(err.Error(), filepath.Join(dir, "junk")) || strings.Contains(err.Error(), "not-a-seed") {
		t.Fatalf("junk: %v", err)
	}

	tokens := write("tokens", " mod-a ,, mod-b \n", 0o600)
	if got, err := ReadTokenFile(tokens); err != nil || strings.Join(got, "|") != "mod-a|mod-b" {
		t.Fatalf("tokens: %v %v", got, err)
	}
	open := write("open-tokens", "mod-a", 0o644)
	if _, err := ReadTokenFile(open); err == nil || !strings.Contains(err.Error(), open) {
		t.Fatalf("an open token file: %v", err)
	}
}
