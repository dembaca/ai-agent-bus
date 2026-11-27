package directory

import (
	"context"
	"errors"
	"fmt"
	"slices"
)

// Plan B's switches (ADR-014 phase 5, RT-6.39 step 5): per channel, the
// pieces of plan B an administrator switched on, beside the channel's other
// settings. Off by default: a channel lists none until an administrator sets
// some. Piece 14 (registration ends) is no switch.

// MaxPiece is the highest plan B piece with a switch.
const MaxPiece = 13

// ErrBadPiece: a plan B piece outside 1 to MaxPiece, or one given twice.
var ErrBadPiece = errors.New("directory: plan B pieces are 1 to 13, each once")

// NormalPieces checks a list an administrator gives and returns it sorted;
// an empty list is nil, so the record leaves the key out.
func NormalPieces(p []int) ([]int, error) {
	if len(p) == 0 {
		return nil, nil
	}
	out := slices.Clone(p)
	slices.Sort(out)
	for i, n := range out {
		if n < 1 || n > MaxPiece {
			return nil, fmt.Errorf("%w: %d", ErrBadPiece, n)
		}
		if i > 0 && out[i-1] == n {
			return nil, fmt.Errorf("%w: %d twice", ErrBadPiece, n)
		}
	}
	return out, nil
}

// Pieces is the valid part of a stored list, sorted and each once, and what
// it dropped: a value outside 1 to MaxPiece or a repeat. A stored list passed
// NormalPieces, so anything dropped was written around it (by hand into the
// bucket, or by a later release with more pieces).
func Pieces(raw []int) (valid, dropped []int) {
	seen := map[int]bool{}
	for _, n := range raw {
		if n < 1 || n > MaxPiece || seen[n] {
			dropped = append(dropped, n)
			continue
		}
		seen[n] = true
		valid = append(valid, n)
	}
	slices.Sort(valid)
	return valid, dropped
}

// PlanB reads the stored pieces of the given channels, as stored: a channel
// the directory does not know, or a name that is no channel token, has none.
// A read that fails is an error. Pieces makes a list safe to use.
func (s *Store) PlanB(ctx context.Context, channels []string) (map[string][]int, error) {
	out := make(map[string][]int, len(channels))
	for _, name := range channels {
		if checkChannel(name) != nil {
			out[name] = nil
			continue
		}
		c, _, ok, err := s.Channel(ctx, name)
		if err != nil {
			return nil, err
		}
		if ok {
			out[name] = c.PlanB
		} else {
			out[name] = nil
		}
	}
	return out, nil
}
