package composer

import (
	"errors"
	"strings"
	"unicode/utf8"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

const (
	MaximumDraftBytes = 32 * 1024
	MaximumPasteBytes = 1024 * 1024
	MaximumUndoDepth  = 64
	MaximumHistory    = 64
	MaximumVisualRows = 6
)

type Mode uint8

const (
	Disabled Mode = iota + 1
	Ordinary
	PasteReview
)

type SubmitAvailability uint8

const (
	SubmitDisabled SubmitAvailability = iota + 1
	SubmitAvailable
	SubmitBlockedControlStale
	SubmitBlockedReadOnly
	SubmitBlockedCapacity
	SubmitBlockedInteraction
)

type Revision struct {
	Revision           uint64
	DraftUTF8          string
	GraphemeCursor     uint32
	PreferredColumn    int
	ContentFingerprint string
	ViewFingerprint    string
}

type PasteReviewState struct {
	payload     string
	preview     string
	cursor      uint32
	byteCount   uint64
	lineCount   uint64
	fingerprint string
}

type FrozenSubmission struct {
	Text                    string
	DraftRevision           uint64
	DraftContentFingerprint string
	FromPasteReview         bool
	Fingerprint             string
}

type historyEntry struct {
	text                 string
	acceptedSubmissionID string
}

type Model struct {
	mode              Mode
	revision          Revision
	availability      SubmitAvailability
	undo              []Revision
	redo              []Revision
	history           []historyEntry
	historyIndex      int
	historyScratch    Revision
	hasHistoryScratch bool
	paste             PasteReviewState
}

func NewDisabled() Model {
	revision, err := newRevision(1, "", 0, 0)
	if err != nil {
		panic(err)
	}
	return Model{mode: Disabled, revision: revision, availability: SubmitDisabled, historyIndex: -1}
}

func (m Model) Validate() error {
	if m.mode < Disabled || m.mode > PasteReview || m.availability < SubmitDisabled || m.availability > SubmitBlockedInteraction {
		return errors.New("terminal composer vocabulary is invalid")
	}
	if err := m.revision.Validate(); err != nil {
		return err
	}
	if len(m.undo) > MaximumUndoDepth || len(m.redo) > MaximumUndoDepth || len(m.history) > MaximumHistory {
		return errors.New("terminal composer bounded history is invalid")
	}
	for _, entry := range m.history {
		if entry.text == "" || !utf8.ValidString(entry.text) || len([]byte(entry.text)) > MaximumDraftBytes || !draftTextSafe(entry.text) || entry.acceptedSubmissionID == "" {
			return errors.New("terminal composer history contains a non-restorable entry")
		}
	}
	for _, revision := range append(append([]Revision(nil), m.undo...), m.redo...) {
		if err := revision.Validate(); err != nil {
			return err
		}
	}
	if m.historyIndex < -1 || m.historyIndex >= len(m.history) {
		return errors.New("terminal composer history cursor is invalid")
	}
	if m.historyIndex >= 0 {
		if !m.hasHistoryScratch || m.historyScratch.Validate() != nil || m.revision.Revision != m.historyScratch.Revision || m.revision.DraftUTF8 != m.history[m.historyIndex].text {
			return errors.New("terminal composer active history traversal is not exact")
		}
	} else if m.hasHistoryScratch || m.historyScratch != (Revision{}) {
		return errors.New("terminal composer retains history scratch outside traversal")
	}
	if m.mode == PasteReview || (m.mode == Disabled && m.paste != (PasteReviewState{})) {
		if err := m.paste.Validate(); err != nil {
			return err
		}
		if uint64(m.paste.cursor) > uint64(len(graphemeClusters(m.revision.DraftUTF8))) {
			return errors.New("terminal paste review cursor is stale")
		}
	} else if m.paste != (PasteReviewState{}) {
		return errors.New("terminal composer retains paste authority outside review")
	}
	if m.mode == Disabled && m.availability != SubmitDisabled {
		return errors.New("disabled terminal composer advertises submit")
	}
	return nil
}

func (r Revision) Validate() error {
	if r.Revision == 0 || !utf8.ValidString(r.DraftUTF8) || len([]byte(r.DraftUTF8)) > MaximumDraftBytes || !draftTextSafe(r.DraftUTF8) || r.ContentFingerprint == "" || r.ViewFingerprint == "" {
		return errors.New("terminal composer revision is invalid")
	}
	clusters := graphemeClusters(r.DraftUTF8)
	if uint64(r.GraphemeCursor) > uint64(len(clusters)) || r.PreferredColumn < 0 {
		return errors.New("terminal composer cursor is invalid")
	}
	expectedContent, err := revisionContentFingerprint(r.Revision, r.DraftUTF8)
	if err != nil || expectedContent != r.ContentFingerprint {
		return errors.New("terminal composer content fingerprint mismatch")
	}
	expectedView, err := revisionViewFingerprint(r.ContentFingerprint, r.GraphemeCursor, r.PreferredColumn)
	if err != nil || expectedView != r.ViewFingerprint {
		return errors.New("terminal composer view fingerprint mismatch")
	}
	return nil
}

func (p PasteReviewState) Validate() error {
	if p.payload == "" || !utf8.ValidString(p.payload) || !draftTextSafe(p.payload) || len([]byte(p.payload)) > MaximumPasteBytes || p.byteCount != uint64(len([]byte(p.payload))) || p.lineCount == 0 || p.fingerprint == "" {
		return errors.New("terminal paste review is invalid")
	}
	expected, err := protocolvalue.CanonicalClientFingerprint("terminal-composer-paste-review:v1", map[string]any{
		"byte_count":      p.byteCount,
		"grapheme_cursor": p.cursor,
		"line_count":      p.lineCount,
		"payload":         p.payload,
		"preview":         p.preview,
	})
	if err != nil || expected != p.fingerprint {
		return errors.New("terminal paste review fingerprint mismatch")
	}
	return nil
}

func (m Model) Enabled() bool                    { return m.mode != Disabled }
func (m Model) Mode() Mode                       { return m.mode }
func (m Model) Draft() string                    { return m.revision.DraftUTF8 }
func (m Model) DraftRevision() uint64            { return m.revision.Revision }
func (m Model) DraftContentFingerprint() string  { return m.revision.ContentFingerprint }
func (m Model) DraftViewFingerprint() string     { return m.revision.ViewFingerprint }
func (m Model) GraphemeCursor() uint32           { return m.revision.GraphemeCursor }
func (m Model) Availability() SubmitAvailability { return m.availability }
func (m Model) AvailabilityStatus() string {
	switch m.availability {
	case SubmitAvailable:
		return ""
	case SubmitBlockedControlStale:
		return "Syncing · draft saved"
	case SubmitBlockedReadOnly:
		return "Read-only · draft saved"
	case SubmitBlockedCapacity:
		return "History capacity reached · draft saved"
	case SubmitBlockedInteraction:
		return "Waiting for interaction · draft saved"
	default:
		return "Input unavailable · draft saved"
	}
}
func (m Model) Empty() bool {
	return m.revision.DraftUTF8 == "" && m.paste == (PasteReviewState{})
}
func (m Model) PastePreview() string           { return m.paste.preview }
func (m Model) PasteByteCount() uint64         { return m.paste.byteCount }
func (m Model) PasteLineCount() uint64         { return m.paste.lineCount }
func (m Model) PasteReviewFingerprint() string { return m.paste.fingerprint }

func (m Model) Configure(enabled bool, availability SubmitAvailability) Model {
	if !enabled {
		m.mode = Disabled
		m.availability = SubmitDisabled
		return m
	}
	if m.mode == Disabled {
		m.mode = Ordinary
		if m.paste != (PasteReviewState{}) {
			m.mode = PasteReview
		}
	}
	if availability < SubmitDisabled || availability > SubmitBlockedInteraction {
		availability = SubmitDisabled
	}
	m.availability = availability
	return m
}

func (m Model) CurrentTextForSubmission() string {
	if m.mode == PasteReview {
		clusters := graphemeClusters(m.revision.DraftUTF8)
		cursor := int(m.paste.cursor)
		if cursor >= 0 && cursor <= len(clusters) {
			return strings.Join(clusters[:cursor], "") + m.paste.payload + strings.Join(clusters[cursor:], "")
		}
	}
	return m.revision.DraftUTF8
}

func (m Model) FreezeSubmission() (FrozenSubmission, error) {
	if m.mode == Disabled || m.availability != SubmitAvailable {
		return FrozenSubmission{}, errors.New("terminal composer submit is unavailable")
	}
	text := m.CurrentTextForSubmission()
	if text == "" || !utf8.ValidString(text) || !draftTextSafe(text) || len([]byte(text)) > MaximumPasteBytes+MaximumDraftBytes {
		return FrozenSubmission{}, errors.New("terminal composer submission is invalid")
	}
	fingerprint, err := protocolvalue.CanonicalClientFingerprint("terminal-composer-frozen-submission:v1", map[string]any{
		"draft_content_fingerprint": m.revision.ContentFingerprint,
		"draft_revision":            m.revision.Revision,
		"from_paste_review":         m.mode == PasteReview,
		"text":                      text,
	})
	if err != nil {
		return FrozenSubmission{}, err
	}
	return FrozenSubmission{Text: text, DraftRevision: m.revision.Revision, DraftContentFingerprint: m.revision.ContentFingerprint, FromPasteReview: m.mode == PasteReview, Fingerprint: fingerprint}, nil
}

// HandoffSubmission transfers the exact frozen content to the command owner
// and installs a fresh, empty draft immediately.  The submitted revision is
// deliberately not added to undo: command history is populated only after a
// durable accepted outcome, and undo must never resurrect an in-flight prompt.
func (m Model) HandoffSubmission(frozen FrozenSubmission) (Model, error) {
	if frozen.Fingerprint == "" ||
		!frozenMatchesRevision(frozen, m.revision, m.CurrentTextForSubmission()) ||
		m.revision.Revision == ^uint64(0) {
		return m, errors.New("terminal composer submission handoff is stale")
	}
	updated, err := newRevision(m.revision.Revision+1, "", 0, 0)
	if err != nil {
		return m, err
	}
	m = m.clearHistoryTraversal()
	m.revision = updated
	m.mode = Ordinary
	m.paste = PasteReviewState{}
	m.undo = nil
	m.redo = nil
	return m, m.Validate()
}

func (m Model) ApplyAccepted(frozen FrozenSubmission, acceptedSubmissionID string) Model {
	// The editable composer already moved to a fresh revision at Enter.  A late
	// terminal receipt may only record the accepted command in prompt history;
	// it must never clear or rewrite the user's newer draft.
	return m.recordAcceptedHistory(frozen, acceptedSubmissionID)
}

func frozenMatchesRevision(frozen FrozenSubmission, revision Revision, submissionText string) bool {
	return frozen.DraftRevision == revision.Revision &&
		frozen.DraftContentFingerprint == revision.ContentFingerprint &&
		frozen.Text == submissionText
}

func (m Model) ApplyRejected(FrozenSubmission) Model {
	return m
}

func newRevision(number uint64, text string, cursor uint32, preferred int) (Revision, error) {
	contentFingerprint, err := revisionContentFingerprint(number, text)
	if err != nil {
		return Revision{}, err
	}
	viewFingerprint, err := revisionViewFingerprint(contentFingerprint, cursor, preferred)
	if err != nil {
		return Revision{}, err
	}
	value := Revision{Revision: number, DraftUTF8: text, GraphemeCursor: cursor, PreferredColumn: preferred, ContentFingerprint: contentFingerprint, ViewFingerprint: viewFingerprint}
	return value, value.Validate()
}

func revisionContentFingerprint(number uint64, text string) (string, error) {
	return protocolvalue.CanonicalClientFingerprint("terminal-composer-content-revision:v1", map[string]any{
		"draft_utf8": text,
		"revision":   number,
	})
}

func revisionViewFingerprint(contentFingerprint string, cursor uint32, preferred int) (string, error) {
	return protocolvalue.CanonicalClientFingerprint("terminal-composer-view-revision:v1", map[string]any{
		"content_fingerprint": contentFingerprint,
		"grapheme_cursor":     cursor,
		"preferred_column":    preferred,
	})
}
