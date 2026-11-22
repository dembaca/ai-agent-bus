package config

import (
	"log/slog"
	"strings"
	"testing"
	"time"
)

// env returns a getenv that answers from a map, so the tests never touch the
// process environment.
func env(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

func TestDefaults(t *testing.T) {
	cfg, err := Load(env(nil))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	if cfg.ListenAddr != DefaultListenAddr {
		t.Errorf("ListenAddr = %q, want %q", cfg.ListenAddr, DefaultListenAddr)
	}
	if cfg.LogLevel != slog.LevelInfo {
		t.Errorf("LogLevel = %v, want info", cfg.LogLevel)
	}
	if cfg.ShutdownTimeout != DefaultShutdownTimeout {
		t.Errorf("ShutdownTimeout = %v, want %v", cfg.ShutdownTimeout, DefaultShutdownTimeout)
	}
}

func TestOverrides(t *testing.T) {
	cfg, err := Load(env(map[string]string{
		"AUTH_LISTEN_ADDR":      "127.0.0.1:9090",
		"AUTH_LOG_LEVEL":        "debug",
		"AUTH_SHUTDOWN_TIMEOUT": "3s",
	}))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}

	if cfg.ListenAddr != "127.0.0.1:9090" {
		t.Errorf("ListenAddr = %q", cfg.ListenAddr)
	}
	if cfg.LogLevel != slog.LevelDebug {
		t.Errorf("LogLevel = %v, want debug", cfg.LogLevel)
	}
	if cfg.ShutdownTimeout != 3*time.Second {
		t.Errorf("ShutdownTimeout = %v, want 3s", cfg.ShutdownTimeout)
	}
}

func TestEmptyValuesFallBackToDefaults(t *testing.T) {
	cfg, err := Load(env(map[string]string{
		"AUTH_LISTEN_ADDR":      "  ",
		"AUTH_LOG_LEVEL":        "",
		"AUTH_SHUTDOWN_TIMEOUT": "\t",
	}))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if cfg.ListenAddr != DefaultListenAddr || cfg.LogLevel != slog.LevelInfo || cfg.ShutdownTimeout != DefaultShutdownTimeout {
		t.Errorf("blank values did not fall back: %+v", cfg)
	}
}

func TestLogLevels(t *testing.T) {
	for in, want := range map[string]slog.Level{
		"debug":   slog.LevelDebug,
		"DEBUG":   slog.LevelDebug,
		"info":    slog.LevelInfo,
		"warn":    slog.LevelWarn,
		"warning": slog.LevelWarn,
		"error":   slog.LevelError,
	} {
		cfg, err := Load(env(map[string]string{"AUTH_LOG_LEVEL": in}))
		if err != nil {
			t.Errorf("%q: Load: %v", in, err)
			continue
		}
		if cfg.LogLevel != want {
			t.Errorf("%q: LogLevel = %v, want %v", in, cfg.LogLevel, want)
		}
	}
}

func TestRejections(t *testing.T) {
	cases := map[string]map[string]string{
		"log level unknown":     {"AUTH_LOG_LEVEL": "verbose"},
		"listen addr no port":   {"AUTH_LISTEN_ADDR": "localhost"},
		"listen addr bare host": {"AUTH_LISTEN_ADDR": "127.0.0.1"},
		"timeout unparsable":    {"AUTH_SHUTDOWN_TIMEOUT": "soon"},
		"timeout zero":          {"AUTH_SHUTDOWN_TIMEOUT": "0s"},
		"timeout negative":      {"AUTH_SHUTDOWN_TIMEOUT": "-5s"},
	}

	for name, values := range cases {
		t.Run(name, func(t *testing.T) {
			cfg, err := Load(env(values))
			if err == nil {
				t.Fatalf("Load accepted %v, got %+v", values, cfg)
			}
			for key := range values {
				if !strings.Contains(err.Error(), key) {
					t.Errorf("error %q does not name %s", err, key)
				}
			}
		})
	}
}
