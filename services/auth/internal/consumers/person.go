package consumers

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
)

// An operator's consumers (RT-6.43, the addition of 2026-11-25 to ADR-015,
// points 5 and 6). A person's credential has no JetStream right at all: the
// login service makes, per login, two push consumers that deliver onto
// deliver.person.<login>.<what>, the only subjects that credential may
// subscribe to, and none else. The deliver subject names no person: the
// console never derives a name; it reads it from the login's own record
// (PersonRecordSubject), which only this login's presence consumer delivers. They carry the person and the login in their
// metadata, never "agent", so neither a bye nor the brake sweep of the agents
// touches them; they end by their inactivity threshold.

// PersonInactiveThreshold: a login's consumers go 15 minutes after nobody
// listens on their deliver subject (shorter than an agent's hour).
const PersonInactiveThreshold = 15 * time.Minute

// ErrTooManyLogins: the person holds consumers for as many logins as allowed;
// a renewal of one of them still passes.
var ErrTooManyLogins = errors.New("consumers: too many logins of this person")

// PersonStatsHistory is how far back the stats consumer starts.
const PersonStatsHistory = 7 * 24 * time.Hour

// Metadata keys of a person's consumers.
const (
	metaPerson      = "person"
	metaPersonLogin = "person_login"
)

// PersonDeliverToken is the second token of every person's deliver subject.
// No agent's name can be it: an agent's name is a prefix and 26 characters
// of hash.
const PersonDeliverToken = "person"

var (
	// A person's name: the person prefix and the anchor with "." as "~".
	personRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_~-]{0,127}$`)
	// A login: its whole public key, lower-cased (a user key: "u" and 55
	// of base32), so no operator can grind a key whose token is another's.
	personLoginRE = regexp.MustCompile(`^u[a-z2-7]{55}$`)
)

// PersonConsumerNames are the two consumers of a person's login, on
// PRESENCE and on STATS. Service-side only: the console needs no name.
func PersonConsumerNames(person, login string) (presence, stats string) {
	return person + "_" + login + "_presence", person + "_" + login + "_stats"
}

// PersonDeliver is the deliver subject of one of a login's consumers
// ("presence" or "stats"): deliver.person.<login>.<what>.
func PersonDeliver(login, what string) string {
	return DeliverPrefix + "." + PersonDeliverToken + "." + login + "." + what
}

// PersonRecordSubject is the login's own record on PRESENCE:
// <presence>.person.<login>.v1, the person's name and anchor, written by the
// login service at each issue. Only this login's presence consumer filters
// on it: no agent's or other login's filter matches it (person_test.go).
func PersonRecordSubject(presence, login string) string {
	return presence + "." + PersonDeliverToken + "." + login + ".v1"
}

// personFilters: the login's own record, every agent's presence and
// attestation, and for each visible channel its crown claims, memberships
// and ask records.
func (m *Manager) personFilters(login string, channels []string) []string {
	p := m.cfg.Presence
	f := []string{PersonRecordSubject(p, login), p + ".*.*", p + ".attest.*.v1"}
	for _, ch := range channels {
		f = append(f, p+".crown."+ch+".*", p+".member."+ch+".*", p+".ask."+ch+".*")
	}
	return f
}

// EnsurePerson makes or keeps the consumers of one login of a person. At a
// renewal with the same visible channels both are kept with their position;
// when the channels changed, the presence consumer is made anew and
// delivers the last message per subject again. A consumer of that name that
// belongs to someone else is an error. A new login of a person who already
// holds maxLogins others is refused with ErrTooManyLogins: without the cap one
// operator could fill the streams with consumers, each staying 15 minutes.
// The consumers are kept in memory: a server restart drops them with the
// connections, and the reconnect's callout makes them again.
func (m *Manager) EnsurePerson(ctx context.Context, person, login string, channels []string, maxLogins int) error {
	if !personRE.MatchString(person) || !personLoginRE.MatchString(login) {
		return fmt.Errorf("consumers: person %q or login %q is not valid", person, login)
	}
	channels = slices.Clone(channels)
	slices.Sort(channels)
	channels = slices.Compact(channels)
	for _, ch := range channels {
		if !channelRE.MatchString(ch) {
			return fmt.Errorf("consumers: channel %q is not valid", ch)
		}
	}
	unlock := m.lock("person\x00" + person) // one person's logins one at a time: the cap holds
	defer unlock()
	presName, statsName := PersonConsumerNames(person, login)
	if err := m.capLogins(ctx, person, login, maxLogins); err != nil {
		return err
	}
	meta := func() map[string]string {
		return map[string]string{metaPerson: person, metaPersonLogin: login}
	}
	filters := m.personFilters(login, channels)
	pres := &nats.ConsumerConfig{
		Durable: presName, DeliverSubject: PersonDeliver(login, "presence"),
		AckPolicy: nats.AckNonePolicy, DeliverPolicy: nats.DeliverLastPerSubjectPolicy,
		FilterSubjects: filters, InactiveThreshold: PersonInactiveThreshold, Metadata: meta(),
		MemoryStorage: true,
	}
	pres.Metadata[metaChannels] = strings.Join(channels, ",")
	if err := m.ensurePersonConsumer(ctx, PresenceStream, pres, person); err != nil {
		return err
	}
	start := m.cfg.Now().Add(-PersonStatsHistory)
	stats := &nats.ConsumerConfig{
		Durable: statsName, DeliverSubject: PersonDeliver(login, "stats"),
		AckPolicy: nats.AckNonePolicy, DeliverPolicy: nats.DeliverByStartTimePolicy, OptStartTime: &start,
		InactiveThreshold: PersonInactiveThreshold, Metadata: meta(), MemoryStorage: true,
	}
	return m.ensurePersonConsumer(ctx, StatsStream, stats, person)
}

// ensurePersonConsumer keeps an existing consumer that is the same, else
// makes it (anew).
func (m *Manager) ensurePersonConsumer(ctx context.Context, stream string, want *nats.ConsumerConfig, person string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	ci, err := m.info(ctx, stream, want.Durable)
	if err != nil {
		return err
	}
	if ci != nil {
		if ci.Config.Metadata[metaPerson] != person || ci.Config.Metadata[metaAgent] != "" {
			return fmt.Errorf("consumer %s belongs to someone else", want.Durable)
		}
		if samePerson(ci.Config, *want) {
			return nil
		}
		if err := m.delete(ctx, stream, want.Durable); err != nil {
			return err
		}
		m.log.Info("consumers", "event", "person consumer made anew", "person", person, "consumer", want.Durable)
	}
	if _, err := m.js.AddConsumer(stream, want, nats.Context(ctx)); err != nil {
		return fmt.Errorf("consumer %s: %w", want.Durable, err)
	}
	return nil
}

// samePerson: the existing consumer delivers where and what is wanted, and
// nothing more (no acks, no flow control, no heartbeat, no group).
func samePerson(have, want nats.ConsumerConfig) bool {
	return slices.Equal(filtersOf(have), filtersOf(want)) &&
		have.DeliverSubject == want.DeliverSubject && have.AckPolicy == nats.AckNonePolicy &&
		!have.FlowControl && have.Heartbeat == 0 && have.DeliverGroup == "" &&
		have.InactiveThreshold == want.InactiveThreshold && have.DeliverPolicy == want.DeliverPolicy &&
		have.MemoryStorage == want.MemoryStorage &&
		have.Metadata[metaPersonLogin] == want.Metadata[metaPersonLogin] &&
		have.Metadata[metaChannels] == want.Metadata[metaChannels]
}

// capLogins refuses a new login when the person's other logins on PRESENCE
// reach maxLogins (< 1: no cap). The login's own consumer counts as a renewal.
func (m *Manager) capLogins(ctx context.Context, person, login string, maxLogins int) error {
	if maxLogins < 1 {
		return nil
	}
	cis, err := m.list(ctx, PresenceStream)
	if err != nil {
		return err
	}
	others := 0
	for _, ci := range cis {
		if ci.Config.Metadata[metaPerson] != person || ci.Config.Metadata[metaAgent] != "" {
			continue
		}
		if ci.Config.Metadata[metaPersonLogin] == login {
			return nil
		}
		others++
	}
	if others >= maxLogins {
		return fmt.Errorf("%w: %d", ErrTooManyLogins, others)
	}
	return nil
}
