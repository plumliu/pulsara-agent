package app

import (
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
	"google.golang.org/protobuf/proto"
)

func testObserveRequest(state AppState) protocolvalue.PreparedObserveRequest {
	durable := state.durable.Durable()
	operational := state.operational.Snapshot()
	return protocolvalue.PreparedObserveRequest{
		RequestID: "observe:s2", AfterAuthorityHighWater: durable.AuthorityHighWater,
		AfterProjectionRevision: durable.ProjectionRevision,
		AfterOperational: protocolvalue.ControlIndependentOperationalCursor{
			Generation: operational.Generation, Cursor: operational.Cursor,
		},
		AfterControl: state.control.ConfirmedCursor(), MaximumWaitMS: 100,
	}
}

func testProjectionDelta(state AppState, request protocolvalue.PreparedObserveRequest) protocolvalue.ProjectionDelta {
	head := state.durable.Durable().ActiveHead
	head.ThroughAuthoritySequence++
	head.ResidentEntryCount++
	head.Fingerprint = testFingerprint("head:s2:delta")
	return protocolvalue.ProjectionDelta{
		RequestID: request.RequestID, BaseRevision: request.AfterProjectionRevision,
		ResultingRevision:           request.AfterProjectionRevision + 1,
		ResultingAuthorityHighWater: request.AfterAuthorityHighWater + 1,
		ResultingActiveHead:         head,
		Changes: []protocolvalue.HistoryChange{{
			Kind: protocolvalue.HistoryChangeUpsert, HistoryEntryID: "entry:delta",
			PlacementKeyFingerprint: testFingerprint("placement:entry:delta"),
			Resulting: protocolvalue.HistoryCell{
				ID: "entry:delta", Kind: "assistant", PublicText: "incremental",
				Fingerprint: testFingerprint("entry:delta"), CellFingerprint: testFingerprint("entry:delta"),
				PlacementKeyFingerprint: testFingerprint("placement:entry:delta"),
				DisplayRank:             2, RankedFingerprint: testFingerprint("ranked:entry:delta"),
			},
		}},
		BaseResidentVectorFingerprint:      state.durable.Durable().ResidentVectorFingerprint,
		ResultingResidentVectorFingerprint: testFingerprint("resident:s2:delta"),
		Fingerprint:                        testFingerprint("projection:s2:delta"),
	}
}

func TestS2ObservationBatchRollsBackAllPlanesOnOperationalMismatch(t *testing.T) {
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:base", Kind: "user", PublicText: "base",
		Fingerprint: testFingerprint("entry:base"),
	}})
	request := testObserveRequest(state)
	batch := protocolvalue.ObservationBatch{
		RequestID: request.RequestID, ProjectionDelta: ptr(testProjectionDelta(state, request)),
		OperationalDelta: &protocolvalue.OperationalDelta{
			RequestID: request.RequestID, Generation: request.AfterOperational.Generation + 1,
			Cursor: 1, Changes: []protocolvalue.OperationalChange{{
				Kind: protocolvalue.OperationalChangeUpsert, Generation: request.AfterOperational.Generation + 1,
				Cursor: 1, CoalesceKey: "model:one", Cell: protocolvalue.OperationalCell{CoalesceKey: "model:one"},
			}}, Fingerprint: testFingerprint("operational:bad"),
		},
		PlaneCount: 2, Fingerprint: testFingerprint("batch:bad-operational"),
	}

	next, effects, err := applyObservationBatch(state, request, batch, time.Now())
	if !errors.Is(err, errOperationalObservation) || effects != nil {
		t.Fatalf("operational mismatch was not isolated: effects=%#v err=%v", effects, err)
	}
	if next.durable.ProjectionRevision() != state.durable.ProjectionRevision() || len(next.durable.Durable().Cells) != 1 {
		t.Fatal("a rejected multi-plane batch partially installed durable state")
	}
}

func TestS2ControlInvalidationAndDurableDeltaInstallAsOneBatch(t *testing.T) {
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:base", Kind: "user", PublicText: "base",
		Fingerprint: testFingerprint("entry:base"),
	}})
	request := testObserveRequest(state)
	latest := request.AfterControl
	latest.Revision++
	latest.ProjectionFingerprint = testFingerprint("control:view:next")
	latest.TransitionAccumulator = testFingerprint("control:transition:next")
	latest.Fingerprint = testFingerprint("control:cursor:next")
	batch := protocolvalue.ObservationBatch{
		RequestID:       request.RequestID,
		ProjectionDelta: ptr(testProjectionDelta(state, request)),
		ControlChange: &protocolvalue.ControlChange{
			RequestID: request.RequestID, ValidatedAfterFingerprint: request.AfterControl.Fingerprint,
			BaseRevision:              request.AfterControl.Revision,
			BaseProjectionFingerprint: request.AfterControl.ProjectionFingerprint,
			ResultingCursor:           latest, ConsumedTransitionCount: 1,
			RangeAccumulator: testFingerprint("control:range"), Fingerprint: testFingerprint("control:change"),
		},
		PlaneCount: 2, Fingerprint: testFingerprint("batch:control-durable"),
	}

	next, effects, err := applyObservationBatch(state, request, batch, time.Now())
	if err != nil || len(effects) != 1 {
		t.Fatalf("control/durable batch failed: effects=%#v err=%v", effects, err)
	}
	if _, ok := effects[0].(RequestSnapshotEffect); !ok {
		t.Fatalf("control invalidation did not request its bounded snapshot: %#v", effects[0])
	}
	if next.durable.ProjectionRevision() != request.AfterProjectionRevision+1 || next.control.Ready() || next.phase != PhaseReadOnly {
		t.Fatal("control invalidation and durable successor were not atomically installed")
	}
	observed, ok := next.control.ObservedLatestCursor()
	if !ok || observed.Fingerprint != latest.Fingerprint {
		t.Fatal("control invalidation lost its observed latest cursor")
	}
}

func TestS2DurableAndOperationalGapsUseIndependentRebuilds(t *testing.T) {
	base := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:base", Kind: "user", PublicText: "base",
		Fingerprint: testFingerprint("entry:base"),
	}})
	request := testObserveRequest(base)
	durable, effects, err := applyObservationBatch(base, request, protocolvalue.ObservationBatch{
		RequestID:  request.RequestID,
		DurableGap: &protocolvalue.DurableGap{RequestID: request.RequestID, Fingerprint: testFingerprint("gap:durable")},
		PlaneCount: 1, Fingerprint: testFingerprint("batch:durable-gap"),
	}, time.Now())
	if err != nil || len(effects) != 1 || !durable.durable.Stale() || !durable.operational.Stale() {
		t.Fatalf("durable GAP did not rebuild both dependent baselines: %#v %v", effects, err)
	}
	if _, ok := effects[0].(RequestSnapshotEffect); !ok {
		t.Fatalf("durable GAP emitted the wrong effect: %#v", effects[0])
	}

	operational, effects, err := applyObservationBatch(base, request, protocolvalue.ObservationBatch{
		RequestID:      request.RequestID,
		OperationalGap: &protocolvalue.OperationalGap{RequestID: request.RequestID, Fingerprint: testFingerprint("gap:operational")},
		PlaneCount:     1, Fingerprint: testFingerprint("batch:operational-gap"),
	}, time.Now())
	if err != nil || len(effects) != 1 || operational.durable.Stale() || !operational.operational.Stale() {
		t.Fatalf("operational GAP was not isolated: %#v %v", effects, err)
	}
	if _, ok := effects[0].(RequestOperationalSnapshotEffect); !ok {
		t.Fatalf("operational GAP emitted the wrong effect: %#v", effects[0])
	}
}

func TestS2ControlInvalidationAndOperationalGapShareOneRecoveryPlan(t *testing.T) {
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:base", Kind: "user", PublicText: "base", Fingerprint: testFingerprint("entry:base"),
	}})
	request := testObserveRequest(state)
	latest := request.AfterControl
	latest.Revision++
	latest.ProjectionFingerprint = testFingerprint("control:view:combined")
	latest.TransitionAccumulator = testFingerprint("control:transition:combined")
	latest.Fingerprint = testFingerprint("control:cursor:combined")
	batch := protocolvalue.ObservationBatch{
		RequestID: request.RequestID,
		ControlChange: &protocolvalue.ControlChange{
			RequestID: request.RequestID, ValidatedAfterFingerprint: request.AfterControl.Fingerprint,
			BaseRevision: request.AfterControl.Revision, BaseProjectionFingerprint: request.AfterControl.ProjectionFingerprint,
			ResultingCursor: latest, ConsumedTransitionCount: 1,
			RangeAccumulator: testFingerprint("control:range:combined"), Fingerprint: testFingerprint("control:change:combined"),
		},
		OperationalGap: &protocolvalue.OperationalGap{
			RequestID: request.RequestID, LatestGeneration: request.AfterOperational.Generation,
			LatestCursor: request.AfterOperational.Cursor + 1, Fingerprint: testFingerprint("gap:operational:combined"),
		},
		PlaneCount: 2, Fingerprint: testFingerprint("batch:combined-recovery"),
	}

	next, effects, err := applyObservationBatch(state, request, batch, time.Now())
	if err != nil || len(effects) != 1 {
		t.Fatalf("combined recovery failed: effects=%#v err=%v", effects, err)
	}
	if _, ok := effects[0].(RequestSnapshotEffect); !ok {
		t.Fatalf("combined recovery emitted the wrong first effect: %#v", effects[0])
	}
	if next.phase != PhaseReadOnly || next.control.Ready() || !next.operational.Stale() || !next.snapshotLoading.OperationalRequired {
		t.Fatal("combined recovery did not atomically preserve both rebuild requirements")
	}
}

func TestS2ScrollToZeroResolvesUnseenCellsAgainstLatestRoot(t *testing.T) {
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:long", Kind: "assistant", PublicText: strings.Repeat("tool output ", 400),
		Fingerprint: testFingerprint("entry:long"),
	}})
	state.transcript = state.transcript.Scroll(2)
	request := testObserveRequest(state)
	var err error
	state, _, err = applyObservationBatch(state, request, protocolvalue.ObservationBatch{
		RequestID:       request.RequestID,
		ProjectionDelta: ptr(testProjectionDelta(state, request)),
		PlaneCount:      1,
		Fingerprint:     testFingerprint("batch:scrolled-unseen"),
	}, time.Now())
	if err != nil {
		t.Fatalf("durable delta did not install over a scrolled viewport: %v", err)
	}
	if state.transcript.FollowTail() || state.transcript.UnseenTerminalCount() == 0 {
		t.Fatal("durable delta did not create the scrolled unseen state")
	}
	if err := state.Validate(); err != nil {
		t.Fatalf("valid scrolled/unseen setup rejected: %v", err)
	}
	pageDown, err := NewNormalizedKey(KeyPageDown, 0, "", false)
	if err != nil {
		t.Fatal(err)
	}
	next, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: pageDown})
	if next.hasPublicFailure || next.phase == PhaseFatal {
		t.Fatalf("scroll-to-tail entered fatal state: %s", next.publicFailure.message)
	}
	if len(effects) != 0 || next.transcript.ScrollOffset() != 0 || !next.transcript.FollowTail() || next.transcript.UnseenTerminalCount() != 0 {
		t.Fatalf("latest-root tail resolution drifted: effects=%#v offset=%d follow=%v unseen=%d", effects, next.transcript.ScrollOffset(), next.transcript.FollowTail(), next.transcript.UnseenTerminalCount())
	}
}

func TestS2PageUpPreparesLosslessMaximumHistoryRequest(t *testing.T) {
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:resident", Kind: "assistant", PublicText: "resident", Fingerprint: testFingerprint("entry:resident"),
		DisplayRank: 100, RankedFingerprint: testFingerprint("ranked:resident"),
	}})
	snapshot := state.durable.Durable()
	root := snapshot.LatestRootCursorPair.Root
	placement := protocolvalue.HistoryPlacementKey{
		ContractID: "presentation-placement-key", ContractVersion: "v1", ContractFingerprint: root.PlacementContractFingerprint,
		HasLeftCoordinate: true, LeftCoordinate: 1,
		RelativePosition:     protocol.PresentationRelativePositionKind_PRESENTATION_RELATIVE_POSITION_CANONICAL_LEAF,
		SourceLedgerSequence: 1, StableSourceTiebreaker: "entry:resident", CanonicalComparableKey: string([]byte{0, 1, 2}),
		Fingerprint: testFingerprint("placement:cursor"),
	}
	cursor := protocolvalue.HistoryCursor{
		RuntimeSessionID: snapshot.RuntimeSessionID, Root: root, AnchorHistoryEntryID: "entry:resident",
		AnchorPlacementKey: placement, HasAnchor: true, Fingerprint: testFingerprint("history:before-cursor"),
	}
	snapshot.LatestRootCursorPair.Before = cursor
	snapshot.LatestRootCursorPair.HasBefore = true
	snapshot.Control = state.control.Projection()
	var err error
	state.durable, err = state.durable.Install(snapshot)
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
	pageUp, err := NewNormalizedKey(KeyPageUp, 0, "", false)
	if err != nil {
		t.Fatal(err)
	}
	next, effects, _ := state.update(KeyInputMsg{Header: testLocalMessageHeader(t, 1), Key: pageUp})
	if next.hasPublicFailure || len(effects) != 1 {
		t.Fatalf("real PageUp entered failure or emitted no request: failure=%v effects=%#v", next.hasPublicFailure, effects)
	}
	page, ok := effects[0].(ReadHistoryPageEffect)
	if !ok {
		t.Fatalf("PageUp emitted the wrong effect: %#v", effects[0])
	}
	if page.Request.MaximumCells != protocolvalue.MaximumHistoryPageCells || page.Request.MaximumDecodedBytes != protocolvalue.MaximumHistoryPageDecodedBytes {
		t.Fatal("PageUp did not use the negotiated history-page maximum")
	}
	wireRequest, err := page.Request.ToProto()
	if err != nil || wireRequest.Cursor.RootIdentity.PlacementKeyContractId != root.PlacementContractID || string(wireRequest.Cursor.AnchorPlacementKey.CanonicalComparableKeyBytes) != placement.CanonicalComparableKey {
		t.Fatalf("PageUp cursor was not losslessly re-encoded: request=%#v err=%v", wireRequest, err)
	}

	entries := make([]protocolvalue.HistoryCell, 0, 40)
	for index := 0; index < 40; index++ {
		entries = append(entries, protocolvalue.HistoryCell{
			ID: fmt.Sprintf("entry:older:%02d", index), Kind: "assistant", PublicText: fmt.Sprintf("older-%02d", index),
			Fingerprint: fmt.Sprintf("sha256:%064x", index+1000), CellFingerprint: fmt.Sprintf("sha256:%064x", index+2000),
			PlacementKeyFingerprint: fmt.Sprintf("sha256:%064x", index+3000), DisplayRank: uint64(index + 1),
			RankedFingerprint: fmt.Sprintf("sha256:%064x", index+4000),
		})
	}
	applied, _, err := applyHistoryPageResult(next, page.Request, protocolvalue.HistoryPageResult{
		Kind: protocolvalue.HistoryPageDataKind, RequestID: page.Request.RequestID,
		RequestedCursorFingerprint: page.Request.Cursor.Fingerprint, Root: root,
		Direction: protocolvalue.HistoryPageBefore, Entries: entries,
	}, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	rendered := strings.Join(applied.transcript.RenderLines(), "\n")
	if !strings.Contains(rendered, "older-") || strings.Contains(rendered, "resident") {
		t.Fatalf("completed PageUp hydrated history but left the new page outside the visible viewport: %q", rendered)
	}
	resized, _, _ := applied.update(ResizeMsg{Header: testLocalMessageHeader(t, 2), Width: 60, Height: 24})
	resizedRoot, ok := resized.pageCache.Current()
	if resized.hasPublicFailure || !ok || len(resizedRoot.Cells) != 41 || resized.transcript.WrappedRowCount() <= 2 {
		t.Fatalf("terminal width change rebuilt the viewport from the resident-only durable snapshot: failure=%v message=%q ok=%v cells=%d rows=%d", resized.hasPublicFailure, resized.publicFailure.message, ok, len(resizedRoot.Cells), resized.transcript.WrappedRowCount())
	}

	end, err := NewNormalizedKey(KeyEnd, 0, "", false)
	if err != nil {
		t.Fatal(err)
	}
	late, _, _ := next.update(KeyInputMsg{Header: testLocalMessageHeader(t, 2), Key: end})
	late, _, err = applyHistoryPageResult(late, page.Request, protocolvalue.HistoryPageResult{
		Kind: protocolvalue.HistoryPageDataKind, RequestID: page.Request.RequestID,
		RequestedCursorFingerprint: page.Request.Cursor.Fingerprint, Root: root,
		Direction: protocolvalue.HistoryPageBefore, Entries: entries,
	}, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	lateRendered := strings.Join(late.transcript.RenderLines(), "\n")
	if !strings.Contains(lateRendered, "resident") {
		t.Fatalf("a late page response overrode a newer End viewport intent: %q", lateRendered)
	}
}

func TestHistoryPageMessageRevalidatesExactRequestBounds(t *testing.T) {
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:resident", Kind: "assistant", PublicText: "resident", Fingerprint: testFingerprint("entry:resident"),
	}})
	root := state.durable.Durable().LatestRootCursorPair.Root
	state, token := state.nextWire(OpHistoryPage, time.Now().Add(time.Second))
	request := protocolvalue.PreparedHistoryPageRequest{
		RequestID: token.RequestID, RuntimeSessionID: root.RuntimeSessionID,
		Cursor:    protocolvalue.HistoryCursor{RuntimeSessionID: root.RuntimeSessionID, Root: root, Fingerprint: testFingerprint("cursor:page-bound")},
		Direction: protocolvalue.HistoryPageBefore, MaximumCells: 2, MaximumDecodedBytes: 1024 * 1024,
		ProjectionContract: root.ProjectionContractFingerprint, ViewportIntentGeneration: 1,
	}
	countResult := testDecodedHistoryPageResult(t, request, 3, 0, nil)
	if err := (HistoryPageAcceptedMsg{Header: ioHeader(token, countResult.Fingerprint), Request: request, Result: countResult}).validate(); err == nil {
		t.Fatal("history page response with too many full entries was accepted")
	}

	request.MaximumCells = 2
	request.MaximumDecodedBytes = 256
	metadataResult := testDecodedHistoryPageResult(t, request, 1, 2048, nil)
	if metadataResult.DecodedCarrierBytes() <= uint64(request.MaximumDecodedBytes) {
		t.Fatal("full carrier accounting ignored large non-public metadata")
	}
	if err := (HistoryPageAcceptedMsg{Header: ioHeader(token, metadataResult.Fingerprint), Request: request, Result: metadataResult}).validate(); err == nil {
		t.Fatal("history page response with oversized metadata was accepted")
	}

	mutatedRootResult := testDecodedHistoryPageResult(t, request, 1, 0, func(page *protocol.HistoryPageData) {
		page.ValidatedRootIdentity.TreeContractFingerprint = testFingerprint("tree:forged")
	})
	if mutatedRootResult.Root.RootFingerprint != request.Cursor.Root.RootFingerprint {
		t.Fatal("root mutation probe changed the fingerprint instead of only its payload")
	}
	if err := (HistoryPageAcceptedMsg{Header: ioHeader(token, mutatedRootResult.Fingerprint), Request: request, Result: mutatedRootResult}).validate(); err == nil {
		t.Fatal("history page response with same root fingerprint and different payload was accepted")
	}
}

func testDecodedHistoryPageResult(
	t *testing.T,
	request protocolvalue.PreparedHistoryPageRequest,
	entryCount int,
	metadataBytes int,
	mutate func(*protocol.HistoryPageData),
) protocolvalue.HistoryPageResult {
	t.Helper()
	wireRequest, err := request.ToProto()
	if err != nil {
		t.Fatal(err)
	}
	page := &protocol.HistoryPageData{
		RequestId: request.RequestID, ValidatedInputCursorFingerprint: request.Cursor.Fingerprint,
		ValidatedRequestDirection:      request.Direction,
		ValidatedRootIdentity:          proto.Clone(wireRequest.Cursor.RootIdentity).(*protocol.PresentationHistoryRootIdentity),
		OrderedHistoryEntryAccumulator: testFingerprint("page:entries"),
		ContinuityProofFingerprint:     testFingerprint("page:continuity"),
		ResponseFingerprint:            testFingerprint("page:response"),
	}
	for index := 0; index < entryCount; index++ {
		id := fmt.Sprintf("history:bounded:%03d", index)
		page.OrderedHistoryEntries = append(page.OrderedHistoryEntries, &protocol.PresentationHistoryRankedEntry{
			Entry: &protocol.PresentationHistoryEntry{
				RuntimeSessionId: request.RuntimeSessionID, HistoryEntryId: id,
				PlacementKey: &protocol.PresentationHistoryPlacementKey{
					PlacementKeyContractId: request.Cursor.Root.PlacementContractID, PlacementKeyContractVersion: request.Cursor.Root.PlacementContractVersion,
					PlacementKeyContractFingerprint: request.Cursor.Root.PlacementContractFingerprint,
					RelativePositionKind:            protocol.PresentationRelativePositionKind_PRESENTATION_RELATIVE_POSITION_CANONICAL_LEAF,
					StableSourceTiebreaker:          id + strings.Repeat("m", metadataBytes), CanonicalComparableKeyBytes: []byte(id),
					PlacementKeyFingerprint: testFingerprint(fmt.Sprintf("placement:%d", index)),
				},
				DurableHistoryCell: &protocol.DurableHistoryCell{Cell: &protocol.DurableHistoryCell_AssistantMessage{AssistantMessage: &protocol.AssistantMessageCell{Common: &protocol.DurableCellCommon{
					StableCellId: id, SemanticRevision: 1, SourceAccumulator: testFingerprint("source"),
					VisibilityPolicy: protocol.PresentationVisibilityPolicy_PRESENTATION_VISIBILITY_NORMAL,
					CellFingerprint:  testFingerprint(fmt.Sprintf("cell:%d", index)),
				}}}},
				EntryFingerprint: testFingerprint(fmt.Sprintf("entry:%d", index)),
			},
			RootLocalDisplayRank:  uint64(index + 1),
			RankedViewFingerprint: testFingerprint(fmt.Sprintf("ranked:%d", index)),
		})
	}
	if mutate != nil {
		mutate(page)
	}
	result, err := protocolvalue.HistoryPageFromProto(&protocol.HistoryPageResponse{Outcome: &protocol.HistoryPageResponse_Page{Page: page}})
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func TestS2UpdateKeepsReadOnlyScreenValidWhileControlSnapshotIsInFlight(t *testing.T) {
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:base", Kind: "user", PublicText: "base",
		Fingerprint: testFingerprint("entry:base"),
	}})
	state, token := state.nextWire(OpObserve, time.Now().Add(time.Second))
	request := testObserveRequest(state)
	request.RequestID = token.RequestID
	latest := request.AfterControl
	latest.Revision++
	latest.ProjectionFingerprint = testFingerprint("control:view:update-next")
	latest.TransitionAccumulator = testFingerprint("control:transition:update-next")
	latest.Fingerprint = testFingerprint("control:cursor:update-next")
	batch := protocolvalue.ObservationBatch{
		RequestID: request.RequestID,
		ControlChange: &protocolvalue.ControlChange{
			RequestID: request.RequestID, ValidatedAfterFingerprint: request.AfterControl.Fingerprint,
			BaseRevision: request.AfterControl.Revision, BaseProjectionFingerprint: request.AfterControl.ProjectionFingerprint,
			ResultingCursor: latest, ConsumedTransitionCount: 1,
			RangeAccumulator: testFingerprint("control:range:update"), Fingerprint: testFingerprint("control:change:update"),
		},
		PlaneCount: 1, Fingerprint: testFingerprint("batch:control:update"),
	}

	next, effects, _ := state.update(ObservationBatchMsg{
		Header: ioHeader(token, batch.Fingerprint), Request: request, Batch: batch,
	})
	if err := next.Validate(); err != nil {
		t.Fatal(err)
	}
	if next.phase != PhaseReadOnly || next.snapshotLoading.Phase != SnapshotAwaitingDurableSnapshot || next.hasPublicFailure || len(effects) != 1 {
		t.Fatalf("control rebuild did not preserve a valid read-only screen: phase=%v loading=%v failure=%v effects=%#v", next.phase, next.snapshotLoading.Phase, next.hasPublicFailure, effects)
	}
	if _, ok := effects[0].(RequestSnapshotEffect); !ok {
		t.Fatalf("control rebuild emitted the wrong effect: %#v", effects[0])
	}
}

func TestS2UpdateKeepsReadOnlyScreenValidWhileOperationalSnapshotIsInFlight(t *testing.T) {
	state := testReadyS2State(t, []protocolvalue.HistoryCell{{
		ID: "entry:base", Kind: "user", PublicText: "base",
		Fingerprint: testFingerprint("entry:base"),
	}})
	state, token := state.nextWire(OpObserve, time.Now().Add(time.Second))
	request := testObserveRequest(state)
	request.RequestID = token.RequestID
	batch := protocolvalue.ObservationBatch{
		RequestID: request.RequestID,
		OperationalGap: &protocolvalue.OperationalGap{
			RequestID: request.RequestID, LatestGeneration: request.AfterOperational.Generation,
			LatestCursor: request.AfterOperational.Cursor + 1, Fingerprint: testFingerprint("gap:operational:update"),
		},
		PlaneCount: 1, Fingerprint: testFingerprint("batch:operational-gap:update"),
	}

	next, effects, _ := state.update(ObservationBatchMsg{
		Header: ioHeader(token, batch.Fingerprint), Request: request, Batch: batch,
	})
	if err := next.Validate(); err != nil {
		t.Fatal(err)
	}
	if next.phase != PhaseReadOnly || next.snapshotLoading.Phase != SnapshotAwaitingOperationalSnapshot || next.hasPublicFailure || len(effects) != 1 {
		t.Fatalf("operational rebuild did not preserve a valid read-only screen: phase=%v loading=%v failure=%v effects=%#v", next.phase, next.snapshotLoading.Phase, next.hasPublicFailure, effects)
	}
	if _, ok := effects[0].(RequestOperationalSnapshotEffect); !ok {
		t.Fatalf("operational rebuild emitted the wrong effect: %#v", effects[0])
	}
}

func ptr[T any](value T) *T { return &value }
