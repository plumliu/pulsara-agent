package supervision

import "errors"

type ChildExitOutcome uint8

const (
	ChildExitedNormally ChildExitOutcome = iota + 1
	ChildRequestedParentRelaunch
	ChildExitedWithFailure
	ChildTerminatedBySignal
)

type ChildExitSummary struct {
	Outcome  ChildExitOutcome
	ExitCode int
	Signal   int
}

func (s ChildExitSummary) Validate() error {
	switch s.Outcome {
	case ChildExitedNormally:
		if s.ExitCode != 0 || s.Signal != 0 {
			return errors.New("normal terminal child exit contains failure attribution")
		}
	case ChildRequestedParentRelaunch:
		if s.ExitCode != 75 || s.Signal != 0 {
			return errors.New("terminal child relaunch exit is invalid")
		}
	case ChildExitedWithFailure:
		if s.ExitCode <= 0 || s.ExitCode == 75 || s.Signal != 0 {
			return errors.New("terminal child failure exit is invalid")
		}
	case ChildTerminatedBySignal:
		if s.ExitCode != 0 || s.Signal <= 0 {
			return errors.New("terminal child signal exit is invalid")
		}
	default:
		return errors.New("terminal child exit outcome is unknown")
	}
	return nil
}
