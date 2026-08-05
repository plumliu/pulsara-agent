package app

import (
	"strings"
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/commandstate"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/components/composer"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/components/notification"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/presentation"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func testReadyS3State(t *testing.T, activeRunID string) AppState {
	t.Helper()
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:s3", Kind: "assistant", PublicText: "ready for input", Fingerprint: testFingerprint("entry:s3"),
	}})
	state.attachment.Identity.Role = protocol.AttachmentRole_ATTACHMENT_ROLE_CONTROLLER
	state.attachment.Identity.ControllerDisposition = protocol.ControllerDisposition_CONTROLLER_GRANTED
	state.attachment.Identity.ControllerGeneration = 1
	state.connection.HelloWinner.Limits.MaximumFrameBytes = protocolvalue.MaximumFrameBytes
	control := state.control.Projection()
	control.SessionLifecycle = protocolvalue.SessionLifecycleOpen
	control.ActiveRunID = activeRunID
	control.Revision++
	control.ProjectionFingerprint = testFingerprint("s3-control-view")
	control.ViewFingerprint = control.ProjectionFingerprint
	control.CursorFingerprint = testFingerprint("s3-control-cursor")
	control.SnapshotFingerprint = testFingerprint("s3-control-snapshot")
	state.control = presentation.NewControlProjection()
	var err error
	state.control, err = state.control.Install(control, state.attachment.Identity.RuntimeSessionID)
	if err != nil {
		t.Fatal(err)
	}
	state.snapshotLoading.DurableControlCursorFingerprint = control.CursorFingerprint
	state.commands, err = state.commands.Activate()
	if err != nil {
		t.Fatal(err)
	}
	state, err = refreshInteractiveState(state)
	if err != nil {
		t.Fatal(err)
	}
	if err := state.Validate(); err != nil {
		t.Fatal(err)
	}
	return state
}

func testCommandOutcome(t *testing.T, token OperationToken, candidate commandstate.Candidate, status commandstate.OutcomeStatus) commandstate.Outcome {
	t.Helper()
	statusText := map[commandstate.OutcomeStatus]string{
		commandstate.OutcomeSucceeded:              "succeeded",
		commandstate.OutcomeRejected:               "rejected",
		commandstate.OutcomePendingConfirmation:    "pending_confirmation",
		commandstate.OutcomeReconciliationRequired: "reconciliation_required",
		commandstate.OutcomeCompatibleWinner:       "superseded_by_compatible_winner",
	}[status]
	fingerprint, err := protocolvalue.CanonicalClientFingerprint("terminal-command-outcome:v1", map[string]any{
		"command_id": candidate.ID(), "durable_reference_ids": []string{},
		"public_result_code": "TEST_RESULT", "public_result_text": "Command accepted.",
		"query_token": "query:test", "status": statusText,
		"target_generation": candidate.Binding().ExpectedTargetGeneration,
		"target_id":         candidate.Binding().ExpectedTargetID,
	})
	if err != nil {
		t.Fatal(err)
	}
	return commandstate.Outcome{
		RequestID: token.RequestID, Status: status, CommandID: candidate.ID(),
		TargetID: candidate.Binding().ExpectedTargetID, TargetGeneration: candidate.Binding().ExpectedTargetGeneration,
		PublicResultCode: "TEST_RESULT", PublicResultText: "Command accepted.",
		DurableReferenceIDs: []string{}, QueryToken: "query:test", Fingerprint: fingerprint,
	}
}

func submitDraft(t *testing.T, state AppState, draft string) (AppState, SendMutationEffect) {
	t.Helper()
	text, err := NewNormalizedKey(KeyText, 0, draft, false)
	if err != nil {
		t.Fatal(err)
	}
	state, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: text})
	if len(effects) != 0 {
		t.Fatalf("draft editing unexpectedly produced I/O: %#v", effects)
	}
	enter, err := NewNormalizedKey(KeyEnter, 0, "", false)
	if err != nil {
		t.Fatal(err)
	}
	state, effects, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 2), Key: enter})
	if len(effects) != 1 {
		t.Fatalf("submit did not produce one mutation: %#v", effects)
	}
	mutation, ok := effects[0].(SendMutationEffect)
	if !ok {
		t.Fatalf("submit produced %T, want SendMutationEffect", effects[0])
	}
	return state, mutation
}

func TestS3SubmitFreezesCandidateBeforeIOAndHandsOffToFreshDraft(t *testing.T) {
	state := testReadyS3State(t, "")
	now := time.Now().UTC()
	text, _ := NewNormalizedKey(KeyText, 0, "你好", false)
	state, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: text})
	if len(effects) != 0 || state.composer.Draft() != "你好" {
		t.Fatal("composer did not retain the typed draft before submission")
	}
	enter, _ := NewNormalizedKey(KeyEnter, 0, "", false)
	state, effects, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 2), Key: enter})
	if len(effects) != 1 || state.commands.Count() != 1 {
		t.Fatalf("submit did not install exactly one stable candidate: %#v", effects)
	}
	mutation, ok := effects[0].(SendMutationEffect)
	if !ok || !state.commands.OwnsExact(mutation.Candidate) || mutation.Candidate.Text() != "你好" {
		t.Fatalf("submit effect did not carry the installed candidate: %#v", effects[0])
	}
	if !state.composer.Empty() {
		t.Fatalf("submitted content remained in the editable composer: %q", state.composer.Draft())
	}
	outcome := testCommandOutcome(t, mutation.Header.Operation, mutation.Candidate, commandstate.OutcomeSucceeded)
	header := ioHeader(mutation.Header.Operation, outcome.Fingerprint)
	header.ReceivedAt = now
	state, followup, _ := state.update(CommandOutcomeMsg{Header: header, Candidate: mutation.Candidate, Outcome: outcome})
	if !state.composer.Empty() {
		t.Fatalf("accepted receipt rewrote the fresh composer: %q", state.composer.Draft())
	}
	if record, ok := state.commands.Record(mutation.Candidate.ID()); !ok || record.Phase() != commandstate.Accepted {
		t.Fatal("successful command did not install its terminal receipt state")
	}
	if !containsEffect[ObserveNextEffect](followup) || !containsEffect[ScheduleTickEffect](followup) {
		t.Fatalf("successful command did not resume observation and schedule notification expiry: %#v", followup)
	}
}

func TestS3LateReceiptDoesNotClearNewerDraft(t *testing.T) {
	state := testReadyS3State(t, "")
	first, _ := NewNormalizedKey(KeyText, 0, "first", false)
	state, _, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: first})
	enter, _ := NewNormalizedKey(KeyEnter, 0, "", false)
	state, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 2), Key: enter})
	mutation := effects[0].(SendMutationEffect)
	newer, _ := NewNormalizedKey(KeyText, 0, "+newer", false)
	state, _, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 3), Key: newer})
	outcome := testCommandOutcome(t, mutation.Header.Operation, mutation.Candidate, commandstate.OutcomeSucceeded)
	state, _, _ = state.update(CommandOutcomeMsg{Header: ioHeader(mutation.Header.Operation, outcome.Fingerprint), Candidate: mutation.Candidate, Outcome: outcome})
	if state.composer.Draft() != "+newer" {
		t.Fatalf("late receipt cleared or rewrote a newer draft: %q", state.composer.Draft())
	}
	history := state.composer.PreviousHistory()
	if history.Draft() != "first" {
		t.Fatalf("late accepted receipt was not recorded in command history: %q", history.Draft())
	}
	history = history.NextHistory()
	if history.Draft() != "+newer" {
		t.Fatalf("returning from late receipt history lost the newer draft: %q", history.Draft())
	}
}

func TestS3AcceptedReceiptDoesNotRewriteFreshHistoryScratch(t *testing.T) {
	state := testReadyS3State(t, "")
	var err error
	state.composer, err = state.composer.Insert("older command")
	if err != nil {
		t.Fatal(err)
	}
	older, err := state.composer.FreezeSubmission()
	if err != nil {
		t.Fatal(err)
	}
	state.composer, err = state.composer.HandoffSubmission(older)
	if err != nil {
		t.Fatal(err)
	}
	state.composer = state.composer.ApplyAccepted(older, "submission:older:app")
	state, mutation := submitDraft(t, state, "submitted draft")
	state.composer, err = state.composer.Insert("fresh scratch")
	if err != nil {
		t.Fatal(err)
	}
	state.composer = state.composer.PreviousHistory()
	if state.composer.Draft() != "older command" {
		t.Fatal("history traversal did not replace the visible submitted draft")
	}
	outcome := testCommandOutcome(t, mutation.Header.Operation, mutation.Candidate, commandstate.OutcomeSucceeded)
	state, _, _ = state.update(CommandOutcomeMsg{Header: ioHeader(mutation.Header.Operation, outcome.Fingerprint), Candidate: mutation.Candidate, Outcome: outcome})
	if state.composer.Draft() != "older command" {
		t.Fatalf("accepted receipt rewrote the visible history item: %q", state.composer.Draft())
	}
	state.composer = state.composer.MoveDown()
	if state.composer.Draft() != "submitted draft" {
		t.Fatalf("accepted submission was not added to prompt history: %q", state.composer.Draft())
	}
	state.composer = state.composer.MoveDown()
	if state.composer.Draft() != "fresh scratch" {
		t.Fatalf("down did not restore the fresh draft: %q", state.composer.Draft())
	}
}

func TestS3RepeatedEnterKeepsOneExactSubmissionAuthority(t *testing.T) {
	state, first := submitDraft(t, testReadyS3State(t, ""), "send exactly once")
	if !state.composer.Empty() {
		t.Fatal("submission did not install a fresh draft")
	}
	left, _ := NewNormalizedKey(KeyLeft, 0, "", false)
	state, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 3), Key: left})
	if len(effects) != 0 || !state.composer.Empty() {
		t.Fatal("caret movement mutated the fresh empty draft")
	}
	enter, _ := NewNormalizedKey(KeyEnter, 0, "", false)
	state, effects, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 4), Key: enter})
	if containsEffect[SendMutationEffect](effects) || state.commands.Count() != 1 || !state.commands.OwnsExact(first.Candidate) {
		t.Fatalf("caret-moved Enter created a second submission authority: effects=%#v count=%d", effects, state.commands.Count())
	}
	outcome := testCommandOutcome(t, first.Header.Operation, first.Candidate, commandstate.OutcomeSucceeded)
	state, _, _ = state.update(CommandOutcomeMsg{Header: ioHeader(first.Header.Operation, outcome.Fingerprint), Candidate: first.Candidate, Outcome: outcome})
	if !state.composer.Empty() {
		t.Fatal("accepted content authority rewrote the fresh empty draft")
	}
}

func TestS3HiddenComposerPreservesDraftButRejectsBlindMutation(t *testing.T) {
	state := testReadyS3State(t, "")
	text, _ := NewNormalizedKey(KeyText, 0, "visible draft", false)
	state, _, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: text})
	state, effects, _ := state.update(ResizeMsg{Header: testLocalMessageHeader(t, 2), Width: 80, Height: 3})
	if len(effects) != 0 || state.composer.Enabled() || state.layout.ComposerRows != 0 || state.composer.Draft() != "visible draft" {
		t.Fatal("tiny layout did not retain a disabled exact draft")
	}
	hiddenText, _ := NewNormalizedKey(KeyText, 0, "hidden", false)
	state, effects, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 3), Key: hiddenText})
	if len(effects) != 0 || state.composer.Draft() != "visible draft" {
		t.Fatal("hidden composer accepted blind text input")
	}
	state, effects, _ = state.update(PasteInputMsg{Header: testLocalMessageHeader(t, 4), ChunkUTF8: "hidden paste", ByteCount: 12})
	if len(effects) != 0 || state.composer.Draft() != "visible draft" {
		t.Fatal("hidden composer accepted a blind paste")
	}
	enter, _ := NewNormalizedKey(KeyEnter, 0, "", false)
	state, effects, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 5), Key: enter})
	for _, effect := range effects {
		if _, mutation := effect.(SendMutationEffect); mutation {
			t.Fatal("hidden composer emitted a blind mutation")
		}
	}
	if state.commands.Count() != 0 || state.composer.Draft() != "visible draft" {
		t.Fatal("hidden composer admitted a blind submission")
	}
	state, effects, _ = state.update(ResizeMsg{Header: testLocalMessageHeader(t, 6), Width: 80, Height: 24})
	if len(effects) != 0 || !state.composer.Enabled() || state.layout.ComposerRows == 0 || state.composer.Draft() != "visible draft" {
		t.Fatal("visible layout did not restore the retained draft owner")
	}
}

func TestS3RepeatedStopUsesOneStableCandidate(t *testing.T) {
	state := testReadyS3State(t, "run:active")
	interrupt, _ := NewNormalizedKey(KeyInterrupt, KeyModCtrl, "", false)
	state, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: interrupt})
	if len(effects) != 1 {
		t.Fatal("first stop did not produce its mutation")
	}
	first := effects[0].(SendMutationEffect)
	state, repeated, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 2), Key: interrupt})
	if len(repeated) != 0 || state.commands.Count() != 1 || !state.commands.OwnsExact(first.Candidate) {
		t.Fatal("repeated stop created a second semantic command")
	}
	if state.phase != PhaseReady || state.teardown.Phase != TeardownIdle {
		t.Fatal("Ctrl-C stop incorrectly entered application teardown")
	}
}

func TestS3CommandOutcomeMatrixPreservesFreshDraft(t *testing.T) {
	tests := []struct {
		name      string
		status    commandstate.OutcomeStatus
		wantPhase commandstate.Phase
	}{
		{name: "succeeded", status: commandstate.OutcomeSucceeded, wantPhase: commandstate.Accepted},
		{name: "compatible winner", status: commandstate.OutcomeCompatibleWinner, wantPhase: commandstate.CompatibleWinner},
		{name: "rejected", status: commandstate.OutcomeRejected, wantPhase: commandstate.Rejected},
		{name: "pending", status: commandstate.OutcomePendingConfirmation, wantPhase: commandstate.PendingConfirmation},
		{name: "reconciliation", status: commandstate.OutcomeReconciliationRequired, wantPhase: commandstate.Reconciliation},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			state, mutation := submitDraft(t, testReadyS3State(t, ""), "retain-or-clear")
			newer, _ := NewNormalizedKey(KeyText, 0, "new draft", false)
			state, _, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 3), Key: newer})
			outcome := testCommandOutcome(t, mutation.Header.Operation, mutation.Candidate, test.status)
			state, _, _ = state.update(CommandOutcomeMsg{
				Header: ioHeader(mutation.Header.Operation, outcome.Fingerprint), Candidate: mutation.Candidate, Outcome: outcome,
			})
			record, ok := state.commands.Record(mutation.Candidate.ID())
			if !ok || record.Phase() != test.wantPhase {
				t.Fatalf("outcome installed phase %v, want %v", record.Phase(), test.wantPhase)
			}
			if state.composer.Draft() != "new draft" {
				t.Fatalf("terminal outcome rewrote the fresh draft: %q", state.composer.Draft())
			}
			if state.phase == PhaseFatal {
				t.Fatal("valid command outcome entered fatal state")
			}
		})
	}
}

func TestS3LostReceiptQueriesThenResendsSameCandidateWhenMissing(t *testing.T) {
	state, mutation := submitDraft(t, testReadyS3State(t, ""), "same candidate")
	now := time.Now().UTC()
	var err error
	state.connection.Outstanding = OutstandingOperation{}
	state.commands, err = state.commands.MarkQueryRequired(mutation.Candidate.ID(), now.Add(-time.Second))
	if err != nil {
		t.Fatal(err)
	}
	state, effects, err := nextLiveEffect(state, now)
	if err != nil || len(effects) != 1 {
		t.Fatalf("lost receipt did not produce one query: effects=%#v err=%v", effects, err)
	}
	query, ok := effects[0].(QueryCommandEffect)
	if !ok || query.Candidate.Fingerprint() != mutation.Candidate.Fingerprint() {
		t.Fatal("lost receipt query did not preserve the exact stable candidate")
	}
	state, followup, _ := state.update(CommandQueryMissingMsg{
		Header: ioHeader(query.Header.Operation, query.Candidate.Fingerprint()), Candidate: query.Candidate,
	})
	record, ok := state.commands.Record(query.Candidate.ID())
	if !ok || record.Phase() != commandstate.Frozen || !state.composer.Empty() {
		t.Fatal("same-attachment missing query did not retain and re-arm the exact command")
	}
	if len(followup) != 1 {
		t.Fatalf("missing query did not resume observation during bounded backoff: %#v", followup)
	}
	if _, ok := followup[0].(ObserveNextEffect); !ok {
		t.Fatalf("missing query resumed with %T, want ObserveNextEffect", followup[0])
	}
	candidate, queryMode, ready := state.commands.NextAction(now.Add(time.Second))
	if !ready || queryMode || candidate.Fingerprint() != mutation.Candidate.Fingerprint() {
		t.Fatal("bounded retry did not expose the original candidate for resend")
	}
}

func TestS3MissingQueryAfterPendingOutcomeRequiresReconciliation(t *testing.T) {
	state, mutation := submitDraft(t, testReadyS3State(t, ""), "pending draft")
	now := time.Now().UTC()
	pending := testCommandOutcome(t, mutation.Header.Operation, mutation.Candidate, commandstate.OutcomePendingConfirmation)
	state, _, _ = state.update(CommandOutcomeMsg{Header: ioHeader(mutation.Header.Operation, pending.Fingerprint), Candidate: mutation.Candidate, Outcome: pending})
	state.connection.Outstanding = OutstandingOperation{}
	state.commands, _ = state.commands.MarkQuerying(mutation.Candidate.ID())
	var token OperationToken
	state, token = state.nextWire(OpCommandQuery, now.Add(time.Second))
	state, _, _ = state.update(CommandQueryMissingMsg{Header: ioHeader(token, mutation.Candidate.Fingerprint()), Candidate: mutation.Candidate})
	record, ok := state.commands.Record(mutation.Candidate.ID())
	if !ok || record.Phase() != commandstate.Reconciliation || !state.composer.Empty() {
		t.Fatal("missing durable pending receipt did not fail closed into reconciliation")
	}
}

func TestS3ObserverSnapshotKeepsComposerDormant(t *testing.T) {
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:observer", Kind: "assistant", PublicText: "read only", Fingerprint: testFingerprint("entry:observer"),
	}})
	if state.composer.Enabled() || state.commands.Enabled() {
		t.Fatal("observer attachment activated command-plane owners")
	}
	key, _ := NewNormalizedKey(KeyText, 0, "must not edit", false)
	next, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: key})
	if next.composer.Draft() != "" || len(effects) != 0 {
		t.Fatal("observer input entered the composer or produced a mutation")
	}
}

func TestS3LargePasteReviewCannotBypassNegotiatedFrameCap(t *testing.T) {
	state := testReadyS3State(t, "")
	large := strings.Repeat("甲", composer.MaximumDraftBytes/3+1)
	state, _, _ = state.update(PasteInputMsg{
		Header: testLocalMessageHeader(t, 1), ChunkUTF8: large, ByteCount: uint32(len([]byte(large))),
	})
	if state.composer.Mode() != composer.PasteReview {
		t.Fatal("large paste did not enter review before any command was frozen")
	}
	state.connection.HelloWinner.Limits.MaximumFrameBytes = 512
	enter, _ := NewNormalizedKey(KeyEnter, 0, "", false)
	state, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 2), Key: enter})
	if containsEffect[SendMutationEffect](effects) || state.commands.Count() != 0 || state.composer.Mode() != composer.PasteReview {
		t.Fatal("oversized reviewed paste crossed the negotiated frame boundary")
	}
	escape, _ := NewNormalizedKey(KeyEscape, 0, "", false)
	state, effects, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 3), Key: escape})
	if state.composer.Mode() != composer.Ordinary || !state.composer.Empty() {
		t.Fatalf("paste review cancellation did not return to the exact ordinary draft: mode=%d empty=%v", state.composer.Mode(), state.composer.Empty())
	}
	for _, effect := range effects {
		if _, mutation := effect.(SendMutationEffect); mutation {
			t.Fatal("paste review cancellation emitted a mutation")
		}
	}
}

func TestS3PendingSubmissionIsPersistentStateAndComposerAcceptsFreshDraft(t *testing.T) {
	state, mutation := submitDraft(t, testReadyS3State(t, ""), "submitted body")
	if !state.composer.Empty() || !strings.Contains(render(state).Content, "Message sending… · new draft ready") {
		t.Fatal("submission was not transferred into the persistent pending-command view")
	}
	newer, _ := NewNormalizedKey(KeyText, 0, "next body", false)
	state, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 3), Key: newer})
	if state.composer.Draft() != "next body" || containsEffect[SendMutationEffect](effects) {
		t.Fatal("fresh composer draft was not independent from the pending command")
	}
	pending := testCommandOutcome(t, mutation.Header.Operation, mutation.Candidate, commandstate.OutcomePendingConfirmation)
	state, _, _ = state.update(CommandOutcomeMsg{Header: ioHeader(mutation.Header.Operation, pending.Fingerprint), Candidate: mutation.Candidate, Outcome: pending})
	if !strings.Contains(render(state).Content, "confirming durable outcome") || state.composer.Draft() != "next body" {
		t.Fatal("pending-confirmation state was transient or rewrote the fresh draft")
	}
}

func TestS3SubmitBlockerRemainsVisibleWhileCommandIsPending(t *testing.T) {
	state, _ := submitDraft(t, testReadyS3State(t, ""), "submitted body")
	state.phase = PhaseReadOnly
	var err error
	state, err = refreshInteractiveState(state)
	if err != nil {
		t.Fatal(err)
	}
	content := render(state).Content
	if !strings.Contains(content, "Syncing · draft saved") || !strings.Contains(content, "Message sending") {
		t.Fatalf("typed blocker or pending command state disappeared from the footer: %q", content)
	}
	key, _ := NewNormalizedKey(KeyText, 0, "new draft", false)
	state, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 3), Key: key})
	if state.composer.Draft() != "new draft" || containsEffect[SendMutationEffect](effects) {
		t.Fatal("blocked composer did not preserve editing while denying mutation")
	}
}

func TestS3NotificationsKeepSeverityAcrossInputAndOwnAnExpiryTick(t *testing.T) {
	state := testReadyS3State(t, "")
	now := time.Date(2026, 8, 5, 12, 0, 0, 0, time.UTC)
	state = appendLocalNotificationKind(state, notification.Failure, "delivery failed", now)
	key, _ := NewNormalizedKey(KeyText, 0, "draft", false)
	state, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeaderAt(t, 1, now.Add(time.Second)), Key: key})
	if state.localNotifications.Count() != 1 || !strings.Contains(render(state).Content, "Error · delivery failed · Esc dismiss") {
		t.Fatal("ordinary input dismissed or flattened a sticky failure")
	}
	if containsEffect[ScheduleTickEffect](effects) {
		t.Fatal("sticky-only notification incorrectly scheduled an expiry")
	}
	state.localNotifications = state.localNotifications.DismissLatestSticky()
	state = appendLocalNotification(state, "saved", now.Add(2*time.Second))
	key, _ = NewNormalizedKey(KeyText, 0, "x", false)
	state, effects, _ = state.update(KeyInputMsg{Header: testLocalMessageHeaderAt(t, 2, now.Add(3*time.Second)), Key: key})
	if state.localNotifications.Count() != 1 || !containsEffect[ScheduleTickEffect](effects) {
		t.Fatal("transient notification was dismissed by input or lacked its timer owner")
	}
	wheel := MouseWheelInputMsg{
		Header:     testLocalMessageHeaderAt(t, 3, now.Add(4*time.Second)),
		Direction:  MouseWheelScrollUp,
		VisualRows: mouseWheelVisualRows,
	}
	state, _, _ = state.update(wheel)
	if state.localNotifications.Count() != 1 {
		t.Fatal("mouse wheel input dismissed a transient notification before expiry")
	}
}

func containsEffect[T any](effects []Effect) bool {
	for _, effect := range effects {
		if _, ok := effect.(T); ok {
			return true
		}
	}
	return false
}
