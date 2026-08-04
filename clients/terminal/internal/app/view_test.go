package app

import (
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func TestProductionViewOwnsAlternateScreenAndCellMotionMouseMode(t *testing.T) {
	state := NewInitialAppState("terminal-client:view")
	view := render(state)
	if !view.AltScreen || view.MouseMode != tea.MouseModeCellMotion {
		t.Fatalf("production terminal mode mismatch: alt=%v mouse=%v", view.AltScreen, view.MouseMode)
	}
	if !strings.Contains(view.Content, "wheel/↑/↓ scroll") {
		t.Fatalf("view does not disclose its scroll controls: %q", view.Content)
	}
}

func TestViewAlwaysFillsValidatedTerminalGeometry(t *testing.T) {
	for _, dimensions := range [][2]int{{1, 1}, {8, 2}, {80, 3}, {80, 24}, {120, 30}, {160, 59}} {
		state := NewInitialAppState("terminal-client:geometry")
		plan, err := NewLayoutPlan(dimensions[0], dimensions[1])
		if err != nil {
			t.Fatal(err)
		}
		state.layout = plan
		state.transcript, err = state.transcript.Resize(protocolvalue.DurableSnapshot{}, plan.Width, plan.TranscriptRows)
		if err != nil {
			t.Fatal(err)
		}
		view := render(state)
		rows := strings.Split(view.Content, "\n")
		if len(rows) != plan.Height {
			t.Fatalf("%dx%d rendered %d rows", plan.Width, plan.Height, len(rows))
		}
		for index, row := range rows {
			if got := ansi.StringWidth(row); got != plan.Width {
				t.Fatalf("%dx%d row %d width=%d: %q", plan.Width, plan.Height, index, got, row)
			}
		}
		if plan.FooterRows == 1 && !strings.Contains(rows[len(rows)-1], "q") {
			t.Fatalf("%dx%d footer is not fixed to the final row: %q", plan.Width, plan.Height, rows[len(rows)-1])
		}
	}
}

func TestReadyViewRendersPreparedWideTranscriptInsideBody(t *testing.T) {
	state := NewInitialAppState("terminal-client:ready-view")
	snapshot := testDurableSnapshot("runtime:view", []protocolvalue.HistoryCell{{
		ID: "entry:view", Kind: "assistant",
		PublicText:  "中文 emoji 🌍 and " + strings.Repeat("long-unbreakable", 20),
		Fingerprint: "cell:view",
	}})
	snapshot.HostSessionID = "host:view"
	var err error
	state.durable, err = state.durable.Install(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	state.transcript, err = state.transcript.Install(snapshot, state.layout.Width, state.layout.TranscriptRows)
	if err != nil {
		t.Fatal(err)
	}
	state.phase = PhaseReady
	view := render(state)
	rows := strings.Split(view.Content, "\n")
	if len(rows) != state.layout.Height || !strings.Contains(view.Content, "🌍") {
		t.Fatalf("ready transcript did not fill its bounded body: rows=%d\n%s", len(rows), view.Content)
	}
	for _, row := range rows {
		if ansi.StringWidth(row) != state.layout.Width {
			t.Fatalf("ready row escaped geometry: %q", row)
		}
	}
}

func TestViewMakesTranscriptControlSequencesInert(t *testing.T) {
	state := NewInitialAppState("terminal-client:public-text")
	snapshot := testDurableSnapshot("runtime:public", []protocolvalue.HistoryCell{{ID: "entry:public", Kind: "assistant", PublicText: "safe\x1b]52;c;ZXZpbA==\aafter", Fingerprint: "cell:public"}})
	snapshot.HostSessionID = "host:public"
	var err error
	state.durable, err = state.durable.Install(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	state.transcript, err = state.transcript.Install(snapshot, state.layout.Width, state.layout.TranscriptRows)
	if err != nil {
		t.Fatal(err)
	}
	content := render(state).Content
	if strings.Contains(content, "\x1b]52") || strings.ContainsRune(content, '\a') {
		t.Fatalf("terminal control sequence reached the production view: %q", content)
	}
}
