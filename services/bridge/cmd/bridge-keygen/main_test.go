package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/signing"
)

// The keygen's output is enough for the bridge: the file holds a seed that
// core.ParseSeed reads, and the printed kid and public key are that seed's.
func TestTheKeygenWritesAUsableKey(t *testing.T) {
	path := filepath.Join(t.TempDir(), "signing-key")
	var out bytes.Buffer
	if err := run(path, &out); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Stat(path)
	if err != nil || fi.Mode().Perm() != 0o600 {
		t.Fatalf("the key file: %v %v", fi, err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	seed, err := core.ParseSeed(strings.TrimSpace(string(raw)))
	if err != nil {
		t.Fatalf("the file is no seed: %v", err)
	}
	a, err := signing.FromSeed(seed)
	if err != nil {
		t.Fatal(err)
	}
	printed := lines(out.String())
	if printed["public"] != a.Key().X || printed["kid"] != a.Kid() {
		t.Fatalf("printed %v, key is %s / %s", printed, a.Key().X, a.Kid())
	}
	if printed["file"] != path {
		t.Fatalf("file %q, want %q", printed["file"], path)
	}
	if _, ok := printed["seed"]; ok {
		t.Fatal("the seed went to stdout")
	}
	if ls := strings.Split(strings.TrimSpace(out.String()), "\n"); ls[len(ls)-1] != "MM_REGISTRAR_KEYS entry: "+a.Key().X {
		t.Fatalf("last line %q", ls[len(ls)-1])
	}

}

// The seed never goes to stdout, and an existing file is never overwritten: a
// second keygen would otherwise break every host's pin.
func TestTheKeygenNeedsANewFileAndWritesNoSeed(t *testing.T) {
	if err := run("", &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "-out") {
		t.Fatalf("run without -out: %v", err)
	}
	path := filepath.Join(t.TempDir(), "signing-key")
	if err := os.WriteFile(path, []byte("old"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := run(path, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "exists") {
		t.Fatalf("run over an existing file: %v", err)
	}
	if b, err := os.ReadFile(path); err != nil || string(b) != "old" {
		t.Fatalf("the existing file changed: %q %v", b, err)
	}
}

// lines reads the keygen's labelled output into a map.
func lines(out string) map[string]string {
	m := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		if k, v, ok := strings.Cut(line, " "); ok && !strings.Contains(k, "MM_REGISTRAR_KEYS") {
			m[k] = v
		}
	}
	return m
}
