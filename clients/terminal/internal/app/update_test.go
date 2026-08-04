package app

import (
	"strings"
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func TestS1SnapshotBootstrapInstallsBaselinesInOrder(t *testing.T) {
	candidate := protocolvalue.HandshakeCandidate{ID: "candidate:one", ClientInstanceID: "terminal-client:unknown", AttachmentAttemptGeneration: 1, HostSessionID: "host:one", RuntimeSessionID: "runtime:one", Fingerprint: "candidate-fingerprint"}
	state := startState(t, candidate)
	state = applyMessage(t, state, connectMessage(state))
	auth := protocolvalue.TransportAuthResult{RequestID: wireToken(state).RequestID, AttemptID: "auth:one", ConnectionID: "connection:one", ClientInstanceID: "terminal-client:unknown", CredentialID: "launch:one", Disposition: protocol.TransportAuthDisposition_TRANSPORT_AUTHENTICATED, CandidateFingerprint: candidate.Fingerprint, ResultFingerprint: "auth-result"}
	state = applyMessage(t, state, TransportAuthenticatedMsg{Header: ioHeader(wireToken(state), auth.ResultFingerprint), ConnectionHandleID: "connection-handle:one", Candidate: candidate, Proof: auth})
	winner := protocolvalue.HelloNegotiationWinner{CandidateID: candidate.ID, CandidateFingerprint: candidate.Fingerprint, AttachmentAttemptGeneration: 1, NegotiationWinnerFingerprint: "winner:hello"}
	receipt := protocolvalue.ValidatedServerHelloReceipt{RequestID: wireToken(state).RequestID, TransportAuthAttemptID: auth.AttemptID, CandidateID: candidate.ID, CandidateFingerprint: candidate.Fingerprint, NegotiationWinnerFingerprint: winner.NegotiationWinnerFingerprint, CurrentConnectionID: auth.ConnectionID, ChallengeCommitment: "challenge", ReceiptFingerprint: "hello-receipt", PreparedChallengeHandleID: "challenge:one"}
	helloOperation := wireToken(state)
	prepared := PreparedAttachmentChallengeHandleIdentity{HandleID: receipt.PreparedChallengeHandleID, HandleGeneration: 1, HelloOperationID: helloOperation.OperationID, HelloOperationGeneration: helloOperation.OperationGeneration, ValidatedReceiptFingerprint: receipt.ReceiptFingerprint, CandidateFingerprint: candidate.Fingerprint, ConnectionID: auth.ConnectionID, ChallengeCommitment: receipt.ChallengeCommitment, HandleFingerprint: "prepared-challenge"}
	state = applyMessage(t, state, HelloAcceptedMsg{Header: ioHeader(helloOperation, receipt.ReceiptFingerprint), Winner: winner, Receipt: receipt, PreparedChallenge: prepared})
	promotionOperation := localToken(state)
	promotion := AttachmentChallengePromotionReceipt{PreparedHandleFingerprint: prepared.HandleFingerprint, PromotionOperationID: promotionOperation.OperationID, PromotionOperationGeneration: promotionOperation.OperationGeneration, PromotionReceiptFingerprint: "promotion"}
	state = applyMessage(t, state, AttachmentChallengePromotedMsg{Header: localHeader(promotionOperation, promotion.PromotionReceiptFingerprint), Receipt: promotion})
	acceptanceOperation := localToken(state)
	acceptance := AttachmentChallengeAcceptanceReceipt{PreparedHandleFingerprint: prepared.HandleFingerprint, PromotionReceiptFingerprint: promotion.PromotionReceiptFingerprint, ConfirmationOperationID: acceptanceOperation.OperationID, ConfirmationOperationGeneration: acceptanceOperation.OperationGeneration, AcceptanceReceiptFingerprint: "acceptance"}
	state = applyMessage(t, state, AttachmentChallengePromotionAcceptedMsg{Header: localHeader(acceptanceOperation, acceptance.AcceptanceReceiptFingerprint), Receipt: acceptance})
	issued := time.Now().UTC()
	binding := testTransportBinding(t, "attachment:one", 1, auth.ConnectionID, 1, issued)
	attachment := protocolvalue.Attachment{ClientInstanceID: candidate.ClientInstanceID, AttachmentAttemptGeneration: 1, ConnectionID: auth.ConnectionID, BindingGeneration: 1, BindingFingerprint: binding.Fingerprint, ID: "attachment:one", Generation: 1, RuntimeSessionID: "runtime:one", Role: protocol.AttachmentRole_ATTACHMENT_ROLE_OBSERVER, IssuedAt: issued, ExpiresAt: issued.Add(time.Minute), IdentityFingerprint: "identity", SemanticWinnerFingerprint: "winner:attach", CurrentReceiptFingerprint: "attach-receipt", ControllerDisposition: protocol.ControllerDisposition_OBSERVER_ATTACHED, BootstrapRequirement: protocol.BootstrapRequirement_PROJECTION_AND_OPERATIONAL_SNAPSHOT_REQUIRED, Heartbeat: protocolvalue.HeartbeatPolicy{Interval: 10 * time.Second, Grace: 20 * time.Second, MaximumMissedCount: 2}}
	attachReceipt := protocolvalue.AttachReceipt{RequestID: wireToken(state).RequestID, TransportAuthAttemptID: auth.AttemptID, CandidateID: candidate.ID, CandidateFingerprint: candidate.Fingerprint, SemanticWinnerFingerprint: attachment.SemanticWinnerFingerprint, CurrentBinding: binding, Disposition: protocol.AttachResultDisposition_ATTACH_CREATED, ReceiptFingerprint: "attach-receipt"}
	state = applyMessage(t, state, AttachAcceptedMsg{Header: ioHeader(wireToken(state), attachReceipt.ReceiptFingerprint), Attachment: attachment, Receipt: attachReceipt})
	ack := protocolvalue.ValidatedAttachAckResult{RequestID: wireToken(state).RequestID, AttachmentID: attachment.ID, AttachmentGeneration: attachment.Generation, SemanticWinnerFingerprint: attachment.SemanticWinnerFingerprint, AcknowledgedBindingFingerprint: attachment.BindingFingerprint, Disposition: protocol.AttachAckDisposition_ATTACH_ACKNOWLEDGED, ResultFingerprint: "ack-result"}
	state = applyMessage(t, state, AttachAcknowledgedMsg{Header: ioHeader(wireToken(state), ack.ResultFingerprint), Result: ack})
	if state.snapshotLoading.Phase != SnapshotAwaitingDurableSnapshot {
		t.Fatalf("loading state = %d", state.snapshotLoading.Phase)
	}
	control := protocolvalue.ControlProjection{RuntimeSessionID: "runtime:one", Generation: 1, Revision: 2, ProjectionFingerprint: "control-view", ViewFingerprint: "control-view", TransitionAccumulator: "control-acc", RegistryFingerprint: "control-registry", CursorFingerprint: "control-cursor", SnapshotFingerprint: "control-snapshot", PendingInteraction: true, PendingInteractionID: "interaction:one", PendingInteractionGeneration: 1, PendingInteractionViewFingerprint: "interaction-view", QueueHeadFingerprint: "queue-head", QueueViewFingerprint: "queue-view", QueueAccumulator: "queue-acc", QueueItems: []protocolvalue.QueueItem{{ID: "queue:one"}, {ID: "queue:two"}}}
	durable := protocolvalue.DurableSnapshot{RequestID: wireToken(state).RequestID, HostSessionID: "host:one", RuntimeSessionID: "runtime:one", AuthorityHighWater: 7, ProjectionRevision: 3, ActiveHeadFingerprint: "head", Control: control, SnapshotFingerprint: "snapshot", Cells: []protocolvalue.HistoryCell{{ID: "entry:one", Revision: 1, Kind: "assistant", PublicText: "你好 🌍", Fingerprint: "cell"}}}
	state = applyMessage(t, state, SnapshotAcceptedMsg{Header: ioHeader(wireToken(state), durable.SnapshotFingerprint), Snapshot: durable})
	if state.snapshotLoading.Phase != SnapshotAwaitingOperationalSnapshot || !state.interaction.Pending() || state.interaction.ActionsEnabled() || state.queue.ActiveCount() != 2 || state.queue.ActionsEnabled() {
		t.Fatal("durable snapshot did not install read-only control state")
	}
	operational := protocolvalue.OperationalSnapshot{RequestID: wireToken(state).RequestID, RuntimeSessionID: "runtime:one", AttachmentID: attachment.ID, AttachmentGeneration: attachment.Generation, AttachmentIdentityFingerprint: attachment.IdentityFingerprint, AcknowledgedBindingFingerprint: attachment.BindingFingerprint, Generation: 1, Cursor: 4, FrameFingerprint: "operational"}
	state = applyMessage(t, state, OperationalSnapshotAcceptedMsg{Header: ioHeader(wireToken(state), operational.FrameFingerprint), Snapshot: operational})
	if state.phase != PhaseReady || !state.durable.Ready() || !state.operational.Ready() || !state.control.Ready() || state.snapshotLoading.Phase != SnapshotBaselinesInstalled {
		t.Fatalf("S1 did not become ready: phase=%d", state.phase)
	}
	originalSemanticExpiry := state.attachment.Identity.ExpiresAt
	heartbeatAt := state.connection.HeartbeatSchedule.NextAt.Add(time.Millisecond)
	state, heartbeatEffects, _ := state.update(TickMsg{Header: testLocalMessageHeaderAt(t, 2, heartbeatAt), Kind: TickHeartbeat, TickGeneration: 1})
	if len(heartbeatEffects) != 1 {
		t.Fatal("heartbeat tick did not install an exact operation")
	}
	heartbeatToken := wireToken(state)
	heartbeatEffect, ok := heartbeatEffects[0].(HeartbeatEffect)
	if !ok {
		t.Fatalf("unexpected heartbeat effect: %#v", heartbeatEffects[0])
	}
	heartbeatRequest := heartbeatEffect.Request
	resultingExpiry := originalSemanticExpiry.Add(time.Minute)
	heartbeatReceipt := protocolvalue.ValidatedHeartbeatAcceptedReceipt{RequestID: heartbeatToken.RequestID, RuntimeSessionID: attachment.RuntimeSessionID, AttachmentID: attachment.ID, AttachmentGeneration: attachment.Generation, AttachmentIdentityFingerprint: attachment.IdentityFingerprint, SemanticWinnerFingerprint: attachment.SemanticWinnerFingerprint, AcknowledgedBindingFingerprint: attachment.BindingFingerprint, HeartbeatGeneration: 1, PreviousAcceptedGeneration: 0, CandidateFingerprint: heartbeatRequest.CandidateFingerprint, LeaseExpiresAt: resultingExpiry, LivenessDisposition: protocolvalue.HeartbeatLeaseRenewed, SemanticResultFingerprint: "heartbeat-semantic", ReceiptFingerprint: "heartbeat-receipt"}
	state = applyMessage(t, state, HeartbeatAcceptedMsg{Header: ioHeader(heartbeatToken, heartbeatReceipt.ReceiptFingerprint), Request: heartbeatRequest, Receipt: heartbeatReceipt})
	if !state.attachment.Identity.ExpiresAt.Equal(originalSemanticExpiry) || !state.connection.HeartbeatSchedule.LeaseExpiresAt.Equal(resultingExpiry) {
		t.Fatal("heartbeat mutated frozen attachment semantics instead of its local lease owner")
	}
	quitKey, err := NewNormalizedKey(KeyText, 0, "q", false)
	if err != nil {
		t.Fatal(err)
	}
	state, teardownEffects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 3), Key: quitKey})
	if len(teardownEffects) != 1 || state.phase != PhaseDetaching {
		t.Fatal("ready S1 state did not enter its sole teardown path")
	}
	teardownToken := localToken(state)
	summary, err := NewPublicTeardownSummary(1, TeardownCompleted, 0, 0, 0, false, false, true, PublicFailure{}, false)
	if err != nil {
		t.Fatal(err)
	}
	fingerprint, err := summary.Fingerprint()
	if err != nil {
		t.Fatal(err)
	}
	state = applyMessage(t, state, TeardownCompletedMsg{Header: localHeader(teardownToken, fingerprint), Summary: summary})
	if state.phase != PhaseExited || state.snapshotLoading.Phase != SnapshotBaselinesInstalled {
		t.Fatal("ready S1 teardown did not preserve its installed baseline through exit")
	}
}

func TestStaleOperationMessageCannotAdvanceState(t *testing.T) {
	candidate := protocolvalue.HandshakeCandidate{ID: "candidate:stale", ClientInstanceID: "terminal-client:stale", AttachmentAttemptGeneration: 1, HostSessionID: "host:one", RuntimeSessionID: "runtime:one", Fingerprint: "candidate-fingerprint"}
	state := startState(t, candidate)
	stale := localToken(state)
	stale.OperationGeneration++
	stale.OperationID = "terminal-local-operation:stale"
	message := connectMessage(state)
	message.Header.Operation = stale
	next, _, _ := state.update(message)
	if next.phase != state.phase || next.connection.Outstanding != state.connection.Outstanding {
		t.Fatal("stale operation response mutated AppState")
	}
}

func TestPublicCopyUsesClosedEffectAndTypedReceipt(t *testing.T) {
	state := NewInitialAppState("terminal-client:copy")
	key, err := NewNormalizedKey(KeyText, 0, "y", false)
	if err != nil {
		t.Fatal(err)
	}
	next, effects, command := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: key})
	if command != nil || len(effects) != 1 {
		t.Fatalf("copy bypassed the closed effect boundary: effects=%d command=%v", len(effects), command)
	}
	effect, ok := effects[0].(CopyPublicTextEffect)
	if !ok || effect.Header.Operation.Kind != OpClipboard || effect.Header.Operation != next.connection.Outstanding.Local {
		t.Fatalf("unexpected copy effect: %#v", effects[0])
	}
	receipt := PublicTextCopiedMsg{Header: localHeader(effect.Header.Operation, "clipboard-receipt")}
	settled, _, _ := next.update(receipt)
	if settled.connection.Outstanding.Carrier != OutstandingNone {
		t.Fatal("clipboard receipt did not settle the installed operation")
	}
	if err := settled.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestPeerIdentityAndTeardownSummaryAreClosedCarriers(t *testing.T) {
	if _, err := NewValidatedPeerIdentity(501, 502, 0, false, 501, "runtime-path"); err == nil {
		t.Fatal("mismatched peer UID was accepted")
	}
	state := NewInitialAppState("terminal-client:teardown")
	key, err := NewNormalizedKey(KeyText, 0, "q", false)
	if err != nil {
		t.Fatal(err)
	}
	next, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: key})
	if len(effects) != 1 || next.teardown.Phase != TeardownStoppingEffects || !next.teardown.StopAcceptingEffects {
		t.Fatal("teardown did not freeze the closed stopping state")
	}
	token := localToken(next)
	summary, err := NewPublicTeardownSummary(1, TeardownCompleted, 0, 0, 0, false, false, true, PublicFailure{}, false)
	if err != nil {
		t.Fatal(err)
	}
	fingerprint, err := summary.Fingerprint()
	if err != nil {
		t.Fatal(err)
	}
	settled := applyMessage(t, next, TeardownCompletedMsg{Header: localHeader(token, fingerprint), Summary: summary})
	if settled.phase != PhaseExited || settled.teardown.Phase != TeardownTerminal || !settled.teardown.TerminalRestoreCompleted || !settled.teardown.SchedulerDrained || !settled.teardown.BridgeDrained {
		t.Fatal("teardown summary did not terminalize the application state")
	}
}

func TestLocalMessageSequenceMustBeContiguous(t *testing.T) {
	state := NewInitialAppState("terminal-client:local-sequence")
	key, err := NewNormalizedKey(KeyDown, 0, "", false)
	if err != nil {
		t.Fatal(err)
	}
	next, _, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 2), Key: key})
	if next.phase != PhaseFatal {
		t.Fatal("non-contiguous local message sequence crossed the reducer boundary")
	}
}

func TestInvalidWindowSizeUsesBoundedFatalViewWithoutHiddenClamp(t *testing.T) {
	state := NewInitialAppState("terminal-client:invalid-size")
	next, effects, _ := state.update(ResizeMsg{Header: testLocalMessageHeader(t, 1), Width: 0, Height: 0})
	if len(effects) != 0 || next.phase != PhaseFatal || next.layout.Width != 80 || next.layout.Height != 24 {
		t.Fatalf("invalid resize did not preserve the last validated layout: %#v", next.layout)
	}
	if rows := strings.Count(render(next).Content, "\n") + 1; rows != 24 {
		t.Fatalf("bounded fatal view rendered %d rows", rows)
	}
}

func TestMouseWheelScrollsOnlyTheResidentTranscriptViewport(t *testing.T) {
	state := NewInitialAppState("terminal-client:mouse-wheel")
	durable, err := state.durable.Install(protocolvalue.DurableSnapshot{
		HostSessionID: "host:mouse", RuntimeSessionID: "runtime:mouse",
		Control:             protocolvalue.ControlProjection{RuntimeSessionID: "runtime:mouse", CursorFingerprint: "control:mouse"},
		SnapshotFingerprint: "snapshot:mouse",
		Cells: []protocolvalue.HistoryCell{{
			ID: "entry:mouse", Kind: "assistant",
			PublicText:  "一段足够长的中文 transcript，用于验证滚轮只改变应用自己的 resident viewport，而不触碰 terminal scrollback。",
			Fingerprint: "cell:mouse",
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	layout, err := NewLayoutPlan(12, 6)
	if err != nil {
		t.Fatal(err)
	}
	state.layout = layout
	state.transcript, err = state.transcript.Install(durable.Durable(), layout.Width, layout.TranscriptRows)
	if err != nil {
		t.Fatal(err)
	}
	state.durable = durable

	state, effects, _ := state.update(MouseWheelInputMsg{
		Header: testLocalMessageHeader(t, 1), Direction: MouseWheelScrollUp, VisualRows: mouseWheelVisualRows,
	})
	if len(effects) != 0 || state.transcript.ScrollOffset() != int(mouseWheelVisualRows) {
		t.Fatalf("wheel up did not scroll the resident viewport: offset=%d effects=%#v", state.transcript.ScrollOffset(), effects)
	}
	state, effects, _ = state.update(MouseWheelInputMsg{
		Header: testLocalMessageHeader(t, 2), Direction: MouseWheelScrollDown, VisualRows: mouseWheelVisualRows,
	})
	if len(effects) != 0 || state.transcript.ScrollOffset() != 0 {
		t.Fatalf("wheel down did not return to follow-tail: offset=%d effects=%#v", state.transcript.ScrollOffset(), effects)
	}
	pageUp, err := NewNormalizedKey(KeyPageUp, 0, "", false)
	if err != nil {
		t.Fatal(err)
	}
	state, effects, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 3), Key: pageUp})
	if len(effects) != 0 || state.transcript.ScrollOffset() != layout.TranscriptRows-1 {
		t.Fatalf("PageUp did not move viewportRows-1: offset=%d", state.transcript.ScrollOffset())
	}
	end, err := NewNormalizedKey(KeyEnd, 0, "", false)
	if err != nil {
		t.Fatal(err)
	}
	state, effects, _ = state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 4), Key: end})
	if len(effects) != 0 || state.transcript.ScrollOffset() != 0 || !state.transcript.FollowTail() {
		t.Fatal("End did not restore the transcript tail")
	}
	state, _, _ = state.update(MouseWheelInputMsg{
		Header: testLocalMessageHeader(t, 5), Direction: MouseWheelScrollUp, VisualRows: mouseWheelVisualRows + 1,
	})
	if state.phase != PhaseFatal {
		t.Fatal("caller-defined wheel magnitude crossed the closed input contract")
	}
}

func startState(t *testing.T, candidate protocolvalue.HandshakeCandidate) AppState {
	t.Helper()
	state := NewInitialAppState(candidate.ClientInstanceID)
	started := AppStartedMsg{
		Header:                      testLocalMessageHeader(t, 1),
		BootstrapHandleID:           "terminal-bootstrap:" + candidate.ClientInstanceID,
		TransportCredentialHandleID: "terminal-launch-credential:" + candidate.ClientInstanceID,
		HandshakeCandidate:          candidate,
	}
	next, effects, _ := state.update(started)
	if len(effects) != 1 {
		t.Fatal("application start did not produce the sole connect effect")
	}
	if _, ok := effects[0].(ConnectEffect); !ok {
		t.Fatalf("unexpected application start effect: %#v", effects[0])
	}
	if err := next.Validate(); err != nil {
		t.Fatal(err)
	}
	return next
}

func TestS1NeverRetriesPostAcknowledgementWithoutReconnectCapability(t *testing.T) {
	state := NewInitialAppState("terminal-client:no-reconnect")
	state.connection.Phase = ConnectionAttached
	token := NewOperationToken(
		OpProjectionSnapshot,
		"terminal-client:no-reconnect",
		1,
		1,
		1,
		AttachmentState{},
		time.Now().Add(time.Second),
	)
	failure, err := classifyPublicFailure(
		NewOutstandingWire(token),
		DeliveryNotStarted,
		FailureConnectionUsable,
		CauseReadFailed,
		"",
		false,
		"connection-terminal-receipt",
		"connection lost",
	)
	if err != nil {
		t.Fatal(err)
	}
	next, effects, command := transitionFailure(state, failure, time.Now())
	if next.phase != PhaseFatal || next.publicFailure.Code() != FailureClientInvariant || len(effects) != 0 || command != nil {
		t.Fatalf("post-ACK S1 failure attempted an unauthenticated reconnect: phase=%d code=%d", next.phase, next.publicFailure.Code())
	}
}

func TestFailureMessageRejectsDifferentOperationWithSameOuterFingerprint(t *testing.T) {
	token := NewOperationToken(
		OpProjectionSnapshot,
		"terminal-client:failure-join",
		1,
		1,
		1,
		AttachmentState{},
		time.Now().Add(time.Second),
	)
	failure, err := classifyPublicFailure(
		NewOutstandingWire(token),
		DeliveryResponseFullyValidated,
		FailureConnectionUsable,
		CauseProjectionValidationFailed,
		"",
		false,
		"projection-validation-receipt",
		"snapshot failed",
	)
	if err != nil {
		t.Fatal(err)
	}
	foreign := token
	foreign.OperationID = "terminal-operation:foreign"
	foreign.RequestID = "terminal-request:foreign"
	if err := validateWireFailure(
		IOMessageHeader{
			Operation:          foreign,
			PayloadFingerprint: failure.EvidenceFingerprint(),
			ReceivedAt:         time.Now(),
		},
		failure,
		OpProjectionSnapshot,
	); err == nil {
		t.Fatal("failure proof was accepted for a different operation")
	}
}

func TestStaleHelloResultProducesExactPreparedChallengeRevocation(t *testing.T) {
	candidate := protocolvalue.HandshakeCandidate{
		ID: "candidate:stale-challenge", ClientInstanceID: "terminal-client:stale-challenge",
		AttachmentAttemptGeneration: 1, HostSessionID: "host:one",
		RuntimeSessionID: "runtime:one", Fingerprint: "candidate:fingerprint",
	}
	state := startState(t, candidate)
	foreign := NewOperationToken(
		OpHello,
		candidate.ClientInstanceID,
		99,
		1,
		1,
		AttachmentState{},
		time.Now().Add(time.Second),
	)
	prepared := PreparedAttachmentChallengeHandleIdentity{
		HandleID: "challenge:stale", HandleGeneration: 1,
		HelloOperationID: foreign.OperationID, HelloOperationGeneration: foreign.OperationGeneration,
		ValidatedReceiptFingerprint: "hello-receipt", CandidateFingerprint: candidate.Fingerprint,
		ConnectionID: "connection:stale", ChallengeCommitment: "challenge-commitment",
		HandleFingerprint: "challenge:fingerprint",
	}
	next, effects, command := state.update(HelloAcceptedMsg{
		Header: IOMessageHeader{
			Operation: foreign, PayloadFingerprint: "hello-receipt", ReceivedAt: time.Now(),
		},
		PreparedChallenge: prepared,
	})
	if command != nil || len(effects) != 1 {
		t.Fatalf("stale Hello did not produce one revocation effect: %#v", effects)
	}
	revoke, ok := effects[0].(RevokePreparedAttachmentChallengeEffect)
	if !ok || revoke.Header.Operation.Kind != OpChallengeRevokePrepared ||
		revoke.HandleFingerprint != prepared.HandleFingerprint ||
		revoke.Reason != ChallengeRevokeStaleApplicationResult ||
		next.connection.Outstanding != state.connection.Outstanding {
		t.Fatalf("stale Hello revocation drifted from its prepared owner: %#v", revoke)
	}
}

func TestAckResponseLossRecoveryRebindsPhysicalAttachmentOnly(t *testing.T) {
	state := NewInitialAppState("terminal-client:recovery")
	issued := time.Now().UTC()
	oldAttachment := protocolvalue.Attachment{
		ClientInstanceID: "terminal-client:recovery", AttachmentAttemptGeneration: 1,
		ConnectionID: "connection:old", BindingGeneration: 1, BindingFingerprint: "binding:old",
		ID: "attachment:one", Generation: 1, RuntimeSessionID: "runtime:one",
		Role:     protocol.AttachmentRole_ATTACHMENT_ROLE_OBSERVER,
		IssuedAt: issued, ExpiresAt: issued.Add(time.Minute), IdentityFingerprint: "identity:one",
		SemanticWinnerFingerprint: "winner:one", CurrentReceiptFingerprint: "ack:old",
		ControllerDisposition: protocol.ControllerDisposition_OBSERVER_ATTACHED,
		BootstrapRequirement:  protocol.BootstrapRequirement_PROJECTION_AND_OPERATIONAL_SNAPSHOT_REQUIRED,
		Heartbeat:             protocolvalue.HeartbeatPolicy{Interval: time.Second, Grace: 2 * time.Second, MaximumMissedCount: 2},
	}
	state.attachment = AttachmentState{Valid: true, Identity: oldAttachment}
	state.phase = PhaseConnecting
	state.connection.Phase = ConnectionAuthPending
	state.connection.Generation = 2
	state.connection.HandshakeCandidate = protocolvalue.HandshakeCandidate{ID: "candidate:one", ClientInstanceID: "terminal-client:recovery", AttachmentAttemptGeneration: 1, HostSessionID: "host:one", RuntimeSessionID: "runtime:one", Fingerprint: "candidate:fingerprint"}
	var token OperationToken
	state, token = state.nextWire(OpTransportAuth, time.Now().Add(time.Second))
	proof := protocolvalue.TransportAuthResult{RequestID: token.RequestID, AttemptID: "auth:new", ConnectionID: "connection:new", ClientInstanceID: "terminal-client:recovery", CredentialID: "launch:one", Disposition: protocolvalue.TransportAuthAckResultRecovery, CandidateFingerprint: state.connection.HandshakeCandidate.Fingerprint, ResultFingerprint: "auth:recovery", RecoveredAttachmentPresent: true}
	ack := protocolvalue.ValidatedAttachAckResult{RequestID: "old-ack-request", AttachmentID: oldAttachment.ID, AttachmentGeneration: oldAttachment.Generation, SemanticWinnerFingerprint: oldAttachment.SemanticWinnerFingerprint, AcknowledgedBindingFingerprint: oldAttachment.BindingFingerprint, Disposition: protocol.AttachAckDisposition_ATTACH_ACKNOWLEDGED, ResultFingerprint: "ack:old"}
	newBinding := testTransportBinding(t, oldAttachment.ID, oldAttachment.Generation, "connection:new", 2, issued.Add(time.Second))
	binding := protocolvalue.RecoveredAttachmentTransportBinding{PreviousBindingFingerprint: oldAttachment.BindingFingerprint, ResultingBinding: newBinding, Disposition: protocol.AttachmentTransportRebindDisposition_ATTACHMENT_TRANSPORT_REBOUND, ReceiptFingerprint: "rebind:receipt"}
	resulting := oldAttachment
	resulting.ConnectionID, resulting.BindingGeneration, resulting.BindingFingerprint = binding.ResultingBinding.ConnectionID, binding.ResultingBinding.Generation, binding.ResultingBinding.Fingerprint
	resulting.CurrentReceiptFingerprint = ack.ResultFingerprint
	message := AttachRecoveredMsg{Header: ioHeader(token, proof.ResultFingerprint), ConnectionHandleID: "connection-handle:new", Candidate: state.connection.HandshakeCandidate, Proof: proof, Recovery: protocolvalue.RecoveredAttachAcknowledgement{Ack: ack, Binding: binding}, Attachment: resulting}
	next := applyMessage(t, state, message)
	if next.phase != PhaseLoadingSnapshot || next.attachment.Identity.IdentityFingerprint != oldAttachment.IdentityFingerprint || next.attachment.Identity.BindingFingerprint != binding.ResultingBinding.Fingerprint || next.snapshotLoading.Phase != SnapshotAwaitingDurableSnapshot {
		t.Fatalf("ACK recovery did not preserve semantic attachment and install the new physical binding: %#v", next.attachment)
	}
}

func testTransportBinding(t *testing.T, attachmentID string, attachmentGeneration uint64, connectionID string, generation uint64, boundAt time.Time) protocolvalue.TransportBinding {
	t.Helper()
	value := &protocol.TerminalClientTransportBindingIdentity{
		AttachmentId: attachmentID, AttachmentGeneration: attachmentGeneration,
		ConnectionId: connectionID, TransportBindingGeneration: generation,
		BoundAtUtc: boundAt.UTC().Format(time.RFC3339Nano),
	}
	if _, err := protocol.InstallFingerprint("terminal-client-transport-binding:v1", value, "binding_fingerprint"); err != nil {
		t.Fatal(err)
	}
	return protocolvalue.TransportBinding{
		AttachmentID: value.AttachmentId, AttachmentGeneration: value.AttachmentGeneration,
		ConnectionID: value.ConnectionId, Generation: value.TransportBindingGeneration,
		BoundAtUTC: value.BoundAtUtc, Fingerprint: value.BindingFingerprint,
	}
}

func connectMessage(state AppState) ConnectSucceededMsg {
	token := localToken(state)
	peer, err := NewValidatedPeerIdentity(501, 501, 0, false, 501, "runtime-path")
	if err != nil {
		panic(err)
	}
	fingerprint, err := ConnectResultFingerprint(token, "connection-handle:one", peer)
	if err != nil {
		panic(err)
	}
	return ConnectSucceededMsg{Header: localHeader(token, fingerprint), ConnectionHandleID: "connection-handle:one", Peer: peer}
}
func wireToken(state AppState) OperationToken       { return state.connection.Outstanding.Wire }
func localToken(state AppState) LocalOperationToken { return state.connection.Outstanding.Local }
func ioHeader(token OperationToken, fingerprint string) IOMessageHeader {
	return IOMessageHeader{Operation: token, PayloadFingerprint: fingerprint, ReceivedAt: time.Now()}
}
func localHeader(token LocalOperationToken, fingerprint string) LocalResultHeader {
	return LocalResultHeader{Operation: token, PayloadFingerprint: fingerprint, ReceivedAt: time.Now()}
}
func applyMessage(t *testing.T, state AppState, message any) AppState {
	t.Helper()
	next, _, _ := state.update(message)
	if err := next.Validate(); err != nil {
		t.Fatal(err)
	}
	return next
}
