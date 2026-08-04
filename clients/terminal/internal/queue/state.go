package queue

import "errors"

const S1MaximumActiveItems uint32 = 64

type Phase uint8

const (
	QueueDormant Phase = iota + 1
	QueueReadOnlyProjected
	QueueActive
)

// State contains only client-local queue controls. The immutable item vector
// has a single owner in presentation.ControlProjectionState.
type State struct {
	phase                    Phase
	controlCursorFingerprint string
	projectedViewFingerprint string
	negotiatedMaximum        uint32
	activeCount              uint32
	pendingCommandCount      uint32
	stale                    bool
	mutationEnabled          bool
}

func NewDormantState(maximum uint32) (State, error) {
	if maximum != S1MaximumActiveItems {
		return State{}, errors.New("terminal queue maximum is incompatible with S1")
	}
	return State{phase: QueueDormant, negotiatedMaximum: maximum}, nil
}

func NewReadOnlyProjectedQueue(cursorFingerprint, projectedViewFingerprint string, negotiatedMaximum, activeCount uint32) (State, error) {
	if cursorFingerprint == "" || projectedViewFingerprint == "" || negotiatedMaximum != S1MaximumActiveItems || activeCount > negotiatedMaximum {
		return State{}, errors.New("terminal read-only queue projection is invalid")
	}
	value := State{phase: QueueReadOnlyProjected, controlCursorFingerprint: cursorFingerprint, projectedViewFingerprint: projectedViewFingerprint, negotiatedMaximum: negotiatedMaximum, activeCount: activeCount}
	return value, value.Validate()
}

func (s State) Validate() error {
	if s.negotiatedMaximum != S1MaximumActiveItems || s.activeCount > s.negotiatedMaximum || s.pendingCommandCount > s.negotiatedMaximum {
		return errors.New("terminal queue state exceeds its negotiated bound")
	}
	switch s.phase {
	case QueueDormant:
		if s.controlCursorFingerprint != "" || s.projectedViewFingerprint != "" || s.activeCount != 0 || s.pendingCommandCount != 0 || s.stale || s.mutationEnabled {
			return errors.New("dormant terminal queue contains projected state")
		}
	case QueueReadOnlyProjected:
		if s.controlCursorFingerprint == "" || s.projectedViewFingerprint == "" || s.pendingCommandCount != 0 || s.stale || s.mutationEnabled {
			return errors.New("read-only terminal queue state is invalid")
		}
	case QueueActive:
		if s.controlCursorFingerprint == "" || s.projectedViewFingerprint == "" {
			return errors.New("active terminal queue lacks control authority")
		}
	default:
		return errors.New("terminal queue phase is unknown")
	}
	return nil
}

func (s State) ActiveCount() uint32       { return s.activeCount }
func (s State) ActionsEnabled() bool      { return s.mutationEnabled && !s.stale }
func (s State) CursorFingerprint() string { return s.controlCursorFingerprint }
func (s State) ViewFingerprint() string   { return s.projectedViewFingerprint }
func (s State) Phase() Phase              { return s.phase }
