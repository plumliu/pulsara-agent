package supervision

import "testing"

func TestChildExitSummaryClosedMatrix(t *testing.T) {
	valid := []ChildExitSummary{
		{Outcome: ChildExitedNormally},
		{Outcome: ChildRequestedParentRelaunch, ExitCode: 75},
		{Outcome: ChildExitedWithFailure, ExitCode: 2},
		{Outcome: ChildTerminatedBySignal, Signal: 15},
	}
	for _, value := range valid {
		if err := value.Validate(); err != nil {
			t.Fatalf("valid child exit rejected: %#v: %v", value, err)
		}
	}
	if err := (ChildExitSummary{Outcome: ChildExitedNormally, ExitCode: 1}).Validate(); err == nil {
		t.Fatal("invalid normal child exit was accepted")
	}
}
