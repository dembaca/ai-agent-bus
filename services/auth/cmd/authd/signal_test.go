package main

import (
	"encoding/json"
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
	cmd.Env = os.Environ()
	if b, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("go build: %v\n%s", err, b)
	}
	return out
}

// TestSigtermStopsWithExitZero is finding 5: the signal path is the one a
// container actually uses, and it was untested. The real binary is started,
// asked for /healthz, then sent SIGTERM; it must answer and exit 0.
func TestSigtermStopsWithExitZero(t *testing.T) {
	bin := buildAuthd(t)
	addr := freePort(t)

	cmd := exec.Command(bin)
	cmd.Env = append(os.Environ(),
		"AUTH_LISTEN_ADDR="+addr,
		"AUTH_LOG_LEVEL=debug",
		"AUTH_SHUTDOWN_TIMEOUT=5s",
	)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatalf("stdout pipe: %v", err)
	}
	if err := cmd.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	defer func() { _ = cmd.Process.Kill() }()

	// Wait until it serves, then send the signal a container would.
	waitForHealth(t, addr)

	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatalf("SIGTERM: %v", err)
	}

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("exited with %v, want 0", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("did not exit after SIGTERM")
	}

	// Every line it wrote is JSON, the stop lines included.
	dec := json.NewDecoder(stdout)
	for {
		var rec map[string]any
		if err := dec.Decode(&rec); err != nil {
			break
		}
		if _, ok := rec["msg"]; !ok {
			t.Errorf("line without msg: %v", rec)
		}
	}
}

// TestSIGINTStopsWithExitZero covers the interactive path next to SIGTERM.
func TestSIGINTStopsWithExitZero(t *testing.T) {
	bin := buildAuthd(t)
	addr := freePort(t)

	cmd := exec.Command(bin)
	cmd.Env = append(os.Environ(), "AUTH_LISTEN_ADDR="+addr)
	if err := cmd.Start(); err != nil {
		t.Fatalf("start: %v", err)
	}
	defer func() { _ = cmd.Process.Kill() }()

	waitForHealth(t, addr)
	if err := cmd.Process.Signal(os.Interrupt); err != nil {
		t.Fatalf("SIGINT: %v", err)
	}

	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("exited with %v, want 0", err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("did not exit after SIGINT")
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
	if !asExitError(err, &exitErr) {
		t.Fatalf("err = %v, want an exit error\n%s", err, out)
	}
	if code := exitErr.ExitCode(); code != exitFailure {
		t.Errorf("exit code = %d, want %d\n%s", code, exitFailure, out)
	}
	if !strings.Contains(string(out), "bind") {
		t.Errorf("output does not say bind: %s", out)
	}
}

func asExitError(err error, target **exec.ExitError) bool {
	e, ok := err.(*exec.ExitError)
	if ok {
		*target = e
	}
	return ok
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
