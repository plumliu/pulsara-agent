package app

import (
	"errors"
	"fmt"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/presentation"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

var errOperationalObservation = errors.New("terminal operational observation requires rebuild")

func applyObservationBatch(s AppState, request protocolvalue.PreparedObserveRequest, batch protocolvalue.ObservationBatch, now time.Time) (AppState, []Effect, error) {
	if batch.RequestID != request.RequestID || batch.PlaneCount == 0 || batch.PlaneCount > 3 {
		return s, nil, errors.New("terminal observation batch attribution is stale")
	}
	next := s
	followLatest := next.pageCache.ShouldFollowLatest(next.transcript.FollowTail())
	newLatestCells := uint32(0)
	controlInvalidated, durableChanged, durableRebuild, operationalRebuild := false, false, false, false

	if batch.ControlChange != nil {
		value := *batch.ControlChange
		confirmed := next.control.ConfirmedCursor()
		if value.RequestID != request.RequestID || value.ValidatedAfterFingerprint != request.AfterControl.Fingerprint ||
			confirmed.Fingerprint != request.AfterControl.Fingerprint || value.BaseRevision != confirmed.Revision ||
			value.BaseProjectionFingerprint != confirmed.ProjectionFingerprint || value.ResultingCursor.Generation != confirmed.Generation ||
			value.ResultingCursor.Revision <= confirmed.Revision || value.ConsumedTransitionCount != value.ResultingCursor.Revision-confirmed.Revision {
			return s, nil, errors.New("terminal control change predecessor is stale")
		}
		var err error
		next.control, err = presentation.RequireControlSnapshot(next.control, value.ResultingCursor)
		if err != nil {
			return s, nil, err
		}
		controlInvalidated = true
	} else if batch.ControlGap != nil {
		value := *batch.ControlGap
		if value.RequestID != request.RequestID || value.RequestedCursorFingerprint != request.AfterControl.Fingerprint || next.control.ConfirmedCursor().Fingerprint != request.AfterControl.Fingerprint {
			return s, nil, errors.New("terminal control gap predecessor is stale")
		}
		var err error
		next.control, err = presentation.RequireControlSnapshot(next.control, value.LatestCursor)
		if err != nil {
			return s, nil, err
		}
		controlInvalidated = true
	}

	durableBranches := 0
	if batch.ProjectionDelta != nil {
		durableBranches++
		value := *batch.ProjectionDelta
		if value.RequestID != request.RequestID || value.BaseRevision != request.AfterProjectionRevision || next.durable.Durable().AuthorityHighWater != request.AfterAuthorityHighWater {
			return s, nil, errors.New("terminal projection delta observe cursor is stale")
		}
		var err error
		next.durable, err = presentation.ApplyProjectionDelta(next.durable, value)
		if err != nil {
			return s, nil, err
		}
		durableChanged = true
	}
	if batch.AuthorityAdvance != nil {
		durableBranches++
		value := *batch.AuthorityAdvance
		if value.RequestID != request.RequestID || value.ProjectionRevision != request.AfterProjectionRevision || next.durable.Durable().AuthorityHighWater != request.AfterAuthorityHighWater {
			return s, nil, errors.New("terminal authority advance observe cursor is stale")
		}
		var err error
		next.durable, err = next.durable.ApplyAuthorityAdvance(value)
		if err != nil {
			return s, nil, err
		}
		durableChanged = true
	}
	if batch.RootAdvance != nil {
		durableBranches++
		value := *batch.RootAdvance
		if value.RequestID != request.RequestID || value.BaseRevision != request.AfterProjectionRevision || next.durable.Durable().AuthorityHighWater != request.AfterAuthorityHighWater {
			return s, nil, errors.New("terminal root advance observe cursor is stale")
		}
		var rebase bool
		var err error
		next.durable, rebase, err = presentation.ApplyRootAdvance(next.durable, value)
		if err != nil {
			return s, nil, err
		}
		durableChanged, durableRebuild = !rebase, rebase
	}
	if batch.DurableGap != nil {
		durableBranches++
		if batch.DurableGap.RequestID != request.RequestID {
			return s, nil, errors.New("terminal durable gap request is stale")
		}
		durableRebuild = true
	}
	if durableBranches > 1 {
		return s, nil, errors.New("terminal durable observation has multiple branches")
	}

	operationalBranches := 0
	if batch.OperationalDelta != nil {
		operationalBranches++
		value := *batch.OperationalDelta
		if value.RequestID != request.RequestID || value.Generation != request.AfterOperational.Generation || next.operational.Snapshot().Cursor != request.AfterOperational.Cursor {
			return s, nil, fmt.Errorf("%w: cursor is stale", errOperationalObservation)
		}
		var err error
		next.operational, err = next.operational.ApplyDelta(value)
		if err != nil {
			return s, nil, fmt.Errorf("%w: %v", errOperationalObservation, err)
		}
	}
	if batch.OperationalGap != nil {
		operationalBranches++
		if batch.OperationalGap.RequestID != request.RequestID {
			return s, nil, errors.New("terminal operational gap request is stale")
		}
		operationalRebuild = true
	}
	if operationalBranches > 1 {
		return s, nil, fmt.Errorf("%w: multiple branches", errOperationalObservation)
	}

	if durableChanged {
		materialized := next.durable.Durable()
		var err error
		newLatestCells = next.pageCache.CountNewLatestCells(materialized)
		next.pageCache, err = next.pageCache.InstallLatest(materialized, followLatest, next.attachment.Identity.ID, next.attachment.Identity.Generation)
		if err != nil {
			return s, nil, err
		}
		viewportMaterialization, err := next.pageCache.MaterializeCurrent(materialized)
		if err != nil {
			return s, nil, err
		}
		if viewportMaterialization.SnapshotFingerprint != next.transcript.SnapshotFingerprint() {
			next.transcript, err = next.transcript.Replace(viewportMaterialization, next.layout.Width, next.layout.TranscriptRows)
			if err != nil {
				return s, nil, err
			}
		}
		if !next.pageCache.CurrentIsLatest() {
			next.transcript = next.transcript.NoteUnseen(newLatestCells)
		}
	}
	next.observation.LastResultFingerprint = batch.Fingerprint
	if operationalRebuild {
		next.operational = next.operational.Invalidate()
	}

	if durableRebuild {
		next.durable, next.operational = presentation.Rebuild(next.durable, next.operational)
		next.pageCache = presentation.NewPageCache()
		next.observation.PendingPage, next.observation.HasPendingPage = protocolvalue.PreparedHistoryPageRequest{}, false
		next.observation.HasPageIntent = false
		next.phase = PhaseReadOnly
		var refreshErr error
		next, refreshErr = refreshInteractiveState(next)
		if refreshErr != nil {
			return s, nil, refreshErr
		}
		var minimum *protocolvalue.ControlCursor
		if controlInvalidated {
			observed, _ := next.control.ObservedLatestCursor()
			minimum = &observed
		}
		rebuilt, effect, err := requestDurableSnapshot(next, now, minimum, true)
		if err != nil {
			return s, nil, err
		}
		return rebuilt, []Effect{effect}, nil
	}
	if controlInvalidated {
		next.phase = PhaseReadOnly
		var refreshErr error
		next, refreshErr = refreshInteractiveState(next)
		if refreshErr != nil {
			return s, nil, refreshErr
		}
		observed, _ := next.control.ObservedLatestCursor()
		rebuilt, effect, err := requestDurableSnapshot(next, now, &observed, operationalRebuild)
		if err != nil {
			return s, nil, err
		}
		return rebuilt, []Effect{effect}, nil
	}
	if operationalRebuild {
		next.phase = PhaseReadOnly
		var refreshErr error
		next, refreshErr = refreshInteractiveState(next)
		if refreshErr != nil {
			return s, nil, refreshErr
		}
		next.snapshotLoading = SnapshotLoadingState{Phase: SnapshotAwaitingOperationalSnapshot, AttachmentID: next.attachment.Identity.ID, AttachmentGeneration: next.attachment.Identity.Generation, TransportBindingFingerprint: next.attachment.Identity.BindingFingerprint, DurableSnapshotFingerprint: next.durable.SnapshotFingerprint(), DurableControlCursorFingerprint: next.control.ConfirmedCursor().Fingerprint, OperationalRequired: true}
		rebuilt, effect, err := requestOperationalSnapshot(next, now)
		if err != nil {
			return s, nil, err
		}
		return rebuilt, []Effect{effect}, nil
	}
	next.phase = PhaseReady
	var refreshErr error
	next, refreshErr = refreshInteractiveState(next)
	if refreshErr != nil {
		return s, nil, refreshErr
	}
	return nextLiveEffect(next, now)
}

func applyHistoryPageResult(s AppState, request protocolvalue.PreparedHistoryPageRequest, result protocolvalue.HistoryPageResult, now time.Time) (AppState, []Effect, error) {
	if !s.observation.HasPendingPage || s.observation.PendingPage != request || result.RequestID != request.RequestID || result.RequestedCursorFingerprint != request.Cursor.Fingerprint {
		return s, nil, errors.New("terminal history page result has no matching intent")
	}
	s.observation.PendingPage, s.observation.HasPendingPage = protocolvalue.PreparedHistoryPageRequest{}, false
	switch result.Kind {
	case protocolvalue.HistoryPageDataKind:
		var err error
		s.pageCache, err = s.pageCache.ApplyPage(result)
		if err != nil {
			return s, nil, err
		}
		current, currentOK := s.pageCache.Current()
		if currentOK && current.Root.RootFingerprint == request.Cursor.Root.RootFingerprint {
			display, materializeErr := s.pageCache.MaterializeCurrent(s.durable.Durable())
			if materializeErr != nil {
				return s, nil, materializeErr
			}
			s.transcript, err = s.transcript.Replace(display, s.layout.Width, s.layout.TranscriptRows)
			if err != nil {
				return s, nil, err
			}
			if request.ViewportIntentGeneration == s.observation.ViewportIntentGeneration {
				if request.Direction == protocolvalue.HistoryPageBefore {
					s.transcript = s.transcript.Page(1)
				} else {
					s.transcript = s.transcript.Page(-1)
					s, err = resolveViewportTailAfterScroll(s)
					if err != nil {
						return s, nil, err
					}
				}
			}
		}
	case protocolvalue.HistoryPageCursorStale:
		if result.HasReplacementCursor {
			var token OperationToken
			s, token = s.nextWire(OpHistoryPage, now.Add(wireOperationDeadline))
			replacement, err := protocolvalue.PrepareHistoryPageRequest(token.RequestID, request.RuntimeSessionID, result.ReplacementCursor, request.Direction, request.MaximumCells, request.MaximumDecodedBytes, request.ProjectionContract, request.ViewportIntentGeneration)
			if err != nil {
				return s, nil, err
			}
			s.observation.PendingPage, s.observation.HasPendingPage = replacement, true
			return s, []Effect{ReadHistoryPageEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Request: replacement}}, nil
		}
		fallthrough
	case protocolvalue.HistoryPageRebaseRequired:
		s.pageCache = s.pageCache.DropRoot(request.Cursor.Root.RootFingerprint)
		s.durable = s.durable.MarkStale()
		s.phase = PhaseReadOnly
		rebuilt, effect, err := requestDurableSnapshot(s, now, nil, false)
		if err != nil {
			return s, nil, err
		}
		return rebuilt, []Effect{effect}, nil
	case protocolvalue.HistoryPageReconciliationRequired:
		s.pageCache = s.pageCache.MarkReconciliation(request.Cursor.Root.RootFingerprint, result.ReconciliationOwnerIdentity)
	default:
		return s, nil, errors.New("terminal history page result kind is unknown")
	}
	return nextLiveEffect(s, now)
}
