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
//	presence          $KV.<bucket>.<agent>
//	own inbox         _INBOX.<host nkey>.>  (the host knows it before it connects)
//	durable consumer  <stream>/<agent>, created by the bus, not by the agent
type Subjects struct {
	Prefix   string // subject prefix, a setting (ADR-014, point 2)
	Stream   string // the tenant's stream of channel and direct traffic
	Presence string // the key-value bucket of presence and crown leases
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
// the last token, in the channels it was granted, to other agents' direct
// subjects; subscribe only to those channels and its own direct subject.
// Nothing else, so the server, not the message, says who sent it.
func (s Subjects) permissions(agent, hostNkey string, channels []string) jwt.Permissions {
	var p jwt.Permissions
	for _, ch := range channels {
		p.Pub.Allow.Add(s.Prefix + ".ch." + ch + "." + agent)
		p.Sub.Allow.Add(s.Prefix + ".ch." + ch + ".*")
	}
	p.Pub.Allow.Add(s.Prefix + ".dm.*." + agent)
	p.Sub.Allow.Add(s.Prefix + ".dm." + agent + ".*")

	// Its durable consumer, read and acknowledge only; creating or changing
	// a consumer would let it choose what it reads.
	p.Pub.Allow.Add("$JS.API.CONSUMER.INFO." + s.Stream + "." + agent)
	p.Pub.Allow.Add("$JS.API.CONSUMER.MSG.NEXT." + s.Stream + "." + agent)
	p.Pub.Allow.Add("$JS.ACK." + s.Stream + "." + agent + ".>")

	// Its own presence entry; everyone's may be read.
	p.Pub.Allow.Add("$KV." + s.Presence + "." + agent)
	p.Pub.Allow.Add("$JS.API.DIRECT.GET.KV_" + s.Presence + ".>")

	// Replies to its own requests: it connects with the inbox prefix
	// _INBOX.<its nkey>, which it knows before it has a name.
	p.Sub.Allow.Add("_INBOX." + hostNkey + ".>")
	return p
}
