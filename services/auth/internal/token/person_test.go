package token

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// The console's path (RT-6.43): the console's client checked against the
// realm without the chat, and neither path takes a token naming both clients.

const console = "bus-console"

func consoleClaims(c jwt.MapClaims) {
	c["azp"], c["aud"] = console, []string{console}
}

func TestVerifyRealmTakesTheConsolesTokenWithoutTheChat(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, brokenChat{}, func(c *Config) { c.ClientID, c.ApproverGroup, c.OtherClient = console, "", client })
	id, err := v.VerifyRealm(context.Background(), r.token(consoleClaims))
	if err != nil {
		t.Fatal(err)
	}
	if id.Subject != aliceSub || len(id.Groups) != 2 || id.ChatOpenID || time.Until(id.Expires) <= 0 {
		t.Fatalf("%+v", id)
	}
	if got := Client(r.token(consoleClaims)); got != console {
		t.Fatalf("Client: %q", got)
	}
	if Client("not a token") != "" {
		t.Fatal("Client of garbage")
	}
	for name, edit := range map[string]func(jwt.MapClaims){
		"the bus's token":        nil,
		"azp console, aud bus":   func(c jwt.MapClaims) { c["azp"] = console },
		"aud without console":    func(c jwt.MapClaims) { consoleClaims(c); c["aud"] = []string{"account"} },
		"aud naming both":        func(c jwt.MapClaims) { consoleClaims(c); c["aud"] = []string{console, client} },
		"expired":                func(c jwt.MapClaims) { consoleClaims(c); c["exp"] = time.Now().Add(-time.Hour).Unix() },
		"no sub":                 func(c jwt.MapClaims) { consoleClaims(c); delete(c, "sub") },
		"an ID token, no access": func(c jwt.MapClaims) { consoleClaims(c); c["typ"] = "ID" },
	} {
		if _, err := v.VerifyRealm(context.Background(), r.token(edit)); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := v.VerifyRealm(context.Background(), r.token(func(c jwt.MapClaims) {
		consoleClaims(c)
		c["exp"] = time.Now().Add(-time.Hour).Unix()
	})); !errors.Is(err, ErrExpired) {
		t.Errorf("expired: %v", err)
	}
}

func TestTheBusPathRefusesATokenThatAlsoNamesTheConsole(t *testing.T) {
	r := newRealm(t)
	v := verifier(t, r, human(nil), func(c *Config) { c.OtherClient = console })
	if _, err := v.Verify(context.Background(), r.token(nil)); err != nil {
		t.Fatalf("the bus's own token: %v", err)
	}
	_, err := v.Verify(context.Background(), r.token(func(c jwt.MapClaims) { c["aud"] = []string{client, console} }))
	if !errors.Is(err, ErrAudience) {
		t.Fatalf("aud naming both: %v", err)
	}
}
