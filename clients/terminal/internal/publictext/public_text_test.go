package publictext

import "testing"

func TestTransformNeutralizesTerminalControls(t *testing.T) {
	input := "safe\n\t\x1b]52;c;payload\x07\x9b31m\r"
	want := "safe\n\t\\x1B]52;c;payload\\x07\\x9B31m\\x0D"
	if got := Transform(input); got != want {
		t.Fatalf("terminal-safe transform mismatch: %q", got)
	}
	if !IsSafe(want) || IsSafe(input) {
		t.Fatal("terminal-safe predicate did not match the transform")
	}
}
