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
	"os"
	"strings"
	"time"
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

// Load reads the configuration from environment variables.
//
// It returns the first invalid value as an error that names the variable, so a
// half-configured service never starts.
func Load(getenv func(string) string) (Config, error) {
	if getenv == nil {
		getenv = os.Getenv
	}

	cfg := Config{
		ListenAddr:      DefaultListenAddr,
		LogLevel:        slog.LevelInfo,
		ShutdownTimeout: DefaultShutdownTimeout,
	}

	if v := strings.TrimSpace(getenv("AUTH_LISTEN_ADDR")); v != "" {
		if err := validateListenAddr(v); err != nil {
			return Config{}, fmt.Errorf("AUTH_LISTEN_ADDR: %w", err)
		}
		cfg.ListenAddr = v
	}

	if v := strings.TrimSpace(getenv("AUTH_LOG_LEVEL")); v != "" {
		level, err := parseLogLevel(v)
		if err != nil {
			return Config{}, fmt.Errorf("AUTH_LOG_LEVEL: %w", err)
		}
		cfg.LogLevel = level
	}

	if v := strings.TrimSpace(getenv("AUTH_SHUTDOWN_TIMEOUT")); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return Config{}, fmt.Errorf("AUTH_SHUTDOWN_TIMEOUT: %w", err)
		}
		if d <= 0 {
			return Config{}, fmt.Errorf("AUTH_SHUTDOWN_TIMEOUT: must be positive, got %s", v)
		}
		cfg.ShutdownTimeout = d
	}

	return cfg, nil
}

func validateListenAddr(addr string) error {
	// A bare port such as ":8080" and "127.0.0.1:8080" are both valid; an
	// empty host is allowed. Only a missing port is rejected, since it leaves
	// the server listening nowhere predictable.
	idx := strings.LastIndex(addr, ":")
	if idx < 0 || idx == len(addr)-1 {
		return fmt.Errorf("must be host:port, got %q", addr)
	}
	return nil
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
