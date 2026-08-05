package composer

import (
	"errors"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/rivo/uniseg"
)

func (m Model) Insert(value string) (Model, error) {
	if m.mode == Disabled || !utf8.ValidString(value) {
		return m, errors.New("terminal composer insert is unavailable")
	}
	value = normalizeNewlines(value)
	if !draftTextSafe(value) {
		return m, errors.New("terminal composer input contains terminal controls")
	}
	if m.mode == PasteReview {
		m = m.CancelPasteReview()
	}
	clusters := graphemeClusters(m.revision.DraftUTF8)
	if int(m.revision.GraphemeCursor) > len(clusters) {
		return m, errors.New("terminal composer cursor is stale")
	}
	insert := graphemeClusters(value)
	result := strings.Join(clusters[:m.revision.GraphemeCursor], "") + value + strings.Join(clusters[m.revision.GraphemeCursor:], "")
	if len([]byte(result)) > MaximumDraftBytes {
		return m, errors.New("terminal composer draft exceeds 32 KiB")
	}
	return m.mutate(result, m.revision.GraphemeCursor+uint32(len(insert)), visualColumnAt(result, m.revision.GraphemeCursor+uint32(len(insert))))
}

func (m Model) Backspace() Model {
	if m.mode == Disabled || m.revision.GraphemeCursor == 0 {
		return m
	}
	if m.mode == PasteReview {
		m = m.CancelPasteReview()
	}
	clusters := graphemeClusters(m.revision.DraftUTF8)
	index := int(m.revision.GraphemeCursor)
	result := strings.Join(clusters[:index-1], "") + strings.Join(clusters[index:], "")
	updated, err := m.mutate(result, uint32(index-1), visualColumnAt(result, uint32(index-1)))
	if err != nil {
		return m
	}
	return updated
}

func (m Model) Delete() Model {
	if m.mode == Disabled {
		return m
	}
	if m.mode == PasteReview {
		m = m.CancelPasteReview()
	}
	clusters := graphemeClusters(m.revision.DraftUTF8)
	index := int(m.revision.GraphemeCursor)
	if index >= len(clusters) {
		return m
	}
	result := strings.Join(clusters[:index], "") + strings.Join(clusters[index+1:], "")
	updated, err := m.mutate(result, uint32(index), visualColumnAt(result, uint32(index)))
	if err != nil {
		return m
	}
	return updated
}

func (m Model) MoveLeft() Model {
	if m.mode == Disabled || m.revision.GraphemeCursor == 0 {
		return m
	}
	return m.moveCursor(m.revision.GraphemeCursor - 1)
}

func (m Model) MoveRight() Model {
	if m.mode == Disabled || int(m.revision.GraphemeCursor) >= len(graphemeClusters(m.revision.DraftUTF8)) {
		return m
	}
	return m.moveCursor(m.revision.GraphemeCursor + 1)
}

func (m Model) MoveHome() Model {
	clusters := graphemeClusters(m.revision.DraftUTF8)
	index := int(m.revision.GraphemeCursor)
	for index > 0 && clusters[index-1] != "\n" {
		index--
	}
	return m.moveCursor(uint32(index))
}

func (m Model) MoveEnd() Model {
	clusters := graphemeClusters(m.revision.DraftUTF8)
	index := int(m.revision.GraphemeCursor)
	for index < len(clusters) && clusters[index] != "\n" {
		index++
	}
	return m.moveCursor(uint32(index))
}

func (m Model) MoveUp() Model {
	if m.historyIndex >= 0 {
		return m.PreviousHistory()
	}
	if !strings.Contains(m.revision.DraftUTF8, "\n") {
		return m.PreviousHistory()
	}
	return m.moveVertical(-1)
}

func (m Model) MoveDown() Model {
	if m.historyIndex >= 0 {
		return m.NextHistory()
	}
	return m.moveVertical(1)
}

func (m Model) moveVertical(delta int) Model {
	if m.mode == Disabled {
		return m
	}
	clusters := graphemeClusters(m.revision.DraftUTF8)
	starts := []int{0}
	for index, cluster := range clusters {
		if cluster == "\n" {
			starts = append(starts, index+1)
		}
	}
	current := 0
	for index := range starts {
		if starts[index] <= int(m.revision.GraphemeCursor) {
			current = index
		}
	}
	target := current + delta
	if target < 0 || target >= len(starts) {
		return m
	}
	column := m.revision.PreferredColumn
	if column == 0 {
		column = visualColumnAt(m.revision.DraftUTF8, m.revision.GraphemeCursor)
	}
	end := len(clusters)
	if target+1 < len(starts) {
		end = starts[target+1] - 1
	}
	position, visual := starts[target], 0
	for position < end {
		width := uniseg.StringWidth(clusters[position])
		if visual+width > column {
			break
		}
		visual += width
		position++
	}
	updated := m.moveCursor(uint32(position))
	updated.revision.PreferredColumn = column
	updated.revision.ViewFingerprint, _ = revisionViewFingerprint(updated.revision.ContentFingerprint, updated.revision.GraphemeCursor, updated.revision.PreferredColumn)
	return updated
}

func (m Model) mutate(text string, cursor uint32, preferred int) (Model, error) {
	if m.revision.Revision == ^uint64(0) {
		return m, errors.New("terminal composer revision exhausted")
	}
	updated, err := newRevision(m.revision.Revision+1, text, cursor, preferred)
	if err != nil {
		return m, err
	}
	m.undo = appendBoundedRevision(m.undo, m.revision)
	m.redo = nil
	m = m.clearHistoryTraversal()
	m.revision = updated
	m.mode = Ordinary
	m.paste = PasteReviewState{}
	return m, m.Validate()
}

func (m Model) moveCursor(cursor uint32) Model {
	updated, err := newRevision(m.revision.Revision, m.revision.DraftUTF8, cursor, visualColumnAt(m.revision.DraftUTF8, cursor))
	if err == nil {
		m.revision = updated
	}
	return m
}

func graphemeClusters(value string) []string {
	result := make([]string, 0, utf8.RuneCountInString(value))
	graphemes := uniseg.NewGraphemes(value)
	for graphemes.Next() {
		result = append(result, graphemes.Str())
	}
	return result
}

func visualColumnAt(value string, cursor uint32) int {
	clusters := graphemeClusters(value)
	limit := int(cursor)
	if limit > len(clusters) {
		limit = len(clusters)
	}
	column := 0
	for index := limit - 1; index >= 0 && clusters[index] != "\n"; index-- {
		column += uniseg.StringWidth(clusters[index])
	}
	return column
}

func draftTextSafe(value string) bool {
	for _, character := range value {
		if character == '\n' || character == '\t' {
			continue
		}
		if unicode.IsControl(character) {
			return false
		}
	}
	return true
}
