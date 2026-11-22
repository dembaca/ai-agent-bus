package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// captureStdout runs f with stdout redirected and returns what it wrote.
func captureStdout(t *testing.T, f func()) string {
	t.Helper()
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatalf("pipe: %v", err)
	}
	saved := os.Stdout
	os.Stdout = w

	done := make(chan string, 1)
	go func() {
		var b strings.Builder
		buf := make([]byte, 4096)
		for {
			n, err := r.Read(buf)
			b.Write(buf[:n])
			if err != nil {
				break
			}
		}
		done <- b.String()
	}()

	f()

	os.Stdout = saved
	_ = w.Close()
	return <-done
}

// TestRunRefusesInvalidConfiguration covers the exit code and the message of
// a configuration the service must not start with: the text must be JSON like
// every other line this program writes, and it must name the variable.
func TestRunRefusesInvalidConfiguration(t *testing.T) {
	cases := []struct {
		name, key, value string
	}{
		{"unknown log level", "AUTH_LOG_LEVEL", "verbose"},
		{"listen address without a port", "AUTH_LISTEN_ADDR", "no-port"},
		{"port not a number", "AUTH_LISTEN_ADDR", "host:abc"},
		{"port out of range", "AUTH_LISTEN_ADDR", ":99999"},
		{"unparsable shutdown timeout", "AUTH_SHUTDOWN_TIMEOUT", "soon"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			getenv := func(k string) string {
				if k == tc.key {
					return tc.value
				}
				return ""
			}

			var code int
			out := captureStdout(t, func() { code = run(getenv) })

			if code != exitConfigError {
				t.Errorf("run = %d, want %d for %s=%q", code, exitConfigError, tc.key, tc.value)
			}

			// One JSON object per line, the field "err" naming the variable.
			line := strings.TrimSpace(out)
			if line == "" {
				t.Fatal("run wrote nothing to stdout")
			}
			line = strings.Split(line, "\n")[0]
			var rec map[string]any
			if err := json.Unmarshal([]byte(line), &rec); err != nil {
				t.Fatalf("first line is not JSON: %q (%v)", line, err)
			}
			msg, _ := rec["err"].(string)
			if !strings.Contains(msg, tc.key) {
				t.Errorf("err %q does not name %s", msg, tc.key)
			}
			if rec["level"] != "ERROR" {
				t.Errorf("level = %v, want ERROR", rec["level"])
			}
		})
	}
}
