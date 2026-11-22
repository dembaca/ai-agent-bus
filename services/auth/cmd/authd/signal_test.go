package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// freePort reserves a port, releases it and returns the address. The signal
// test needs the address before the child starts.
func freePort(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve port: %v", err)
	}
	addr := ln.Addr().String()
	if err := ln.Close(); err != nil {
		t.Fatalf("release port: %v", err)
	}
	return addr
}

// buildAuthd compiles the program once per test binary and returns its path.
func buildAuthd(t *testing.T) string {
	t.Helper()
	if testing.Short() {
		t.Skip("builds the binary; skipped with -short")
	}
	dir := t.TempDir()
	out := filepath.Join(dir, "authd")
	cmd := exec.Command("go", "build", "-o", out, ".")
	if b, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, b)
	}
	return out
}

// startAuthd starts the binary on a free port and returns it with its output
// buffer. The caller stops it.
func startAuthd(t *testing.T) (*exec.Cmd, *bytes.Buffer, string) {
	t.Helper()
	bin := buildAuthd(t)
	addr := freePort(t)

	cmd := exec.Command(bin)
	cmd.Env = append(os.Environ(),
		"AUTH_LISTEN_ADDR="+addr,
		"AUTH_LOG_LEVEL=debug",
		"AUTH_SHUTDOWN_TIMEOUT=5s",
	)
	// A buffer, not a pipe: a pipe read after Wait deadlocks or loses the
	// output, and every JSON assertion then passes on an empty string.
	var out bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &out

	if err := cmd.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill() })

	waitForHealth(t, addr)
	return cmd, &out, addr
}

// jsonLines parses every non-empty line of out and fails on the first that is
// not a JSON object with a "msg" field. It returns the messages.
func jsonLines(t *testing.T, out string) []string {
	t.Helper()
	var msgs []string
	for _, line := range strings.Split(strings.TrimSpace(out), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("output line is not JSON: %q (%v)", line, err)
		}
		msg, ok := rec["msg"].(string)
		if !ok {
			t.Fatalf("JSON line without a msg field: %q", line)
		}
		msgs = append(msgs, msg)
	}
	if len(msgs) == 0 {
		t.Fatal("the process wrote no output at all")
	}
	return msgs
}

func waitExit(t *testing.T, cmd *exec.Cmd, within time.Duration) error {
	t.Helper()
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		return err
	case <-time.After(within):
		t.Fatalf("did not exit within %s", within)
		return nil
	}
}

// TestSigtermStopsWithExitZero is finding 5: the signal path is the one a
// container actually uses. The real binary is started, asked for /healthz,
// then sent SIGTERM; it must answer, log its start and stop, and exit 0.
func TestSigtermStopsWithExitZero(t *testing.T) {
	cmd, out, _ := startAuthd(t)

	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatalf("SIGTERM: %v", err)
	}
	if err := waitExit(t, cmd, 15*time.Second); err != nil {
		t.Fatalf("exited with %v, want 0", err)
	}

	// Every line is JSON, and both ends of the lifecycle are there: an empty
	// or broken output must not pass.
	msgs := jsonLines(t, out.String())
	for _, want := range []string{"starting", "listening", "stopping", "stopped"} {
		found := false
		for _, m := range msgs {
			if m == want {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("no %q line in the output: %v", want, msgs)
		}
	}
}

// TestSIGINTStopsWithExitZero covers the interactive path next to SIGTERM.
func TestSIGINTStopsWithExitZero(t *testing.T) {
	cmd, out, _ := startAuthd(t)

	if err := cmd.Process.Signal(os.Interrupt); err != nil {
		t.Fatalf("SIGINT: %v", err)
	}
	if err := waitExit(t, cmd, 15*time.Second); err != nil {
		t.Fatalf("exited with %v, want 0", err)
	}
	jsonLines(t, out.String())
}

// TestSecondSignalEndsTheProcess is finding 8: while a stop is under way, a
// second signal must end the program at once. The session's stop is slow here
// (a held connection), so the second signal is what actually ends it.
func TestSecondSignalEndsTheProcess(t *testing.T) {
	cmd, _, addr := startAuthd(t)

	// Hold a connection open mid-request, so the graceful stop has to wait
	// for it and the process is still alive when the second signal arrives.
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer func() { _ = conn.Close() }()
	_, _ = conn.Write([]byte("GET /healthz HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n"))

	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatalf("first SIGTERM: %v", err)
	}
	time.Sleep(300 * time.Millisecond)

	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatalf("second SIGTERM: %v", err)
	}

	start := time.Now()
	err = waitExit(t, cmd, 5*time.Second)
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("exited %s after the second signal, want under a second", elapsed)
	}

	// A signal-terminated process is not a clean stop: the code must say so
	// rather than pretend everything went well.
	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Fatalf("err = %v, want a non-zero exit after the second signal", err)
	}
	if exitErr.ExitCode() == exitOK {
		t.Errorf("exit code = 0, want non-zero after a forced stop")
	}
}

// TestExitOneOnATakenPort is finding 5's second half: a start failure is exit
// 1, not the configuration code.
func TestExitOneOnATakenPort(t *testing.T) {
	bin := buildAuthd(t)

	taken, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	defer func() { _ = taken.Close() }()

	cmd := exec.Command(bin)
	cmd.Env = append(os.Environ(), "AUTH_LISTEN_ADDR="+taken.Addr().String())
	out, err := cmd.CombinedOutput()

	var exitErr *exec.ExitError
	if !errors.As(err, &exitErr) {
		t.Fatalf("err = %v, want an exit error\n%s", err, out)
	}
	if code := exitErr.ExitCode(); code != exitFailure {
		t.Errorf("exit code = %d, want %d\n%s", code, exitFailure, out)
	}
	if !strings.Contains(string(out), "bind") {
		t.Errorf("output does not say bind: %s", out)
	}
	// The failure line is JSON too.
	jsonLines(t, string(out))
}

// waitForHealth polls /healthz over a plain TCP+HTTP request until the child
// answers.
func waitForHealth(t *testing.T, addr string) {
	t.Helper()
	deadline := time.Now().Add(20 * time.Second)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", addr, 200*time.Millisecond)
		if err == nil {
			_, _ = conn.Write([]byte("GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"))
			buf := make([]byte, 128)
			n, _ := conn.Read(buf)
			_ = conn.Close()
			if strings.HasPrefix(string(buf[:n]), "HTTP/1.1 200") {
				return
			}
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("child at %s never answered /healthz", addr)
}
