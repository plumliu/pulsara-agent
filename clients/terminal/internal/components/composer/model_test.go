package composer

import (
	"strings"
	"testing"

	"github.com/rivo/uniseg"
)

func enabledComposer(t *testing.T) Model {
	t.Helper()
	model := NewDisabled().Configure(true, SubmitAvailable)
	if err := model.Validate(); err != nil {
		t.Fatal(err)
	}
	return model
}

func handoff(t *testing.T, model Model, frozen FrozenSubmission) Model {
	t.Helper()
	next, err := model.HandoffSubmission(frozen)
	if err != nil {
		t.Fatal(err)
	}
	return next
}

func TestComposerEditsByGraphemeAndSupportsMultilineUndo(t *testing.T) {
	model := enabledComposer(t)
	var err error
	model, err = model.Insert("你🙂a\n第二行")
	if err != nil {
		t.Fatal(err)
	}
	model = model.Backspace()
	if model.Draft() != "你🙂a\n第二" {
		t.Fatalf("grapheme backspace drifted: %q", model.Draft())
	}
	model = model.Undo()
	if model.Draft() != "你🙂a\n第二行" {
		t.Fatalf("undo did not restore exact revision: %q", model.Draft())
	}
	model = model.Redo()
	if model.Draft() != "你🙂a\n第二" {
		t.Fatalf("redo did not restore exact revision: %q", model.Draft())
	}
}

func TestComposerPasteReviewAndExactAcceptedDraft(t *testing.T) {
	model := enabledComposer(t)
	large := strings.Repeat("甲", MaximumDraftBytes/3+1)
	var err error
	model, err = model.Paste(large)
	if err != nil {
		t.Fatal(err)
	}
	if model.Mode() != PasteReview || model.PasteByteCount() <= MaximumDraftBytes {
		t.Fatal("large paste did not enter bounded review")
	}
	frozen, err := model.FreezeSubmission()
	if err != nil {
		t.Fatal(err)
	}
	submitted := handoff(t, model, frozen)
	newer := submitted
	newer, err = newer.Insert("keep this newer draft")
	if err != nil {
		t.Fatal(err)
	}
	newer = newer.ApplyAccepted(frozen, "submission:late")
	if newer.Draft() != "keep this newer draft" {
		t.Fatal("late accepted command cleared a newer composer revision")
	}
	model = submitted.ApplyAccepted(frozen, "submission:late")
	if !model.Empty() || model.Mode() != Ordinary {
		t.Fatal("matching accepted command rewrote the fresh empty draft")
	}
}

func TestComposerCaretMovementDoesNotChangeSubmissionContentIdentity(t *testing.T) {
	model := enabledComposer(t)
	var err error
	model, err = model.Insert("same content")
	if err != nil {
		t.Fatal(err)
	}
	frozen, err := model.FreezeSubmission()
	if err != nil {
		t.Fatal(err)
	}
	contentFingerprint := model.DraftContentFingerprint()
	viewFingerprint := model.DraftViewFingerprint()
	model = model.MoveLeft()
	if model.DraftContentFingerprint() != contentFingerprint {
		t.Fatal("caret movement changed the composer content identity")
	}
	if model.DraftViewFingerprint() == viewFingerprint {
		t.Fatal("caret movement did not advance the composer view identity")
	}
	model = handoff(t, model, frozen)
	model = model.ApplyAccepted(frozen, "submission:caret")
	if !model.Empty() {
		t.Fatal("accepted content authority rewrote the fresh empty draft")
	}
}

func TestComposerAcceptedLargePasteIsNotInstalledInOrdinaryHistory(t *testing.T) {
	model := enabledComposer(t)
	large := strings.Repeat("x", MaximumDraftBytes+1)
	var err error
	model, err = model.Paste(large)
	if err != nil {
		t.Fatal(err)
	}
	frozen, err := model.FreezeSubmission()
	if err != nil {
		t.Fatal(err)
	}
	model = handoff(t, model, frozen)
	model = model.ApplyAccepted(frozen, "submission:large")
	if !model.Empty() || len(model.history) != 0 {
		t.Fatal("large paste was retained in non-restorable ordinary history")
	}
	model = model.PreviousHistory()
	if !model.Empty() || model.historyIndex != -1 {
		t.Fatal("large paste history traversal advanced without a restorable entry")
	}
}

func TestComposerUndoAndHistoryAreBounded(t *testing.T) {
	model := enabledComposer(t)
	for range MaximumUndoDepth + 8 {
		var err error
		model, err = model.Insert("x")
		if err != nil {
			t.Fatal(err)
		}
	}
	if len(model.undo) != MaximumUndoDepth {
		t.Fatalf("undo bound drifted: %d", len(model.undo))
	}
	if err := model.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestComposerHistoryTraversesWithoutResettingItsCursor(t *testing.T) {
	model := enabledComposer(t)
	for _, value := range []string{"first", "second"} {
		var err error
		model, err = model.Insert(value)
		if err != nil {
			t.Fatal(err)
		}
		frozen, err := model.FreezeSubmission()
		if err != nil {
			t.Fatal(err)
		}
		model = handoff(t, model, frozen)
		model = model.ApplyAccepted(frozen, "submission:history:"+value)
	}
	model = model.PreviousHistory()
	if model.Draft() != "second" || model.historyIndex != 1 {
		t.Fatal("first history traversal did not retain its stable index")
	}
	model = model.PreviousHistory()
	if model.Draft() != "first" || model.historyIndex != 0 {
		t.Fatal("second history traversal did not reach the older command")
	}
	model = model.NextHistory()
	if model.Draft() != "second" || model.historyIndex != 1 {
		t.Fatal("forward history traversal did not return to the newer command")
	}
}

func TestComposerHistoryRestoresExactUnsubmittedScratch(t *testing.T) {
	model := enabledComposer(t)
	for _, value := range []string{"first", "second"} {
		var err error
		model, err = model.Insert(value)
		if err != nil {
			t.Fatal(err)
		}
		frozen, err := model.FreezeSubmission()
		if err != nil {
			t.Fatal(err)
		}
		model = handoff(t, model, frozen)
		model = model.ApplyAccepted(frozen, "submission:scratch:"+value)
	}
	var err error
	model, err = model.Insert("unsent draft")
	if err != nil {
		t.Fatal(err)
	}
	model = model.MoveLeft()
	scratch := model.revision
	model = model.PreviousHistory()
	if model.Draft() != "second" || !model.hasHistoryScratch {
		t.Fatal("history traversal did not freeze the current draft")
	}
	model = model.NextHistory()
	if model.revision != scratch || model.historyIndex != -1 || model.hasHistoryScratch {
		t.Fatal("history traversal did not restore the exact unsubmitted revision")
	}
}

func TestComposerBodyMutationAndUndoExitHistoryTraversal(t *testing.T) {
	model := enabledComposer(t)
	model, _ = model.Insert("history")
	frozen, _ := model.FreezeSubmission()
	model = handoff(t, model, frozen)
	model = model.ApplyAccepted(frozen, "submission:history:mutation")
	model, _ = model.Insert("scratch")
	model = model.PreviousHistory()
	model, _ = model.Insert(" edited")
	if model.historyIndex != -1 || model.hasHistoryScratch {
		t.Fatal("body mutation retained a stale history traversal cursor")
	}
	model = model.PreviousHistory()
	model = model.Undo()
	if model.historyIndex != -1 || model.hasHistoryScratch || model.Validate() != nil {
		t.Fatal("undo retained an inconsistent history traversal cursor")
	}
	model = model.Redo()
	if model.historyIndex != -1 || model.hasHistoryScratch || model.Validate() != nil {
		t.Fatal("redo retained an inconsistent history traversal cursor")
	}
}

func TestComposerDelayedAcceptedSubmissionIsRecordedWithoutClearingNewDraft(t *testing.T) {
	model := enabledComposer(t)
	model, _ = model.Insert("submitted")
	frozen, _ := model.FreezeSubmission()
	model = handoff(t, model, frozen)
	model, _ = model.Insert("newer draft")
	current := model.revision
	model = model.ApplyAccepted(frozen, "submission:delayed")
	model = model.ApplyAccepted(frozen, "submission:delayed")
	if model.revision != current || len(model.history) != 1 {
		t.Fatal("delayed accepted receipt cleared the new draft or duplicated history")
	}
	model = model.PreviousHistory()
	if model.Draft() != "submitted" {
		t.Fatal("delayed accepted command was not installed in history")
	}
	model = model.NextHistory()
	if model.revision != current {
		t.Fatal("returning from delayed accepted history lost the newer draft")
	}
}

func TestComposerAcceptedReceiptPreservesFreshScratchWhileBrowsingHistory(t *testing.T) {
	model := enabledComposer(t)
	model, _ = model.Insert("older command")
	older, _ := model.FreezeSubmission()
	model = handoff(t, model, older)
	model = model.ApplyAccepted(older, "submission:older")
	model, _ = model.Insert("submitted draft")
	submitted, _ := model.FreezeSubmission()
	model = handoff(t, model, submitted)
	model, _ = model.Insert("new scratch")
	model = model.PreviousHistory()
	if model.Draft() != "older command" || !model.hasHistoryScratch {
		t.Fatal("history traversal did not retain the fresh draft authority")
	}
	model = model.ApplyAccepted(submitted, "submission:accepted-while-browsing")
	if model.Draft() != "older command" || model.historyIndex != 0 || !model.hasHistoryScratch {
		t.Fatal("accepted receipt rewrote the active history traversal")
	}
	model = model.NextHistory()
	if model.Draft() != "submitted draft" {
		t.Fatal("accepted receipt was not inserted into prompt history")
	}
	model = model.NextHistory()
	if model.Draft() != "new scratch" || model.historyIndex != -1 || model.hasHistoryScratch {
		t.Fatal("accepted receipt lost the fresh unsubmitted scratch")
	}
}

func TestComposerMultilineHistoryUsesSymmetricTraversal(t *testing.T) {
	model := enabledComposer(t)
	for index, value := range []string{"older", "multi\nline"} {
		model, _ = model.Insert(value)
		frozen, _ := model.FreezeSubmission()
		model = handoff(t, model, frozen)
		model = model.ApplyAccepted(frozen, "submission:multiline:"+string(rune('0'+index)))
	}
	model = model.MoveUp()
	if model.Draft() != "multi\nline" {
		t.Fatal("history did not load the newest multiline entry")
	}
	model = model.MoveUp()
	if model.Draft() != "older" {
		t.Fatal("multiline history entry trapped upward traversal")
	}
	model = model.MoveDown()
	if model.Draft() != "multi\nline" {
		t.Fatal("multiline history traversal was not symmetric")
	}
}

func TestComposerLargePasteReviewKeepsHeaderAndBalancedPreview(t *testing.T) {
	model := enabledComposer(t)
	large := "HEAD\n" + strings.Repeat("middle-content ", 3000) + "\nTAIL"
	var err error
	model, err = model.Paste(large)
	if err != nil {
		t.Fatal(err)
	}
	rendered := model.Render(80, 6)
	if len(rendered.Rows) != 6 || !strings.Contains(rendered.Rows[0], "Paste review") ||
		!strings.Contains(strings.Join(rendered.Rows, "\n"), "HEAD") ||
		!strings.Contains(strings.Join(rendered.Rows, "\n"), "TAIL") ||
		!strings.Contains(strings.Join(rendered.Rows, "\n"), "…") {
		t.Fatalf("bounded paste review lost header/head/tail: %#v", rendered.Rows)
	}
}

func TestComposerRenderKeepsCursorInsideTinyAndClippedViewports(t *testing.T) {
	model := enabledComposer(t)
	var err error
	model, err = model.Insert("你🙂\nline two\nline three")
	if err != nil {
		t.Fatal(err)
	}
	for range 12 {
		model = model.MoveLeft()
	}
	rendered := model.Render(1, 2)
	if !rendered.HasCursor || rendered.CursorX < 0 || rendered.CursorX >= 1 || rendered.CursorY < 0 || rendered.CursorY >= 2 {
		t.Fatalf("composer cursor escaped its exact viewport: %#v", rendered)
	}
	for _, row := range rendered.Rows {
		if uniseg.StringWidth(row) > 1 {
			t.Fatalf("composer row escaped its exact width: %q", row)
		}
	}
}

func TestComposerRendersTypedSubmitBlockedStatusWithoutDisablingEditing(t *testing.T) {
	tests := []struct {
		availability SubmitAvailability
		want         string
	}{
		{SubmitBlockedControlStale, "Syncing · draft saved"},
		{SubmitBlockedReadOnly, "Read-only · draft saved"},
		{SubmitBlockedCapacity, "History capacity reached · draft saved"},
		{SubmitBlockedInteraction, "Waiting for interaction · draft saved"},
	}
	for _, test := range tests {
		model := NewDisabled().Configure(true, test.availability)
		rendered := model.Render(80, 1)
		if len(rendered.Rows) != 1 || !strings.Contains(rendered.Rows[0], test.want) || !rendered.HasCursor {
			t.Fatalf("blocked composer did not expose its typed state: %#v", rendered)
		}
		updated, err := model.Insert("draft")
		if err != nil || updated.Draft() != "draft" {
			t.Fatalf("blocked composer stopped preserving editable drafts: draft=%q err=%v", updated.Draft(), err)
		}
	}
}
