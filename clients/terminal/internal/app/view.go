package app

import (
	"strings"

	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/commandstate"
	notificationview "github.com/plumliu/pulsara-agent/clients/terminal/internal/components/notification"
	statusview "github.com/plumliu/pulsara-agent/clients/terminal/internal/components/status"
	transcriptview "github.com/plumliu/pulsara-agent/clients/terminal/internal/components/transcript"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/publictext"
)

func render(state AppState) tea.View {
	plan := state.layout
	rows := make([]string, 0, plan.Height)
	composerCursorX, composerCursorY, composerHasCursor := 0, 0, false
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
		if plan.ComposerRows > 0 {
			rendered := state.composer.Render(plan.Width, plan.ComposerRows)
			for index := 0; index < plan.ComposerRows; index++ {
				line := ""
				if index < len(rendered.Rows) {
					line = rendered.Rows[index]
				}
				rows = append(rows, fitLayoutLine(line, plan.Width))
			}
			composerCursorX = rendered.CursorX
			composerCursorY = plan.HeaderRows + plan.TranscriptRows + rendered.CursorY
			composerHasCursor = rendered.HasCursor
		}
		footer := compactFooter(plan.Width)
		blocked := ""
		if state.composer.Enabled() {
			footer = interactiveFooter(plan.Width, state.activeRunID() != "")
			blocked = state.composer.AvailabilityStatus()
			if blocked != "" {
				footer = blocked + " · ↑↓ prompts · Ctrl-D detach"
			}
		}
		latest := notificationview.RenderLatest(state.localNotifications)
		pending := commandStatusLine(state)
		switch {
		case state.localNotifications.LatestSticky():
			footer = latest
		case blocked != "" && pending != "":
			// The current admission reason is first so a narrow terminal never
			// hides why Enter is blocked. Pending command state remains visible
			// on wider layouts and reappears once the control blocker clears.
			footer = blocked + " · " + pending
		case pending != "":
			footer = pending
		case latest != "":
			footer = latest
		}
		rows = append(rows, fitLayoutLine(footer, plan.Width))
	}

	view := tea.NewView(strings.Join(rows, "\n"))
	if plan.ComposerRows > 0 && composerHasCursor {
		view.Cursor = tea.NewCursor(composerCursorX, composerCursorY)
		view.Cursor.Blink = true
	}
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

func commandStatusLine(state AppState) string {
	record, ok := state.commands.LatestPendingRecord()
	if !ok {
		return ""
	}
	verb := "Command"
	suffix := ""
	if record.Candidate().Kind() == commandstate.SubmitPrompt {
		verb = "Message"
		suffix = " · new draft ready"
	} else if record.Candidate().Kind() == commandstate.StopRun {
		verb = "Stop"
	}
	switch record.Phase() {
	case commandstate.Frozen, commandstate.Sending, commandstate.AwaitingOutcome:
		return verb + " sending…" + suffix
	case commandstate.QueryRequired, commandstate.Querying, commandstate.PendingConfirmation:
		return verb + " confirming durable outcome…" + suffix
	default:
		return ""
	}
}

func headerLine(state AppState) string {
	product := lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("63")).Render("Pulsara")
	status := phaseLabel(state.phase)
	if state.attachment.Valid {
		role := "observer"
		if state.controllerGranted() {
			role = "controller"
		}
		status += " · " + role
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
	if state.hasPublicFailure && (state.phase == PhaseFatal || state.phase == PhaseReadOnly) {
		message := publictext.Transform("Unable to continue terminal client: " + state.publicFailure.message)
		return strings.Split(ansi.Hardwrap(message, width, false), "\n")
	}
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
