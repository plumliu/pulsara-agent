package app

import (
	"errors"
	"fmt"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/commandstate"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/components/composer"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/components/notification"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func refreshInteractiveState(s AppState) (AppState, error) {
	controller := s.controllerGranted()
	// A mutation-capable composer must always be visible. Tiny layouts retain
	// the exact draft authority but disable all edit/paste/submit transitions.
	composerVisible := controller && s.layout.Height >= 4
	availability := composer.SubmitBlockedReadOnly
	if controller {
		availability = composer.SubmitBlockedControlStale
		if s.phase == PhaseReady && s.control.Ready() {
			view := s.control.Projection()
			switch {
			case view.SessionLifecycle != protocolvalue.SessionLifecycleOpen:
				availability = composer.SubmitBlockedReadOnly
			case view.PendingInteraction:
				availability = composer.SubmitBlockedInteraction
			case s.durable.Durable().ActiveHead.CapacityKind != 0 && s.durable.Durable().ActiveHead.CapacityKind != protocolvalue.HistoryCapacityAvailable:
				availability = composer.SubmitBlockedCapacity
			default:
				availability = composer.SubmitAvailable
			}
		}
	}
	s.composer = s.composer.Configure(composerVisible, availability)
	plan, err := NewLayoutPlan(s.layout.Width, s.layout.Height)
	if composerVisible {
		plan, err = NewInteractiveLayoutPlan(s.layout.Width, s.layout.Height, s.composer.DesiredRows(s.layout.Width))
	}
	if err != nil {
		return s, err
	}
	if plan != s.layout || s.transcript.Width() != plan.Width || s.transcript.Height() != plan.TranscriptRows {
		snapshot := protocolvalueSnapshot(s)
		s.transcript, err = s.transcript.Resize(snapshot, plan.Width, plan.TranscriptRows)
		if err != nil {
			return s, err
		}
		s.layout = plan
	}
	return s, s.composer.Validate()
}

func protocolvalueSnapshot(s AppState) (result protocolvalue.DurableSnapshot) {
	if s.durable.Installed() && s.pageCache.Ready() {
		if materialized, err := s.pageCache.MaterializeCurrent(s.durable.Durable()); err == nil {
			return materialized
		}
	}
	if s.durable.Installed() {
		return s.durable.Durable()
	}
	return result
}

func freezeSubmitCommand(s AppState) (AppState, error) {
	frozen, err := s.composer.FreezeSubmission()
	if err != nil {
		return s, err
	}
	if _, phase, exists := s.commands.ExactSubmission(
		frozen.DraftRevision,
		frozen.DraftContentFingerprint,
		frozen.Text,
	); exists {
		switch phase {
		case commandstate.Rejected:
			// A confirmed rejection has no side effect, so an explicit second
			// Enter may freeze a fresh retry candidate.
		case commandstate.Reconciliation:
			return s, errors.New("the previous submission requires reconciliation; edit the draft before retrying")
		default:
			// Frozen, in-flight, pending-confirmation, accepted and compatible
			// winner states already own this exact composer revision. Reuse
			// that authority rather than allocating another command ID.
			return s, nil
		}
	}
	candidate, err := commandstate.NewSubmitCandidate(commandstate.CandidateInput{
		ClientInstanceID:             s.connection.ClientInstanceID,
		AttachmentID:                 s.attachment.Identity.ID,
		AttachmentGeneration:         s.attachment.Identity.Generation,
		RuntimeSessionID:             s.attachment.Identity.RuntimeSessionID,
		ExpectedTargetID:             s.attachment.Identity.RuntimeSessionID,
		ExpectedTargetGeneration:     1,
		ExpectedControllerGeneration: s.attachment.Identity.ControllerGeneration,
		CandidateOrdinal:             s.commands.NextOrdinal(), ComposerRevision: frozen.DraftRevision,
		ComposerContentFingerprint: frozen.DraftContentFingerprint, Text: frozen.Text, DeliveryMode: commandstate.DeliveryAuto,
	})
	if err != nil {
		return s, err
	}
	maximum := s.connection.HelloWinner.Limits.MaximumFrameBytes
	if maximum == 0 || candidate.ValidateMutationPayloadBound(maximum) != nil {
		return s, errors.New("large paste is not supported by the negotiated command frame")
	}
	nextCommands, err := s.commands.Install(candidate)
	if err != nil {
		return s, err
	}
	nextComposer, err := s.composer.HandoffSubmission(frozen)
	if err != nil {
		return s, err
	}
	// Install the command owner and fresh draft as one pure Update transition;
	// no I/O can observe only one side of the handoff.
	s.commands = nextCommands
	s.composer = nextComposer
	return s, nil
}

func freezeStopCommand(s AppState) (AppState, error) {
	runID := s.activeRunID()
	if runID == "" {
		return s, errors.New("no active run is available to stop")
	}
	if _, exists := s.commands.Pending(commandstate.StopRun, runID); exists {
		return s, nil
	}
	candidate, err := commandstate.NewStopCandidate(commandstate.CandidateInput{
		ClientInstanceID:     s.connection.ClientInstanceID,
		AttachmentID:         s.attachment.Identity.ID,
		AttachmentGeneration: s.attachment.Identity.Generation,
		RuntimeSessionID:     s.attachment.Identity.RuntimeSessionID,
		ExpectedTargetID:     runID, ExpectedTargetGeneration: 1,
		ExpectedControllerGeneration: s.attachment.Identity.ControllerGeneration,
		CandidateOrdinal:             s.commands.NextOrdinal(),
	})
	if err != nil {
		return s, err
	}
	s.commands, err = s.commands.Install(candidate)
	return s, err
}

func requestCommandEffect(s AppState, now time.Time) (AppState, []Effect, error) {
	candidate, query, ok := s.commands.NextAction(now)
	if !ok {
		return s, nil, nil
	}
	if query {
		var token OperationToken
		s, token = s.nextWire(OpCommandQuery, now.Add(wireOperationDeadline))
		var err error
		s.commands, err = s.commands.MarkQuerying(candidate.ID())
		if err != nil {
			return s, nil, err
		}
		return s, []Effect{QueryCommandEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Candidate: candidate}}, nil
	}
	var token OperationToken
	s, token = s.nextWire(OpMutation, now.Add(30*time.Second))
	var err error
	s.commands, err = s.commands.MarkMutationSending(candidate.ID())
	if err != nil {
		return s, nil, err
	}
	return s, []Effect{SendMutationEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Candidate: candidate}}, nil
}

func applyCommandOutcome(s AppState, candidate commandstate.Candidate, outcome commandstate.Outcome, observedAt time.Time) (AppState, error) {
	var err error
	s.commands, err = s.commands.ApplyOutcome(candidate.ID(), outcome, observedAt)
	if err != nil {
		return s, err
	}
	if candidate.Kind() == commandstate.SubmitPrompt {
		frozen := composer.FrozenSubmission{Text: candidate.Text(), DraftRevision: candidate.ComposerRevision(), DraftContentFingerprint: candidate.ComposerContentFingerprint()}
		switch outcome.Status {
		case commandstate.OutcomeSucceeded, commandstate.OutcomeCompatibleWinner:
			s.composer = s.composer.ApplyAccepted(frozen, candidate.ClientSubmissionID())
		case commandstate.OutcomeRejected, commandstate.OutcomeReconciliationRequired:
			s.composer = s.composer.ApplyRejected(frozen)
		}
	}
	switch outcome.Status {
	case commandstate.OutcomePendingConfirmation:
		// Pending delivery is rendered from the durable command registry.  It is
		// state, not a transient notification.
	case commandstate.OutcomeReconciliationRequired:
		s = appendLocalNotificationKind(
			s,
			notification.Warning,
			fmt.Sprintf("%s: %s", outcome.PublicResultCode, outcome.PublicResultText),
			observedAt,
		)
	case commandstate.OutcomeRejected:
		s = appendLocalNotificationKind(
			s,
			notification.Failure,
			fmt.Sprintf("%s: %s", outcome.PublicResultCode, outcome.PublicResultText),
			observedAt,
		)
	default:
		s = appendLocalNotificationKind(
			s,
			notification.CommandOutcome,
			fmt.Sprintf("%s: %s", outcome.PublicResultCode, outcome.PublicResultText),
			observedAt,
		)
	}
	return refreshInteractiveState(s)
}

func handleComposerKey(s AppState, key NormalizedKey, now time.Time) (AppState, []Effect, bool, error) {
	if !s.composer.Enabled() {
		return s, nil, false, nil
	}
	var err error
	switch key.Action {
	case KeyPageUp, KeyPageDown:
		return s, nil, false, nil
	case KeyEnd:
		// End belongs to the composer while it is active. The transcript's
		// jump-to-tail remains PageDown at the bottom and later S3 keymap help.
		s.composer = s.composer.MoveEnd()
	case KeyHome:
		s.composer = s.composer.MoveHome()
	case KeyLeft:
		s.composer = s.composer.MoveLeft()
	case KeyRight:
		s.composer = s.composer.MoveRight()
	case KeyUp:
		s.composer = s.composer.MoveUp()
	case KeyDown:
		s.composer = s.composer.MoveDown()
	case KeyBackspace:
		s.composer = s.composer.Backspace()
	case KeyDelete:
		s.composer = s.composer.Delete()
	case KeyUndo:
		s.composer = s.composer.Undo()
	case KeyRedo:
		s.composer = s.composer.Redo()
	case KeyText:
		s.composer, err = s.composer.Insert(key.TextUTF8)
	case KeyEnter:
		if key.Modifiers&(KeyModAlt|KeyModShift) != 0 {
			s.composer, err = s.composer.Insert("\n")
		} else {
			s, err = freezeSubmitCommand(s)
			if err == nil && s.connection.Outstanding.Carrier == OutstandingNone {
				s, effects, effectErr := nextLiveEffect(s, now)
				return s, effects, true, effectErr
			}
		}
	case KeyEscape:
		if s.composer.Mode() == composer.PasteReview {
			s.composer = s.composer.CancelPasteReview()
		} else if s.localNotifications.LatestSticky() {
			s.localNotifications = s.localNotifications.DismissLatestSticky()
		} else if s.activeRunID() != "" {
			s, err = freezeStopCommand(s)
		} else {
			return appendLocalNotification(s, "Esc has no active run to stop", now), nil, true, nil
		}
	case KeyInterrupt:
		if s.activeRunID() == "" {
			return appendLocalNotification(s, "Ctrl-C has no active run to stop", now), nil, true, nil
		}
		s, err = freezeStopCommand(s)
	case KeyEOF:
		if s.composer.Empty() {
			return s, nil, false, nil
		}
		return appendLocalNotification(s, "Ctrl-D detaches only when the draft is empty", now), nil, true, nil
	default:
		return s, nil, true, nil
	}
	if err != nil {
		return appendLocalNotification(s, err.Error(), now), nil, true, nil
	}
	s, err = refreshInteractiveState(s)
	if err != nil {
		return s, nil, true, err
	}
	if (key.Action == KeyEscape || key.Action == KeyInterrupt) && s.connection.Outstanding.Carrier == OutstandingNone {
		s, effects, effectErr := nextLiveEffect(s, now)
		return s, effects, true, effectErr
	}
	return s, nil, true, nil
}
