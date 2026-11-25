package netloc

import "testing"

func TestLoopback(t *testing.T) {
	for host, want := range map[string]bool{
		"127.0.0.1": true, "127.8.9.10": true, "::1": true, "localhost": true, "LocalHost": true,
		"localhost.example": false, "10.0.0.5": false, "chat.example": false, "": false, "::": false,
	} {
		if got := Loopback(host); got != want {
			t.Errorf("Loopback(%q) = %v, want %v", host, got, want)
		}
	}
}
