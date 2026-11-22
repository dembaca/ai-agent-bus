package callout

import (
	"bufio"
	"errors"
	"os"
	"strings"
	"sync"
	"time"
)

// ErrStopped: the emergency brake holds this owner, or everyone.
var ErrStopped = errors.New("stopped by the emergency brake")

// Brake is the emergency brake: a file with one owner name per line, or a
// line "*" for everyone; "#" starts a comment. It is read again whenever its
// size or modification time changes, so an edit takes effect at the next
// issue, without a restart, and thus within one credential lifetime (15 min).
// Ending a connection at once needs a disconnect through the server's system
// account; that is not this service's job.
//
// A brake file that is configured but cannot be read stops everyone: a
// missing brake must not mean an open door.
type Brake struct {
	path string

	mu      sync.Mutex
	size    int64
	mod     time.Time
	all     bool
	owners  map[string]bool
	readErr error
}

// NewBrake returns a brake on path; an empty path is a brake that never holds.
func NewBrake(path string) *Brake { return &Brake{path: path} }

// Check returns ErrStopped when the brake holds owner ("" asks only for the
// global stop).
func (b *Brake) Check(owner string) error {
	if b == nil || b.path == "" {
		return nil
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	b.reload()
	if b.readErr != nil || b.all || (owner != "" && b.owners[owner]) {
		return ErrStopped
	}
	return nil
}

// reload reads the file again when it changed. Called with mu held.
func (b *Brake) reload() {
	fi, err := os.Stat(b.path)
	if err != nil {
		b.readErr, b.size, b.mod = err, -1, time.Time{}
		return
	}
	if b.readErr == nil && b.owners != nil && fi.Size() == b.size && fi.ModTime().Equal(b.mod) {
		return
	}
	f, err := os.Open(b.path)
	if err != nil {
		b.readErr = err
		return
	}
	defer f.Close()
	all, owners := false, map[string]bool{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := sc.Text()
		if i := strings.IndexByte(line, '#'); i >= 0 {
			line = line[:i]
		}
		switch line = strings.TrimSpace(line); line {
		case "":
		case "*":
			all = true
		default:
			owners[line] = true
		}
	}
	if err := sc.Err(); err != nil {
		b.readErr = err
		return
	}
	b.all, b.owners, b.readErr = all, owners, nil
	b.size, b.mod = fi.Size(), fi.ModTime()
}
