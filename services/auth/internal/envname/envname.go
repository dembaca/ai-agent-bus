// Package envname reads the login service's environment variables under
// their CB_ names, with the old names as a fallback (docs/env-names.md,
// "The reading rule").
//
// For each setting it reads the new name first. Unset, it reads the old name
// and warns once per Env (one Env per process) that the new name replaces it.
// Two names set to different values are an error that names both, never a
// value. Equal values pass, with a warning that the old name can go. A name
// that is in the environment with an empty value is set. A value that is
// exactly an unexpanded placeholder such as ${CB_AUTH_NATS_CA_FILE} refuses:
// these services have no plugin manifest that would expand it, so it is a
// deployment mistake, and read as unset it would silently drop the setting
// (a pinned CA file, say, and the system roots would apply).
package envname

import (
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"
)

// removalRelease is the one-line switch for the release after 0.103.0 (the
// removal release, docs/env-names.md "After the fallbacks go"): set to true,
// an old name set without its new name refuses to start instead of being
// read. Never simply drop the fallback: an ignored old name fails open.
const removalRelease = false

// renamed maps each new name to its old names, oldest last. Every name the
// login service and authd read is here; Get panics on any other.
var renamed = map[string][]string{
	"CB_AUTH_ACCOUNT":               {"AUTH_ACCOUNT"},
	"CB_AUTH_ADMIN_LISTEN_ADDR":     {"AUTH_ADMIN_LISTEN_ADDR"},
	"CB_AUTH_ADMIN_TLS_CERT_FILE":   {"AUTH_ADMIN_TLS_CERT_FILE"},
	"CB_AUTH_ADMIN_TLS_KEY_FILE":    {"AUTH_ADMIN_TLS_KEY_FILE"},
	"CB_AUTH_APPROVER_CLAIM":        {"AUTH_APPROVER_CLAIM"},
	"CB_AUTH_APPROVER_GROUP":        {"AUTH_APPROVER_GROUP"},
	"CB_AUTH_BRAKE_FILE":            {"AUTH_BRAKE_FILE"},
	"CB_AUTH_CONSOLE_CLIENT_ID":     {"AUTH_CONSOLE_CLIENT_ID"},
	"CB_AUTH_CONSOLE_METRICS":       {"AUTH_CONSOLE_METRICS"},
	"CB_AUTH_DIRECTORY_ADMIN_GROUP": {"AUTH_DIRECTORY_ADMIN_GROUP"},
	"CB_AUTH_DIRECTORY_BUCKET":      {"AUTH_DIRECTORY_BUCKET"},
	"CB_AUTH_DIRECTORY_IMPORTED":    {"AUTH_DIRECTORY_IMPORTED"},
	"CB_AUTH_ISSUER_SEED_FILE":      {"AUTH_ISSUER_SEED_FILE"},
	"CB_AUTH_JS_SEED_FILE":          {"AUTH_JS_SEED_FILE"},
	"CB_AUTH_JS_URL":                {"AUTH_JS_URL"},
	"CB_AUTH_KEYCLOAK_AUDIENCE":     {"AUTH_KEYCLOAK_AUDIENCE"},
	"CB_AUTH_KEYCLOAK_CLIENT_ID":    {"AUTH_KEYCLOAK_CLIENT_ID"},
	"CB_AUTH_KEYCLOAK_ISSUER":       {"AUTH_KEYCLOAK_ISSUER"},
	"CB_AUTH_KEYCLOAK_JWKS_URL":     {"AUTH_KEYCLOAK_JWKS_URL"},
	"CB_AUTH_LISTEN_ADDR":           {"AUTH_LISTEN_ADDR"},
	"CB_AUTH_LOBBY":                 {"AUTH_LOBBY"},
	"CB_AUTH_LOG_LEVEL":             {"AUTH_LOG_LEVEL"},
	"CB_AUTH_MM_TEAM":               {"AUTH_MATTERMOST_TEAM"},
	"CB_AUTH_MM_TOKEN_FILE":         {"AUTH_MATTERMOST_TOKEN_FILE"},
	"CB_AUTH_MM_URL":                {"AUTH_MATTERMOST_URL"},
	"CB_AUTH_NAME_BINDING":          {"AUTH_NAME_BINDING"},
	"CB_AUTH_NAME_PREFIX":           {"AUTH_NAME_PREFIX"},
	"CB_AUTH_NATS_CA_FILE":          {"AUTH_NATS_CA_FILE"},
	"CB_AUTH_NATS_CERT_FILE":        {"AUTH_NATS_CERT_FILE"},
	"CB_AUTH_NATS_KEY_FILE":         {"AUTH_NATS_KEY_FILE"},
	"CB_AUTH_NATS_SEED_FILE":        {"AUTH_NATS_SEED_FILE"},
	"CB_AUTH_NATS_URL":              {"AUTH_NATS_URL"},
	"CB_AUTH_OPERATOR_GROUP":        {"AUTH_OPERATOR_GROUP"},
	"CB_AUTH_PERSON_MAX_LOGINS":     {"AUTH_PERSON_MAX_LOGINS"},
	"CB_AUTH_PERSON_PREFIX":         {"AUTH_PERSON_PREFIX"},
	"CB_AUTH_PLAIN_REQUESTS":        {"AUTH_PLAIN_REQUESTS"},
	"CB_AUTH_PRESENCE_PREFIX":       {"AUTH_PRESENCE_PREFIX"},
	"CB_AUTH_REQUEST_TIMEOUT":       {"AUTH_REQUEST_TIMEOUT"},
	"CB_AUTH_SHUTDOWN_TIMEOUT":      {"AUTH_SHUTDOWN_TIMEOUT"},
	"CB_AUTH_STATS_PREFIX":          {"AUTH_STATS_PREFIX"},
	"CB_AUTH_SUBJECT_PREFIX":        {"AUTH_SUBJECT_PREFIX"},
	"CB_AUTH_TOKEN_LEEWAY":          {"AUTH_TOKEN_LEEWAY"},
	"CB_AUTH_XKEY_SEED_FILE":        {"AUTH_XKEY_SEED_FILE"},
	"CB_AUTHD_DIR_API":              {"AUTHD_DIR_API"},
	"CB_AUTHD_DIR_CA_FILE":          {"AUTHD_DIR_CA_FILE"},
	// The stats channel the STATS import reads (RT-6.45): the MCP server's
	// setting, read here by the same rule.
	"CB_MM_STATS_CHANNEL": {"MM_STATS_CHANNEL"},
}

// placeholderRE: an unexpanded ${NAME}. The MCP server reads it as unset
// (env() in mcp/src/mattermost_ai_agent_bus/config.py); here it refuses.
var placeholderRE = regexp.MustCompile(`^\$\{[A-Za-z_][A-Za-z0-9_]*\}$`)

// Lookup is os.LookupEnv's shape: the value, and whether the name is set.
type Lookup func(string) (string, bool)

// Map is a Lookup that answers from m, for tests and callers that hold the
// environment as a map. A key in m is set, even with an empty value.
func Map(m map[string]string) Lookup {
	return func(k string) (string, bool) { v, ok := m[k]; return v, ok }
}

// Warn receives a warning: a message and slog-style key/value pairs.
type Warn func(msg string, args ...any)

// Env reads names by the reading rule. Make one per process: it warns about
// each old name once.
type Env struct {
	lookup    Lookup
	warn      Warn
	refuseOld bool // removalRelease; a field so the tests can cover it

	mu      sync.Mutex
	warned  map[string]bool
	checked map[string]bool // the names a Check covered; Value panics on others
}

// New returns an Env over lookup. A nil warn drops the warnings.
func New(lookup Lookup, warn Warn) *Env {
	if warn == nil {
		warn = func(string, ...any) {}
	}
	return &Env{lookup: lookup, warn: warn, refuseOld: removalRelease,
		warned: map[string]bool{}, checked: map[string]bool{}}
}

// Service lists the login service's names (CB_AUTH_…, and the one CB_MM_ name
// it shares with the MCP server), sorted.
func Service() []string { return names("CB_AUTH_", "CB_MM_") }

// Authd lists the names only authd's directory commands read (CB_AUTHD_…).
func Authd() []string { return names("CB_AUTHD_") }

func names(prefixes ...string) []string {
	var out []string
	for n := range renamed {
		for _, p := range prefixes {
			if strings.HasPrefix(n, p) {
				out = append(out, n)
			}
		}
	}
	sort.Strings(out)
	return out
}

// Old returns the old names of name, for messages that name both.
func Old(name string) []string { return mustOld(name) }

func mustOld(name string) []string {
	old, ok := renamed[name]
	if !ok {
		panic("envname: " + name + " is not a known CB_ name")
	}
	return old
}

// set reads one name: unset when absent; an unexpanded placeholder is an
// error that names the name, never the value.
func (e *Env) set(name string) (string, bool, error) {
	v, ok := e.lookup(name)
	if !ok {
		return "", false, nil
	}
	if placeholderRE.MatchString(v) {
		return "", true, fmt.Errorf("%s holds an unexpanded ${...} placeholder: set it to a value or unset it", name)
	}
	return v, true, nil
}

// Get returns the value of the setting name (a new CB_ name) by the reading
// rule, and whether any of its names is set. Its error names the names that
// disagree, or the name that holds an unexpanded placeholder, never a value.
func (e *Env) Get(name string) (string, bool, error) {
	old := mustOld(name)
	type hit struct{ name, value string }
	var hits []hit
	for _, n := range append([]string{name}, old...) {
		v, ok, err := e.set(n)
		if err != nil {
			return "", true, err
		}
		if ok {
			hits = append(hits, hit{n, v})
		}
	}
	if len(hits) == 0 {
		return "", false, nil
	}
	for _, h := range hits[1:] {
		if h.value != hits[0].value {
			set := make([]string, len(hits))
			for i, h := range hits {
				set[i] = h.name
			}
			return "", true, fmt.Errorf("%s are set to different values: keep %s only", strings.Join(set, " and "), name)
		}
	}
	newSet := hits[0].name == name
	if !newSet && e.refuseOld {
		return "", true, fmt.Errorf("%s is no longer read: rename it to %s", hits[0].name, name)
	}
	for _, h := range hits {
		if h.name == name {
			continue
		}
		if newSet {
			e.once(h.name, "old environment variable set alongside its new name with the same value; remove it",
				"old", h.name, "new", name)
		} else {
			e.once(h.name, "deprecated environment variable; rename it, the next release refuses the old name",
				"old", h.name, "new", name)
		}
	}
	return hits[0].value, true, nil
}

// Value is Get without the error, for callers that ran Check over the name
// and stopped on its error. It panics, a programming error, when no Check
// covered name or when name fails the reading rule: read as unset, a
// disagreement would pass silently.
func (e *Env) Value(name string) string {
	e.mu.Lock()
	covered := e.checked[name]
	e.mu.Unlock()
	if !covered {
		panic("envname: Value(" + name + ") before a Check covered it")
	}
	v, _, err := e.Get(name)
	if err != nil {
		panic("envname: Value(" + name + ") after its Check refused: " + err.Error())
	}
	return v
}

// Check runs Get over names and returns every refusal at once. Afterwards
// Value may read each of names.
func (e *Env) Check(names ...string) error {
	var errs []error
	for _, n := range names {
		if _, _, err := e.Get(n); err != nil {
			errs = append(errs, err)
		}
		e.mu.Lock()
		e.checked[n] = true
		e.mu.Unlock()
	}
	return errors.Join(errs...)
}

func (e *Env) once(old, msg string, args ...any) {
	e.mu.Lock()
	seen := e.warned[old]
	e.warned[old] = true
	e.mu.Unlock()
	if !seen {
		e.warn(msg, args...)
	}
}
