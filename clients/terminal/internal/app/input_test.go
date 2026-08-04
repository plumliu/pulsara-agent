package app

import (
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
)

func TestProductionInputBoundaryRejectsUnknownFrameworkMessage(t *testing.T) {
	boundary := productionInputBoundary{inner: NewModel(nil)}
	next, _ := boundary.Update(struct{ Unexpected string }{Unexpected: "framework"})
	result, ok := next.(productionInputBoundary)
	if !ok {
		t.Fatal("production input boundary changed concrete type")
	}
	if result.inner.state.phase != PhaseFatal {
		t.Fatal("unknown framework message crossed the closed application boundary")
	}
}

func TestProductionInputBoundaryCountsKnownFrameworkAdvisories(t *testing.T) {
	boundary := productionInputBoundary{inner: NewModel(nil)}
	next, _ := boundary.Update(tea.EnvMsg(nil))
	result := next.(productionInputBoundary)
	if result.inner.state.phase == PhaseFatal || result.inner.state.frameworkAdvisories != 1 {
		t.Fatal("known framework advisory was not operationally ignored and counted")
	}
}

func TestProductionInputBoundaryConsumesBubbleTeaSetClipboardCarrier(t *testing.T) {
	message := tea.SetClipboard("public transcript")()
	boundary := productionInputBoundary{inner: NewModel(nil)}
	next, _ := boundary.Update(message)
	result := next.(productionInputBoundary)
	if result.inner.state.phase == PhaseFatal || result.inner.state.frameworkAdvisories != 1 {
		t.Fatal("Bubble Tea clipboard carrier crossed the closed application boundary")
	}
}

func TestFrameworkFocusAndClosedApplicationMessagesAreNormalizedExactlyOnce(t *testing.T) {
	header := testLocalMessageHeader(t, 1)
	message, ok := normalizeFrameworkMessage(tea.FocusMsg{}, header)
	if !ok {
		t.Fatal("focus message was not normalized")
	}
	if focused, ok := message.(FocusChangedMsg); !ok || !focused.Focused || focused.Header != header {
		t.Fatalf("unexpected focus normalization: %#v", message)
	}

	boundary := productionInputBoundary{inner: NewModel(nil)}
	next, _ := boundary.Update(FrameworkAdvisoryIgnoredMsg{Kind: FrameworkAdvisoryEnvironment})
	result := next.(productionInputBoundary)
	if result.inner.state.phase == PhaseFatal || result.inner.state.frameworkAdvisories != 1 {
		t.Fatal("closed application message was reclassified as raw framework input")
	}
}

func TestFrameworkMouseWheelIsNormalizedToClosedVisualRowInput(t *testing.T) {
	header := testLocalMessageHeader(t, 1)
	tests := []struct {
		button    tea.MouseButton
		direction MouseWheelDirection
	}{
		{button: tea.MouseWheelUp, direction: MouseWheelScrollUp},
		{button: tea.MouseWheelDown, direction: MouseWheelScrollDown},
	}
	for _, test := range tests {
		message, ok := normalizeFrameworkMessage(
			tea.MouseWheelMsg(tea.Mouse{X: 4, Y: 7, Button: test.button}),
			header,
		)
		wheel, isWheel := message.(MouseWheelInputMsg)
		if !ok || !isWheel || wheel.Header != header || wheel.Direction != test.direction || wheel.VisualRows != mouseWheelVisualRows {
			t.Fatalf("unexpected mouse wheel normalization: %#v", message)
		}
	}
}

func TestFrameworkNonWheelMouseInputIsOperationallyIgnored(t *testing.T) {
	header := testLocalMessageHeader(t, 1)
	message, ok := normalizeFrameworkMessage(
		tea.MouseClickMsg(tea.Mouse{X: 2, Y: 3, Button: tea.MouseLeft}),
		header,
	)
	advisory, isAdvisory := message.(FrameworkAdvisoryIgnoredMsg)
	if !ok || !isAdvisory || advisory.Header != header || advisory.Kind != FrameworkAdvisoryMousePointer {
		t.Fatalf("unexpected pointer normalization: %#v", message)
	}
}

func testLocalMessageHeader(t *testing.T, sequence uint64) LocalMessageHeader {
	t.Helper()
	return testLocalMessageHeaderAt(t, sequence, time.Now())
}

func testLocalMessageHeaderAt(t *testing.T, sequence uint64, producedAt time.Time) LocalMessageHeader {
	t.Helper()
	header, err := NewLocalMessageHeader(1, sequence, producedAt)
	if err != nil {
		t.Fatal(err)
	}
	return header
}
