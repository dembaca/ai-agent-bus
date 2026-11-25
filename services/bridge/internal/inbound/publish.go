package inbound

import (
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
)

// Confirmed publishes on a core connection and waits until the server has
// taken the message: a flush is a round trip, and the server answers a
// refused publish (a missing right) before it answers the flush, which the
// client records as the connection's last error. A refusal is an error, so
// nothing is marked carried that never reached the bus (RT-6.42 step 4).
type Confirmed struct {
	NC      *nats.Conn
	Timeout time.Duration // 5 s when zero
}

// Publish implements Publisher. One goroutine publishes through it at a time
// (the inbound loop), so the last error it sees after its flush is its own.
func (c Confirmed) Publish(subject string, data []byte) error {
	before := c.NC.LastError()
	if err := c.NC.Publish(subject, data); err != nil {
		return err
	}
	timeout := c.Timeout
	if timeout == 0 {
		timeout = 5 * time.Second
	}
	if err := c.NC.FlushTimeout(timeout); err != nil {
		return fmt.Errorf("publish %s: not confirmed: %w", subject, err)
	}
	if after := c.NC.LastError(); after != nil && after != before &&
		errors.Is(after, nats.ErrPermissionViolation) && strings.Contains(after.Error(), `"`+subject+`"`) {
		return fmt.Errorf("publish %s: refused: %w", subject, after)
	}
	return nil
}
