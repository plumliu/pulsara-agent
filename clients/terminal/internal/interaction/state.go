package interaction

import "errors"

type Phase uint8

const (
	InteractionDormant Phase = iota + 1
	InteractionReadOnlyProjected
	InteractionActive
)

// State owns client-local interaction selection and action enablement. The
// server-projected interaction payload remains in presentation control state.
type State struct {
	phase                    Phase
	controlCursorFingerprint string
	projectedViewFingerprint string
	targetID                 string
	targetGeneration         uint64
	enabledActions           uint64
	selectionFingerprint     string
}

func NewDormantState() State { return State{phase: InteractionDormant} }

func NewReadOnlyProjectedInteraction(cursorFingerprint, projectedViewFingerprint, targetID string, targetGeneration uint64) (State, error) {
	if cursorFingerprint == "" || projectedViewFingerprint == "" || (targetID == "") != (targetGeneration == 0) {
		return State{}, errors.New("terminal read-only interaction projection is invalid")
	}
	value := State{
		phase:                    InteractionReadOnlyProjected,
		controlCursorFingerprint: cursorFingerprint,
		projectedViewFingerprint: projectedViewFingerprint,
		targetID:                 targetID,
		targetGeneration:         targetGeneration,
	}
	return value, value.Validate()
}

func (s State) Validate() error {
	switch s.phase {
	case InteractionDormant:
		if s.controlCursorFingerprint != "" || s.projectedViewFingerprint != "" || s.targetID != "" || s.targetGeneration != 0 || s.enabledActions != 0 || s.selectionFingerprint != "" {
			return errors.New("dormant terminal interaction contains projected state")
		}
	case InteractionReadOnlyProjected:
		if s.controlCursorFingerprint == "" || s.projectedViewFingerprint == "" || (s.targetID == "") != (s.targetGeneration == 0) || s.enabledActions != 0 || s.selectionFingerprint != "" {
			return errors.New("read-only terminal interaction state is invalid")
		}
	case InteractionActive:
		if s.controlCursorFingerprint == "" || s.projectedViewFingerprint == "" || s.targetID == "" || s.targetGeneration == 0 {
			return errors.New("active terminal interaction state is invalid")
		}
	default:
		return errors.New("terminal interaction phase is unknown")
	}
	return nil
}

func (s State) Pending() bool             { return s.targetID != "" }
func (s State) ActionsEnabled() bool      { return s.enabledActions != 0 }
func (s State) CursorFingerprint() string { return s.controlCursorFingerprint }
func (s State) ViewFingerprint() string   { return s.projectedViewFingerprint }
func (s State) Phase() Phase              { return s.phase }
