package app

import (
	"fmt"
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"

	transcript "github.com/plumliu/pulsara-agent/clients/terminal/internal/components/transcript"
)

func render(state AppState) tea.View {
	width := clamp(state.durable.Width(), 1, 240)
	height := clamp(state.durable.Height(), 1, 100)
	header := lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("63")).Render("Pulsara")
	status := phaseLabel(state.phase)
	if state.attachment.Valid {
		status += " · observer"
	}
	if state.durable.Ready() {
		snapshot := state.durable.Durable()
		status += fmt.Sprintf(" · revision %d · queue %d", snapshot.ProjectionRevision, len(state.control.Projection().QueueItems))
	}
	var body string
	if state.durable.Ready() {
		body = transcript.Render(state.durable, width, max(height-4, 1))
	} else if state.hasPublicFailure {
		body = "Unable to start terminal client:\n" + state.publicFailure.message
	} else {
		body = "Connecting to the Pulsara runtime…"
	}
	footer := "↑/↓ scroll · y copy resident transcript · q quit"
	view := tea.NewView(strings.Join([]string{header + "  " + status, body, footer}, "\n"))
	view.AltScreen = true
	return view
}

func phaseLabel(phase AppPhase) string {
	return map[AppPhase]string{
		PhaseBooting: "booting", PhaseConnecting: "connecting", PhaseNegotiating: "negotiating",
		PhaseAttaching: "attaching", PhaseLoadingSnapshot: "loading", PhaseReady: "ready",
		PhaseReconnecting: "reconnecting", PhaseReadOnly: "read-only", PhaseFatal: "fatal",
		PhaseDetaching: "closing", PhaseExited: "exited",
	}[phase]
}
