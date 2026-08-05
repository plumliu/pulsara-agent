package app

import (
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/components/transcript"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/presentation"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func testSnapshotValidationFailure(t *testing.T, state AppState) PublicFailure {
	t.Helper()
	failure, err := classifyPublicFailure(
		state.connection.Outstanding,
		DeliveryResponseFullyValidated,
		FailureConnectionUsable,
		CauseProjectionValidationFailed,
		"",
		false,
		testFingerprint("snapshot-validation-receipt"),
		"terminal history root identity is invalid",
	)
	if err != nil {
		t.Fatal(err)
	}
	return failure
}

func TestDurableSnapshotValidationFailureRetriesOnceAfterBackoff(t *testing.T) {
	now := time.Now().UTC()
	state := testReadyS2State(t, nil)
	state.phase = PhaseReadOnly
	var err error
	state, _, err = requestDurableSnapshot(state, now, nil, true)
	if err != nil {
		t.Fatal(err)
	}
	firstToken := wireToken(state)
	failure := testSnapshotValidationFailure(t, state)
	next, effects, _ := state.update(SnapshotRejectedMsg{
		Header:  ioHeader(firstToken, failure.EvidenceFingerprint()),
		Failure: failure,
	})
	if next.phase != PhaseReadOnly || next.snapshotLoading.Phase != SnapshotDurableRetryBackoff ||
		next.snapshotRetry.Kind != SnapshotRetryDurable || !next.snapshotRetry.Pending || len(effects) != 1 {
		t.Fatalf("first durable failure did not install one retry owner: phase=%v loading=%v retry=%#v effects=%#v", next.phase, next.snapshotLoading.Phase, next.snapshotRetry, effects)
	}
	tick, ok := effects[0].(ScheduleTickEffect)
	if !ok || tick.Kind != TickSnapshotRetry || tick.TickGeneration != next.snapshotRetry.RetryGeneration || !tick.DueAt.After(now) || tick.DueAt.Sub(now) > time.Second {
		t.Fatalf("durable retry timer drifted: %#v", effects[0])
	}

	retrying, retryEffects, _ := next.update(TickMsg{
		Header:         testLocalMessageHeaderAt(t, next.lastLocalSequence+1, tick.DueAt),
		Kind:           TickSnapshotRetry,
		TickGeneration: tick.TickGeneration,
	})
	if retrying.snapshotLoading.Phase != SnapshotAwaitingDurableSnapshot || retrying.snapshotRetry.Pending || len(retryEffects) != 1 {
		t.Fatalf("durable retry timer did not install exactly one request: loading=%v retry=%#v effects=%#v", retrying.snapshotLoading.Phase, retrying.snapshotRetry, retryEffects)
	}
	if _, ok := retryEffects[0].(RequestSnapshotEffect); !ok {
		t.Fatalf("durable retry emitted the wrong effect: %#v", retryEffects[0])
	}

	secondToken := wireToken(retrying)
	secondFailure := testSnapshotValidationFailure(t, retrying)
	terminal, terminalEffects, _ := retrying.update(SnapshotRejectedMsg{
		Header:  ioHeader(secondToken, secondFailure.EvidenceFingerprint()),
		Failure: secondFailure,
	})
	if terminal.phase != PhaseFatal || !terminal.hasPublicFailure || terminal.publicFailure.Message() != "terminal history root identity is invalid" || len(terminalEffects) != 0 {
		t.Fatalf("second durable validation failure did not preserve the root cause: phase=%v failure=%q effects=%#v", terminal.phase, terminal.publicFailure.Message(), terminalEffects)
	}
}

func TestDurableSnapshotRetrySuccessClearsRetryAuthority(t *testing.T) {
	now := time.Now().UTC()
	state := testReadyS2State(t, nil)
	state.phase = PhaseReadOnly
	state, _, _ = requestDurableSnapshot(state, now, nil, true)
	failure := testSnapshotValidationFailure(t, state)
	state, effects, _ := state.update(SnapshotRejectedMsg{Header: ioHeader(wireToken(state), failure.EvidenceFingerprint()), Failure: failure})
	tick := effects[0].(ScheduleTickEffect)
	state, _, _ = state.update(TickMsg{Header: testLocalMessageHeaderAt(t, 1, tick.DueAt), Kind: TickSnapshotRetry, TickGeneration: tick.TickGeneration})

	snapshot := testDurableSnapshot(state.attachment.Identity.RuntimeSessionID, nil)
	snapshot.RequestID = wireToken(state).RequestID
	snapshot.HostSessionID = "host:snapshot-retry"
	snapshot.Control.PendingInteractionViewFingerprint = testFingerprint("interaction-view:snapshot-retry")
	snapshot.Control.QueueViewFingerprint = testFingerprint("queue-view:snapshot-retry")
	next, acceptedEffects, _ := state.update(SnapshotAcceptedMsg{
		Header: ioHeader(wireToken(state), snapshot.SnapshotFingerprint),
		Request: protocolvalue.PreparedProjectionSnapshotRequest{
			RequestID: wireToken(state).RequestID, RuntimeSessionID: state.attachment.Identity.RuntimeSessionID,
		},
		Snapshot: snapshot,
	})
	if next.snapshotRetry.Kind != SnapshotRetryNone || next.snapshotLoading.Phase != SnapshotAwaitingOperationalSnapshot || len(acceptedEffects) != 1 {
		t.Fatalf("accepted durable retry retained retry authority: phase=%v failure=%q retry=%#v loading=%v effects=%#v", next.phase, next.publicFailure.Message(), next.snapshotRetry, next.snapshotLoading.Phase, acceptedEffects)
	}
}

func TestOperationalSnapshotValidationFailureRetriesOnce(t *testing.T) {
	now := time.Now().UTC()
	state := testReadyS2State(t, nil)
	state.phase = PhaseReadOnly
	state.snapshotLoading = SnapshotLoadingState{
		Phase:        SnapshotBaselinesInstalled,
		AttachmentID: state.attachment.Identity.ID, AttachmentGeneration: state.attachment.Identity.Generation,
		TransportBindingFingerprint:     state.attachment.Identity.BindingFingerprint,
		DurableSnapshotFingerprint:      state.durable.SnapshotFingerprint(),
		DurableControlCursorFingerprint: state.control.ConfirmedCursor().Fingerprint,
		OperationalSnapshotFingerprint:  state.operational.Snapshot().FrameFingerprint,
		OperationalGeneration:           state.operational.Snapshot().Generation,
	}
	state, _, _ = requestOperationalSnapshot(state, now)
	failure := testSnapshotValidationFailure(t, state)
	first, effects, _ := state.update(OperationalSnapshotRejectedMsg{Header: ioHeader(wireToken(state), failure.EvidenceFingerprint()), Failure: failure})
	if first.snapshotRetry.Kind != SnapshotRetryOperational || first.snapshotLoading.Phase != SnapshotOperationalRetryBackoff || len(effects) != 1 {
		t.Fatalf("operational retry owner was not installed: retry=%#v loading=%v effects=%#v", first.snapshotRetry, first.snapshotLoading.Phase, effects)
	}
	tick := effects[0].(ScheduleTickEffect)
	retrying, retryEffects, _ := first.update(TickMsg{Header: testLocalMessageHeaderAt(t, 1, tick.DueAt), Kind: TickSnapshotRetry, TickGeneration: tick.TickGeneration})
	if retrying.snapshotLoading.Phase != SnapshotAwaitingOperationalSnapshot || len(retryEffects) != 1 {
		t.Fatalf("operational retry was not resumed: loading=%v effects=%#v", retrying.snapshotLoading.Phase, retryEffects)
	}
	secondFailure := testSnapshotValidationFailure(t, retrying)
	terminal, _, _ := retrying.update(OperationalSnapshotRejectedMsg{Header: ioHeader(wireToken(retrying), secondFailure.EvidenceFingerprint()), Failure: secondFailure})
	if terminal.phase != PhaseFatal || terminal.publicFailure.Message() != "terminal history root identity is invalid" {
		t.Fatalf("second operational failure did not terminate with its cause: phase=%v failure=%q", terminal.phase, terminal.publicFailure.Message())
	}
}

func TestReconnectDuringInitialSnapshotClearsPartialBaselineAndDropsStaleTimers(t *testing.T) {
	now := time.Now().UTC()
	state := testReadyS2State(t, nil)
	state.connection.ReconnectCredentialHandleID = "terminal-reconnect-credential:test"
	state.connection.ReconnectCredentialCarrierFingerprint = testFingerprint("reconnect-carrier:test")
	state.connection.HasReconnectCredentialHandle = true
	state.durable = presentation.New()
	state.operational = presentation.NewOperational()
	state.control = presentation.NewControlProjection()
	state.pageCache = presentation.NewPageCache()
	state.transcript = transcript.New(state.layout.Width, state.layout.TranscriptRows)
	state.snapshotLoading = SnapshotLoadingState{Phase: SnapshotLoadingUninitialized}
	state.phase = PhaseLoadingSnapshot
	state, _, _ = requestDurableSnapshot(state, now, nil, true)
	failure, err := classifyPublicFailure(
		state.connection.Outstanding,
		DeliveryNotStarted,
		FailureConnectionUsable,
		CauseReadFailed,
		"",
		false,
		testFingerprint("reconnect-read-failure"),
		"connection lost during initial snapshot",
	)
	if err != nil {
		t.Fatal(err)
	}
	next, effects, _ := state.update(SnapshotRejectedMsg{Header: ioHeader(wireToken(state), failure.EvidenceFingerprint()), Failure: failure})
	if next.phase != PhaseReconnecting || next.snapshotLoading.Phase != SnapshotLoadingUninitialized || next.durable.Installed() || next.transcript.Ready() || len(effects) != 1 {
		t.Fatalf("initial snapshot reconnect retained partial authority: phase=%v loading=%v failure=%q effects=%#v", next.phase, next.snapshotLoading.Phase, next.publicFailure.Message(), effects)
	}

	oldGeneration := next.connection.Generation - 1
	stale, staleEffects, _ := next.update(ReconnectDueMsg{Header: testLocalMessageHeader(t, 1), ReconnectGeneration: oldGeneration})
	if stale.phase != PhaseReconnecting || len(staleEffects) != 0 || stale.hasPublicFailure {
		t.Fatalf("old reconnect timer was not dropped: phase=%v effects=%#v failure=%v", stale.phase, staleEffects, stale.hasPublicFailure)
	}

	connected, connectEffects, _ := stale.update(ReconnectDueMsg{Header: testLocalMessageHeader(t, 2), ReconnectGeneration: stale.connection.Generation})
	if connected.phase != PhaseConnecting || connected.connection.Phase != ConnectionDialing || len(connectEffects) != 1 {
		t.Fatalf("exact reconnect timer did not start one connection: phase=%v connection=%v effects=%#v", connected.phase, connected.connection.Phase, connectEffects)
	}
	late, lateEffects, _ := connected.update(ReconnectDueMsg{Header: testLocalMessageHeader(t, 3), ReconnectGeneration: connected.connection.Generation})
	if late.phase != PhaseConnecting || len(lateEffects) != 0 || late.hasPublicFailure {
		t.Fatalf("consumed reconnect timer was not dropped: phase=%v effects=%#v failure=%v", late.phase, lateEffects, late.hasPublicFailure)
	}

	future, _, _ := next.update(ReconnectDueMsg{Header: testLocalMessageHeader(t, 1), ReconnectGeneration: next.connection.Generation + 1})
	if future.phase != PhaseFatal || future.publicFailure.Message() != "terminal reconnect timer generation is from the future" {
		t.Fatalf("future reconnect timer did not fail closed: phase=%v failure=%q", future.phase, future.publicFailure.Message())
	}
}

func TestReconnectDropsDurableOnlyPartialBaseline(t *testing.T) {
	now := time.Now().UTC()
	state := testReadyS2State(t, nil)
	state.connection.ReconnectCredentialHandleID = "terminal-reconnect-credential:partial"
	state.connection.ReconnectCredentialCarrierFingerprint = testFingerprint("reconnect-carrier:partial")
	state.connection.HasReconnectCredentialHandle = true
	state.operational = presentation.NewOperational()
	state.phase = PhaseLoadingSnapshot
	state.snapshotLoading = SnapshotLoadingState{
		Phase:        SnapshotAwaitingOperationalSnapshot,
		AttachmentID: state.attachment.Identity.ID, AttachmentGeneration: state.attachment.Identity.Generation,
		TransportBindingFingerprint:     state.attachment.Identity.BindingFingerprint,
		DurableSnapshotFingerprint:      state.durable.SnapshotFingerprint(),
		DurableControlCursorFingerprint: state.control.ConfirmedCursor().Fingerprint,
		OperationalOperationID:          "terminal-operation:partial", OperationalOperationGeneration: 1,
		OperationalRequired: true,
	}
	token := NewOperationToken(
		OpOperationalSnapshot, state.connection.ClientInstanceID, state.connection.NextOperationGeneration,
		state.appGeneration, state.connection.Generation, state.attachment, now.Add(time.Second),
	)
	state.connection.NextOperationGeneration++
	state.connection.Outstanding = NewOutstandingWire(token)
	state.snapshotLoading.OperationalOperationID = token.OperationID
	state.snapshotLoading.OperationalOperationGeneration = token.OperationGeneration
	failure, err := classifyPublicFailure(
		state.connection.Outstanding, DeliveryNotStarted, FailureConnectionUsable, CauseReadFailed,
		"", false, testFingerprint("partial-baseline-read-failure"), "connection lost during operational bootstrap",
	)
	if err != nil {
		t.Fatal(err)
	}
	next, effects, _ := state.update(OperationalSnapshotRejectedMsg{Header: ioHeader(token, failure.EvidenceFingerprint()), Failure: failure})
	if next.phase != PhaseReconnecting || next.snapshotLoading.Phase != SnapshotLoadingUninitialized ||
		next.durable.Installed() || next.control.Installed() || next.operational.Installed() || next.transcript.Ready() || len(effects) != 1 {
		t.Fatalf("durable-only partial baseline survived reconnect: phase=%v loading=%v failure=%q effects=%#v", next.phase, next.snapshotLoading.Phase, next.publicFailure.Message(), effects)
	}
}
