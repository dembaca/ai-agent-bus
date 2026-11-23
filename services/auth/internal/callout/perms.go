package callout

import (
	"errors"
	"regexp"
	"strings"

	"github.com/nats-io/jwt/v2"
)

// Subjects names every subject an agent's credential opens, in one place.
// The sender is the last token of the subject, never a header or the body: the
// server, not the message, says who sent it.
//
//	channel post      <prefix>.ch.<channel>.<sender>
//	direct message    <prefix>.dm.<recipient>.<sender>
//	presence          <presence>.<owner>.<sender>
//	crown claim       <presence>.crown.<channel>.<sender>
//	reading           deliver.<agent>.<...>, only from push consumers the login
//	                  service creates (ack none, no flow control, subscribed by
//	                  their literal deliver subject)
//
// The owner token of a presence subject is the owner from the Keycloak token's
// preferred_username, with "." written as "~" (a NATS token holds no dot, a
// Mattermost username no "~"), or "-" when that is not a lower-case username.
// The permission pins the owner exactly like the sender, so readers take the
// owner from the subject, not from a name that only hashes it.
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

var ownerRE = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]{0,63}$`)

// ownerToken is the owner as one subject token: "." becomes "~", which no
// Mattermost username holds, so the step is reversible. An owner that is not
// a lower-case username (nothing is lower-cased or trimmed) gives "-": no
// owner can be proven.
func ownerToken(owner string) string {
	if !ownerRE.MatchString(owner) {
		return "-"
	}
	return strings.ReplaceAll(owner, ".", "~")
}

var errBadName = errors.New("not a valid channel name")

func checkToken(s string) error {
	if !tokenRE.MatchString(s) {
		return errBadName
	}
	return nil
}

// permissions are what an agent may do: publish only under its own name as
// the last token, in the channels it was granted and to other agents' direct
// subjects, plus presence (under its owner) and crown claims; subscribe only to its own deliver
// subjects. Nothing else.
func (s Subjects) permissions(agent, owner string, channels []string) jwt.Permissions {
	var p jwt.Permissions
	for _, ch := range channels {
		p.Pub.Allow.Add(s.Prefix + ".ch." + ch + "." + agent)
		p.Pub.Allow.Add(s.Presence + ".crown." + ch + "." + agent)
	}
	p.Pub.Allow.Add(s.Prefix + ".dm.*." + agent)
	p.Pub.Allow.Add(s.Presence + "." + ownerToken(owner) + "." + agent)
	p.Sub.Allow.Add(DeliverPrefix + "." + agent + ".>")
	return p
}
