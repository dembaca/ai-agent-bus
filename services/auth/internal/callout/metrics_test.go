package callout

import (
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/dembaca/ai-agent-bus/services/auth/internal/consumers"
)

// The console's unattended metrics page (#158): the login service makes the
// two consumers of the static subscribe-only account at its start, keeps an
// existing pair, and never takes over a consumer of the same names that is
// not its own.

func info(t *testing.T, c *cl, stream, name string) *nats.ConsumerInfo {
	t.Helper()
	ci, err := c.js.ConsumerInfo(stream, name)
	if err != nil {
		t.Fatalf("consumer %s/%s: %v", stream, name, err)
	}
	return ci
}

func TestEnsureMetricsMakesThePair(t *testing.T) {
	c := newConsumerLab(t, nil)
	ctx := ctx5(t)
	if err := c.mgr.EnsureMetrics(ctx); err != nil {
		t.Fatalf("EnsureMetrics: %v", err)
	}
	pres := info(t, c, "PRESENCE", consumers.MetricsPresenceName)
	if got := pres.Config.DeliverSubject; got != consumers.MetricsDeliver("presence") {
		t.Errorf("presence delivers to %q", got)
	}
	if got := pres.Config.AckPolicy; got != nats.AckNonePolicy {
		t.Errorf("presence acks: %v", got)
	}
	if got := pres.Config.InactiveThreshold; got != 0 {
		t.Errorf("presence ages out on its own: %v", got)
	}
	want := []string{"presence.*.*", "presence.ask.*.*"}
	if got := pres.Config.FilterSubjects; strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("presence filters: %v, want %v", got, want)
	}
	stats := info(t, c, "STATS", consumers.MetricsStatsName)
	if got := stats.Config.DeliverSubject; got != consumers.MetricsDeliver("stats") {
		t.Errorf("stats delivers to %q", got)
	}
	if got := stats.Config.DeliverPolicy; got != nats.DeliverByStartTimePolicy {
		t.Errorf("stats deliver policy: %v", got)
	}
	d := time.Since(*stats.Config.OptStartTime)
	if d < consumers.MetricsHistory-time.Minute || d > consumers.MetricsHistory+time.Hour {
		t.Errorf("stats starts at %v ago", d)
	}
	// idempotent: the pair is kept, its position untouched
	if err := c.mgr.EnsureMetrics(ctx); err != nil {
		t.Fatalf("EnsureMetrics again: %v", err)
	}
	if info(t, c, "PRESENCE", consumers.MetricsPresenceName).Config.Durable != consumers.MetricsPresenceName {
		t.Error("the presence consumer was made anew")
	}
}

func TestEnsureMetricsRefusesAForeignConsumerOfItsNames(t *testing.T) {
	c := newConsumerLab(t, nil)
	ctx := ctx5(t)
	// a consumer of the same name without the service's mark: the metrics
	// pair must never take it over
	if _, err := c.js.AddConsumer("PRESENCE", &nats.ConsumerConfig{
		Durable: consumers.MetricsPresenceName, DeliverSubject: "deliver.metrics.presence",
		AckPolicy: nats.AckNonePolicy,
	}, nats.Context(ctx)); err != nil {
		t.Fatalf("the foreign consumer: %v", err)
	}
	err := c.mgr.EnsureMetrics(ctx)
	if err == nil || !strings.Contains(err.Error(), "someone else") {
		t.Fatalf("EnsureMetrics took over a foreign consumer: %v", err)
	}
}

func TestTheMetricsConsumersAreMadeAnewWhenTheirShapeChanges(t *testing.T) {
	c := newConsumerLab(t, nil)
	ctx := ctx5(t)
	if err := c.mgr.EnsureMetrics(ctx); err != nil {
		t.Fatalf("EnsureMetrics: %v", err)
	}
	// the presence consumer with a wider filter than the narrowing allows:
	// the next start makes it anew, the same way as a person's
	if err := c.js.DeleteConsumer("PRESENCE", consumers.MetricsPresenceName); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := c.js.AddConsumer("PRESENCE", &nats.ConsumerConfig{
		Durable: consumers.MetricsPresenceName, DeliverSubject: consumers.MetricsDeliver("presence"),
		AckPolicy: nats.AckNonePolicy, FilterSubjects: []string{"presence.>"},
		Metadata: map[string]string{"service": "console-metrics"},
	}, nats.Context(ctx)); err != nil {
		t.Fatalf("the shifted consumer: %v", err)
	}
	if err := c.mgr.EnsureMetrics(ctx); err != nil {
		t.Fatalf("EnsureMetrics anew: %v", err)
	}
	ci := info(t, c, "PRESENCE", consumers.MetricsPresenceName)
	if strings.Join(ci.Config.FilterSubjects, ",") != "presence.*.*,presence.ask.*.*" {
		t.Errorf("the shifted filter stayed: %v", ci.Config.FilterSubjects)
	}
}
