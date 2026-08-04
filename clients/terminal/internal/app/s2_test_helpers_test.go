package app

import (
	"crypto/sha256"
	"fmt"
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func testFingerprint(value string) string {
	return fmt.Sprintf("sha256:%x", sha256.Sum256([]byte(value)))
}

func testReadyS2State(t *testing.T, cells []protocolvalue.HistoryCell) AppState {
	t.Helper()
	now := time.Now().UTC()
	wireBinding := &protocol.TerminalClientTransportBindingIdentity{
		AttachmentId: "attachment:s2", AttachmentGeneration: 1,
		ConnectionId: "connection:s2", TransportBindingGeneration: 1,
		BoundAtUtc: now.Format(time.RFC3339Nano),
	}
	bindingFingerprint, err := protocol.InstallFingerprint(
		"terminal-client-transport-binding:v1", wireBinding, "binding_fingerprint",
	)
	if err != nil {
		t.Fatal(err)
	}
	state := NewInitialAppState("terminal-client:s2")
	state.connection.Phase = ConnectionAttached
	state.connection.HandleID = "connection-handle:s2"
	state.connection.Generation = 1
	state.connection.HandshakeCandidate = protocolvalue.HandshakeCandidate{
		ID: "candidate:s2", ClientInstanceID: "terminal-client:s2",
		AttachmentAttemptGeneration: 1, HostSessionID: "host:s2",
		RuntimeSessionID: "runtime:s2", Fingerprint: "candidate:s2",
	}
	state.connection.AttachReceipt = protocolvalue.AttachReceipt{
		RequestID: "attach-request:s2", SemanticWinnerFingerprint: "winner:s2",
		ReceiptFingerprint: "attach-receipt:s2",
		CurrentBinding: protocolvalue.TransportBinding{
			AttachmentID: "attachment:s2", AttachmentGeneration: 1,
			ConnectionID: "connection:s2", Generation: 1,
			BoundAtUTC: now.Format(time.RFC3339Nano), Fingerprint: bindingFingerprint,
		},
	}
	state.connection.HeartbeatSchedule = HeartbeatScheduleState{
		NextGeneration: 1, NextAt: now.Add(time.Minute), LeaseExpiresAt: now.Add(2 * time.Minute),
	}
	state.attachment = AttachmentState{Valid: true, Identity: protocolvalue.Attachment{
		ID: "attachment:s2", Generation: 1, RuntimeSessionID: "runtime:s2",
		ClientInstanceID: "terminal-client:s2", ConnectionID: "connection:s2",
		BindingGeneration: 1, BindingFingerprint: bindingFingerprint,
		IdentityFingerprint: "attachment-identity:s2", SemanticWinnerFingerprint: "winner:s2",
		ExpiresAt: now.Add(2 * time.Minute),
		Heartbeat: protocolvalue.HeartbeatPolicy{Interval: 10 * time.Second, Grace: 20 * time.Second, MaximumMissedCount: 2},
	}}
	snapshot := testDurableSnapshot("runtime:s2", cells)
	state.durable, err = state.durable.Install(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	state.transcript, err = state.transcript.Install(snapshot, state.layout.Width, state.layout.TranscriptRows)
	if err != nil {
		t.Fatal(err)
	}
	state.control, err = state.control.Install(snapshot.Control, snapshot.RuntimeSessionID)
	if err != nil {
		t.Fatal(err)
	}
	operational := protocolvalue.OperationalSnapshot{
		RequestID: "operational-request:s2", RuntimeSessionID: "runtime:s2",
		AttachmentID: "attachment:s2", AttachmentGeneration: 1,
		AttachmentIdentityFingerprint:  "attachment-identity:s2",
		AcknowledgedBindingFingerprint: bindingFingerprint, Generation: 1,
		FrameFingerprint: testFingerprint("operational:s2"),
	}
	state.operational, err = state.operational.Install(operational, snapshot.RuntimeSessionID)
	if err != nil {
		t.Fatal(err)
	}
	state.pageCache, err = state.pageCache.InstallLatest(snapshot, true, state.attachment.Identity.ID, state.attachment.Identity.Generation)
	if err != nil {
		t.Fatal(err)
	}
	viewport, err := state.pageCache.MaterializeCurrent(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	state.transcript, err = state.transcript.Replace(viewport, state.layout.Width, state.layout.TranscriptRows)
	if err != nil {
		t.Fatal(err)
	}
	state.snapshotLoading = SnapshotLoadingState{
		Phase: SnapshotBaselinesInstalled, AttachmentID: "attachment:s2", AttachmentGeneration: 1,
		TransportBindingFingerprint: bindingFingerprint, DurableSnapshotFingerprint: snapshot.SnapshotFingerprint,
		DurableControlCursorFingerprint: snapshot.Control.CursorFingerprint,
		OperationalSnapshotFingerprint:  operational.FrameFingerprint, OperationalGeneration: 1,
	}
	state.phase = PhaseReady
	if err := state.Validate(); err != nil {
		t.Fatal(err)
	}
	return state
}

func testDurableSnapshot(runtimeSessionID string, cells []protocolvalue.HistoryCell) protocolvalue.DurableSnapshot {
	root := protocolvalue.HistoryRootIdentity{
		RuntimeSessionID:              runtimeSessionID,
		ProjectionContractFingerprint: testFingerprint("projection-contract"),
		MaterializationFingerprint:    testFingerprint("materialization-policy"),
		TreeContractFingerprint:       testFingerprint("tree-contract"),
		PlacementContractID:           "presentation-placement-key",
		PlacementContractVersion:      "v1",
		PlacementContractFingerprint:  testFingerprint("placement-contract"),
		CheckpointGeneration:          1,
		CheckpointFingerprint:         testFingerprint("checkpoint"),
		ProjectionGeneration:          1,
		ProjectionRootFingerprint:     testFingerprint("projection-root"),
		ThroughAuthoritySequence:      1,
		SourceSegmentCount:            1,
		SourcePrefixAccumulator:       testFingerprint("source-prefix"),
		PolicyRegistryFingerprint:     testFingerprint("policy-registry"),
		AuditRegistryFingerprint:      testFingerprint("audit-registry"),
		RootFingerprint:               testFingerprint("root"),
	}
	for index := range cells {
		if cells[index].CellFingerprint == "" {
			cells[index].CellFingerprint = cells[index].Fingerprint
		}
		if cells[index].PlacementKeyFingerprint == "" {
			cells[index].PlacementKeyFingerprint = testFingerprint("placement:" + cells[index].ID)
		}
		if cells[index].DisplayRank == 0 {
			cells[index].DisplayRank = uint64(index + 1)
		}
		if cells[index].RankedFingerprint == "" {
			cells[index].RankedFingerprint = testFingerprint("ranked:" + cells[index].ID)
		}
	}
	head := protocolvalue.ActiveHead{
		RuntimeSessionID:         runtimeSessionID,
		ConfirmedRoot:            root,
		ThroughAuthoritySequence: root.ThroughAuthoritySequence,
		ResidentEntryCount:       uint64(len(cells)),
		ResidentAccumulator:      testFingerprint("resident"),
		Fingerprint:              testFingerprint("head"),
	}
	pair := protocolvalue.RootCursorPair{Root: root, Fingerprint: testFingerprint("root-cursor-pair")}
	return protocolvalue.DurableSnapshot{
		RuntimeSessionID:              runtimeSessionID,
		AuthorityHighWater:            head.ThroughAuthoritySequence,
		ProjectionRevision:            1,
		ProjectionContractFingerprint: root.ProjectionContractFingerprint,
		ActiveHead:                    head,
		ActiveHeadFingerprint:         head.Fingerprint,
		LatestRootCursorPair:          pair,
		Control: protocolvalue.ControlProjection{
			RuntimeSessionID:      runtimeSessionID,
			Generation:            1,
			Revision:              1,
			ProjectionFingerprint: testFingerprint("control-view"),
			ViewFingerprint:       testFingerprint("control-view"),
			TransitionAccumulator: testFingerprint("control-transition"),
			RegistryFingerprint:   testFingerprint("control-registry"),
			CursorFingerprint:     testFingerprint("control-cursor"),
			SnapshotFingerprint:   testFingerprint("control-snapshot"),
		},
		SnapshotFingerprint:       testFingerprint("snapshot"),
		ResidentVectorFingerprint: testFingerprint("resident-vector"),
		ViewportFingerprint:       testFingerprint("viewport"),
		Cells:                     cells,
	}
}
