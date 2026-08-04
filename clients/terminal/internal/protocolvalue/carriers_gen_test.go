package protocolvalue

import (
	"testing"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"google.golang.org/protobuf/proto"
)

func TestDurableSnapshotSpineRequiresExactRootAndHeadJoin(t *testing.T) {
	root := &protocol.PresentationHistoryRootIdentity{
		RuntimeSessionId: "runtime:one", HistoryProjectionContractFingerprint: "projection-contract",
		ThroughAuthoritySequence: 4, RootIdentityFingerprint: "root:one",
	}
	head := &protocol.PresentationHistoryActiveHeadIdentity{
		RuntimeSessionId: "runtime:one", ConfirmedRootIdentity: root,
		TailFromSequenceExclusive: 4, ThroughAuthoritySequence: 7,
		ResultingResidentEntryCount: 0, ActiveHeadFingerprint: "head:one",
		CapacityState: &protocol.PresentationHistoryCapacityState{State: &protocol.PresentationHistoryCapacityState_Available{Available: &protocol.AvailableHistoryCapacity{CapacityStateFingerprint: "capacity:one"}}},
	}
	frame := &protocol.ProjectionSnapshotFrame{
		RequestId: "request:one", HostSessionId: "host:one", RuntimeSessionId: "runtime:one",
		AuthorityHighWater: 7, ProjectionContractFingerprint: "projection-contract",
		ActiveHead:           head,
		LatestRootCursorPair: &protocol.PresentationHistoryLatestRootCursorPair{RootIdentity: root, CursorPairFingerprint: "cursor-pair:one"},
		SnapshotFingerprint:  "snapshot:one",
	}
	if err := validateDurableSnapshotSpine(frame); err != nil {
		t.Fatal(err)
	}
	frame.LatestRootCursorPair.RootIdentity = &protocol.PresentationHistoryRootIdentity{RuntimeSessionId: "runtime:one", RootIdentityFingerprint: "root:forged"}
	if err := validateDurableSnapshotSpine(frame); err == nil {
		t.Fatal("snapshot with a foreign cursor root was accepted")
	}
}

func TestS1CapabilityVocabularyIsExactAndRejectsUnknown(t *testing.T) {
	if len(S1RequiredCapabilities) != 5 || len(S1SupportedCapabilities) != 5 {
		t.Fatalf("unexpected S1 capability set: required=%d supported=%d", len(S1RequiredCapabilities), len(S1SupportedCapabilities))
	}
	for _, forbidden := range []protocol.TerminalClientCapability{
		protocol.TerminalClientCapability_HISTORY_PAGE_V1,
		protocol.TerminalClientCapability_OBSERVATION_STREAM_V1,
		protocol.TerminalClientCapability_CONTROL_PROJECTION_OBSERVATION_V1,
		protocol.TerminalClientCapability_RECONNECT_AUTH_ROTATION_V1,
	} {
		for _, selected := range S1SupportedCapabilities {
			if selected == forbidden {
				t.Fatalf("S1 over-advertised capability %v", forbidden)
			}
		}
	}
	if err := ValidateCapabilities(append(append([]protocol.TerminalClientCapability(nil), S1SupportedCapabilities...), protocol.TerminalClientCapability(65535))); err == nil {
		t.Fatal("unknown capability was accepted")
	}
}

func TestTransportAuthResultRejectsUnknownDisposition(t *testing.T) {
	value := &protocol.TerminalTransportAuthResult{
		AuthRequestId:                     "request",
		AuthAttemptId:                     "attempt",
		ConnectionId:                      "connection",
		ClientInstanceId:                  "client",
		CredentialId:                      "credential",
		Disposition:                       protocol.TransportAuthDisposition(65535),
		AuthenticatedCandidateFingerprint: ptr("candidate"),
	}
	if _, err := protocol.InstallFingerprint("terminal-transport-auth-result:v1", value, "result_fingerprint"); err != nil {
		t.Fatal(err)
	}
	if _, err := TransportAuthResultFromProto(value); err == nil {
		t.Fatal("unknown transport auth disposition was accepted")
	}
}

func TestHeartbeatRejectedReceiptRecomputesItsOwnFingerprint(t *testing.T) {
	value := &protocol.HeartbeatRejectedReceipt{
		RequestId:                            "request:heartbeat",
		RuntimeSessionId:                     "runtime:one",
		AttachmentId:                         "attachment:one",
		AttachmentGeneration:                 1,
		AttachmentIdentityFingerprint:        sha('a'),
		AttachSemanticWinnerFingerprint:      sha('b'),
		SubmittedTransportBindingFingerprint: sha('c'),
		HeartbeatGeneration:                  2,
		PreviousAcceptedHeartbeatGeneration:  1,
		HeartbeatCandidateFingerprint:        sha('d'),
		RejectionReason:                      protocol.HeartbeatRejectedReason_STALE_TRANSPORT_BINDING,
		HeartbeatSemanticResultFingerprint:   sha('e'),
	}
	if _, err := protocol.InstallFingerprint("terminal-heartbeat-rejected-receipt:v1", value, "receipt_fingerprint"); err != nil {
		t.Fatal(err)
	}
	if _, err := HeartbeatRejectedFromProto(value); err != nil {
		t.Fatal(err)
	}
	value.RejectionReason = protocol.HeartbeatRejectedReason_ATTACHMENT_REVOKED
	if _, err := HeartbeatRejectedFromProto(value); err == nil {
		t.Fatal("mutated heartbeat rejection receipt was accepted")
	}
}

func TestQueueProjectionAcceptsGenerationZeroTailAndCheckpointOnlyHead(t *testing.T) {
	for name, committed := range map[string]*protocol.CommittedPromptQueueHead{
		"generation-zero-tail": {
			CheckpointGeneration: 0, CheckpointThroughSequence: 0,
			CheckpointFingerprint: "checkpoint:genesis", CheckpointTransitionCount: 0,
			CheckpointTransitionAccumulator: "checkpoint:accumulator",
			BoundedTailFirstSequenceOrZero:  7, BoundedTailLastSequenceOrZero: 7,
			BoundedTailCount: 1, BoundedTailAccumulator: "tail:one",
			HeadEventId: "event:one", HeadEventSequence: 7,
			HeadEventPayloadFingerprint: "event:payload", HeadReceiptFingerprint: "head:receipt",
			CommittedHeadFingerprint: "head:semantic",
		},
		"checkpoint-only": {
			CheckpointGeneration: 1, CheckpointThroughSequence: 7,
			CheckpointFingerprint: "checkpoint:one", CheckpointTransitionCount: 1,
			CheckpointTransitionAccumulator: "checkpoint:accumulator",
			BoundedTailCount:                0, BoundedTailAccumulator: "tail:empty",
			HeadEventId: "event:one", HeadEventSequence: 7,
			HeadEventPayloadFingerprint: "event:payload", HeadReceiptFingerprint: "head:receipt",
			CommittedHeadFingerprint: "head:semantic",
		},
	} {
		t.Run(name, func(t *testing.T) {
			value := &protocol.PromptQueueClientProjection{
				ProjectionContractId:      "terminal-active-prompt-queue-projection",
				ProjectionContractVersion: 1, ProjectionContractFingerprint: ActiveQueueContractFingerprint,
				QueueHead:       &protocol.PromptQueueProjectionHead{Head: &protocol.PromptQueueProjectionHead_Committed{Committed: committed}},
				ActiveItemCount: 0, ActiveItemAccumulator: "active:empty", ProjectionFingerprint: "projection",
			}
			projection, err := queueProjectionFromWire(value)
			if err != nil {
				t.Fatal(err)
			}
			if projection.headFingerprint != committed.CommittedHeadFingerprint {
				t.Fatalf("head identity used physical receipt: %q", projection.headFingerprint)
			}
		})
	}
}

func TestHelloRejectsNegotiatedLimitDrift(t *testing.T) {
	limits := exactS1Limits()
	limits.MaximumObservationWaitMs++
	value := &protocol.ServerHello{
		NegotiationWinner: &protocol.HelloNegotiationSemanticWinner{
			SelectedProtocol:     &protocol.ProtocolVersion{Major: ProtocolMajor, Minor: ProtocolMinor, SchemaContractFingerprint: SchemaFingerprint},
			SelectedCapabilities: append([]protocol.TerminalClientCapability(nil), S1SupportedCapabilities...),
			NegotiatedLimits:     limits,
		},
		Receipt: &protocol.ServerHelloReceipt{},
	}
	if _, _, err := HelloFromProto(value, "challenge:one"); err == nil {
		t.Fatal("negotiated limit drift was accepted")
	}
}

func TestOperationalSnapshotRecomputesEncodedBytes(t *testing.T) {
	cell := &protocol.OperationalActivityCell{Activity: &protocol.OperationalActivityCell_ModelActivity{ModelActivity: &protocol.ModelActivityCell{Common: &protocol.OperationalActivityCommon{
		OwnerKind: "model_call", OwnerId: "model:one", OwnerGeneration: 1,
		OperationalGeneration: 1, OperationalCursor: 1, CoalesceKey: "model:model:one",
		ReplacementSemantics: protocol.OperationalReplacementSemantics_OPERATIONAL_REPLACE_SAME_KEY,
		BoundedPublicText:    "working", ActivityFingerprint: "activity:one",
	}}}}
	encoded, err := protocol.CanonicalProtobufJSONVectorBytes([]*protocol.OperationalActivityCell{cell})
	if err != nil {
		t.Fatal(err)
	}
	accumulator, err := protocol.OperationalActivityAccumulator([]*protocol.OperationalActivityCell{cell})
	if err != nil {
		t.Fatal(err)
	}
	value := &protocol.OperationalSnapshotFrame{
		RequestId: "request:one", RuntimeSessionId: "runtime:one",
		AttachmentId: "attachment:one", AttachmentGeneration: 1,
		AttachmentIdentityFingerprint:           "attachment-identity",
		AcknowledgedTransportBindingFingerprint: "binding:one",
		OperationalGeneration:                   1, OperationalCursor: 1,
		OrderedActivityCells: []*protocol.OperationalActivityCell{cell}, ActivityCount: 1,
		EncodedActivityBytes: uint64(len(encoded)), ActivityFingerprintAccumulator: accumulator,
		OperationalStateFingerprint: "state", SnapshotContractFingerprint: OperationalContractFingerprint,
	}
	if _, err := protocol.InstallFingerprint("terminal-operational-snapshot-frame:v1", value, "snapshot_frame_fingerprint"); err != nil {
		t.Fatal(err)
	}
	if _, err := OperationalSnapshotFromWire(value); err != nil {
		t.Fatal(err)
	}
	value.EncodedActivityBytes--
	if _, err := protocol.InstallFingerprint("terminal-operational-snapshot-frame:v1", value, "snapshot_frame_fingerprint"); err != nil {
		t.Fatal(err)
	}
	if _, err := OperationalSnapshotFromWire(value); err == nil {
		t.Fatal("forged operational byte count was accepted")
	}
}

func TestS2HistoryCursorDecodeEncodeIsLosslessAtNegotiatedMaximum(t *testing.T) {
	root := &protocol.PresentationHistoryRootIdentity{
		RuntimeSessionId: "runtime:history", HistoryProjectionContractFingerprint: sha('a'),
		MaterializationPolicyFingerprint: sha('b'), TreeContractFingerprint: sha('c'),
		PlacementKeyContractId: "presentation-placement-key", PlacementKeyContractVersion: "v1",
		PlacementKeyContractFingerprint: sha('d'), CheckpointGeneration: 2, CheckpointFingerprint: sha('e'),
		ProjectionGeneration: 3, ProjectionRootFingerprint: sha('f'), ThroughAuthoritySequence: 17,
		PresentationSourceSegmentCount: 4, PresentationSourcePrefixAccumulator: sha('1'),
		PresentationPolicyRegistryContractFingerprint: sha('2'), AuditExtractorRegistryContractFingerprint: sha('3'),
		RootIdentityFingerprint: sha('4'),
	}
	left, right := uint64(11), uint64(12)
	cursor := &protocol.PresentationHistoryCursor{
		RuntimeSessionId: "runtime:history", RootIdentity: root,
		AnchorHistoryEntryId: ptr("history:anchor"),
		AnchorPlacementKey: &protocol.PresentationHistoryPlacementKey{
			PlacementKeyContractId: "presentation-placement-key", PlacementKeyContractVersion: "v1",
			PlacementKeyContractFingerprint: sha('d'), CanonicalSpineLeftCoordinate: &left,
			CanonicalSpineRightCoordinate: &right, RelativePositionKind: protocol.PresentationRelativePositionKind_PRESENTATION_RELATIVE_POSITION_CANONICAL_LEAF,
			SourceLedgerSequenceOrZero: 17, RelativeLocalOrdinal: 2, StableSourceTiebreaker: "history:anchor",
			CanonicalComparableKeyBytes: []byte{0, 1, 2, 255}, PlacementKeyFingerprint: sha('5'),
		},
		CursorFingerprint: sha('6'),
	}
	decoded, err := cursorFromProto(cursor)
	if err != nil {
		t.Fatal(err)
	}
	request, err := PrepareHistoryPageRequest(
		"request:history", "runtime:history", decoded, HistoryPageBefore,
		MaximumHistoryPageCells, MaximumHistoryPageDecodedBytes, root.HistoryProjectionContractFingerprint, 1,
	)
	if err != nil {
		t.Fatal(err)
	}
	wireRequest, err := request.ToProto()
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(wireRequest.Cursor, cursor) {
		t.Fatalf("history cursor carrier lost fields:\nwant=%v\n got=%v", cursor, wireRequest.Cursor)
	}
	unknownPosition := proto.Clone(cursor).(*protocol.PresentationHistoryCursor)
	unknownPosition.AnchorPlacementKey.RelativePositionKind = protocol.PresentationRelativePositionKind(65535)
	if _, err := cursorFromProto(unknownPosition); err == nil {
		t.Fatal("unknown history placement vocabulary was accepted")
	}
	crossContract := proto.Clone(cursor).(*protocol.PresentationHistoryCursor)
	crossContract.AnchorPlacementKey.PlacementKeyContractVersion = "v2"
	if _, err := cursorFromProto(crossContract); err == nil {
		t.Fatal("history cursor with placement authority foreign to its root was accepted")
	}
	foreignContinuation := proto.Clone(cursor).(*protocol.PresentationHistoryCursor)
	foreignContinuation.RootIdentity.TreeContractFingerprint = sha('9')
	page := &protocol.HistoryPageResponse{Outcome: &protocol.HistoryPageResponse_Page{Page: &protocol.HistoryPageData{
		RequestId: "request:history", ValidatedInputCursorFingerprint: cursor.CursorFingerprint,
		ValidatedRequestDirection: protocol.HistoryPageRequest_BEFORE,
		ValidatedRootIdentity:     root, BeforeCursor: foreignContinuation,
		OrderedHistoryEntryAccumulator: sha('7'), ContinuityProofFingerprint: sha('8'),
		ResponseFingerprint: sha('0'),
	}}}
	if _, err := HistoryPageFromProto(page); err == nil {
		t.Fatal("history page continuation with same root fingerprint and different root payload was accepted")
	}
}

func exactS1Limits() *protocol.NegotiatedLimits {
	return &protocol.NegotiatedLimits{
		MaximumFrameBytes: MaximumFrameBytes, MaximumHistoryPageCells: MaximumHistoryPageCells,
		MaximumHistoryPageDecodedBytes: MaximumHistoryPageDecodedBytes, MaximumObservationWaitMs: MaximumObservationWaitMS,
		SecretFrameMaximumBytes: SecretFrameMaximumBytes, MaximumActiveQueueItems: MaximumActiveQueueItems,
		MaximumServerControlNotifications: MaximumServerNotifications, MaximumOperationalActivityCells: MaximumOperationalActivities,
		MaximumDurableObservationBytes: MaximumDurableObservationBytes, MaximumOperationalObservationBytes: MaximumOperationalBytes,
		MaximumControlObservationBytes: MaximumControlObservationBytes, MaximumObservationBatchBytes: MaximumObservationBatchBytes,
	}
}

func ptr(value string) *string { return &value }

func sha(character byte) string {
	value := make([]byte, 64)
	for index := range value {
		value[index] = character
	}
	return "sha256:" + string(value)
}
