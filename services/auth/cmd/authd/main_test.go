package main

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/envname"
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
		{"unknown log level", "CB_AUTH_LOG_LEVEL", "verbose"},
		{"listen address without a port", "CB_AUTH_LISTEN_ADDR", "no-port"},
		{"port not a number", "CB_AUTH_LISTEN_ADDR", "host:abc"},
		{"port out of range", "CB_AUTH_LISTEN_ADDR", ":99999"},
		{"unparsable shutdown timeout", "CB_AUTH_SHUTDOWN_TIMEOUT", "soon"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			lookup := envname.Map(map[string]string{tc.key: tc.value})

			var code int
			out := captureStdout(t, func() { code = run(lookup) })

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

// Both names of one setting set to different values: the start exits 2, the
// message names both names and shows neither value.
func TestRunRefusesDisagreeingNames(t *testing.T) {
	lookup := envname.Map(map[string]string{"CB_AUTH_LISTEN_ADDR": ":8101", "AUTH_LISTEN_ADDR": ":8202"})

	var code int
	out := captureStdout(t, func() { code = run(lookup) })

	if code != exitConfigError {
		t.Fatalf("run = %d, want %d", code, exitConfigError)
	}
	for _, n := range []string{"CB_AUTH_LISTEN_ADDR", " AUTH_LISTEN_ADDR", "AUTH_LISTEN_ADDR "} {
		if !strings.Contains(out, n) {
			t.Errorf("output does not name %q: %q", n, out)
		}
	}
	for _, v := range []string{"8101", "8202"} {
		if strings.Contains(out, v) {
			t.Errorf("output shows the value %s: %q", v, out)
		}
	}
}

func TestDispatchDirWithoutArgsPrintsUsage(t *testing.T) {
	var out, errb strings.Builder
	code, ok := dispatch([]string{"authd", "dir"}, envname.Map(nil), &out, &errb)
	if !ok || code != 2 || !strings.Contains(errb.String(), "usage: authd dir") {
		t.Fatalf("ok=%v code=%d stderr=%q", ok, code, errb.String())
	}
	if _, ok := dispatch([]string{"authd"}, envname.Map(nil), &out, &errb); ok {
		t.Fatal("the service was not started by plain authd")
	}
}
