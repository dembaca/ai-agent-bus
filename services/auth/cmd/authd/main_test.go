package main

import "testing"

// TestRunRefusesInvalidConfiguration covers the one branch of run that needs
// no network: a configuration the service must not start with, which also
// proves the refused value is named. The serving path is covered in
// internal/httpserver.
func TestRunRefusesInvalidConfiguration(t *testing.T) {
	cases := []struct {
		name  string
		key   string
		value string
	}{
		{"unknown log level", "AUTH_LOG_LEVEL", "verbose"},
		{"listen address without a port", "AUTH_LISTEN_ADDR", "no-port"},
		{"unparsable shutdown timeout", "AUTH_SHUTDOWN_TIMEOUT", "soon"},
		{"zero shutdown timeout", "AUTH_SHUTDOWN_TIMEOUT", "0s"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			getenv := func(k string) string {
				if k == tc.key {
					return tc.value
				}
				return ""
			}

			if got := run(getenv); got != exitConfigError {
				t.Errorf("run = %d, want %d for %s=%q", got, exitConfigError, tc.key, tc.value)
			}
		})
	}
}

// TestExitCodesAreDistinct keeps the three codes apart, so a script can tell a
// refused configuration from a server failure.
func TestExitCodesAreDistinct(t *testing.T) {
	if exitOK == exitFailure || exitOK == exitConfigError || exitFailure == exitConfigError {
		t.Errorf("exit codes must differ: ok=%d failure=%d config=%d", exitOK, exitFailure, exitConfigError)
	}
}
