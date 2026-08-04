package presentation

import (
	"fmt"
	"testing"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func testHistoryRoot(id string, through uint64) protocolvalue.HistoryRootIdentity {
	return protocolvalue.HistoryRootIdentity{
		RuntimeSessionID:              "runtime:test",
		ProjectionContractFingerprint: "projection:contract",
		MaterializationFingerprint:    "materialization:contract",
		TreeContractFingerprint:       "tree:contract",
		PlacementContractID:           "placement-contract",
		PlacementContractVersion:      "v1",
		PlacementContractFingerprint:  "placement:contract",
		CheckpointGeneration:          through,
		CheckpointFingerprint:         "checkpoint:" + id,
		ProjectionGeneration:          through,
		ProjectionRootFingerprint:     "projection-root:" + id,
		ThroughAuthoritySequence:      through,
		SourceSegmentCount:            through,
		SourcePrefixAccumulator:       "source:" + id,
		PolicyRegistryFingerprint:     "policy:registry",
		AuditRegistryFingerprint:      "audit:registry",
		RootFingerprint:               "root:" + id,
	}
}

func testHistoryCell(id string, rank uint64) protocolvalue.HistoryCell {
	return protocolvalue.HistoryCell{
		ID: id, Kind: "assistant", PublicText: id,
		Fingerprint: "entry:" + id, CellFingerprint: "cell:" + id,
		PlacementKeyFingerprint: "placement:" + id,
		DisplayRank:             rank, RankedFingerprint: "ranked:" + id,
	}
}

func testDurableState(t *testing.T, root protocolvalue.HistoryRootIdentity, cells []protocolvalue.HistoryCell, resident string) State {
	t.Helper()
	head := protocolvalue.ActiveHead{
		RuntimeSessionID: "runtime:test", ConfirmedRoot: root,
		ThroughAuthoritySequence: root.ThroughAuthoritySequence,
		ResidentEntryCount:       uint64(len(cells)), ResidentAccumulator: "resident:acc",
		Fingerprint: "head:" + root.RootFingerprint,
	}
	snapshot := protocolvalue.DurableSnapshot{
		RuntimeSessionID: "runtime:test", AuthorityHighWater: head.ThroughAuthoritySequence,
		ProjectionRevision: 1, ProjectionContractFingerprint: "projection:contract",
		ActiveHead: head, ActiveHeadFingerprint: head.Fingerprint,
		LatestRootCursorPair: protocolvalue.RootCursorPair{Root: root, Fingerprint: "pair:" + root.RootFingerprint},
		Control: protocolvalue.ControlProjection{
			RuntimeSessionID: "runtime:test", Generation: 1, Revision: 1,
			ProjectionFingerprint: "control:view", ViewFingerprint: "control:view",
			TransitionAccumulator: "control:transition", RegistryFingerprint: "control:registry",
			CursorFingerprint: "control:cursor",
		},
		SnapshotFingerprint: "snapshot:one", ResidentVectorFingerprint: resident,
		ViewportFingerprint: "viewport:one", Cells: cells,
	}
	state, err := New().Install(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	return state
}

func TestProjectionDeltaAcceptsExactDuplicateAndRejectsRevisionConflict(t *testing.T) {
	root := testHistoryRoot("one", 1)
	state := testDurableState(t, root, []protocolvalue.HistoryCell{testHistoryCell("a", 1)}, "resident:one")
	resultingHead := state.Durable().ActiveHead
	resultingHead.ThroughAuthoritySequence = 2
	resultingHead.ResidentEntryCount = 2
	resultingHead.Fingerprint = "head:two"
	delta := protocolvalue.ProjectionDelta{
		BaseRevision: 1, ResultingRevision: 2, ResultingAuthorityHighWater: 2,
		ResultingActiveHead: resultingHead,
		Changes: []protocolvalue.HistoryChange{{
			Kind: protocolvalue.HistoryChangeUpsert, HistoryEntryID: "b",
			PlacementKeyFingerprint: "placement:b", Resulting: testHistoryCell("b", 2),
		}},
		BaseResidentVectorFingerprint:      "resident:one",
		ResultingResidentVectorFingerprint: "resident:two",
		Fingerprint:                        "delta:two",
	}
	next, err := state.ApplyProjectionDelta(delta)
	if err != nil || next.ProjectionRevision() != 2 || len(next.Durable().Cells) != 2 {
		t.Fatalf("projection delta failed: state=%#v err=%v", next.Durable(), err)
	}
	duplicate, err := next.ApplyProjectionDelta(delta)
	if err != nil || duplicate.SnapshotFingerprint() != next.SnapshotFingerprint() {
		t.Fatalf("exact duplicate was not idempotent: %v", err)
	}
	conflict := delta
	conflict.Fingerprint = "delta:conflict"
	if _, err := next.ApplyProjectionDelta(conflict); err == nil {
		t.Fatal("same-revision projection conflict was accepted")
	}
	overlap := delta
	overlap.ResultingRevision = 3
	overlap.Fingerprint = "delta:overlap"
	if _, err := next.ApplyProjectionDelta(overlap); err == nil {
		t.Fatal("overlapping projection delta was accepted")
	}
}

func TestRootAdvanceRetainsOldPinnedRootAndInstallsNewLatest(t *testing.T) {
	oldRoot := testHistoryRoot("old", 1)
	cell := testHistoryCell("a", 2)
	state := testDurableState(t, oldRoot, []protocolvalue.HistoryCell{cell}, "resident:one")
	cache, err := NewPageCache().InstallLatest(state.Durable(), true, "attachment:test", 1)
	if err != nil {
		t.Fatal(err)
	}
	newRoot := testHistoryRoot("new", 2)
	newHead := state.Durable().ActiveHead
	newHead.ConfirmedRoot = newRoot
	newHead.ThroughAuthoritySequence = 2
	newHead.Fingerprint = "head:new"
	advance := protocolvalue.RootAdvance{
		BaseRevision: 1, ResultingRevision: 2,
		PreviousActiveHeadFingerprint: state.Durable().ActiveHeadFingerprint,
		ResultingActiveHead:           newHead,
		LatestRootCursorPair:          protocolvalue.RootCursorPair{Root: newRoot, Fingerprint: "pair:new"},
		PreviousRoot:                  oldRoot, ResultingRoot: newRoot,
		TransitionKind:            protocolvalue.ResidentTransitionUnchanged,
		BeforeResidentFingerprint: "resident:one", AfterResidentFingerprint: "resident:one",
		ConsumedTailPrefixThroughSequence:       2,
		RetainedTailSuffixFromSequenceExclusive: 2,
		RetainedTailSuffixThroughSequence:       2,
		Fingerprint:                             "root-advance:new",
	}
	next, rebase, err := state.ApplyRootAdvance(advance)
	if err != nil || rebase || next.Durable().LatestRootCursorPair.Root.RootFingerprint != newRoot.RootFingerprint {
		t.Fatalf("root advance failed: rebase=%v err=%v", rebase, err)
	}
	cache, err = cache.InstallLatest(next.Durable(), false, "attachment:test", 1)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := cache.Root(oldRoot.RootFingerprint); !ok {
		t.Fatal("new latest root evicted the pinned predecessor")
	}
	latest, ok := cache.Latest()
	if !ok || latest.Root.RootFingerprint != newRoot.RootFingerprint {
		t.Fatal("new root did not become the sole latest root")
	}
	cache, err = cache.SwitchToLatest()
	if err != nil {
		t.Fatal(err)
	}
	oldPage := protocolvalue.HistoryPageResult{
		Kind: protocolvalue.HistoryPageDataKind, Root: oldRoot, Direction: protocolvalue.HistoryPageBefore,
		Entries: []protocolvalue.HistoryCell{testHistoryCell("older", 0)},
	}
	oldPage.Entries[0].DisplayRank = 1
	cache, err = cache.ApplyPage(oldPage)
	if err != nil {
		t.Fatal(err)
	}
	latest, _ = cache.Latest()
	if latest.Root.RootFingerprint != newRoot.RootFingerprint {
		t.Fatal("old pinned-root page overwrote the latest root pair")
	}
	oldPinned, ok := cache.Root(oldRoot.RootFingerprint)
	if !ok || len(oldPinned.Cells) != 2 || !cache.CurrentIsLatest() {
		t.Fatal("late old-root page was not cached independently of the latest viewport")
	}
}

func TestPageCacheRejectsSameFingerprintDifferentRootPayload(t *testing.T) {
	root := testHistoryRoot("exact-root", 1)
	state := testDurableState(t, root, []protocolvalue.HistoryCell{testHistoryCell("resident", 2)}, "resident:exact-root")
	cache, err := NewPageCache().InstallLatest(state.Durable(), true, "attachment:exact-root", 1)
	if err != nil {
		t.Fatal(err)
	}
	forged := root
	forged.TreeContractFingerprint = "tree:forged"
	if forged.RootFingerprint != root.RootFingerprint {
		t.Fatal("root conflict probe changed the lookup fingerprint")
	}
	if _, err := cache.ApplyPage(protocolvalue.HistoryPageResult{
		Kind: protocolvalue.HistoryPageDataKind, Root: forged, Direction: protocolvalue.HistoryPageBefore,
	}); err == nil {
		t.Fatal("page cache accepted a different root payload under the pinned fingerprint")
	}
}

func TestHistoryPageCacheRejectsServerOrderDrift(t *testing.T) {
	root := testHistoryRoot("ordered", 3)
	state := testDurableState(t, root, []protocolvalue.HistoryCell{testHistoryCell("resident", 10)}, "resident:ordered")
	cache, err := NewPageCache().InstallLatest(state.Durable(), true, "attachment:test", 1)
	if err != nil {
		t.Fatal(err)
	}
	_, err = cache.ApplyPage(protocolvalue.HistoryPageResult{
		Kind: protocolvalue.HistoryPageDataKind,
		Root: root,
		Entries: []protocolvalue.HistoryCell{
			testHistoryCell("later", 2),
			testHistoryCell("earlier", 1),
		},
	})
	if err == nil {
		t.Fatal("history page client silently reordered a malformed server page")
	}
}

func TestHistoryPageCacheMatchesAttachmentLeaseFIFOAndRebinds(t *testing.T) {
	cache := NewPageCache()
	for index := 0; index < maximumPinnedRoots+1; index++ {
		root := testHistoryRoot(fmt.Sprintf("fifo-%d", index), uint64(index+1))
		state := testDurableState(t, root, []protocolvalue.HistoryCell{testHistoryCell(fmt.Sprintf("resident-%d", index), 1)}, fmt.Sprintf("resident:fifo-%d", index))
		var err error
		cache, err = cache.InstallLatest(state.Durable(), index == 0, "attachment:test", 1)
		if err != nil {
			t.Fatal(err)
		}
	}
	if cache.Ready() && !cache.CurrentIsLatest() {
		t.Fatal("client retained a current root after the matching server FIFO lease was released")
	}
	if _, ok := cache.Root("root:fifo-0"); ok {
		t.Fatal("client retained the root evicted from the bounded attachment lease set")
	}

	latest, _ := cache.Latest()
	reboundState := testDurableState(t, latest.Root, latest.Cells, "resident:rebound")
	rebound, err := cache.InstallLatest(reboundState.Durable(), false, "attachment:next", 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(rebound.order) != 1 || rebound.attachmentID != "attachment:next" || rebound.attachmentGeneration != 2 {
		t.Fatal("new semantic attachment inherited predecessor root leases")
	}
}

func TestHydratedViewportSurvivesDurableResidentReplacement(t *testing.T) {
	root := testHistoryRoot("stable-viewport", 7)
	resident := testHistoryCell("resident", 10)
	state := testDurableState(t, root, []protocolvalue.HistoryCell{resident}, "resident:stable")
	cache, err := NewPageCache().InstallLatest(state.Durable(), true, "attachment:test", 1)
	if err != nil {
		t.Fatal(err)
	}
	cache, err = cache.ApplyPage(protocolvalue.HistoryPageResult{
		Kind: protocolvalue.HistoryPageDataKind, Direction: protocolvalue.HistoryPageBefore, Root: root,
		Entries: []protocolvalue.HistoryCell{testHistoryCell("hydrated", 1)},
	})
	if err != nil {
		t.Fatal(err)
	}
	updated := state.Durable()
	updated.Cells = append(updated.Cells, testHistoryCell("new-resident", 11))
	cache, err = cache.InstallLatest(updated, false, "attachment:test", 1)
	if err != nil {
		t.Fatal(err)
	}
	viewport, err := cache.MaterializeCurrent(updated)
	if err != nil {
		t.Fatal(err)
	}
	if len(viewport.Cells) != 3 || viewport.Cells[0].ID != "hydrated" || viewport.Cells[2].ID != "new-resident" {
		t.Fatalf("durable replacement erased or reordered hydrated history: %#v", viewport.Cells)
	}
}

func TestHistoryPageCacheBoundsHydratedWindowPerRoot(t *testing.T) {
	root := testHistoryRoot("bounded", 9)
	resident := make([]protocolvalue.HistoryCell, 0, maximumResidentCellsPerRoot)
	for index := 0; index < maximumResidentCellsPerRoot; index++ {
		resident = append(resident, testHistoryCell(fmt.Sprintf("resident-%03d", index), uint64(1000+index)))
	}
	state := testDurableState(t, root, resident, "resident:bounded")
	cache, err := NewPageCache().InstallLatest(state.Durable(), true, "attachment:test", 1)
	if err != nil {
		t.Fatal(err)
	}
	after := protocolvalue.HistoryCursor{RuntimeSessionID: root.RuntimeSessionID, Root: root, Fingerprint: "cursor:after"}
	for pageIndex, firstRank := range []int{500, 1} {
		entries := make([]protocolvalue.HistoryCell, 0, 256)
		for index := 0; index < 256; index++ {
			entries = append(entries, testHistoryCell(fmt.Sprintf("page-%d-%03d", pageIndex, index), uint64(firstRank+index)))
		}
		cache, err = cache.ApplyPage(protocolvalue.HistoryPageResult{
			Kind: protocolvalue.HistoryPageDataKind, Direction: protocolvalue.HistoryPageBefore, Root: root,
			Entries: entries, AfterCursor: after, HasAfterCursor: true,
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	current, ok := cache.Current()
	if !ok || len(current.Cells) > maximumCachedCellsPerRoot || current.CachedBytes > maximumCachedBytesPerRoot || countHydrated(current) > maximumHydratedCellsPerRoot || hydratedBytes(current) > maximumHydratedBytesPerRoot {
		t.Fatalf("history page cache exceeded its hard bounds: %#v", current)
	}
	if !current.HasMoreAfter || !current.HasAfterCursor {
		t.Fatal("bounded eviction did not preserve the opposite-direction continuation")
	}
}
