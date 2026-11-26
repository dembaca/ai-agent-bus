// Package netloc says where a network host is, and how a token-bearing
// client treats a redirect: a leaf the config, the WebSocket reader, the
// REST client and the registrar's Mattermost adapter share, so all refuse
// the same plain-http URLs and follow no redirect.
package netloc

import (
	"net"
	"net/http"
	"strings"
)

// NoRedirect is an http.Client's CheckRedirect that keeps a redirect from
// being followed: Mattermost's API does not redirect, and Go's client
// resends the Authorization header to a redirect on the same host, so a
// 30x from https to http would carry the token in clear text (#131). The
// 30x comes back as the response, which the caller reports as the status
// it is.
func NoRedirect(*http.Request, []*http.Request) error {
	return http.ErrUseLastResponse
}

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
