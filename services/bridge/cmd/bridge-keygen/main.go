// bridge-keygen makes the bridge's own Ed25519 signing key (issue #151 step 3):
// the seed for CB_BRIDGE_SIGNING_KEY_FILE and the public key for every host's
// MM_REGISTRAR_KEYS.
//
//	bridge-keygen -out /run/secrets/bridge-signing-key
//
// The seed goes to the file, 0600, and never to stdout or a log: the operator
// writes it straight into the secret store. The file must not exist yet.
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/signing"
)

func main() {
	out := flag.String("out", "", "the file the base64url seed goes to (required, 0600, must not exist)")
	flag.Parse()
	if *out == "" {
		fmt.Fprintln(os.Stderr, "bridge-keygen: -out <file> is required: the seed never goes to stdout")
		os.Exit(2)
	}
	if err := run(*out, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, "bridge-keygen:", err)
		os.Exit(1)
	}
}

// run makes a key, writes the seed to out (which must not exist) and prints
// what the operator needs: the public key, its kid, the file, and the
// MM_REGISTRAR_KEYS entry. The seed is never printed.
func run(out string, w io.Writer) error {
	if out == "" {
		return fmt.Errorf("-out <file> is required: the seed never goes to stdout")
	}
	seed := make([]byte, ed25519.SeedSize)
	if _, err := rand.Read(seed); err != nil {
		return err
	}
	a, err := signing.FromSeed(seed)
	if err != nil {
		return err
	}
	enc := base64.RawURLEncoding.EncodeToString(seed)
	pub := a.Key().X
	fmt.Fprintf(w, "public %s\n", pub)
	fmt.Fprintf(w, "kid %s\n", a.Kid())
	if err := writePrivate(out, enc+"\n"); err != nil {
		return err
	}
	fmt.Fprintf(w, "file %s\n", out)
	// The form a host pins: base64url raw public keys, comma-separated.
	fmt.Fprintf(w, "MM_REGISTRAR_KEYS entry: %s\n", pub)
	return nil
}

// writePrivate creates the seed file 0600 and refuses an existing one: a
// second keygen must not overwrite the key the hosts pinned.
func writePrivate(path, text string) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		if errors.Is(err, os.ErrExist) {
			return fmt.Errorf("%s exists: refusing to overwrite a signing key", path)
		}
		return err
	}
	_, err = fmt.Fprint(f, text)
	if cerr := f.Close(); err == nil {
		err = cerr
	}
	return err
}
