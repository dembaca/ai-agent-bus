package app

import (
	"errors"
	"testing"
)

// The STATS import's channel (RT-6.45) is CB_MM_STATS_CHANNEL as the MCP
// server reads it, its old name too; the archive's prefix is a setting.
func TestTheStatsImportSettings(t *testing.T) {
	for v, want := range map[string]string{"": "", "off": "", "None": "", "0": "", "false": "", "~my-stats": "my-stats", " st ": "st"} {
		s, _, err := LoadSettings(env(map[string]string{"CB_MM_STATS_CHANNEL": v}))
		if err != nil || s.StatsChannel != want {
			t.Errorf("%q: %q, %v", v, s.StatsChannel, err)
		}
	}
	s, _, err := LoadSettings(env(nil))
	if err != nil || s.StatsChannel != "agents-stats" {
		t.Errorf("unset: %q, %v", s.StatsChannel, err)
	}
	if s, _, err = LoadSettings(env(map[string]string{"MM_STATS_CHANNEL": "old"})); err != nil || s.StatsChannel != "old" {
		t.Errorf("the old name: %q, %v", s.StatsChannel, err)
	}
	if _, _, err = LoadSettings(env(map[string]string{"MM_STATS_CHANNEL": "a", "CB_MM_STATS_CHANNEL": "b"})); !errors.Is(err, ErrConfig) {
		t.Errorf("two names, two values: %v", err)
	}
	if s, _, err = LoadSettings(env(map[string]string{"CB_AUTH_STATS_ARCHIVE_PREFIX": "hist"})); err != nil || s.Callout.Subjects.Archive != "hist" {
		t.Errorf("archive prefix: %q, %v", s.Callout.Subjects.Archive, err)
	}
}
