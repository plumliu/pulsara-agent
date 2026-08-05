package composer

import "unicode/utf8"

func appendBoundedRevision(values []Revision, value Revision) []Revision {
	values = append(values, value)
	if len(values) > MaximumUndoDepth {
		values = append([]Revision(nil), values[len(values)-MaximumUndoDepth:]...)
	}
	return values
}

func (m Model) Undo() Model {
	if m.mode == Disabled || len(m.undo) == 0 {
		return m
	}
	previous := m.undo[len(m.undo)-1]
	m.undo = append([]Revision(nil), m.undo[:len(m.undo)-1]...)
	m.redo = appendBoundedRevision(m.redo, m.revision)
	m.revision = previous
	m = m.clearHistoryTraversal()
	m.paste = PasteReviewState{}
	m.mode = Ordinary
	return m
}

func (m Model) Redo() Model {
	if m.mode == Disabled || len(m.redo) == 0 {
		return m
	}
	next := m.redo[len(m.redo)-1]
	m.redo = append([]Revision(nil), m.redo[:len(m.redo)-1]...)
	m.undo = appendBoundedRevision(m.undo, m.revision)
	m.revision = next
	m = m.clearHistoryTraversal()
	m.paste = PasteReviewState{}
	m.mode = Ordinary
	return m
}

func (m Model) PreviousHistory() Model {
	if m.mode != Ordinary || len(m.history) == 0 {
		return m
	}
	index := m.historyIndex
	if index < 0 {
		m.historyScratch = m.revision
		m.hasHistoryScratch = true
		index = len(m.history) - 1
	} else if index > 0 {
		index--
	}
	return m.installHistory(index)
}

func (m Model) NextHistory() Model {
	if m.mode != Ordinary || m.historyIndex < 0 || !m.hasHistoryScratch {
		return m
	}
	index := m.historyIndex + 1
	if index >= len(m.history) {
		m.revision = m.historyScratch
		return m.clearHistoryTraversal()
	}
	return m.installHistory(index)
}

func (m Model) installHistory(index int) Model {
	if index < 0 || index >= len(m.history) {
		return m
	}
	entry := m.history[index]
	if entry.text == "" || !utf8.ValidString(entry.text) || len([]byte(entry.text)) > MaximumDraftBytes || !draftTextSafe(entry.text) || entry.acceptedSubmissionID == "" || !m.hasHistoryScratch {
		return m
	}
	clusters := graphemeClusters(entry.text)
	revision, err := newRevision(m.historyScratch.Revision, entry.text, uint32(len(clusters)), visualColumnAt(entry.text, uint32(len(clusters))))
	if err != nil {
		return m
	}
	m.revision = revision
	m.historyIndex = index
	m.paste = PasteReviewState{}
	m.mode = Ordinary
	return m
}

func (m Model) clearHistoryTraversal() Model {
	m.historyIndex = -1
	m.historyScratch = Revision{}
	m.hasHistoryScratch = false
	return m
}

func (m Model) recordAcceptedHistory(frozen FrozenSubmission, acceptedSubmissionID string) Model {
	if frozen.Text == "" || acceptedSubmissionID == "" || len([]byte(frozen.Text)) > MaximumDraftBytes || !utf8.ValidString(frozen.Text) || !draftTextSafe(frozen.Text) {
		return m
	}
	for _, entry := range m.history {
		if entry.acceptedSubmissionID == acceptedSubmissionID {
			return m
		}
	}
	m.history = append(append([]historyEntry(nil), m.history...), historyEntry{text: frozen.Text, acceptedSubmissionID: acceptedSubmissionID})
	if len(m.history) <= MaximumHistory {
		return m
	}
	removeIndex := 0
	if m.historyIndex == 0 {
		removeIndex = 1
	}
	trimmed := make([]historyEntry, 0, MaximumHistory)
	trimmed = append(trimmed, m.history[:removeIndex]...)
	trimmed = append(trimmed, m.history[removeIndex+1:]...)
	m.history = trimmed
	if m.historyIndex > removeIndex {
		m.historyIndex--
	}
	return m
}
