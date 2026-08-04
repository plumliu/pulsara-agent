package app

import (
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	statusview "github.com/plumliu/pulsara-agent/clients/terminal/internal/components/status"
	transcriptview "github.com/plumliu/pulsara-agent/clients/terminal/internal/components/transcript"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/publictext"
)

func render(state AppState) tea.View {
	plan := state.layout
	rows := make([]string, 0, plan.Height)
	if plan.Mode == LayoutSingleLine {
		rows = append(rows, fitLayoutLine(singleLineStatus(state), plan.Width))
	} else {
		rows = append(rows, fitLayoutLine(headerLine(state), plan.Width))
		if plan.TranscriptRows > 0 {
			body := bodyLines(state, plan.Width)
			for index := 0; index < plan.TranscriptRows; index++ {
				line := ""
				if index < len(body) {
					line = body[index]
				}
				rows = append(rows, fitLayoutLine(line, plan.Width))
			}
		}
		footer := compactFooter(plan.Width)
		if count := len(state.localNotifications.Items); count > 0 {
			footer = state.localNotifications.Items[count-1]
		}
		rows = append(rows, fitLayoutLine(footer, plan.Width))
	}

	view := tea.NewView(strings.Join(rows, "\n"))
	view.AltScreen = true
	switch state.mouseMode {
	case MouseCellMotion:
		view.MouseMode = tea.MouseModeCellMotion
	case MouseAllMotion:
		view.MouseMode = tea.MouseModeAllMotion
	default:
		view.MouseMode = tea.MouseModeNone
	}
	return view
}

func headerLine(state AppState) string {
	product := lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("63")).Render("Pulsara")
	status := phaseLabel(state.phase)
	if state.attachment.Valid {
		status += " · observer"
	}
	if state.durable.Installed() {
		status += " · " + statusview.Durable(state.durable.ProjectionRevision(), state.control.QueueItemCount(), state.transcript.UnseenTerminalCount(), state.durable.Stale() || state.control.SnapshotRequired())
	}
	if state.hasPublicFailure {
		status += " · failed"
	}
	return product + "  " + status
}

func singleLineStatus(state AppState) string {
	status := "Pulsara · " + phaseLabel(state.phase)
	if state.hasPublicFailure {
		status = "Pulsara · fatal"
	}
	return status + " · q"
}

func bodyLines(state AppState, width int) []string {
	if state.durable.Installed() {
		return transcriptview.Render(state.transcript)
	}
	message := "Connecting to the Pulsara runtime…"
	if state.hasPublicFailure {
		message = "Unable to start terminal client: " + state.publicFailure.message
	}
	message = publictext.Transform(message)
	return strings.Split(ansi.Hardwrap(message, width, false), "\n")
}

func phaseLabel(phase AppPhase) string {
	return map[AppPhase]string{
		PhaseBooting: "booting", PhaseConnecting: "connecting", PhaseNegotiating: "negotiating",
		PhaseAttaching: "attaching", PhaseLoadingSnapshot: "loading", PhaseReady: "ready",
		PhaseReconnecting: "reconnecting", PhaseReadOnly: "read-only", PhaseFatal: "fatal",
		PhaseDetaching: "closing", PhaseExited: "exited",
	}[phase]
}
