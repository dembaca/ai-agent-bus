package consumers

import (
	"context"
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
// deliver.<name>.<login>.<what>, the only subjects that credential may
// subscribe to, and none else. They carry the person and the login in their
// metadata, never "agent", so neither a bye nor the brake sweep of the agents
// touches them; they end by their inactivity threshold.

// PersonInactiveThreshold: a login's consumers go 15 minutes after nobody
// listens on their deliver subject (shorter than an agent's hour).
const PersonInactiveThreshold = 15 * time.Minute

// PersonStatsHistory is how far back the stats consumer starts.
const PersonStatsHistory = 7 * 24 * time.Hour

// Metadata keys of a person's consumers.
const (
	metaPerson      = "person"
	metaPersonLogin = "person_login"
)

var (
	// A person's name: the person prefix and the anchor with "." as "~".
	personRE = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_~-]{0,127}$`)
	// A login: the first ten characters of its public key after the "U",
	// lower-cased (base32: letters and 2-7).
	personLoginRE = regexp.MustCompile(`^[a-z2-7]{10}$`)
)

// PersonConsumerNames are the two consumers of a person's login, on
// PRESENCE and on STATS.
func PersonConsumerNames(person, login string) (presence, stats string) {
	return person + "_" + login + "_presence", person + "_" + login + "_stats"
}

// PersonDeliver is the deliver subject of one of a login's consumers
// ("presence" or "stats").
func PersonDeliver(person, login, what string) string {
	return DeliverPrefix + "." + person + "." + login + "." + what
}

// personFilters: every agent's presence and attestation, and for each visible
// channel its crown claims, memberships and ask records.
func (m *Manager) personFilters(channels []string) []string {
	p := m.cfg.Presence
	f := []string{p + ".*.*", p + ".attest.*.v1"}
	for _, ch := range channels {
		f = append(f, p+".crown."+ch+".*", p+".member."+ch+".*", p+".ask."+ch+".*")
	}
	return f
}

// EnsurePerson makes or keeps the consumers of one login of a person. At a
// renewal with the same visible channels both are kept with their position;
// when the channels changed, the presence consumer is made anew and
// delivers the last message per subject again. A consumer of that name that
// belongs to someone else is an error.
func (m *Manager) EnsurePerson(ctx context.Context, person, login string, channels []string) error {
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
	unlock := m.lock("person\x00" + person + "\x00" + login)
	defer unlock()
	presName, statsName := PersonConsumerNames(person, login)
	meta := func() map[string]string {
		return map[string]string{metaPerson: person, metaPersonLogin: login}
	}
	filters := m.personFilters(channels)
	pres := &nats.ConsumerConfig{
		Durable: presName, DeliverSubject: PersonDeliver(person, login, "presence"),
		AckPolicy: nats.AckNonePolicy, DeliverPolicy: nats.DeliverLastPerSubjectPolicy,
		FilterSubjects: filters, InactiveThreshold: PersonInactiveThreshold, Metadata: meta(),
	}
	pres.Metadata[metaChannels] = strings.Join(channels, ",")
	if err := m.ensurePersonConsumer(ctx, PresenceStream, pres, person); err != nil {
		return err
	}
	start := m.cfg.Now().Add(-PersonStatsHistory)
	stats := &nats.ConsumerConfig{
		Durable: statsName, DeliverSubject: PersonDeliver(person, login, "stats"),
		AckPolicy: nats.AckNonePolicy, DeliverPolicy: nats.DeliverByStartTimePolicy, OptStartTime: &start,
		InactiveThreshold: PersonInactiveThreshold, Metadata: meta(),
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
		have.Metadata[metaPersonLogin] == want.Metadata[metaPersonLogin] &&
		have.Metadata[metaChannels] == want.Metadata[metaChannels]
}
