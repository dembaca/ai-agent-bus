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
//	membership        <presence>.member.<channel>.<sender>   (an agent's channel, apart from the crown)
//	ask record        <presence>.ask.<home>.<sender>         (its open asks, one record, only in its home; RT-6.43)
//	stats line        <stats>.<sender>                       (a part of its STATS line, stream STATS; RT-6.43)
//	request           <req>.<kind>.<agent>                    (history, file or header; never kept; ADR-014 phase 5)
//	reading           deliver.<agent>.<...>, only from push consumers the login
//	                  service creates (ack none, no flow control, subscribed by
//	                  their literal deliver subject)
//
// By token count: <presence>.<owner>.<sender> (3 tokens) is a presence, so an
// owner named "crown", "member" or "ask" still gives a presence; the claim, the
// membership and the ask record have 4 tokens. The ask record is one subject
// per agent, never one per ask: an agent cannot fill the PRESENCE stream with
// subjects of its own and push out presence, claims or account records.
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
	Stats    string // STATS line subject prefix (CB_AUTH_STATS_PREFIX), a setting like Prefix
	Req      string // request subjects <req>.<kind>.<agent> (CB_AUTH_REQ_PREFIX), a setting like Prefix
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
// subjects, plus presence (under its owner), memberships in every granted channel and crown
// claims only in its home (the hello's Home, when it is granted): a guest could otherwise
// claim the crown of a channel it only visits and block that home (RT-6.38); its one ask
// record, in that home alone, its STATS line (RT-6.43) and its requests, which the
// bridge answers on deliver (ADR-014 phase 5); subscribe only to its own
// deliver subjects. Nothing else.
func (s Subjects) permissions(agent, owner, home string, channels []string) jwt.Permissions {
	var p jwt.Permissions
	for _, ch := range channels {
		p.Pub.Allow.Add(s.Prefix + ".ch." + ch + "." + agent)
		if ch == home {
			p.Pub.Allow.Add(s.Presence + ".crown." + ch + "." + agent)
			p.Pub.Allow.Add(s.Presence + ".ask." + ch + "." + agent)
		}
		p.Pub.Allow.Add(s.Presence + ".member." + ch + "." + agent)
	}
	p.Pub.Allow.Add(s.Prefix + ".dm.*." + agent)
	p.Pub.Allow.Add(s.Presence + "." + ownerToken(owner) + "." + agent)
	p.Pub.Allow.Add(s.Stats + "." + agent)
	p.Pub.Allow.Add(s.Req + ".*." + agent)
	p.Sub.Allow.Add(DeliverPrefix + "." + agent + ".>")
	return p
}
