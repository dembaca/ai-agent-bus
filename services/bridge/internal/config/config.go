// Package config reads the bridge's settings from the environment, the way
// authd reads its own: the values are files where the value is a secret.
package config

import (
	"fmt"
	"net"
	"net/netip"
	"net/url"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/bridge/internal/netloc"
	"github.com/dembaca/ai-agent-bus/services/bridge/internal/signing"
)

// Config is the bridge's whole configuration.
type Config struct {
	NATSURL     string   // CB_BRIDGE_NATS_URL, tls:// in the lab
	SeedFile    string   // CB_BRIDGE_NATS_SEED_FILE, the read-only user's nkey seed
	CAFile      string   // CB_BRIDGE_CA_FILE, optional: the server's CA
	MMURL       string   // CB_BRIDGE_MM_URL, https; http only to a loopback host or with MMAllowHTTP
	MMAllowHTTP bool     // CB_BRIDGE_MM_ALLOW_HTTP, default false: http to any host, the token in clear text
	MMPlainHTTP bool     // derived: http to a host that is not loopback, allowed by MMAllowHTTP; the token goes in clear text
	MMToken     string   // CB_BRIDGE_MM_TOKEN_FILE
	MMTeam      string   // CB_BRIDGE_MM_TEAM
	Homes       []string // CB_BRIDGE_HOMES, the home channel names to mirror
	Prefix      string   // CB_BRIDGE_PROPS_PREFIX, the homes' MM_PROPS_PREFIX (CB_PROPS_PREFIX from 0.103.0)
	KVBucket    string   // CB_BRIDGE_KV_BUCKET, default BUS_BRIDGE
	SeenBucket  string   // CB_BRIDGE_SEEN_BUCKET, default BUS_BRIDGE_SEEN: the keys of people's events carried (RT-6.42)
	Listen      string   // CB_BRIDGE_LISTEN_ADDR, /healthz
	NATSStream  string   // CB_BRIDGE_STREAM, default BUS
	BusPrefix   string   // CB_BRIDGE_BUS_PREFIX, the bus's subject prefix, default bus
	Deliver     string   // CB_BRIDGE_DELIVER, the operator's push consumer prefix, default deliver.bridge
	Inbox       string   // CB_BRIDGE_INBOX, the bridge's own reply inbox prefix, default _INBOX_bridge; it must match the right _INBOX_bridge.>
	// The agents' own bots (RT-6.44): with an admin token the bridge posts each
	// agent's copies under a bot of its own; unset, under the one bridge bot.
	AdminToken string // CB_BRIDGE_MM_ADMIN_TOKEN_FILE, optional: the admin bot's token (system_admin), used by the bot management and the slash commands' lookups
	BotPrefix  string // CB_BRIDGE_MM_BOT_PREFIX, required with AdminToken: the agent bots' username prefix
	HideBotDM  bool   // CB_BRIDGE_MM_HIDE_BOT_DM, default true: hide the admin bot's welcome DM with each new bot

	// The slash commands and their signed records (issue #151 step 3). The whole
	// feature is off while CommandsListen is empty.
	CommandsListen      string   // CB_BRIDGE_COMMANDS_LISTEN_ADDR, the command listener; empty: the feature is off
	CommandsTLSCertFile string   // CB_BRIDGE_COMMANDS_TLS_CERT_FILE
	CommandsTLSKeyFile  string   // CB_BRIDGE_COMMANDS_TLS_KEY_FILE
	CommandsAllowHTTP   bool     // CB_BRIDGE_COMMANDS_ALLOW_HTTP, default false: plain http to a listener that is not loopback
	CommandsPlainHTTP   bool     // derived: no TLS on a listener that is not loopback, allowed by CommandsAllowHTTP; the command tokens go in clear text
	SigningKeyFile      string   // CB_BRIDGE_SIGNING_KEY_FILE, base64 or base64url of the bridge's 32-byte Ed25519 seed
	ModeCommandTokens   []string // CB_BRIDGE_MODE_COMMAND_TOKEN_FILE, comma-separated; none: no mode command
	// MandateCommandTokens is CB_BRIDGE_MANDATE_COMMAND_TOKEN_FILE; none: no mandate command.
	MandateCommandTokens []string
	// DebugCommandTokens is CB_BRIDGE_DEBUG_COMMAND_TOKEN_FILE; none: no debug command.
	DebugCommandTokens []string
	RecordsBucket      string // CB_BRIDGE_RECORDS_BUCKET, default BUS_BRIDGE_RECORDS
	// TrustedProxies is CB_BRIDGE_TRUSTED_PROXIES: the IPs or CIDRs whose
	// X-Forwarded-For counts; empty: the header is ignored.
	TrustedProxies []string

	// BotKeep is CB_BRIDGE_MM_BOT_KEEP, default 24h, at least 1h: the sweep
	// disables a bot the bridge made once its agent showed no presence for
	// longer (RT-6.44 step 8). Only with AdminToken.
	BotKeep time.Duration
}

// The keep time's default and minimum (RT-6.44 step 8), the one source the
// sweep in agentbots reads too.
const (
	DefaultBotKeep = 24 * time.Hour
	MinBotKeep     = time.Hour
)

// Load reads the environment. A missing required value is an error naming it.
//
// Each setting is read by its CB_ name, else by its old name (env.Get); both
// set and different is an error naming both.
func Load(env *Env) (Config, error) {
	var bad error
	get := func(newName, oldName string) string {
		v, err := env.Get(newName, oldName)
		if err != nil && bad == nil {
			bad = err
		}
		return v
	}
	homes := get("CB_BRIDGE_HOMES", "BRIDGE_HOMES")
	hideBotDM := strings.TrimSpace(get("CB_BRIDGE_MM_HIDE_BOT_DM", "BRIDGE_HIDE_BOT_DM"))
	allowHTTP := strings.TrimSpace(get("CB_BRIDGE_MM_ALLOW_HTTP", "BRIDGE_MM_ALLOW_HTTP"))
	commandsListen := strings.TrimSpace(get("CB_BRIDGE_COMMANDS_LISTEN_ADDR", ""))
	commandsCert := strings.TrimSpace(get("CB_BRIDGE_COMMANDS_TLS_CERT_FILE", ""))
	commandsKey := strings.TrimSpace(get("CB_BRIDGE_COMMANDS_TLS_KEY_FILE", ""))
	commandsAllowHTTP := strings.TrimSpace(get("CB_BRIDGE_COMMANDS_ALLOW_HTTP", ""))
	signingKey := strings.TrimSpace(get("CB_BRIDGE_SIGNING_KEY_FILE", ""))
	modeTokenFile := strings.TrimSpace(get("CB_BRIDGE_MODE_COMMAND_TOKEN_FILE", ""))
	mandateTokenFile := strings.TrimSpace(get("CB_BRIDGE_MANDATE_COMMAND_TOKEN_FILE", ""))
	debugTokenFile := strings.TrimSpace(get("CB_BRIDGE_DEBUG_COMMAND_TOKEN_FILE", ""))
	recordsBucket := strings.TrimSpace(get("CB_BRIDGE_RECORDS_BUCKET", ""))
	trustedProxies := strings.TrimSpace(get("CB_BRIDGE_TRUSTED_PROXIES", ""))
	// new in #151 step 2: no old spelling
	botKeep := strings.TrimSpace(get("CB_BRIDGE_MM_BOT_KEEP", ""))
	c := Config{
		NATSURL:    strings.TrimSpace(get("CB_BRIDGE_NATS_URL", "BRIDGE_NATS_URL")),
		SeedFile:   strings.TrimSpace(get("CB_BRIDGE_NATS_SEED_FILE", "BRIDGE_NATS_SEED_FILE")),
		CAFile:     strings.TrimSpace(get("CB_BRIDGE_CA_FILE", "BRIDGE_CA_FILE")),
		MMURL:      strings.TrimRight(strings.TrimSpace(get("CB_BRIDGE_MM_URL", "BRIDGE_MM_URL")), "/"),
		MMToken:    strings.TrimSpace(get("CB_BRIDGE_MM_TOKEN_FILE", "BRIDGE_MM_TOKEN_FILE")),
		MMTeam:     strings.TrimSpace(get("CB_BRIDGE_MM_TEAM", "BRIDGE_MM_TEAM")),
		Prefix:     strings.TrimSpace(get("CB_BRIDGE_PROPS_PREFIX", "BRIDGE_PROPS_PREFIX")),
		KVBucket:   strings.TrimSpace(get("CB_BRIDGE_KV_BUCKET", "BRIDGE_KV_BUCKET")),
		SeenBucket: strings.TrimSpace(get("CB_BRIDGE_SEEN_BUCKET", "BRIDGE_SEEN_BUCKET")),
		Listen:     strings.TrimSpace(get("CB_BRIDGE_LISTEN_ADDR", "BRIDGE_LISTEN_ADDR")),
		NATSStream: strings.TrimSpace(get("CB_BRIDGE_STREAM", "BRIDGE_STREAM")),
		BusPrefix:  strings.TrimSpace(get("CB_BRIDGE_BUS_PREFIX", "BRIDGE_BUS_PREFIX")),
		Deliver:    strings.TrimSpace(get("CB_BRIDGE_DELIVER", "BRIDGE_DELIVER")),
		Inbox:      strings.TrimSpace(get("CB_BRIDGE_INBOX", "BRIDGE_INBOX")),
		AdminToken: strings.TrimSpace(get("CB_BRIDGE_MM_ADMIN_TOKEN_FILE", "BRIDGE_MM_ADMIN_TOKEN_FILE")),
		BotPrefix:  strings.TrimSpace(get("CB_BRIDGE_MM_BOT_PREFIX", "BRIDGE_BOT_PREFIX")),
		HideBotDM:  true,

		CommandsListen:      commandsListen,
		CommandsTLSCertFile: commandsCert,
		CommandsTLSKeyFile:  commandsKey,
		SigningKeyFile:      signingKey,
		RecordsBucket:       recordsBucket,
	}
	if bad != nil {
		return Config{}, bad
	}
	if c.KVBucket == "" {
		c.KVBucket = "BUS_BRIDGE"
	}
	if c.SeenBucket == "" {
		c.SeenBucket = "BUS_BRIDGE_SEEN"
	}
	if c.NATSStream == "" {
		c.NATSStream = "BUS"
	}
	if c.BusPrefix == "" {
		c.BusPrefix = "bus"
	}
	if c.Deliver == "" {
		c.Deliver = "deliver.bridge"
	}
	if c.Inbox == "" {
		c.Inbox = "_INBOX_bridge"
	}
	if c.Prefix == "" {
		c.Prefix = "x_"
	}
	if !propsPrefix.MatchString(c.Prefix) || reservedPrefixes[c.Prefix] {
		return Config{}, fmt.Errorf("CB_BRIDGE_PROPS_PREFIX %q is no props prefix: lower-case letters and "+
			"digits, starting with a letter, ending with _ (for example x_ or abc_), and none the bus "+
			"or Mattermost writes (a2a_, agent_, from_, override_, webhook_, channel_, disable_, icon_)", c.Prefix)
	}
	// The homes' servers refuse a prefix longer than this (a2a.PREFIX_MAX): the
	// per-turn instructions name it, so the two sides must agree on one limit.
	if len(c.Prefix) > prefixMax {
		return Config{}, fmt.Errorf("CB_BRIDGE_PROPS_PREFIX %q is longer than %d characters, the homes' limit",
			c.Prefix, prefixMax)
	}
	if c.Listen == "" {
		c.Listen = "127.0.0.1:8095"
	}
	if c.RecordsBucket == "" {
		c.RecordsBucket = "BUS_BRIDGE_RECORDS"
	}
	if v := commandsAllowHTTP; v != "" {
		allow, err := strconv.ParseBool(v)
		if err != nil {
			return Config{}, fmt.Errorf("CB_BRIDGE_COMMANDS_ALLOW_HTTP %q is neither true nor false", v)
		}
		c.CommandsAllowHTTP = allow
	}
	if trustedProxies != "" {
		for _, raw := range strings.Split(trustedProxies, ",") {
			raw = strings.TrimSpace(raw)
			if raw == "" {
				continue
			}
			if _, err := netip.ParseAddr(raw); err == nil {
				c.TrustedProxies = append(c.TrustedProxies, raw)
				continue
			}
			if _, err := netip.ParsePrefix(raw); err != nil {
				return Config{}, fmt.Errorf("CB_BRIDGE_TRUSTED_PROXIES %q is no IP or CIDR", raw)
			}
			c.TrustedProxies = append(c.TrustedProxies, raw)
		}
	}
	for _, h := range strings.Split(homes, ",") {
		if h = strings.TrimSpace(h); h != "" {
			c.Homes = append(c.Homes, h)
		}
	}
	switch {
	case c.NATSURL == "":
		return Config{}, fmt.Errorf("CB_BRIDGE_NATS_URL is required")
	case c.SeedFile == "":
		return Config{}, fmt.Errorf("CB_BRIDGE_NATS_SEED_FILE is required")
	case c.MMURL == "":
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_URL is required")
	case c.MMToken == "":
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_TOKEN_FILE is required")
	case c.MMTeam == "":
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_TEAM is required")
	case len(c.Homes) == 0:
		return Config{}, fmt.Errorf("CB_BRIDGE_HOMES is required: at least one home channel name")

	}
	switch {
	case c.AdminToken != "" && c.BotPrefix == "":
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_BOT_PREFIX is required with CB_BRIDGE_MM_ADMIN_TOKEN_FILE")
	case c.AdminToken == "" && c.BotPrefix != "":
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_BOT_PREFIX is set without CB_BRIDGE_MM_ADMIN_TOKEN_FILE: " +
			"the agents' own bots need the admin token")
	case c.BotPrefix != "" && !BotPrefix.MatchString(c.BotPrefix):
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_BOT_PREFIX %q is no username's start: a lower-case letter, "+
			"then at most 31 of a-z, 0-9, '.', '_' and '-'", c.BotPrefix)
	}
	c.BotKeep = DefaultBotKeep
	if v := botKeep; v != "" {
		if c.AdminToken == "" {
			return Config{}, fmt.Errorf("CB_BRIDGE_MM_BOT_KEEP is set without CB_BRIDGE_MM_ADMIN_TOKEN_FILE: " +
				"only the agents' own bots are swept")
		}
		keep, err := time.ParseDuration(v)
		if err != nil || keep < MinBotKeep {
			return Config{}, fmt.Errorf("CB_BRIDGE_MM_BOT_KEEP %q is no duration of at least %s (for example 24h)", v, MinBotKeep)
		}
		c.BotKeep = keep
	}
	if v := hideBotDM; v != "" {
		hide, err := strconv.ParseBool(v)
		if err != nil {
			return Config{}, fmt.Errorf("CB_BRIDGE_MM_HIDE_BOT_DM %q is neither true nor false", v)
		}
		c.HideBotDM = hide
	}
	if v := allowHTTP; v != "" {
		allow, err := strconv.ParseBool(v)
		if err != nil {
			return Config{}, fmt.Errorf("CB_BRIDGE_MM_ALLOW_HTTP %q is neither true nor false", v)
		}
		c.MMAllowHTTP = allow
	}
	// The bot token goes to Mattermost with every REST call and in the
	// WebSocket's auth challenge: over http it crosses the network in clear
	// text. Only a loopback host (a local lab) or the explicit opt-in may
	// have it (#131).
	u, err := url.Parse(c.MMURL)
	if err != nil {
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_URL does not parse: %w", err)
	}
	if u.Hostname() == "" || (u.Scheme != "https" && u.Scheme != "http") {
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_URL %s is no http(s) URL", u.Redacted())
	}
	c.MMPlainHTTP = u.Scheme == "http" && !netloc.Loopback(u.Hostname())
	if c.MMPlainHTTP && !c.MMAllowHTTP {
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_URL %s is http to a host that is not loopback: the bot "+
			"token would cross the network in clear text; use https, or set CB_BRIDGE_MM_ALLOW_HTTP=true "+
			"on a network you trust", u.Redacted())
	}
	if c.CommandsListen != "" {
		if err := c.checkCommands(modeTokenFile, mandateTokenFile, debugTokenFile); err != nil {
			return Config{}, err
		}
	}
	return c, nil
}

// checkCommands validates the slash commands' settings and reads their token
// files: an empty CommandsListen leaves the whole feature off, so none of this
// is read then.
func (c *Config) checkCommands(modeFile, mandateFile, debugFile string) error {
	switch {
	case c.SigningKeyFile == "":
		return fmt.Errorf("CB_BRIDGE_COMMANDS_LISTEN_ADDR needs CB_BRIDGE_SIGNING_KEY_FILE: " +
			"the records are signed with the bridge's own Ed25519 key")
	case c.AdminToken == "":
		return fmt.Errorf("CB_BRIDGE_COMMANDS_LISTEN_ADDR needs CB_BRIDGE_MM_ADMIN_TOKEN_FILE: " +
			"the commands look their callers up in Mattermost")
	case (c.CommandsTLSCertFile == "") != (c.CommandsTLSKeyFile == ""):
		return fmt.Errorf("set CB_BRIDGE_COMMANDS_TLS_CERT_FILE and CB_BRIDGE_COMMANDS_TLS_KEY_FILE " +
			"together, or neither")
	}
	host, _, err := net.SplitHostPort(c.CommandsListen)
	if err != nil {
		return fmt.Errorf("CB_BRIDGE_COMMANDS_LISTEN_ADDR %q is no host:port: %w", c.CommandsListen, err)
	}
	// The command tokens and the records cross the wire: TLS, or a loopback
	// listener, or the operator's explicit opt-in for a network it trusts, as
	// with CB_BRIDGE_MM_URL (#131).
	c.CommandsPlainHTTP = c.CommandsTLSCertFile == "" && !netloc.Loopback(host)
	if c.CommandsPlainHTTP && !c.CommandsAllowHTTP {
		return fmt.Errorf("CB_BRIDGE_COMMANDS_LISTEN_ADDR %s is not loopback and has no TLS: the command "+
			"tokens and the records would cross the network in clear text; set "+
			"CB_BRIDGE_COMMANDS_TLS_CERT_FILE and CB_BRIDGE_COMMANDS_TLS_KEY_FILE, or "+
			"CB_BRIDGE_COMMANDS_ALLOW_HTTP=true on a network you trust", c.CommandsListen)
	}
	for _, f := range []struct {
		name, path string
		into       *[]string
	}{
		{"CB_BRIDGE_MODE_COMMAND_TOKEN_FILE", modeFile, &c.ModeCommandTokens},
		{"CB_BRIDGE_MANDATE_COMMAND_TOKEN_FILE", mandateFile, &c.MandateCommandTokens},
		{"CB_BRIDGE_DEBUG_COMMAND_TOKEN_FILE", debugFile, &c.DebugCommandTokens},
	} {
		if f.path == "" {
			continue
		}
		tokens, err := signing.ReadTokenFile(f.path)
		if err != nil {
			return fmt.Errorf("%s: %w", f.name, err)
		}
		*f.into = tokens
	}
	if len(c.ModeCommandTokens)+len(c.MandateCommandTokens)+len(c.DebugCommandTokens) == 0 {
		return fmt.Errorf("CB_BRIDGE_COMMANDS_LISTEN_ADDR needs at least one of " +
			"CB_BRIDGE_MODE_COMMAND_TOKEN_FILE, CB_BRIDGE_MANDATE_COMMAND_TOKEN_FILE and " +
			"CB_BRIDGE_DEBUG_COMMAND_TOKEN_FILE")
	}
	return nil
}

// propsPrefix is the bus's own rule (a2a.user_prefix): one segment, ending in _.
var propsPrefix = regexp.MustCompile(`^[a-z][a-z0-9]*_$`)

// BotPrefix is the agents' bot prefix as the hosts decode it from the bridge's
// signal (nats_port.BOT_PREFIX_RE, RT-6.36 step 4): a username's first part.
var BotPrefix = regexp.MustCompile(`^[a-z][a-z0-9._-]{0,31}$`)

// prefixMax is the homes' limit (a2a.PREFIX_MAX): the prefix is in their
// per-turn instructions, so it stays short.
const prefixMax = 16

// reservedPrefixes are the first segments the bus or Mattermost writes into
// props; a prefix that names one of them would let a sender's prop be read as
// the bus's.
var reservedPrefixes = map[string]bool{"a2a_": true, "agent_": true, "from_": true, "override_": true,
	"webhook_": true, "channel_": true, "disable_": true, "icon_": true}

// FromEnv is Load over the process's environment, with the token read from
// its file; warn gets the deprecation warnings of old names.
func FromEnv(warn Warn) (Config, error) {
	c, err := Load(OSEnv(warn))
	if err != nil {
		return Config{}, err
	}
	raw, err := os.ReadFile(c.MMToken)
	if err != nil {
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_TOKEN_FILE: %w", err)
	}
	c.MMToken = strings.TrimSpace(string(raw))
	if c.MMToken == "" {
		return Config{}, fmt.Errorf("CB_BRIDGE_MM_TOKEN_FILE is empty")
	}
	if c.AdminToken != "" {
		raw, err := os.ReadFile(c.AdminToken)
		if err != nil {
			return Config{}, fmt.Errorf("CB_BRIDGE_MM_ADMIN_TOKEN_FILE: %w", err)
		}
		c.AdminToken = strings.TrimSpace(string(raw))
		if err := c.checkTokens(); err != nil {
			return Config{}, err
		}
	}
	return c, nil
}

// checkTokens refuses an empty admin token, and one that is the posting
// token: the bot that posts must not hold system_admin (RT-6.44).
func (c Config) checkTokens() error {
	switch {
	case c.AdminToken == "":
		return fmt.Errorf("CB_BRIDGE_MM_ADMIN_TOKEN_FILE is empty")
	case c.AdminToken == c.MMToken:
		return fmt.Errorf("CB_BRIDGE_MM_ADMIN_TOKEN_FILE holds the posting bot's token " +
			"(CB_BRIDGE_MM_TOKEN_FILE): the admin bot must be an account of its own")
	}
	return nil
}
