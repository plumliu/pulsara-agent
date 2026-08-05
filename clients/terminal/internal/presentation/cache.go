package presentation

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

const (
	maximumPinnedRoots          = int(protocolvalue.MaximumPinnedHistoryRoots)
	maximumResidentCellsPerRoot = 200
	maximumResidentBytesPerRoot = uint64(protocolvalue.MaximumFrameBytes)
	maximumHydratedCellsPerRoot = 312
	maximumHydratedBytesPerRoot = uint64(protocolvalue.MaximumFrameBytes)
	maximumCachedCellsPerRoot   = maximumResidentCellsPerRoot + maximumHydratedCellsPerRoot
	maximumCachedBytesPerRoot   = maximumResidentBytesPerRoot + maximumHydratedBytesPerRoot
)

// RootPageState is a bounded client-local materialization of one immutable
// Foundation root. ResidentIDs distinguish the latest server-owned viewport
// from page-hydrated cells so an incremental durable transition can replace
// the resident suffix without discarding the hydrated window.
type RootPageState struct {
	Root                       protocolvalue.HistoryRootIdentity
	Cells                      []protocolvalue.HistoryCell
	ResidentIDs                []string
	BeforeCursor               protocolvalue.HistoryCursor
	HasBeforeCursor            bool
	AfterCursor                protocolvalue.HistoryCursor
	HasAfterCursor             bool
	ResidentBeforeCursor       protocolvalue.HistoryCursor
	HasResidentBeforeCursor    bool
	ResidentAfterCursor        protocolvalue.HistoryCursor
	HasResidentAfterCursor     bool
	ResidentHasMoreBefore      bool
	HasMoreBefore              bool
	HasMoreAfter               bool
	CachedBytes                uint64
	MaterializationFingerprint string
	Reconciliation             bool
	ReconciliationOwner        string
}

// PageCache is the only owner of latest-root versus currently displayed-root
// identity. The transcript component owns visual scrolling; this cache owns
// which bounded immutable root/vector that scrolling renders.
type PageCache struct {
	roots                map[string]RootPageState
	order                []string
	latest               string
	current              string
	attachmentID         string
	attachmentGeneration uint64
}

func NewPageCache() PageCache { return PageCache{roots: map[string]RootPageState{}} }

func (c PageCache) InstallLatest(snapshot protocolvalue.DurableSnapshot, followLatest bool, attachmentID string, attachmentGeneration uint64) (PageCache, error) {
	root := snapshot.LatestRootCursorPair.Root
	if root.RootFingerprint == "" || attachmentID == "" || attachmentGeneration == 0 || len(snapshot.Cells) > maximumResidentCellsPerRoot || cachedBytes(snapshot.Cells) > maximumResidentBytesPerRoot {
		return PageCache{}, errors.New("history cache latest root is invalid")
	}
	next := c.clone()
	if next.attachmentID != "" && (next.attachmentID != attachmentID || next.attachmentGeneration != attachmentGeneration) {
		next = NewPageCache()
	}
	next.attachmentID, next.attachmentGeneration = attachmentID, attachmentGeneration
	state, exists := next.roots[root.RootFingerprint]
	if !exists {
		state = RootPageState{Root: root}
	}

	oldResident := make(map[string]struct{}, len(state.ResidentIDs))
	for _, id := range state.ResidentIDs {
		oldResident[id] = struct{}{}
	}
	byID := make(map[string]protocolvalue.HistoryCell, len(state.Cells)+len(snapshot.Cells))
	for _, cell := range state.Cells {
		if _, wasResident := oldResident[cell.ID]; !wasResident {
			byID[cell.ID] = cell
		}
	}
	state.ResidentIDs = state.ResidentIDs[:0]
	for _, cell := range snapshot.Cells {
		if previous, present := byID[cell.ID]; present && previous.Fingerprint != cell.Fingerprint {
			return PageCache{}, errors.New("resident history overlaps with conflicting page entry")
		}
		byID[cell.ID] = cell
		state.ResidentIDs = append(state.ResidentIDs, cell.ID)
	}
	state.Cells = cellsFromMap(byID)
	state.Root = root
	state.ResidentBeforeCursor, state.HasResidentBeforeCursor = snapshot.LatestRootCursorPair.Before, snapshot.LatestRootCursorPair.HasBefore
	state.ResidentAfterCursor, state.HasResidentAfterCursor = snapshot.LatestRootCursorPair.After, snapshot.LatestRootCursorPair.HasAfter
	// A durable snapshot is always the newest resident suffix of its root.
	// Cursor presence identifies an anchor; it does not prove that another
	// entry exists on that side. In particular, Foundation intentionally emits
	// an after cursor for the newest resident cell. Treating that cursor as
	// "has more after" pins the client to the predecessor root forever.
	state.ResidentHasMoreBefore = len(snapshot.Cells) > 0 && snapshot.Cells[0].DisplayRank > 0 && state.HasResidentBeforeCursor
	if countHydrated(state) == 0 {
		state.BeforeCursor, state.HasBeforeCursor = state.ResidentBeforeCursor, state.HasResidentBeforeCursor
		state.AfterCursor, state.HasAfterCursor = state.ResidentAfterCursor, state.HasResidentAfterCursor
		state.HasMoreBefore, state.HasMoreAfter = state.ResidentHasMoreBefore, false
	}
	if err := finalizeRootState(&state); err != nil {
		return PageCache{}, err
	}
	next.latest = root.RootFingerprint
	if next.current == "" || followLatest {
		next.current = root.RootFingerprint
	}
	next.put(root.RootFingerprint, state)
	return next, next.Validate()
}

func (c PageCache) ApplyPage(value protocolvalue.HistoryPageResult) (PageCache, error) {
	if value.Kind != protocolvalue.HistoryPageDataKind || value.Root.RootFingerprint == "" {
		return PageCache{}, errors.New("history page result has no target root")
	}
	next := c.clone()
	state, ok := next.roots[value.Root.RootFingerprint]
	if !ok {
		return PageCache{}, errors.New("history page root is not pinned")
	}
	if state.Root != value.Root {
		return PageCache{}, errors.New("history page root identity conflicts with the pinned authority")
	}
	for index := 1; index < len(value.Entries); index++ {
		if value.Entries[index-1].DisplayRank >= value.Entries[index].DisplayRank {
			return PageCache{}, errors.New("history page server order is invalid")
		}
	}
	byID := make(map[string]protocolvalue.HistoryCell, len(state.Cells)+len(value.Entries))
	for _, cell := range state.Cells {
		byID[cell.ID] = cell
	}
	newEntries := make([]protocolvalue.HistoryCell, 0, len(value.Entries))
	pageSeen := make(map[string]struct{}, len(value.Entries))
	for _, cell := range value.Entries {
		if _, duplicate := pageSeen[cell.ID]; duplicate {
			return PageCache{}, errors.New("history page contains a duplicate entry")
		}
		pageSeen[cell.ID] = struct{}{}
		if previous, exists := byID[cell.ID]; exists && previous.Fingerprint != cell.Fingerprint {
			return PageCache{}, errors.New("history page overlaps with conflicting entry")
		}
		if _, exists := byID[cell.ID]; exists {
			continue
		}
		byID[cell.ID] = cell
		newEntries = append(newEntries, cell)
	}
	state.Root = value.Root
	switch value.Direction {
	case protocolvalue.HistoryPageBefore:
		state.Cells = append(newEntries, state.Cells...)
		state.BeforeCursor, state.HasBeforeCursor = value.BeforeCursor, value.HasBeforeCursor
		state.HasMoreBefore = value.HasMoreBefore
	case protocolvalue.HistoryPageAfter:
		state.Cells = append(state.Cells, newEntries...)
		state.AfterCursor, state.HasAfterCursor = value.AfterCursor, value.HasAfterCursor
		state.HasMoreAfter = value.HasMoreAfter
	default:
		return PageCache{}, errors.New("history page direction is invalid")
	}
	if err := trimHydratedWindow(&state, value); err != nil {
		return PageCache{}, err
	}
	state.Reconciliation, state.ReconciliationOwner = false, ""
	if err := finalizeRootState(&state); err != nil {
		return PageCache{}, err
	}
	next.put(value.Root.RootFingerprint, state)
	return next, next.Validate()
}

func (c PageCache) SwitchToLatest() (PageCache, error) {
	if c.latest == "" {
		return PageCache{}, errors.New("history latest root is unavailable")
	}
	next := c.clone()
	state := next.roots[next.latest]
	resident := make(map[string]struct{}, len(state.ResidentIDs))
	for _, id := range state.ResidentIDs {
		resident[id] = struct{}{}
	}
	filtered := state.Cells[:0]
	for _, cell := range state.Cells {
		if _, ok := resident[cell.ID]; ok {
			filtered = append(filtered, cell)
		}
	}
	state.Cells = filtered
	state.BeforeCursor, state.HasBeforeCursor = state.ResidentBeforeCursor, state.HasResidentBeforeCursor
	state.AfterCursor, state.HasAfterCursor = state.ResidentAfterCursor, state.HasResidentAfterCursor
	state.HasMoreBefore, state.HasMoreAfter = state.ResidentHasMoreBefore, false
	if err := finalizeRootState(&state); err != nil {
		return PageCache{}, err
	}
	next.roots[next.latest] = state
	next.current = next.latest
	return next, next.Validate()
}

func (c PageCache) MaterializeCurrent(base protocolvalue.DurableSnapshot) (protocolvalue.DurableSnapshot, error) {
	state, ok := c.roots[c.current]
	if !ok || state.Root.RuntimeSessionID != base.RuntimeSessionID || state.MaterializationFingerprint == "" {
		return protocolvalue.DurableSnapshot{}, errors.New("history current viewport materialization is unavailable")
	}
	result := base
	result.Cells = cloneHistoryCells(state.Cells)
	result.SnapshotFingerprint = state.MaterializationFingerprint
	result.ViewportFingerprint = state.MaterializationFingerprint
	return result, nil
}

func (c PageCache) MarkReconciliation(rootFingerprint, owner string) PageCache {
	next := c.clone()
	state, ok := next.roots[rootFingerprint]
	if ok {
		state.Reconciliation, state.ReconciliationOwner = true, owner
		next.roots[rootFingerprint] = state
	}
	return next
}

func (c PageCache) DropRoot(rootFingerprint string) PageCache {
	next := c.clone()
	if next.latest == rootFingerprint {
		return NewPageCache()
	}
	delete(next.roots, rootFingerprint)
	filtered := next.order[:0]
	for _, value := range next.order {
		if value != rootFingerprint {
			filtered = append(filtered, value)
		}
	}
	next.order = filtered
	if next.current == rootFingerprint {
		next.current = next.latest
	}
	if len(next.roots) == 0 {
		return NewPageCache()
	}
	return next
}

func (c PageCache) Root(rootFingerprint string) (RootPageState, bool) {
	value, ok := c.roots[rootFingerprint]
	value.Cells = cloneHistoryCells(value.Cells)
	value.ResidentIDs = append([]string(nil), value.ResidentIDs...)
	return value, ok
}

func (c PageCache) Latest() (RootPageState, bool)  { return c.Root(c.latest) }
func (c PageCache) Current() (RootPageState, bool) { return c.Root(c.current) }
func (c PageCache) Ready() bool                    { return c.latest != "" && c.current != "" }
func (c PageCache) CurrentIsLatest() bool          { return c.current != "" && c.current == c.latest }
func (c PageCache) CurrentMaterializationFingerprint() string {
	state, _ := c.roots[c.current]
	return state.MaterializationFingerprint
}
func (c PageCache) ShouldFollowLatest(viewAtTail bool) bool {
	state, ok := c.roots[c.current]
	return ok && c.current == c.latest && viewAtTail && !state.HasMoreAfter
}

func (c PageCache) CountNewLatestCells(snapshot protocolvalue.DurableSnapshot) uint32 {
	state, ok := c.roots[c.latest]
	if !ok {
		return uint32(len(snapshot.Cells))
	}
	known := make(map[string]struct{}, len(state.ResidentIDs))
	for _, id := range state.ResidentIDs {
		known[id] = struct{}{}
	}
	var count uint32
	for _, cell := range snapshot.Cells {
		if _, exists := known[cell.ID]; !exists {
			count++
		}
	}
	return count
}

func (c PageCache) ValidateAgainstDurable(snapshot protocolvalue.DurableSnapshot) error {
	if !c.Ready() || c.latest != snapshot.LatestRootCursorPair.Root.RootFingerprint {
		return errors.New("history page cache latest root diverges from durable authority")
	}
	state := c.roots[c.latest]
	if state.Root != snapshot.LatestRootCursorPair.Root || len(state.ResidentIDs) != len(snapshot.Cells) {
		return errors.New("history page cache resident authority is stale")
	}
	byID := make(map[string]protocolvalue.HistoryCell, len(state.Cells))
	for _, cell := range state.Cells {
		byID[cell.ID] = cell
	}
	for index, id := range state.ResidentIDs {
		if id != snapshot.Cells[index].ID || byID[id].Fingerprint != snapshot.Cells[index].Fingerprint {
			return errors.New("history page cache resident vector diverges from durable authority")
		}
	}
	return nil
}

func (c PageCache) Validate() error {
	if len(c.roots) > maximumPinnedRoots || len(c.order) != len(c.roots) || (len(c.roots) == 0) != (c.latest == "" && c.current == "") {
		return errors.New("history page cache bound is invalid")
	}
	if (len(c.roots) == 0) != (c.attachmentID == "" && c.attachmentGeneration == 0) {
		return errors.New("history page cache attachment binding is invalid")
	}
	seen := map[string]bool{}
	for _, key := range c.order {
		if key == "" || seen[key] {
			return errors.New("history page cache order is invalid")
		}
		seen[key] = true
		state, ok := c.roots[key]
		if !ok || state.Root.RootFingerprint != key || state.Root.RuntimeSessionID == "" || state.Reconciliation != (state.ReconciliationOwner != "") {
			return errors.New("history page cache root identity is invalid")
		}
		if err := validateRootState(state); err != nil {
			return err
		}
	}
	if c.latest != "" {
		if _, ok := c.roots[c.latest]; !ok {
			return errors.New("history latest root is missing")
		}
		if _, ok := c.roots[c.current]; !ok {
			return errors.New("history current viewport root is missing")
		}
	}
	return nil
}

func (c PageCache) clone() PageCache {
	next := PageCache{roots: make(map[string]RootPageState, len(c.roots)), order: append([]string(nil), c.order...), latest: c.latest, current: c.current, attachmentID: c.attachmentID, attachmentGeneration: c.attachmentGeneration}
	for key, value := range c.roots {
		value.Cells = cloneHistoryCells(value.Cells)
		value.ResidentIDs = append([]string(nil), value.ResidentIDs...)
		next.roots[key] = value
	}
	return next
}

func (c *PageCache) put(key string, value RootPageState) {
	if _, exists := c.roots[key]; !exists {
		c.order = append(c.order, key)
	}
	value.Cells = cloneHistoryCells(value.Cells)
	value.ResidentIDs = append([]string(nil), value.ResidentIDs...)
	c.roots[key] = value
	for len(c.order) > maximumPinnedRoots {
		// The Python attachment lease owner uses the same bounded FIFO policy.
		// Keeping this rule deterministic prevents a contiguous client from
		// retaining a root after the server has released its physical lease.
		victim := c.order[0]
		delete(c.roots, victim)
		c.order = c.order[1:]
		if c.current == victim {
			c.current = c.latest
		}
	}
}

func cellsFromMap(values map[string]protocolvalue.HistoryCell) []protocolvalue.HistoryCell {
	result := make([]protocolvalue.HistoryCell, 0, len(values))
	for _, cell := range values {
		result = append(result, cell)
	}
	sortHistoryCells(result)
	return result
}

func trimHydratedWindow(state *RootPageState, page protocolvalue.HistoryPageResult) error {
	resident := make(map[string]struct{}, len(state.ResidentIDs))
	for _, id := range state.ResidentIDs {
		resident[id] = struct{}{}
	}
	for countHydrated(*state) > maximumHydratedCellsPerRoot || hydratedBytes(*state) > maximumHydratedBytesPerRoot {
		victim := -1
		if page.Direction == protocolvalue.HistoryPageBefore {
			for index := len(state.Cells) - 1; index >= 0; index-- {
				if _, isResident := resident[state.Cells[index].ID]; !isResident {
					victim = index
					break
				}
			}
			if !page.HasAfterCursor {
				return errors.New("bounded before-page eviction lacks an after cursor")
			}
			state.AfterCursor, state.HasAfterCursor, state.HasMoreAfter = page.AfterCursor, true, true
		} else {
			for index := range state.Cells {
				if _, isResident := resident[state.Cells[index].ID]; !isResident {
					victim = index
					break
				}
			}
			if !page.HasBeforeCursor {
				return errors.New("bounded after-page eviction lacks a before cursor")
			}
			state.BeforeCursor, state.HasBeforeCursor, state.HasMoreBefore = page.BeforeCursor, true, true
		}
		if victim < 0 {
			return errors.New("history hydrated cache cannot satisfy its hard bound")
		}
		state.Cells = append(state.Cells[:victim], state.Cells[victim+1:]...)
	}
	return nil
}

func finalizeRootState(state *RootPageState) error {
	state.CachedBytes = cachedBytes(state.Cells)
	state.MaterializationFingerprint = viewportMaterializationFingerprint(state.Root.RootFingerprint, state.Cells)
	return validateRootState(*state)
}

func validateRootState(state RootPageState) error {
	if len(state.Cells) > maximumCachedCellsPerRoot || state.CachedBytes > maximumCachedBytesPerRoot ||
		len(state.ResidentIDs) > maximumResidentCellsPerRoot || residentBytes(state) > maximumResidentBytesPerRoot || countHydrated(state) > maximumHydratedCellsPerRoot || hydratedBytes(state) > maximumHydratedBytesPerRoot ||
		state.CachedBytes != cachedBytes(state.Cells) || state.MaterializationFingerprint != viewportMaterializationFingerprint(state.Root.RootFingerprint, state.Cells) {
		return errors.New("history page cache cell/byte bound is invalid")
	}
	cellIDs := make(map[string]struct{}, len(state.Cells))
	var previousRank uint64
	for index, cell := range state.Cells {
		if cell.ID == "" || cell.Fingerprint == "" {
			return errors.New("history page cache cell is incomplete")
		}
		if _, duplicate := cellIDs[cell.ID]; duplicate || (index > 0 && cell.DisplayRank <= previousRank) {
			return errors.New("history page cache cells are not uniquely ordered")
		}
		cellIDs[cell.ID] = struct{}{}
		previousRank = cell.DisplayRank
	}
	residentIDs := make(map[string]struct{}, len(state.ResidentIDs))
	for _, id := range state.ResidentIDs {
		if _, duplicate := residentIDs[id]; id == "" || duplicate {
			return errors.New("history page cache resident identity is invalid")
		}
		if _, exists := cellIDs[id]; !exists {
			return errors.New("history page cache resident identity has no cell")
		}
		residentIDs[id] = struct{}{}
	}
	if state.HasBeforeCursor != (state.BeforeCursor.Fingerprint != "") || state.HasAfterCursor != (state.AfterCursor.Fingerprint != "") ||
		state.HasResidentBeforeCursor != (state.ResidentBeforeCursor.Fingerprint != "") ||
		state.HasResidentAfterCursor != (state.ResidentAfterCursor.Fingerprint != "") ||
		state.ResidentHasMoreBefore && !state.HasResidentBeforeCursor ||
		(state.HasBeforeCursor && state.BeforeCursor.Root.RootFingerprint != state.Root.RootFingerprint) ||
		(state.HasAfterCursor && state.AfterCursor.Root.RootFingerprint != state.Root.RootFingerprint) ||
		(state.HasResidentBeforeCursor && state.ResidentBeforeCursor.Root.RootFingerprint != state.Root.RootFingerprint) ||
		(state.HasResidentAfterCursor && state.ResidentAfterCursor.Root.RootFingerprint != state.Root.RootFingerprint) ||
		state.HasMoreBefore && !state.HasBeforeCursor || state.HasMoreAfter && !state.HasAfterCursor {
		return errors.New("history page cache cursor matrix is invalid")
	}
	return nil
}

func countHydrated(state RootPageState) int {
	return len(state.Cells) - len(state.ResidentIDs)
}

func hydratedBytes(state RootPageState) uint64 {
	resident := make(map[string]struct{}, len(state.ResidentIDs))
	for _, id := range state.ResidentIDs {
		resident[id] = struct{}{}
	}
	var result uint64
	for _, cell := range state.Cells {
		if _, ok := resident[cell.ID]; !ok {
			result += cellCacheBytes(cell)
		}
	}
	return result
}

func residentBytes(state RootPageState) uint64 {
	resident := make(map[string]struct{}, len(state.ResidentIDs))
	for _, id := range state.ResidentIDs {
		resident[id] = struct{}{}
	}
	var result uint64
	for _, cell := range state.Cells {
		if _, ok := resident[cell.ID]; ok {
			result += cellCacheBytes(cell)
		}
	}
	return result
}

func cachedBytes(cells []protocolvalue.HistoryCell) uint64 {
	var result uint64
	for _, cell := range cells {
		result += cellCacheBytes(cell)
	}
	return result
}

func cellCacheBytes(cell protocolvalue.HistoryCell) uint64 {
	return uint64(len(cell.ID)+len(cell.Kind)+len(cell.PublicText)+len(cell.Fingerprint)+len(cell.CellFingerprint)+len(cell.PlacementKeyFingerprint)+len(cell.RankedFingerprint)) + 64
}

func viewportMaterializationFingerprint(rootFingerprint string, cells []protocolvalue.HistoryCell) string {
	hash := sha256.New()
	writeHashValue(hash, rootFingerprint)
	for _, cell := range cells {
		writeHashValue(hash, cell.ID)
		writeHashValue(hash, cell.Fingerprint)
		var rank [8]byte
		binary.BigEndian.PutUint64(rank[:], cell.DisplayRank)
		_, _ = hash.Write(rank[:])
	}
	return fmt.Sprintf("sha256:%x", hash.Sum(nil))
}

type hashWriter interface{ Write([]byte) (int, error) }

func writeHashValue(hash hashWriter, value string) {
	var length [8]byte
	binary.BigEndian.PutUint64(length[:], uint64(len(value)))
	_, _ = hash.Write(length[:])
	_, _ = hash.Write([]byte(value))
}
