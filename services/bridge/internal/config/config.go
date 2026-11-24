// Package config reads the bridge's settings from the environment, the way
// authd reads its own: the values are files where the value is a secret.
package config

import (
	"fmt"
	"os"
	"strings"
)

// Config is the bridge's whole configuration.
type Config struct {
	NATSURL   string   // BRIDGE_NATS_URL, tls:// in the lab
	SeedFile  string   // BRIDGE_NATS_SEED_FILE, the read-only user's nkey seed
	CAFile    string   // BRIDGE_CA_FILE, optional: the server's CA
	MMURL     string   // BRIDGE_MM_URL
	MMToken   string   // BRIDGE_MM_TOKEN_FILE
	MMTeam    string   // BRIDGE_MM_TEAM
	Homes     []string // BRIDGE_HOMES, the home channel names to mirror
	Prefix    string   // BRIDGE_PROPS_PREFIX, the homes' MM_PROPS_PREFIX
	KVBucket  string   // BRIDGE_KV_BUCKET, default BUS_BRIDGE
	Listen    string   // BRIDGE_LISTEN_ADDR, /healthz
	NATSStream string  // BRIDGE_STREAM, default BUS
}

// Load reads the environment. A missing required value is an error naming it.
func Load(get func(string) string) (Config, error) {
	c := Config{
		NATSURL:    strings.TrimSpace(get("BRIDGE_NATS_URL")),
		SeedFile:   strings.TrimSpace(get("BRIDGE_NATS_SEED_FILE")),
		CAFile:     strings.TrimSpace(get("BRIDGE_CA_FILE")),
		MMURL:      strings.TrimRight(strings.TrimSpace(get("BRIDGE_MM_URL")), "/"),
		MMToken:    strings.TrimSpace(get("BRIDGE_MM_TOKEN_FILE")),
		MMTeam:     strings.TrimSpace(get("BRIDGE_MM_TEAM")),
		Prefix:     strings.TrimSpace(get("BRIDGE_PROPS_PREFIX")),
		KVBucket:   strings.TrimSpace(get("BRIDGE_KV_BUCKET")),
		Listen:     strings.TrimSpace(get("BRIDGE_LISTEN_ADDR")),
		NATSStream: strings.TrimSpace(get("BRIDGE_STREAM")),
	}
	if c.KVBucket == "" {
		c.KVBucket = "BUS_BRIDGE"
	}
	if c.NATSStream == "" {
		c.NATSStream = "BUS"
	}
	if c.Listen == "" {
		c.Listen = "127.0.0.1:8095"
	}
	for _, h := range strings.Split(get("BRIDGE_HOMES"), ",") {
		if h = strings.TrimSpace(h); h != "" {
			c.Homes = append(c.Homes, h)
		}
	}
	switch {
	case c.NATSURL == "":
		return Config{}, fmt.Errorf("BRIDGE_NATS_URL is required")
	case c.SeedFile == "":
		return Config{}, fmt.Errorf("BRIDGE_NATS_SEED_FILE is required")
	case c.MMURL == "":
		return Config{}, fmt.Errorf("BRIDGE_MM_URL is required")
	case c.MMToken == "":
		return Config{}, fmt.Errorf("BRIDGE_MM_TOKEN_FILE is required")
	case c.MMTeam == "":
		return Config{}, fmt.Errorf("BRIDGE_MM_TEAM is required")
	case len(c.Homes) == 0:
		return Config{}, fmt.Errorf("BRIDGE_HOMES is required: at least one home channel name")
	}
	return c, nil
}

// FromEnv is Load(os.Getenv) with the token read from its file.
func FromEnv() (Config, error) {
	c, err := Load(os.Getenv)
	if err != nil {
		return Config{}, err
	}
	raw, err := os.ReadFile(c.MMToken)
	if err != nil {
		return Config{}, fmt.Errorf("BRIDGE_MM_TOKEN_FILE: %w", err)
	}
	c.MMToken = strings.TrimSpace(string(raw))
	if c.MMToken == "" {
		return Config{}, fmt.Errorf("BRIDGE_MM_TOKEN_FILE %s is empty", c.MMToken)
	}
	return c, nil
}
