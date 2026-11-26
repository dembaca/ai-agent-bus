// Package config reads the login service's configuration from the environment.
//
// Every value has a default that is safe to run locally. Nothing secret is a
// configuration value here: Keycloak's issuer, client id and the NATS account
// arrive with the pull requests that use them, and their secrets come from the
// host, never from this repository.
package config

import (
	"fmt"
	"log/slog"
	"net"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/envname"
)

// Defaults, also the values documented in services/auth/README.md.
const (
	DefaultListenAddr      = ":8080"
	DefaultLogLevel        = "info"
	DefaultShutdownTimeout = 10 * time.Second
)

// Config is everything the service needs to start.
type Config struct {
	// ListenAddr is the host:port the HTTP server binds to.
	ListenAddr string
	// LogLevel is the minimum level the logger reports.
	LogLevel slog.Level
	// ShutdownTimeout bounds a graceful stop before it is cut off.
	ShutdownTimeout time.Duration
}

// Load reads the configuration from environment variables, by their CB_AUTH_
// names with the old AUTH_ names as a fallback (envname, docs/env-names.md).
//
// It returns the first invalid value as an error that names the variable, so a
// half-configured service never starts. Two names of one setting with
// different values are such an error too; nil env reads the process
// environment.
func Load(env *envname.Env) (Config, error) {
	if env == nil {
		env = envname.New(os.LookupEnv, slog.Default().Warn)
	}
	if err := env.Check(envname.Service()...); err != nil {
		return Config{}, err
	}
	getenv := env.Value

	cfg := Config{
		ListenAddr:      DefaultListenAddr,
		LogLevel:        slog.LevelInfo,
		ShutdownTimeout: DefaultShutdownTimeout,
	}

	if v := strings.TrimSpace(getenv("CB_AUTH_LISTEN_ADDR")); v != "" {
		if err := validateListenAddr(v); err != nil {
			return Config{}, fmt.Errorf("CB_AUTH_LISTEN_ADDR: %w", err)
		}
		cfg.ListenAddr = v
	}

	if v := strings.TrimSpace(getenv("CB_AUTH_LOG_LEVEL")); v != "" {
		level, err := parseLogLevel(v)
		if err != nil {
			return Config{}, fmt.Errorf("CB_AUTH_LOG_LEVEL: %w", err)
		}
		cfg.LogLevel = level
	}

	if v := strings.TrimSpace(getenv("CB_AUTH_SHUTDOWN_TIMEOUT")); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return Config{}, fmt.Errorf("CB_AUTH_SHUTDOWN_TIMEOUT: %w", err)
		}
		if d <= 0 {
			return Config{}, fmt.Errorf("CB_AUTH_SHUTDOWN_TIMEOUT: must be positive, got %s", v)
		}
		cfg.ShutdownTimeout = d
	}

	return cfg, nil
}

func validateListenAddr(addr string) error {
	// A bare port such as ":8080", "127.0.0.1:8080" and "[::1]:8080" are all
	// valid; an empty host is allowed, it means every interface.
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return fmt.Errorf("must be host:port, got %q: %w", addr, err)
	}

	n, err := strconv.Atoi(port)
	if err != nil {
		return fmt.Errorf("port must be a number, got %q", port)
	}
	if n < 1 || n > 65535 {
		return fmt.Errorf("port must be between 1 and 65535, got %d", n)
	}

	// A host that is neither empty nor a literal address must be a name the
	// resolver accepts; this catches "a b" and stray brackets without
	// resolving anything.
	if host != "" && net.ParseIP(host) == nil && !validHostname(host) {
		return fmt.Errorf("host must be an address or a host name, got %q", host)
	}
	return nil
}

// validHostname accepts the letters, digits, dots and hyphens of a DNS name.
// It does not resolve: whether the name exists is the network's answer, not
// a configuration error.
func validHostname(host string) bool {
	if len(host) > 253 {
		return false
	}
	for _, label := range strings.Split(host, ".") {
		if label == "" || len(label) > 63 {
			return false
		}
		for i, r := range label {
			switch {
			case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9':
			case r == '-' && i > 0 && i < len(label)-1:
			default:
				return false
			}
		}
	}
	return true
}

func parseLogLevel(s string) (slog.Level, error) {
	switch strings.ToLower(s) {
	case "debug":
		return slog.LevelDebug, nil
	case "info":
		return slog.LevelInfo, nil
	case "warn", "warning":
		return slog.LevelWarn, nil
	case "error":
		return slog.LevelError, nil
	default:
		return 0, fmt.Errorf("must be debug, info, warn or error, got %q", s)
	}
}
