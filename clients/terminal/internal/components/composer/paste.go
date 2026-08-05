package composer

import (
	"errors"
	"strings"
	"unicode/utf8"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

const maximumPastePreviewBytes = 4096

const pastePreviewOmission = "\n… omitted …\n"

func (m Model) Paste(value string) (Model, error) {
	if m.mode == Disabled || !utf8.ValidString(value) {
		return m, errors.New("terminal paste is unavailable")
	}
	value = normalizeNewlines(value)
	if value == "" {
		return m, nil
	}
	if !draftTextSafe(value) || len([]byte(value)) > MaximumPasteBytes {
		return m, errors.New("terminal paste exceeds its safe bound")
	}
	if len([]byte(m.revision.DraftUTF8))+len([]byte(value)) <= MaximumDraftBytes {
		return m.Insert(value)
	}
	m = m.clearHistoryTraversal()
	preview := boundedPastePreview(value)
	lineCount := uint64(strings.Count(value, "\n") + 1)
	fingerprint, err := protocolvalue.CanonicalClientFingerprint("terminal-composer-paste-review:v1", map[string]any{
		"byte_count":      uint64(len([]byte(value))),
		"grapheme_cursor": m.revision.GraphemeCursor,
		"line_count":      lineCount,
		"payload":         value,
		"preview":         preview,
	})
	if err != nil {
		return m, err
	}
	m.mode = PasteReview
	m.paste = PasteReviewState{payload: value, preview: preview, cursor: m.revision.GraphemeCursor, byteCount: uint64(len([]byte(value))), lineCount: lineCount, fingerprint: fingerprint}
	return m, m.Validate()
}

func boundedPastePreview(value string) string {
	if len([]byte(value)) <= maximumPastePreviewBytes {
		return value
	}
	omissionBytes := len([]byte(pastePreviewOmission))
	headBudget := (maximumPastePreviewBytes - omissionBytes) / 2
	tailBudget := maximumPastePreviewBytes - omissionBytes - headBudget
	head := truncateUTF8Bytes(value, headBudget)
	tailStart := len(value) - tailBudget
	if tailStart < 0 {
		tailStart = 0
	}
	for tailStart < len(value) && !utf8.RuneStart(value[tailStart]) {
		tailStart++
	}
	return head + pastePreviewOmission + value[tailStart:]
}

func (m Model) CancelPasteReview() Model {
	if m.mode == PasteReview {
		m.mode = Ordinary
		m.paste = PasteReviewState{}
	}
	return m
}

func normalizeNewlines(value string) string {
	value = strings.ReplaceAll(value, "\r\n", "\n")
	return strings.ReplaceAll(value, "\r", "\n")
}

func truncateUTF8Bytes(value string, maximum int) string {
	if len(value) <= maximum {
		return value
	}
	value = value[:maximum]
	for !utf8.ValidString(value) {
		value = value[:len(value)-1]
	}
	return value
}
