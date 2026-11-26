package consumers

import (
	"context"
	"fmt"
	"slices"
	"time"

	"github.com/nats-io/nats.go"
)

// The console's unattended metrics page (#158, RT-6.43 step 10): one pair of
// push consumers, made at the login service's start, that feeds the
// console's service reading — aggregates only, no person, no anchor. The
// account that consumes them is a static nkey of the configuration
// (subscribe-only, its seed a file only the console reads: the narrowing of
// the owner's go), so its consumers are kept like every consumer the bus
// has: made by this service, never by the reader.

// MetricsPresenceName and MetricsStatsName are the two consumers of the
// console's metrics service, one set for the whole console process.
const (
	MetricsPresenceName = "console-metrics_presence"
	MetricsStatsName    = "console-metrics_stats"
)

// MetricsDeliver is the deliver subject of one of the service's consumers
// ("presence" or "stats"): deliver.metrics.<what>. The token cannot be an
// agent's name (PersonDeliverToken's rule: a name is a prefix and a hash),
// and it is no person's login either.
func MetricsDeliver(what string) string {
	return DeliverPrefix + ".metrics." + what
}

// metaService marks a consumer as one of the service's own: only EnsureMetrics
// may make or keep it, and nobody else's consumer of these names is taken over.
const metaService = "service"

// MetricsHistory is how far back the stats consumer starts, like a person's.
const MetricsHistory = PersonStatsHistory

// EnsureMetrics makes or keeps the consumers of the console's unattended
// metrics page at the service's start (#158): PRESENCE with every agent's
// claim and every ask record (the page's agents by state and harness and its
// count of open asks — no attestations, no memberships, no crown claims, no
// person records: the narrowing), and STATS from a week back. The consumers
// are kept in memory like every consumer of the bus: a restart drops them,
// and the next start of this service makes them again. Idempotent: an
// existing consumer of the same shape is left with its position.
func (m *Manager) EnsureMetrics(ctx context.Context) error {
	p := m.cfg.Presence
	// MetricsInactiveThreshold: without a listener this long the pair ages
	// out (the login service re-ensures hourly). A console down longer
	// gets the full replay from the next ensure; one that restarts inside
	// the window keeps the consumers' position and its in-memory books
	// start empty — a named limit (the docs say it), not a claim.
	const MetricsInactiveThreshold = 5 * time.Minute
	pres := &nats.ConsumerConfig{
		Durable: MetricsPresenceName, DeliverSubject: MetricsDeliver("presence"),
		AckPolicy: nats.AckNonePolicy, DeliverPolicy: nats.DeliverLastPerSubjectPolicy,
		FilterSubjects:    []string{p + ".*.*", p + ".ask.*.*"},
		InactiveThreshold: MetricsInactiveThreshold,
		Metadata:          map[string]string{metaService: "console-metrics"},
		MemoryStorage:     true,
	}
	if err := m.ensureMetricsConsumer(ctx, PresenceStream, pres); err != nil {
		return err
	}
	start := m.cfg.Now().Add(-MetricsHistory)
	stats := &nats.ConsumerConfig{
		Durable: MetricsStatsName, DeliverSubject: MetricsDeliver("stats"),
		AckPolicy: nats.AckNonePolicy, DeliverPolicy: nats.DeliverByStartTimePolicy, OptStartTime: &start,
		InactiveThreshold: MetricsInactiveThreshold,
		Metadata:          map[string]string{metaService: "console-metrics"},
		MemoryStorage:     true,
	}
	return m.ensureMetricsConsumer(ctx, StatsStream, stats)
}

// ensureMetricsConsumer keeps an existing consumer that is the same, else
// makes it (anew); a consumer of these names that is not the service's own is
// an error, never taken over.
func (m *Manager) ensureMetricsConsumer(ctx context.Context, stream string, want *nats.ConsumerConfig) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	ci, err := m.info(ctx, stream, want.Durable)
	if err != nil {
		return err
	}
	if ci != nil {
		if ci.Config.Metadata[metaService] != want.Metadata[metaService] || ci.Config.Metadata[metaAgent] != "" {
			return fmt.Errorf("consumer %s belongs to someone else", want.Durable)
		}
		if sameMetricsConsumer(ci.Config, *want) {
			return nil
		}
		if err := m.delete(ctx, stream, want.Durable); err != nil {
			return err
		}
		m.log.Info("consumers", "event", "metrics consumer made anew", "consumer", want.Durable)
	}
	if _, err := m.js.AddConsumer(stream, want, nats.Context(ctx)); err != nil {
		return fmt.Errorf("consumer %s: %w", want.Durable, err)
	}
	return nil
}

// sameMetricsConsumer: the existing consumer delivers where and what is
// wanted, and nothing more (no acks, no flow control, no heartbeat, no group).
// The service's consumers have no InactiveThreshold on purpose: only this
// service manages their life.
func sameMetricsConsumer(have, want nats.ConsumerConfig) bool {
	return slices.Equal(filtersOf(have), filtersOf(want)) &&
		have.DeliverSubject == want.DeliverSubject && have.AckPolicy == nats.AckNonePolicy &&
		!have.FlowControl && have.Heartbeat == 0 && have.DeliverGroup == "" &&
		have.InactiveThreshold == want.InactiveThreshold &&
		have.DeliverPolicy == want.DeliverPolicy && have.MemoryStorage == want.MemoryStorage
}
