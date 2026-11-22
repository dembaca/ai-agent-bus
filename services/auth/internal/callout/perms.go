package callout

import (
	"errors"
	"regexp"

	"github.com/nats-io/jwt/v2"
)

// Subjects names every subject an agent's credential opens, in one place.
// The sender is the last token of the subject, never a header or the body: the
// server, not the message, says who sent it.
//
//	channel post      <prefix>.ch.<channel>.<sender>
//	direct message    <prefix>.dm.<recipient>.<sender>
//	presence          <presence>.<sender>
//	crown claim       <presence>.crown.<channel>.<sender>
//	reading           deliver.<agent>.<...>, only from push consumers the login
//	                  service creates (ack none, no flow control, subscribed by
//	                  their literal deliver subject)
//
// No JetStream API, no $KV, no $SYS, no _INBOX right: NATS checks only the
// subject of a publish, never its reply subject, so any service an agent may
// call (JetStream acks, the API) could be made to answer onto another agent's
// subject under its name. Streams must be created with no_ack and every allow_*
// off (ADR-014).
type Subjects struct {
	Prefix   string // subject prefix, a setting (ADR-014, point 2)
	Presence string // presence and crown subject prefix, a setting like Prefix
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
// subjects, plus presence and crown claims; subscribe only to its own deliver
// subjects. Nothing else.
func (s Subjects) permissions(agent string, channels []string) jwt.Permissions {
	var p jwt.Permissions
	for _, ch := range channels {
		p.Pub.Allow.Add(s.Prefix + ".ch." + ch + "." + agent)
		p.Pub.Allow.Add(s.Presence + ".crown." + ch + "." + agent)
	}
	p.Pub.Allow.Add(s.Prefix + ".dm.*." + agent)
	p.Pub.Allow.Add(s.Presence + "." + agent)
	p.Sub.Allow.Add(DeliverPrefix + "." + agent + ".>")
	return p
}
