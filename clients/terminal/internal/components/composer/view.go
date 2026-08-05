package composer

import (
	"fmt"
	"strings"

	"github.com/charmbracelet/x/ansi"
	"github.com/rivo/uniseg"
)

type Rendered struct {
	Rows      []string
	CursorX   int
	CursorY   int
	HasCursor bool
}

func (m Model) DesiredRows(width int) int {
	if m.mode == Disabled || width < 1 {
		return 0
	}
	rows := m.renderRows(width)
	if len(rows) < 1 {
		return 1
	}
	if len(rows) > MaximumVisualRows {
		return MaximumVisualRows
	}
	return len(rows)
}

func (m Model) Render(width, height int) Rendered {
	if m.mode == Disabled || width < 1 || height < 1 {
		return Rendered{}
	}
	rows := m.renderRows(width)
	if m.mode == PasteReview {
		rows = boundedPasteReviewRows(rows, height)
	}
	start := 0
	if len(rows) > height {
		start = len(rows) - height
		if m.mode == Ordinary {
			for index, row := range rows {
				if !row.cursor {
					continue
				}
				if index < start {
					start = index
				} else if index >= start+height {
					start = index - height + 1
				}
				break
			}
		}
		rows = append([]renderRow(nil), rows[start:start+height]...)
	}
	result := Rendered{Rows: make([]string, 0, len(rows))}
	for index, row := range rows {
		result.Rows = append(result.Rows, ansi.Truncate(row.text, width, ""))
		if row.cursor && m.mode == Ordinary && m.availability != SubmitDisabled {
			result.CursorX, result.CursorY, result.HasCursor = row.cursorX, index, true
			if result.CursorX >= width {
				result.CursorX = width - 1
			}
		}
	}
	return result
}

type renderRow struct {
	text    string
	cursor  bool
	cursorX int
}

func (m Model) renderRows(width int) []renderRow {
	if m.mode == PasteReview {
		header := ansi.Truncate(
			fmt.Sprintf("Paste review · %d bytes · %d lines · Enter submit · Esc cancel", m.paste.byteCount, m.paste.lineCount),
			width,
			"",
		)
		return append([]renderRow{{text: header}}, wrapDisplay(m.paste.preview, width, 0, false)...)
	}
	text := m.revision.DraftUTF8
	if text == "" {
		placeholder := "Type a message…"
		if status := m.AvailabilityStatus(); status != "" {
			placeholder = status
		}
		return []renderRow{{text: "› " + placeholder, cursor: true, cursorX: 2}}
	}
	return wrapDisplay(text, width, m.revision.GraphemeCursor, true)
}

func boundedPasteReviewRows(rows []renderRow, height int) []renderRow {
	if height < 1 || len(rows) <= height {
		return rows
	}
	// The review contract always keeps its header.  Remaining rows show a
	// balanced head/tail preview instead of the tail of a truncated prefix.
	previewRows := height - 1
	if previewRows <= 0 {
		return append([]renderRow(nil), rows[:1]...)
	}
	if previewRows == 1 {
		return []renderRow{rows[0], {text: "  …"}}
	}
	headCount := (previewRows - 1 + 1) / 2
	tailCount := previewRows - 1 - headCount
	result := make([]renderRow, 0, height)
	result = append(result, rows[0])
	result = append(result, rows[1:1+headCount]...)
	result = append(result, renderRow{text: "  …"})
	if tailCount > 0 {
		result = append(result, rows[len(rows)-tailCount:]...)
	}
	return result
}

func wrapDisplay(value string, width int, cursor uint32, showCursor bool) []renderRow {
	if width < 1 {
		return nil
	}
	bodyWidth := width - 2
	if bodyWidth < 1 {
		bodyWidth = 1
	}
	clusters := graphemeClusters(value)
	rows := []renderRow{{text: "› "}}
	rowIndex, rowWidth := 0, 0
	for index, cluster := range clusters {
		if showCursor && uint32(index) == cursor {
			rows[rowIndex].cursor, rows[rowIndex].cursorX = true, 2+rowWidth
		}
		if cluster == "\n" {
			rows = append(rows, renderRow{text: "  "})
			rowIndex, rowWidth = len(rows)-1, 0
			continue
		}
		display := cluster
		if cluster == "\t" {
			display = "    "
		}
		clusterWidth := uniseg.StringWidth(display)
		if rowWidth > 0 && rowWidth+clusterWidth > bodyWidth {
			rows = append(rows, renderRow{text: "  "})
			rowIndex, rowWidth = len(rows)-1, 0
		}
		rows[rowIndex].text += display
		rowWidth += clusterWidth
	}
	if showCursor && uint32(len(clusters)) == cursor {
		rows[rowIndex].cursor, rows[rowIndex].cursorX = true, 2+rowWidth
	}
	for index := range rows {
		rows[index].text = strings.TrimSuffix(rows[index].text, "\r")
	}
	return rows
}
