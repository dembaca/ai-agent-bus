package config

import (
	"fmt"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/envname"
)

// env returns a reader that answers from a map, so the tests never touch the
// process environment.
func env(values map[string]string) *envname.Env {
	return envname.New(envname.Map(values), nil)
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
		"CB_AUTH_LISTEN_ADDR":      "127.0.0.1:9090",
		"CB_AUTH_LOG_LEVEL":        "debug",
		"CB_AUTH_SHUTDOWN_TIMEOUT": "3s",
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
		"CB_AUTH_LISTEN_ADDR":      "  ",
		"CB_AUTH_LOG_LEVEL":        "",
		"CB_AUTH_SHUTDOWN_TIMEOUT": "\t",
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
		cfg, err := Load(env(map[string]string{"CB_AUTH_LOG_LEVEL": in}))
		if err != nil {
			t.Errorf("%q: Load: %v", in, err)
			continue
		}
		if cfg.LogLevel != want {
			t.Errorf("%q: LogLevel = %v, want %v", in, cfg.LogLevel, want)
		}
	}
}

func TestRejectListenAddr(t *testing.T) {
	cases := []string{
		"localhost", // no port
		"127.0.0.1", // no port
		"host:abc",  // port is not a number
		":99999",    // port out of range
		":0",        // port 0 means "any free port", not a fixed one
		"a:b:c",     // too many colons
	}

	for _, addr := range cases {
		t.Run(addr, func(t *testing.T) {
			cfg, err := Load(env(map[string]string{"CB_AUTH_LISTEN_ADDR": addr}))
			if err == nil {
				t.Fatalf("Load accepted CB_AUTH_LISTEN_ADDR=%q, got %+v", addr, cfg)
			}
			if !strings.Contains(err.Error(), "CB_AUTH_LISTEN_ADDR") {
				t.Errorf("error %q does not name CB_AUTH_LISTEN_ADDR", err)
			}
		})
	}
}

func TestAcceptListenAddr(t *testing.T) {
	cases := []string{
		":8080",
		"127.0.0.1:8080",
		"localhost:8080",
		"[::1]:8080",
		"0.0.0.0:1",
		"127.0.0.1:65535",
	}

	for _, addr := range cases {
		t.Run(addr, func(t *testing.T) {
			cfg, err := Load(env(map[string]string{"CB_AUTH_LISTEN_ADDR": addr}))
			if err != nil {
				t.Fatalf("Load rejected CB_AUTH_LISTEN_ADDR=%q: %v", addr, err)
			}
			if cfg.ListenAddr != addr {
				t.Errorf("ListenAddr = %q, want %q", cfg.ListenAddr, addr)
			}
		})
	}
}

func TestRejections(t *testing.T) {
	cases := map[string]map[string]string{
		"log level unknown":  {"CB_AUTH_LOG_LEVEL": "verbose"},
		"timeout unparsable": {"CB_AUTH_SHUTDOWN_TIMEOUT": "soon"},
		"timeout zero":       {"CB_AUTH_SHUTDOWN_TIMEOUT": "0s"},
		"timeout negative":   {"CB_AUTH_SHUTDOWN_TIMEOUT": "-5s"},
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

// The old AUTH_ names are still read until the removal release, with one
// warning that names the new name (docs/env-names.md).
func TestOldNamesStillWork(t *testing.T) {
	var warnings []string
	e := envname.New(envname.Map(map[string]string{"AUTH_LOG_LEVEL": "debug"}), func(msg string, args ...any) {
		warnings = append(warnings, fmt.Sprint(append([]any{msg}, args...)...))
	})
	cfg, err := Load(e)
	if err != nil || cfg.LogLevel != slog.LevelDebug {
		t.Fatalf("Load: %+v %v", cfg, err)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "CB_AUTH_LOG_LEVEL") {
		t.Fatalf("want one warning naming CB_AUTH_LOG_LEVEL, got %q", warnings)
	}
}

func TestTwoNamesThatDifferRefuse(t *testing.T) {
	_, err := Load(env(map[string]string{"CB_AUTH_LOG_LEVEL": "debug", "AUTH_LOG_LEVEL": "info"}))
	if err == nil || !strings.Contains(err.Error(), "CB_AUTH_LOG_LEVEL") || !strings.Contains(err.Error(), " AUTH_LOG_LEVEL") {
		t.Fatalf("got %v", err)
	}
}
