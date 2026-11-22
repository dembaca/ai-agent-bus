package callout

import (
	"errors"
	"regexp"

	"github.com/nats-io/jwt/v2"
)

// Subjects names every subject an agent's credential opens, in one place.
// The names follow the bus's own scheme until the protocol description the
// wire layer takes over is pinned (ADR-014, points 2 and 5); then this table
// is aligned with it.
//
//	channel post      <prefix>.ch.<channel>.<sender>
//	direct message    <prefix>.dm.<recipient>.<sender>
//	own inbox         _INBOX.<host nkey>.>  (the host knows it before it connects)
//
// No JetStream API, no key-value subject, no $SYS: NATS checks only the
// subject of a publish, never its reply subject, so any service an agent may
// call can be made to answer onto another agent's subject, under that
// agent's name (the reply-subject forgery the review of PR #8 showed). What
// an agent reads from JetStream and what it writes to shared buckets goes
// through the bus's own service, not through this credential.
type Subjects struct {
	Prefix string // subject prefix, a setting (ADR-014, point 2)
	// Native switches to the candidate "sender = subject, read only through
	// push consumers the login service creates" (proof of concept).
	Native bool
}

// nativePermissions: publish only under the own name, subscribe only to the
// own deliver subjects. No channel subject, no inbox, no JetStream API.
func (s Subjects) nativePermissions(agent string, channels []string) jwt.Permissions {
	var p jwt.Permissions
	for _, ch := range channels {
		p.Pub.Allow.Add(s.Prefix + ".ch." + ch + "." + agent)
		p.Pub.Allow.Add("presence.crown." + ch + "." + agent)
	}
	p.Pub.Allow.Add(s.Prefix + ".dm.*." + agent)
	p.Pub.Allow.Add("presence." + agent)
	p.Sub.Allow.Add("deliver." + agent + ".>")
	return p
}

// token is a NATS subject token the bus uses: no wildcards, no dots, no
// spaces. Channel names of Mattermost fit (lowercase letters, digits, - and
// _); agent names are made by this service.
var tokenRE = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,63}$`)

var errBadName = errors.New("not a valid channel name")

func checkToken(s string) error {
	if !tokenRE.MatchString(s) {
		return errBadName
	}
	return nil
}

// permissions are what an agent may do: publish only under its own name as
// the last token, in the channels it was granted and to other agents' direct
// subjects; subscribe only to those channels, its own direct subject and its
// own inbox. Nothing else, so the server, not the message, says who sent it.
func (s Subjects) permissions(agent, hostNkey string, channels []string) jwt.Permissions {
	if s.Native {
		return s.nativePermissions(agent, channels)
	}
	var p jwt.Permissions
	for _, ch := range channels {
		p.Pub.Allow.Add(s.Prefix + ".ch." + ch + "." + agent)
		p.Sub.Allow.Add(s.Prefix + ".ch." + ch + ".*")
	}
	p.Pub.Allow.Add(s.Prefix + ".dm.*." + agent)
	p.Sub.Allow.Add(s.Prefix + ".dm." + agent + ".*")
	p.Sub.Allow.Add("_INBOX." + hostNkey + ".>")
	return p
}
