package config

import (
	"fmt"
	"os"
	"regexp"
)

// Lookup is os.LookupEnv's shape: the value, and whether the name is set at
// all. An empty value counts as set (docs/env-names.md, the reading rule).
type Lookup func(name string) (string, bool)

// Warn logs one warning, slog style: a message and key-value pairs.
type Warn func(msg string, args ...any)

// oldNamesRemoved is the removal release's switch (docs/env-names.md, "After
// the fallbacks go: refuse, never ignore"). In 0.103.0 it is false: an old
// name alone is read with a deprecation warning. The release after it sets
// it to true: an old name alone then refuses the start, naming both names,
// instead of being ignored. Nothing else changes with it.
const oldNamesRemoved = false

// Env reads the bridge's settings by the reading rule of docs/env-names.md:
// the new CB_ name first, else the old name with one deprecation warning,
// and a refusal when both are set and differ. One Env is one process's
// reader: it warns once per old name.
type Env struct {
	lookup Lookup
	warn   Warn
	warned map[string]bool
}

// NewEnv is a reader over lookup that warns through warn (nil: no warnings).
func NewEnv(lookup Lookup, warn Warn) *Env {
	if warn == nil {
		warn = func(string, ...any) {}
	}
	return &Env{lookup: lookup, warn: warn, warned: map[string]bool{}}
}

// OSEnv is the process's environment.
func OSEnv(warn Warn) *Env { return NewEnv(os.LookupEnv, warn) }

// Get is the value of the setting newName, formerly oldName; "" when neither
// is set. The error names both names, never a value: a value may be a secret.
func (e *Env) Get(newName, oldName string) (string, error) {
	nv, nset := e.read(newName)
	ov, oset := e.read(oldName)
	switch {
	case !oset:
		return nv, nil
	case nset && nv != ov:
		return "", fmt.Errorf("%s and %s are both set, to different values: set only %s",
			newName, oldName, newName)
	case nset:
		e.once(oldName, "config: "+oldName+" is set with the same value as "+newName+
			"; the old name can go", "old", oldName, "new", newName)
		return nv, nil
	case oldNamesRemoved:
		return "", fmt.Errorf("%s is no longer read: rename it to %s", oldName, newName)
	default:
		e.once(oldName, "config: "+oldName+" is deprecated, read as "+newName+
			"; rename it, the old name goes in the release after 0.103.0", "old", oldName, "new", newName)
		return ov, nil
	}
}

// placeholder is an unexpanded ${NAME}: unset, as in the login service and
// the MCP server (docs/env-names.md, the reading rule).
var placeholder = regexp.MustCompile(`^\$\{[A-Za-z_][A-Za-z0-9_]*\}$`)

// read is one name: unset when absent or an unexpanded placeholder.
func (e *Env) read(name string) (string, bool) {
	v, ok := e.lookup(name)
	if !ok || placeholder.MatchString(v) {
		return "", false
	}
	return v, true
}

func (e *Env) once(oldName, msg string, args ...any) {
	if e.warned[oldName] {
		return
	}
	e.warned[oldName] = true
	e.warn(msg, args...)
}
