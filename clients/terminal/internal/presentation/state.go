package presentation

import (
	"errors"
	"sort"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

// State owns the last confirmed durable presentation materialization. It does
// not interpret EventLog or Foundation semantics; every transition is applied
// only from a validated Protocol value.
type State struct {
	snapshot       protocolvalue.DurableSnapshot
	installed      bool
	stale          bool
	lastTransition string
}

func New() State { return State{} }

func (s State) Install(value protocolvalue.DurableSnapshot) (State, error) {
	if value.RuntimeSessionID == "" || value.Control.CursorFingerprint == "" ||
		value.SnapshotFingerprint == "" || value.ActiveHead.Fingerprint == "" ||
		value.LatestRootCursorPair.Fingerprint == "" || value.ResidentVectorFingerprint == "" || value.ViewportFingerprint == "" {
		return State{}, errors.New("durable snapshot is invalid")
	}
	value.Cells = cloneHistoryCells(value.Cells)
	// The control view has its own closed state. Durable history retains only
	// the root/head/materialization authority.
	value.Control = protocolvalue.ControlProjection{}
	s.snapshot, s.installed, s.stale = value, true, false
	s.lastTransition = value.SnapshotFingerprint
	return s, s.Validate()
}

func (s State) ApplyProjectionDelta(value protocolvalue.ProjectionDelta) (State, error) {
	if !s.installed || s.stale {
		return State{}, errors.New("projection delta predecessor is stale")
	}
	if value.ResultingRevision == s.snapshot.ProjectionRevision {
		if value.Fingerprint == s.lastTransition {
			return s, nil
		}
		return State{}, errors.New("projection delta conflicts at the installed revision")
	}
	if value.BaseRevision != s.snapshot.ProjectionRevision ||
		value.ResultingRevision != value.BaseRevision+1 ||
		value.BaseResidentVectorFingerprint != s.snapshot.ResidentVectorFingerprint ||
		value.ResultingAuthorityHighWater != value.ResultingActiveHead.ThroughAuthoritySequence ||
		value.ResultingActiveHead.ConfirmedRoot.RootFingerprint != s.snapshot.ActiveHead.ConfirmedRoot.RootFingerprint {
		return State{}, errors.New("projection delta predecessor is stale")
	}
	cells, err := applyHistoryChanges(s.snapshot.Cells, value.Changes)
	if err != nil || uint64(len(cells)) != value.ResultingActiveHead.ResidentEntryCount {
		return State{}, errors.New("projection delta resident vector is invalid")
	}
	s.snapshot.Cells = cells
	s.snapshot.ProjectionRevision = value.ResultingRevision
	s.snapshot.AuthorityHighWater = value.ResultingAuthorityHighWater
	s.snapshot.ActiveHead = value.ResultingActiveHead
	s.snapshot.ActiveHeadFingerprint = value.ResultingActiveHead.Fingerprint
	s.snapshot.ResidentVectorFingerprint = value.ResultingResidentVectorFingerprint
	s.snapshot.SnapshotFingerprint = value.Fingerprint
	s.snapshot.ViewportFingerprint = value.Fingerprint
	s.lastTransition = value.Fingerprint
	return s, s.Validate()
}

func (s State) ApplyAuthorityAdvance(value protocolvalue.AuthorityAdvance) (State, error) {
	if !s.installed || s.stale {
		return State{}, errors.New("authority advance predecessor is stale")
	}
	if value.Fingerprint == s.lastTransition {
		return s, nil
	}
	if value.ProjectionRevision != s.snapshot.ProjectionRevision ||
		value.BaseActiveHeadFingerprint != s.snapshot.ActiveHeadFingerprint ||
		value.ResultingActiveHead.ConfirmedRoot.RootFingerprint != s.snapshot.ActiveHead.ConfirmedRoot.RootFingerprint ||
		value.ResultingActiveHead.ThroughAuthoritySequence < s.snapshot.AuthorityHighWater {
		return State{}, errors.New("authority advance predecessor is stale")
	}
	s.snapshot.AuthorityHighWater = value.ResultingActiveHead.ThroughAuthoritySequence
	s.snapshot.ActiveHead = value.ResultingActiveHead
	s.snapshot.ActiveHeadFingerprint = value.ResultingActiveHead.Fingerprint
	s.snapshot.SnapshotFingerprint = value.Fingerprint
	s.snapshot.ViewportFingerprint = value.Fingerprint
	s.lastTransition = value.Fingerprint
	return s, s.Validate()
}

func (s State) ApplyRootAdvance(value protocolvalue.RootAdvance) (State, bool, error) {
	if !s.installed || s.stale {
		return State{}, false, errors.New("root advance predecessor is stale")
	}
	if value.ResultingRevision == s.snapshot.ProjectionRevision {
		if value.Fingerprint == s.lastTransition {
			return s, false, nil
		}
		return State{}, false, errors.New("root advance conflicts at the installed revision")
	}
	if value.BaseRevision != s.snapshot.ProjectionRevision ||
		value.ResultingRevision != value.BaseRevision+1 ||
		value.PreviousActiveHeadFingerprint != s.snapshot.ActiveHeadFingerprint ||
		value.PreviousRoot.RootFingerprint != s.snapshot.ActiveHead.ConfirmedRoot.RootFingerprint ||
		value.BeforeResidentFingerprint != s.snapshot.ResidentVectorFingerprint ||
		value.ConsumedTailPrefixThroughSequence != value.ResultingRoot.ThroughAuthoritySequence ||
		value.RetainedTailSuffixFromSequenceExclusive != value.ConsumedTailPrefixThroughSequence ||
		value.RetainedTailSuffixThroughSequence != value.ResultingActiveHead.ThroughAuthoritySequence ||
		value.ResultingRoot.RootFingerprint != value.ResultingActiveHead.ConfirmedRoot.RootFingerprint ||
		value.LatestRootCursorPair.Root.RootFingerprint != value.ResultingRoot.RootFingerprint {
		return State{}, false, errors.New("root advance predecessor is stale")
	}
	if value.TransitionKind == protocolvalue.ResidentTransitionRebaseRequired {
		s.stale = true
		s.lastTransition = value.Fingerprint
		return s, true, nil
	}
	cells := s.snapshot.Cells
	if value.TransitionKind == protocolvalue.ResidentTransitionBoundedChanges {
		var err error
		cells, err = applyHistoryChanges(cells, value.Changes)
		if err != nil {
			return State{}, false, err
		}
	} else if value.TransitionKind != protocolvalue.ResidentTransitionUnchanged {
		return State{}, false, errors.New("root resident transition is unknown")
	}
	if uint64(len(cells)) != value.ResultingActiveHead.ResidentEntryCount {
		return State{}, false, errors.New("root advance resident count mismatch")
	}
	s.snapshot.Cells = cells
	s.snapshot.ProjectionRevision = value.ResultingRevision
	s.snapshot.AuthorityHighWater = value.ResultingActiveHead.ThroughAuthoritySequence
	s.snapshot.ActiveHead = value.ResultingActiveHead
	s.snapshot.ActiveHeadFingerprint = value.ResultingActiveHead.Fingerprint
	s.snapshot.LatestRootCursorPair = value.LatestRootCursorPair
	s.snapshot.ResidentVectorFingerprint = value.AfterResidentFingerprint
	s.snapshot.SnapshotFingerprint = value.Fingerprint
	s.snapshot.ViewportFingerprint = value.Fingerprint
	s.lastTransition = value.Fingerprint
	return s, false, s.Validate()
}

func applyHistoryChanges(existing []protocolvalue.HistoryCell, changes []protocolvalue.HistoryChange) ([]protocolvalue.HistoryCell, error) {
	byID := make(map[string]protocolvalue.HistoryCell, len(existing)+len(changes))
	for _, cell := range existing {
		if cell.ID == "" || byID[cell.ID].ID != "" {
			return nil, errors.New("resident history vector contains duplicate identity")
		}
		byID[cell.ID] = cell
	}
	for _, change := range changes {
		previous, exists := byID[change.HistoryEntryID]
		switch change.Kind {
		case protocolvalue.HistoryChangeUpsert:
			if change.HasExpectedPrevious != exists || (exists && previous.Fingerprint != change.ExpectedPreviousFingerprint) || change.Resulting.ID != change.HistoryEntryID {
				return nil, errors.New("history upsert predecessor mismatch")
			}
			byID[change.HistoryEntryID] = change.Resulting
		case protocolvalue.HistoryChangeRemove:
			if !exists || !change.HasExpectedPrevious || previous.Fingerprint != change.ExpectedPreviousFingerprint || previous.PlacementKeyFingerprint != change.PlacementKeyFingerprint {
				return nil, errors.New("history removal predecessor mismatch")
			}
			delete(byID, change.HistoryEntryID)
		default:
			return nil, errors.New("history change kind is unknown")
		}
	}
	result := make([]protocolvalue.HistoryCell, 0, len(byID))
	for _, cell := range byID {
		result = append(result, cell)
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].DisplayRank != result[j].DisplayRank {
			return result[i].DisplayRank < result[j].DisplayRank
		}
		return result[i].ID < result[j].ID
	})
	for index := 1; index < len(result); index++ {
		if result[index-1].DisplayRank >= result[index].DisplayRank {
			return nil, errors.New("history display rank is not strictly ordered")
		}
	}
	return result, nil
}

func (s State) MarkStale() State { s.stale = s.installed; return s }
func (s State) Validate() error {
	if !s.installed {
		if s.stale || s.snapshot.RuntimeSessionID != "" || s.lastTransition != "" {
			return errors.New("uninstalled durable presentation owns authority")
		}
		return nil
	}
	if s.snapshot.RuntimeSessionID == "" || s.snapshot.SnapshotFingerprint == "" ||
		s.snapshot.ResidentVectorFingerprint == "" || s.snapshot.ViewportFingerprint == "" ||
		s.snapshot.ActiveHeadFingerprint != s.snapshot.ActiveHead.Fingerprint ||
		s.snapshot.AuthorityHighWater != s.snapshot.ActiveHead.ThroughAuthoritySequence ||
		s.snapshot.LatestRootCursorPair.Root.RootFingerprint != s.snapshot.ActiveHead.ConfirmedRoot.RootFingerprint ||
		s.lastTransition == "" {
		return errors.New("durable presentation baseline is invalid")
	}
	return nil
}

func (s State) Ready() bool                 { return s.installed && !s.stale }
func (s State) Installed() bool             { return s.installed }
func (s State) Stale() bool                 { return s.stale }
func (s State) SnapshotFingerprint() string { return s.snapshot.SnapshotFingerprint }
func (s State) ProjectionRevision() uint64  { return s.snapshot.ProjectionRevision }
func (s State) Durable() protocolvalue.DurableSnapshot {
	value := s.snapshot
	value.Cells = cloneHistoryCells(value.Cells)
	return value
}

type OperationalState struct {
	snapshot  protocolvalue.OperationalSnapshot
	installed bool
	stale     bool
}

func NewOperational() OperationalState { return OperationalState{} }
func (s OperationalState) Install(value protocolvalue.OperationalSnapshot, runtimeSessionID string) (OperationalState, error) {
	if value.RuntimeSessionID == "" || value.RuntimeSessionID != runtimeSessionID || value.FrameFingerprint == "" {
		return OperationalState{}, errors.New("operational snapshot is invalid")
	}
	value.Cells = cloneOperationalCells(value.Cells)
	s.snapshot, s.installed, s.stale = value, true, false
	return s, s.Validate()
}

func (s OperationalState) ApplyDelta(value protocolvalue.OperationalDelta) (OperationalState, error) {
	if !s.installed || s.stale || value.Generation != s.snapshot.Generation || len(value.Changes) == 0 || value.Changes[0].Cursor != s.snapshot.Cursor+1 || value.Cursor < value.Changes[0].Cursor {
		return OperationalState{}, errors.New("operational delta predecessor is stale")
	}
	byKey := make(map[string]protocolvalue.OperationalCell, len(s.snapshot.Cells)+len(value.Changes))
	for _, cell := range s.snapshot.Cells {
		byKey[cell.CoalesceKey] = cell
	}
	expected := s.snapshot.Cursor
	for _, change := range value.Changes {
		expected++
		if change.Generation != value.Generation || change.Cursor != expected || change.CoalesceKey == "" {
			return OperationalState{}, errors.New("operational delta continuity mismatch")
		}
		previous, exists := byKey[change.CoalesceKey]
		switch change.Kind {
		case protocolvalue.OperationalChangeUpsert:
			if change.Cell.CoalesceKey != change.CoalesceKey {
				return OperationalState{}, errors.New("operational upsert identity mismatch")
			}
			byKey[change.CoalesceKey] = change.Cell
		case protocolvalue.OperationalChangeRemove:
			if !exists || previous.Fingerprint != change.ExpectedPreviousFingerprint {
				return OperationalState{}, errors.New("operational removal predecessor mismatch")
			}
			delete(byKey, change.CoalesceKey)
		default:
			return OperationalState{}, errors.New("operational change kind is unknown")
		}
	}
	if expected != value.Cursor {
		return OperationalState{}, errors.New("operational delta terminal cursor mismatch")
	}
	cells := make([]protocolvalue.OperationalCell, 0, len(byKey))
	for _, cell := range byKey {
		cells = append(cells, cell)
	}
	sort.Slice(cells, func(i, j int) bool {
		if cells[i].Cursor != cells[j].Cursor {
			return cells[i].Cursor < cells[j].Cursor
		}
		return cells[i].CoalesceKey < cells[j].CoalesceKey
	})
	s.snapshot.Cells, s.snapshot.Cursor, s.snapshot.FrameFingerprint = cells, value.Cursor, value.Fingerprint
	return s, s.Validate()
}

func (s OperationalState) Invalidate() OperationalState { s.stale = s.installed; return s }
func (s OperationalState) Validate() error {
	if !s.installed {
		if s.stale {
			return errors.New("uninstalled operational state is stale")
		}
		return nil
	}
	if s.snapshot.RuntimeSessionID == "" || s.snapshot.FrameFingerprint == "" {
		return errors.New("operational presentation baseline is invalid")
	}
	return nil
}
func (s OperationalState) Ready() bool     { return s.installed && !s.stale }
func (s OperationalState) Installed() bool { return s.installed }
func (s OperationalState) Stale() bool     { return s.stale }
func (s OperationalState) Snapshot() protocolvalue.OperationalSnapshot {
	value := s.snapshot
	value.Cells = cloneOperationalCells(value.Cells)
	return value
}

type ControlProjectionPhase uint8

const (
	ControlProjectionUninitialized ControlProjectionPhase = iota + 1
	ControlProjectionFresh
	ControlProjectionSnapshotRequired
)

type ControlProjectionState struct {
	phase             ControlProjectionPhase
	projection        protocolvalue.ControlProjection
	confirmedCursor   protocolvalue.ControlCursor
	observedLatest    protocolvalue.ControlCursor
	hasObservedLatest bool
}

func NewControlProjection() ControlProjectionState {
	return ControlProjectionState{phase: ControlProjectionUninitialized}
}
func (s ControlProjectionState) Install(value protocolvalue.ControlProjection, runtimeSessionID string) (ControlProjectionState, error) {
	if value.RuntimeSessionID != runtimeSessionID || value.CursorFingerprint == "" || value.ViewFingerprint == "" || value.ProjectionFingerprint != value.ViewFingerprint {
		return ControlProjectionState{}, errors.New("control projection baseline is invalid")
	}
	value.QueueItems = append([]protocolvalue.QueueItem(nil), value.QueueItems...)
	value.ServerNotifications = append([]protocolvalue.ServerNotification(nil), value.ServerNotifications...)
	s.projection = value
	s.confirmedCursor = protocolvalue.ControlCursor{Generation: value.Generation, Revision: value.Revision, ProjectionFingerprint: value.ProjectionFingerprint, TransitionAccumulator: value.TransitionAccumulator, RegistryFingerprint: value.RegistryFingerprint, Fingerprint: value.CursorFingerprint}
	s.phase, s.hasObservedLatest = ControlProjectionFresh, false
	s.observedLatest = protocolvalue.ControlCursor{}
	return s, s.Validate()
}

func (s ControlProjectionState) RequireSnapshot(latest protocolvalue.ControlCursor) (ControlProjectionState, error) {
	if s.phase != ControlProjectionFresh || latest.Fingerprint == "" || (latest.Generation == s.confirmedCursor.Generation && latest.Revision <= s.confirmedCursor.Revision) {
		return ControlProjectionState{}, errors.New("control invalidation target is stale")
	}
	s.phase, s.observedLatest, s.hasObservedLatest = ControlProjectionSnapshotRequired, latest, true
	return s, s.Validate()
}

func (s ControlProjectionState) Validate() error {
	switch s.phase {
	case ControlProjectionUninitialized:
		if s.projection.CursorFingerprint != "" || s.hasObservedLatest {
			return errors.New("uninitialized control projection owns authority")
		}
	case ControlProjectionFresh:
		if s.projection.CursorFingerprint == "" || s.projection.ProjectionFingerprint != s.projection.ViewFingerprint || s.confirmedCursor.Fingerprint != s.projection.CursorFingerprint || s.hasObservedLatest {
			return errors.New("fresh control projection join is invalid")
		}
	case ControlProjectionSnapshotRequired:
		if s.projection.CursorFingerprint == "" || s.confirmedCursor.Fingerprint != s.projection.CursorFingerprint || !s.hasObservedLatest || s.observedLatest.Fingerprint == "" {
			return errors.New("stale control projection proof is incomplete")
		}
	default:
		return errors.New("control projection phase is unknown")
	}
	return nil
}
func (s ControlProjectionState) Ready() bool     { return s.phase == ControlProjectionFresh }
func (s ControlProjectionState) Installed() bool { return s.phase != ControlProjectionUninitialized }
func (s ControlProjectionState) SnapshotRequired() bool {
	return s.phase == ControlProjectionSnapshotRequired
}
func (s ControlProjectionState) QueueItemCount() int { return len(s.projection.QueueItems) }
func (s ControlProjectionState) ConfirmedCursor() protocolvalue.ControlCursor {
	return s.confirmedCursor
}
func (s ControlProjectionState) ObservedLatestCursor() (protocolvalue.ControlCursor, bool) {
	return s.observedLatest, s.hasObservedLatest
}
func (s ControlProjectionState) Projection() protocolvalue.ControlProjection {
	value := s.projection
	value.QueueItems = append([]protocolvalue.QueueItem(nil), value.QueueItems...)
	value.ServerNotifications = append([]protocolvalue.ServerNotification(nil), value.ServerNotifications...)
	return value
}
