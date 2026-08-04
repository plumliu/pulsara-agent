package transcript

import (
	"strings"

	"charm.land/lipgloss/v2"
	"github.com/charmbracelet/x/ansi"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/presentation"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/publictext"
)

func Render(state presentation.State, width, height int) string {
	cells := state.Durable().Cells
	if len(cells) == 0 {
		return lipgloss.NewStyle().Faint(true).Render("No conversation history yet.")
	}
	contentWidth := max(width-2, 1)
	lines := make([]string, 0, min(len(cells)*3, max(height, 1)*4))
	for _, cell := range cells {
		lines = append(lines, lipgloss.NewStyle().Bold(true).Render(cell.Kind))
		text := publictext.Transform(cell.PublicText)
		wrapped := ansi.Hardwrap(text, contentWidth, false)
		lines = append(lines, strings.Split(wrapped, "\n")...)
	}
	if height < 1 {
		height = 1
	}
	end := len(lines) - state.ScrollOffset()
	if end < min(height, len(lines)) {
		end = min(height, len(lines))
	}
	start := max(end-height, 0)
	return strings.Join(lines[start:end], "\n")
}
