package config

import "testing"

// The homes' servers refuse a props prefix longer than 16 characters
// (a2a.PREFIX_MAX), and the bridge's prop keys must use the homes' prefix: the
// same limit here turns a misconfiguration into a start-up error instead of a
// mismatch discovered later.
func TestThePropsPrefixHasTheHomesLimit(t *testing.T) {
	base := map[string]string{
		"BRIDGE_NATS_URL": "tls://nats.example:4222", "BRIDGE_NATS_SEED_FILE": "/run/seed",
		"BRIDGE_MM_URL": "https://chat.example", "BRIDGE_MM_TOKEN_FILE": "/run/token",
		"BRIDGE_MM_TEAM": "ai-agents", "BRIDGE_HOMES": "nats-pilot",
	}
	load := func(prefix string) (Config, error) {
		env := map[string]string{}
		for k, v := range base {
			env[k] = v
		}
		env["BRIDGE_PROPS_PREFIX"] = prefix
		return Load(func(k string) string { return env[k] })
	}
	if _, err := load("abcdefghijklmno_"); err != nil { // 16 characters: the limit itself
		t.Fatalf("a 16-character prefix: %v", err)
	}
	if _, err := load("abcdefghijklmnop_"); err == nil { // 17
		t.Fatal("a 17-character prefix was accepted")
	}
}
