// Package netloc says where a network host is: a leaf the config and the
// WebSocket reader share, so both refuse the same plain-http URLs.
package netloc

import (
	"net"
	"strings"
)

// Loopback says whether host (a URL's Hostname) never leaves the
// machine: a loopback address, or the name localhost, which is trusted
// by name (RFC 6761 reserves it for loopback). Any other name that
// merely resolves to a loopback address does not count, since its
// resolution is not this check's to trust.
func Loopback(host string) bool {
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}
