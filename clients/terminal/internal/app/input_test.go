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
