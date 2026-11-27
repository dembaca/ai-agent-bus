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
// the fallbacks go: refuse, never ignore"). In 0.103.0 and 0.104.0 it is
// false: an old name alone is read with a deprecation warning. 0.105.0 sets
// it to true: an old name alone then refuses the start, naming both names,
// instead of being ignored. Nothing else changes with it. NewEnv copies it
// into Env.refuseOld, so the tests can cover the refusal.
const oldNamesRemoved = false

// Env reads the bridge's settings by the reading rule of docs/env-names.md:
// the new CB_ name first, else the old name with one deprecation warning,
// and a refusal when both are set and differ. One Env is one process's
// reader: it warns once per old name.
type Env struct {
	lookup    Lookup
	warn      Warn
	refuseOld bool // oldNamesRemoved; a field so the tests can cover it
	warned    map[string]bool
}

// NewEnv is a reader over lookup that warns through warn (nil: no warnings).
func NewEnv(lookup Lookup, warn Warn) *Env {
	if warn == nil {
		warn = func(string, ...any) {}
	}
	return &Env{lookup: lookup, warn: warn, refuseOld: oldNamesRemoved, warned: map[string]bool{}}
}

// OSEnv is the process's environment.
func OSEnv(warn Warn) *Env { return NewEnv(os.LookupEnv, warn) }

// Get is the value of the setting newName, formerly oldName; "" when neither
// is set. The error names both names, or the one that holds an unexpanded
// placeholder, never a value: a value may be a secret. An oldName of ""
// is a setting new after the rename, with no old spelling: only newName is
// read (docs/env-names.md lists it with the same name in both columns).
func (e *Env) Get(newName, oldName string) (string, error) {
	if oldName == "" || newName == oldName {
		// A setting that was never renamed: one name, one read, and no
		// deprecation warning about itself.
		v, _, err := e.read(newName)
		return v, err
	}
	nv, nset, err := e.read(newName)
	if err != nil {
		return "", err
	}
	ov, oset, err := e.read(oldName)
	if err != nil {
		return "", err
	}
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
	case e.refuseOld:
		return "", fmt.Errorf("%s is no longer read: rename it to %s", oldName, newName)
	default:
		e.once(oldName, "config: "+oldName+" is deprecated, read as "+newName+
			"; rename it, the old name goes in 0.105.0", "old", oldName, "new", newName)
		return ov, nil
	}
}

// placeholder is an unexpanded ${NAME}. The MCP server, started from a
// plugin manifest, treats it as unset (env() in its config.py). The Go
// services, the bridge and the login service, have no manifest, so there it
// is a deployment mistake, and read as unset it would silently drop the
// setting (a pinned CA file, say, and the system roots would apply): both
// refuse the start, naming the variable (docs/env-names.md, the reading
// rule).
var placeholder = regexp.MustCompile(`^\$\{[A-Za-z_][A-Za-z0-9_]*\}$`)

// read is one name: unset when absent; an unexpanded placeholder is an error
// that names the name, never the value.
func (e *Env) read(name string) (string, bool, error) {
	v, ok := e.lookup(name)
	if !ok {
		return "", false, nil
	}
	if placeholder.MatchString(v) {
		return "", true, fmt.Errorf("%s holds an unexpanded ${...} placeholder: set it to a value or unset it", name)
	}
	return v, true, nil
}

func (e *Env) once(oldName, msg string, args ...any) {
	if e.warned[oldName] {
		return
	}
	e.warned[oldName] = true
	e.warn(msg, args...)
}
