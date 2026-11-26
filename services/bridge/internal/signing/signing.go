// Package signing reads the bridge's own Ed25519 key and the slash commands'
// verification tokens. Both are secrets on disk, so both must be readable by
// the bridge's user alone: a group- or world-readable copy is a leaked copy.
//
// The bridge signs the mode, mandate and debug records with this key (issue
// #151 step 3); its kid and public key go to every host's MM_REGISTRAR_KEYS,
// as the registrar's did.
package signing

import (
	"fmt"
	"io"
	"os"
	"strings"
	"syscall"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/registrar/core"
)

// TTL is the lifetime of the claims the attestor signs itself. The mode,
// mandate and debug records carry their own fixed TTLs (core.ModeTTL and its
// siblings); this only bounds a record the bridge would sign as an attestation,
// which it does not.
const TTL = time.Hour

// Open reads a 32-byte Ed25519 seed (base64 or base64url) from path.
func Open(path string) (*core.Attestor, error) {
	raw, err := readPrivate(path)
	if err != nil {
		return nil, err
	}
	seed, err := core.ParseSeed(strings.TrimSpace(string(raw)))
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return FromSeed(seed)
}

// FromSeed builds the bridge's attestor from a 32-byte seed, as the keygen
// makes it.
func FromSeed(seed []byte) (*core.Attestor, error) {
	a, err := core.NewAttestor(seed, TTL)
	if err != nil {
		return nil, err
	}
	return a, nil
}

// ReadTokenFile reads a comma-separated list of slash command verification
// tokens, trimmed, empty entries dropped.
func ReadTokenFile(path string) ([]string, error) {
	raw, err := readPrivate(path)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, t := range strings.Split(string(raw), ",") {
		if t = strings.TrimSpace(t); t != "" {
			out = append(out, t)
		}
	}
	return out, nil
}

// readPrivate opens the file once, checks the very handle it then reads (no
// stat-then-read race), and refuses a file any other user can read or owns: a
// group- or world-readable copy, or one owned by another account, is a leaked
// or swapped copy. The error names the file and its mode or owner, never what
// is in it.
func readPrivate(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if m := info.Mode().Perm(); m&0o077 != 0 {
		return nil, fmt.Errorf("%s is %04o: the bridge's key and token files must be 0600, "+
			"readable by its own user only", path, m)
	}
	if st, ok := info.Sys().(*syscall.Stat_t); ok && int(st.Uid) != os.Getuid() {
		return nil, fmt.Errorf("%s is owned by uid %d, not by the bridge's own uid %d: the "+
			"bridge reads only key and token files of its own user", path, st.Uid, os.Getuid())
	}
	b, err := io.ReadAll(f)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	return b, nil
}
