package app

import (
	"errors"
	"fmt"
	"strings"
	"time"

	tea "charm.land/bubbletea/v2"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/interaction"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/presentation"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/queue"
)

const (
	localOperationDeadline      = 5 * time.Second
	wireOperationDeadline       = 10 * time.Second
	MaximumPublicClipboardBytes = 4 * 1024 * 1024
	MaximumPublicClipboardRunes = 1_000_000
	maximumLocalNotifications   = 8
)

func (s AppState) update(message tea.Msg) (AppState, []Effect, tea.Cmd) {
	if header, ok := localMessageHeader(message); ok {
		if !header.Valid() || header.AppGeneration != s.appGeneration || header.Sequence != s.lastLocalSequence+1 {
			return fatalState(s, "terminal local message header is stale or non-contiguous"), nil, nil
		}
		s.lastLocalSequence = header.Sequence
	}
	if started, ok := message.(AppStartedMsg); ok {
		if s.phase != PhaseBooting || started.BootstrapHandleID != "terminal-bootstrap:"+s.connection.ClientInstanceID || started.TransportCredentialHandleID != "terminal-launch-credential:"+s.connection.ClientInstanceID || started.HandshakeCandidate.ID == "" || started.HandshakeCandidate.Fingerprint == "" || started.HandshakeCandidate.ClientInstanceID != s.connection.ClientInstanceID {
			return fatalState(s, "terminal application start authority is invalid"), nil, nil
		}
		s.connection.BootstrapHandleID = started.BootstrapHandleID
		s.connection.TransportCredentialHandleID = started.TransportCredentialHandleID
		s.connection.HandshakeCandidate = started.HandshakeCandidate
		s.phase = PhaseConnecting
		s.connection.Phase = ConnectionDialing
		var token LocalOperationToken
		s, token = s.nextLocal(OpConnect, started.Header.ProducedAt.Add(localOperationDeadline))
		return s, []Effect{ConnectEffect{Header: newLocalHeader(token), BootstrapHandleID: started.BootstrapHandleID, AttachmentAttemptGeneration: started.HandshakeCandidate.AttachmentAttemptGeneration}}, nil
	}
	if revoked, ok := message.(AttachmentChallengeRevokedMsg); ok {
		if revoked.Receipt.Validate() != nil {
			return fatalState(s, "terminal attachment challenge revocation receipt is invalid"), nil, nil
		}
		if s.connection.AttachmentChallenge.Prepared.HandleFingerprint == revoked.Receipt.HandleFingerprint {
			s.connection.AttachmentChallenge.Phase = AttachmentChallengeRevoked
		}
		return validatedState(s), nil, nil
	}
	if size, ok := message.(ResizeMsg); ok {
		layout, err := NewLayoutPlan(size.Width, size.Height)
		if err != nil {
			return fatalState(s, "terminal resize exceeds the bounded layout contract"), nil, nil
		}
		snapshot := protocolvalue.DurableSnapshot{}
		if s.durable.Installed() && s.pageCache.Ready() {
			snapshot, err = s.pageCache.MaterializeCurrent(s.durable.Durable())
			if err != nil {
				return fatalState(s, fmt.Sprintf("terminal viewport materialization: %v", err)), nil, nil
			}
		} else if s.durable.Installed() {
			snapshot = s.durable.Durable()
		}
		transcriptModel, err := s.transcript.Resize(snapshot, layout.Width, layout.TranscriptRows)
		if err != nil {
			return fatalState(s, fmt.Sprintf("terminal transcript resize: %v", err)), nil, nil
		}
		s.transcript = transcriptModel
		s.layout = layout
		return s, nil, nil
	}
	if key, ok := message.(KeyInputMsg); ok {
		s.localNotifications.Items = nil
		observedAt := key.Header.ProducedAt
		switch key.Key.Action {
		case KeyInterrupt, KeyEOF, KeyEscape:
			s.phase = PhaseDetaching
			s.teardown = NewActiveTeardownState(TeardownUserQuit, 1, observedAt.Add(5*time.Second))
			var token LocalOperationToken
			s, token = s.nextLocal(OpTeardown, s.teardown.Deadline)
			return s, []Effect{BeginTeardownEffect{Header: newLocalHeader(token), Reason: TeardownUserQuit}}, nil
		case KeyUp:
			if !advanceViewportIntent(&s) {
				return fatalState(s, "terminal viewport intent generation exhausted"), nil, nil
			}
			s.transcript = s.transcript.Scroll(1)
		case KeyDown:
			if !advanceViewportIntent(&s) {
				return fatalState(s, "terminal viewport intent generation exhausted"), nil, nil
			}
			s.transcript = s.transcript.Scroll(-1)
		case KeyPageUp:
			if !advanceViewportIntent(&s) {
				return fatalState(s, "terminal viewport intent generation exhausted"), nil, nil
			}
			s.transcript = s.transcript.Page(1)
			if s.transcript.AtTop() {
				if root, ok := s.pageCache.Current(); ok && root.HasMoreBefore && root.HasBeforeCursor {
					s.transcript = s.transcript.Pin()
					s.observation.PageIntentDirection, s.observation.HasPageIntent = protocolvalue.HistoryPageBefore, true
				}
			}
		case KeyPageDown:
			if !advanceViewportIntent(&s) {
				return fatalState(s, "terminal viewport intent generation exhausted"), nil, nil
			}
			s.transcript = s.transcript.Page(-1)
			if s.transcript.FollowTail() {
				if root, ok := s.pageCache.Current(); ok && root.HasMoreAfter && root.HasAfterCursor {
					s.transcript = s.transcript.Pin()
					s.observation.PageIntentDirection, s.observation.HasPageIntent = protocolvalue.HistoryPageAfter, true
				}
			}
		case KeyEnd:
			if !advanceViewportIntent(&s) {
				return fatalState(s, "terminal viewport intent generation exhausted"), nil, nil
			}
			if !s.pageCache.Ready() {
				s.transcript = s.transcript.End()
				break
			}
			var err error
			s.pageCache, err = s.pageCache.SwitchToLatest()
			if err != nil {
				return fatalState(s, fmt.Sprintf("terminal latest viewport switch: %v", err)), nil, nil
			}
			materialized, materializeErr := s.pageCache.MaterializeCurrent(s.durable.Durable())
			if materializeErr != nil {
				return fatalState(s, fmt.Sprintf("terminal latest viewport materialization: %v", materializeErr)), nil, nil
			}
			s.transcript, err = s.transcript.Replace(materialized, s.layout.Width, s.layout.TranscriptRows)
			if err != nil {
				return fatalState(s, fmt.Sprintf("terminal latest viewport replacement: %v", err)), nil, nil
			}
			s.transcript = s.transcript.End()
		case KeyText:
			if key.Key.Modifiers == 0 && key.Key.TextUTF8 == "q" {
				s.phase = PhaseDetaching
				s.teardown = NewActiveTeardownState(TeardownUserQuit, 1, observedAt.Add(5*time.Second))
				var token LocalOperationToken
				s, token = s.nextLocal(OpTeardown, s.teardown.Deadline)
				return s, []Effect{BeginTeardownEffect{Header: newLocalHeader(token), Reason: TeardownUserQuit}}, nil
			}
			if key.Key.Modifiers != 0 || key.Key.TextUTF8 != "y" {
				return s, nil, nil
			}
			if !s.durable.Installed() || !s.pageCache.Ready() {
				return s, nil, nil
			}
			if s.clipboard.Pending {
				s = appendLocalNotification(s, "copy already in progress")
				return s, nil, nil
			}
			materialized, materializeErr := s.pageCache.MaterializeCurrent(s.durable.Durable())
			if materializeErr != nil {
				return fatalState(s, fmt.Sprintf("terminal clipboard materialization: %v", materializeErr)), nil, nil
			}
			publicText, copyErr := boundedClipboardText(materialized.Cells)
			if copyErr != nil {
				s = appendLocalNotification(s, "copy unavailable: current history exceeds 4 MiB")
				return s, nil, nil
			}
			var token LocalOperationToken
			s, token = s.nextDetachedLocal(OpClipboard, observedAt.Add(localOperationDeadline))
			s.clipboard = ClipboardOperationState{Pending: true, Token: token}
			return s, []Effect{CopyPublicTextEffect{Header: newLocalHeader(token), PublicUTF8: publicText}}, nil
		}
		if s.connection.Outstanding.Carrier == OutstandingNone {
			next, liveEffects, err := nextLiveEffect(s, observedAt)
			if err != nil {
				return fatalState(s, err.Error()), nil, nil
			}
			return next, liveEffects, nil
		}
		return s, nil, nil
	}
	if wheel, ok := message.(MouseWheelInputMsg); ok {
		s.localNotifications.Items = nil
		if wheel.VisualRows != mouseWheelVisualRows || wheel.Direction < MouseWheelScrollUp || wheel.Direction > MouseWheelScrollDown {
			return fatalState(s, "terminal mouse wheel input is outside its closed contract"), nil, nil
		}
		delta := int(wheel.VisualRows)
		if wheel.Direction == MouseWheelScrollDown {
			delta = -delta
		}
		if !advanceViewportIntent(&s) {
			return fatalState(s, "terminal viewport intent generation exhausted"), nil, nil
		}
		s.transcript = s.transcript.Scroll(delta)
		return s, nil, nil
	}
	if overflow, ok := message.(LocalObservationOverflowMsg); ok {
		if overflow.Reason == 0 || !s.attachment.Valid || !s.durable.Installed() {
			return fatalState(s, "terminal local observation overflow is invalid"), nil, nil
		}
		s.durable, s.operational = presentation.Rebuild(s.durable, s.operational)
		s.pageCache = presentation.NewPageCache()
		s.observation.PendingPage, s.observation.HasPendingPage = protocolvalue.PreparedHistoryPageRequest{}, false
		s.observation.HasPageIntent = false
		s.phase = PhaseReadOnly
		next, effect, err := requestDurableSnapshot(s, overflow.Header.ProducedAt, nil, true)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		return validatedState(next), []Effect{effect}, nil
	}
	if due, ok := message.(ReconnectDueMsg); ok {
		if s.phase != PhaseReconnecting || s.connection.Phase != ConnectionBackoff || due.ReconnectGeneration != s.connection.Generation || s.connection.Outstanding.Carrier != OutstandingNone {
			return fatalState(s, "terminal reconnect timer authority is stale"), nil, nil
		}
		var token LocalOperationToken
		s.phase = PhaseConnecting
		s.connection.Phase = ConnectionDialing
		s, token = s.nextLocal(OpConnect, due.Header.ProducedAt.Add(localOperationDeadline))
		return s, []Effect{ConnectEffect{Header: newLocalHeader(token), BootstrapHandleID: s.connection.BootstrapHandleID, AttachmentAttemptGeneration: s.connection.HandshakeCandidate.AttachmentAttemptGeneration}}, nil
	}
	if tick, ok := message.(TickMsg); ok {
		observedAt := tick.Header.ProducedAt
		if tick.Kind == TickHeartbeat && s.phase == PhaseReady && s.connection.Outstanding.Carrier == OutstandingNone && !observedAt.Before(s.connection.HeartbeatSchedule.NextAt) {
			if tick.TickGeneration != s.connection.HeartbeatSchedule.NextGeneration {
				return fatalState(s, "terminal heartbeat tick generation is stale"), nil, nil
			}
			var token OperationToken
			s, token = s.nextWire(OpHeartbeat, observedAt.Add(wireOperationDeadline))
			request, err := protocolvalue.PrepareHeartbeatRequest(
				token.RequestID,
				s.attachment.Identity,
				s.connection.AttachReceipt,
				s.connection.HeartbeatSchedule.NextGeneration,
				s.connection.HeartbeatSchedule.LastAcceptedGeneration,
			)
			if err != nil {
				return fatalState(s, fmt.Sprintf("terminal heartbeat preparation: %v", err)), nil, nil
			}
			return s, []Effect{HeartbeatEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Request: request}}, nil
		}
		s, tickEffects := scheduleTickEffect(s, TickHeartbeat, s.connection.HeartbeatSchedule.NextGeneration, s.connection.HeartbeatSchedule.NextAt, observedAt)
		return s, tickEffects, nil
	}
	switch message.(type) {
	case ParentShutdownMsg:
		shutdown := message.(ParentShutdownMsg)
		observedAt := shutdown.Header.ProducedAt
		s.phase = PhaseDetaching
		reason := TeardownSignal
		if shutdown.Reason == ParentProcessExited {
			reason = TeardownParentShutdown
		} else if shutdown.Reason < ParentRequestedShutdown || shutdown.Reason > ParentProtocolRevoked {
			return fatalState(s, "terminal parent shutdown reason is invalid"), nil, nil
		}
		s.teardown = NewActiveTeardownState(reason, 1, observedAt.Add(5*time.Second))
		var token LocalOperationToken
		s, token = s.nextLocal(OpTeardown, s.teardown.Deadline)
		return s, []Effect{BeginTeardownEffect{Header: newLocalHeader(token), Reason: reason}}, nil
	case FrameworkInputRejectedMsg:
		return fatalState(s, "terminal framework input was outside its closed contract"), nil, nil
	case FrameworkAdvisoryIgnoredMsg:
		advisory := message.(FrameworkAdvisoryIgnoredMsg)
		if advisory.Kind < FrameworkAdvisoryEnvironment || advisory.Kind > FrameworkAdvisoryMousePointer || s.frameworkAdvisories == ^uint64(0) {
			return fatalState(s, "terminal framework advisory was outside its closed contract"), nil, nil
		}
		s.frameworkAdvisories++
		return s, nil, nil
	case PasteInputMsg, PasteBoundaryMsg, FocusChangedMsg, KeyboardEnhancementsObservedMsg:
		// Composer/secret behavior is intentionally dormant in S1.
		return s, nil, nil
	}
	if copied, ok := message.(PublicTextCopiedMsg); ok {
		if !s.clipboard.Pending || copied.Header.Operation != s.clipboard.Token {
			return validatedState(s), nil, nil
		}
		if err := validateMessage(copied); err != nil {
			return fatalState(s, fmt.Sprintf("terminal clipboard result contract: %v", err)), nil, nil
		}
		s.clipboard = ClipboardOperationState{}
		return validatedState(s), nil, nil
	}
	if failed, ok := message.(PublicTextCopyFailedMsg); ok {
		if !s.clipboard.Pending || failed.Header.Operation != s.clipboard.Token {
			return validatedState(s), nil, nil
		}
		if err := validateMessage(failed); err != nil {
			return fatalState(s, fmt.Sprintf("terminal clipboard failure contract: %v", err)), nil, nil
		}
		s.clipboard = ClipboardOperationState{}
		s = appendLocalNotification(s, "copy failed")
		return validatedState(s), nil, nil
	}

	operation, hasOperation := messageOutstanding(message)
	if hasOperation && operation != s.connection.Outstanding {
		next, effects := staleChallengeRevocationEffects(s, message, messageObservedAt(message))
		return validatedState(next), effects, nil
	}
	if hasOperation {
		if err := validateMessage(message); err != nil {
			return fatalState(s, fmt.Sprintf("terminal message contract: %v", err)), nil, nil
		}
		s = s.clearOutstanding()
	}

	now := messageObservedAt(message)
	if hasOperation && now.IsZero() {
		return fatalState(s, "terminal operation result has no observation time"), nil, nil
	}
	var effects []Effect
	switch value := message.(type) {
	case ConnectSucceededMsg:
		if value.Candidate.AttachmentAttemptGeneration != s.connection.HandshakeCandidate.AttachmentAttemptGeneration || value.Candidate.ClientInstanceID != s.connection.ClientInstanceID {
			return fatalState(s, "terminal connected handshake candidate is stale"), nil, nil
		}
		s.connection.Phase = ConnectionAuthPending
		s.connection.HandleID = value.ConnectionHandleID
		s.connection.HandshakeCandidate = value.Candidate
		if value.Candidate.AttachmentAttemptGeneration > 1 {
			if !s.connection.HasReconnectCredentialHandle {
				return fatalState(s, "terminal reconnect credential handle is unavailable"), nil, nil
			}
			s.connection.TransportCredentialHandleID = s.connection.ReconnectCredentialHandleID
		}
		var token OperationToken
		s, token = s.nextWire(OpTransportAuth, now.Add(wireOperationDeadline))
		effects = append(effects, AuthenticateTransportEffect{Header: newWireHeader(token), ConnectionHandleID: value.ConnectionHandleID, CredentialHandleID: s.connection.TransportCredentialHandleID, Candidate: s.connection.HandshakeCandidate})
	case ConnectFailedMsg:
		return transitionFailure(s, value.Failure, now)
	case TransportAuthenticatedMsg:
		s.phase = PhaseNegotiating
		s.connection.TransportAuth = value.Proof
		s.connection.HandshakeCandidate = value.Candidate
		s.connection.ServerConnectionID = value.Proof.ConnectionID
		s.connection.Phase = ConnectionHelloPending
		var token OperationToken
		s, token = s.nextWire(OpHello, now.Add(wireOperationDeadline))
		effects = append(effects, NegotiateHelloEffect{Header: newWireHeader(token), ConnectionHandleID: value.ConnectionHandleID, TransportAuthAttemptID: value.Proof.AttemptID, TransportAuthResultFingerprint: value.Proof.ResultFingerprint, Candidate: value.Candidate})
	case AttachRecoveredMsg:
		if !s.attachment.Valid || s.attachment.Identity.BindingFingerprint != value.Recovery.Binding.PreviousBindingFingerprint || s.attachment.Identity.SemanticWinnerFingerprint != value.Attachment.SemanticWinnerFingerprint {
			return fatalState(s, "terminal recovered attachment predecessor is stale"), nil, nil
		}
		s.connection.TransportAuth = value.Proof
		s.connection.HandshakeCandidate = value.Candidate
		s.connection.ServerConnectionID = value.Proof.ConnectionID
		s.connection.HandleID = value.ConnectionHandleID
		s.connection.Phase = ConnectionAttached
		s.connection.AttachReceipt.CurrentBinding = value.Recovery.Binding.ResultingBinding
		s.attachment = AttachmentState{Valid: true, Identity: value.Attachment}
		if s.connection.HeartbeatSchedule.NextGeneration == 0 {
			s.connection.HeartbeatSchedule = HeartbeatScheduleState{NextGeneration: 1, NextAt: now.Add(value.Attachment.Heartbeat.Interval), LeaseExpiresAt: value.Attachment.ExpiresAt}
		} else {
			s.connection.HeartbeatSchedule.NextAt = now.Add(value.Attachment.Heartbeat.Interval)
			s.connection.HeartbeatSchedule.LeaseExpiresAt = value.Attachment.ExpiresAt
		}
		s.phase = PhaseLoadingSnapshot
		var snapshotEffect RequestSnapshotEffect
		var err error
		s, snapshotEffect, err = requestDurableSnapshot(s, now, nil, true)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		effects = append(effects, snapshotEffect)
	case TransportAuthenticationFailedMsg:
		return transitionFailure(s, value.Failure, now)
	case HelloAcceptedMsg:
		if value.Winner.CandidateFingerprint != s.connection.HandshakeCandidate.Fingerprint || value.Receipt.CurrentConnectionID != s.connection.ServerConnectionID {
			return fatalState(s, "terminal Hello authority is stale"), nil, nil
		}
		s.connection.HelloWinner, s.connection.HelloReceipt = value.Winner, value.Receipt
		s.connection.AttachmentChallenge = AttachmentChallengeState{Phase: AttachmentChallengePreparedPromotionPending, Prepared: value.PreparedChallenge}
		var token LocalOperationToken
		s, token = s.nextLocal(OpChallengePromote, now.Add(localOperationDeadline))
		effects = append(effects, PromotePreparedAttachmentChallengeEffect{Header: newLocalHeader(token), Prepared: value.PreparedChallenge, ExpectedCandidateFingerprint: value.Winner.CandidateFingerprint, ExpectedHelloReceiptFingerprint: value.Receipt.ReceiptFingerprint, ExpectedConnectionID: value.Receipt.CurrentConnectionID})
	case HelloTransportFailedMsg:
		return transitionFailure(s, value.Failure, now)
	case HelloNegativeMsg:
		if value.Outcome.CandidateID != s.connection.HandshakeCandidate.ID || value.Outcome.CandidateFingerprint != s.connection.HandshakeCandidate.Fingerprint || value.Outcome.CurrentConnectionID != s.connection.ServerConnectionID {
			return fatalState(s, "terminal negative Hello authority is stale"), nil, nil
		}
		s.candidateTerminal = value.Outcome.TerminalReceipt
		s.connection.AttachmentChallenge = NewNoAttachmentChallenge()
		if value.Outcome.RequiresParentRelaunch() {
			cause := ParentRelaunchHelloRejected
			if value.Outcome.Kind == protocolvalue.HelloNegotiationUnavailable {
				cause = ParentRelaunchNegotiationWinnerUnavailable
			}
			s.phase = PhaseDetaching
			s.teardown = NewActiveTeardownState(TeardownParentRelaunch, 1, now.Add(localOperationDeadline))
			var token LocalOperationToken
			s, token = s.nextLocal(OpParentRelaunch, s.teardown.Deadline)
			effects = append(effects, RequestParentRelaunchEffect{Header: newLocalHeader(token), CandidateTerminalReceipt: value.Outcome.TerminalReceipt, Cause: cause, NegativeOutcomeFingerprint: value.Outcome.OutcomeFingerprint})
			break
		}
		failure, err := classifyPublicFailure(
			NewOutstandingWire(value.Header.Operation),
			DeliveryResponseFullyValidated,
			FailureConnectionUsable,
			CauseProtocolSchemaRejected,
			"",
			false,
			value.Outcome.OutcomeFingerprint,
			"The terminal client is incompatible with the server.",
		)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		s.publicFailure, s.hasPublicFailure = failure, true
		s.phase = PhaseDetaching
		s.teardown = NewActiveTeardownState(TeardownFatalCompatibility, 1, now.Add(localOperationDeadline))
		var token LocalOperationToken
		s, token = s.nextLocal(OpTeardown, s.teardown.Deadline)
		effects = append(effects, BeginTeardownEffect{Header: newLocalHeader(token), Reason: TeardownFatalCompatibility})
	case AttachmentChallengePromotedMsg:
		if value.Receipt.PreparedHandleFingerprint != s.connection.AttachmentChallenge.Prepared.HandleFingerprint {
			return fatalState(s, "terminal challenge promotion is stale"), nil, nil
		}
		s.connection.AttachmentChallenge = AttachmentChallengeState{Phase: AttachmentChallengeActiveAcceptancePending, Prepared: s.connection.AttachmentChallenge.Prepared, Promotion: value.Receipt}
		var token LocalOperationToken
		s, token = s.nextLocal(OpChallengePromotionConfirm, now.Add(localOperationDeadline))
		effects = append(effects, ConfirmAttachmentChallengePromotionEffect{Header: newLocalHeader(token), Promotion: value.Receipt, ExpectedCandidateFingerprint: s.connection.HandshakeCandidate.Fingerprint, ExpectedHelloReceiptFingerprint: s.connection.HelloReceipt.ReceiptFingerprint, ExpectedConnectionID: s.connection.ServerConnectionID})
	case AttachmentChallengePromotionAcceptedMsg:
		if value.Receipt.PreparedHandleFingerprint != s.connection.AttachmentChallenge.Prepared.HandleFingerprint || value.Receipt.PromotionReceiptFingerprint != s.connection.AttachmentChallenge.Promotion.PromotionReceiptFingerprint {
			return fatalState(s, "terminal challenge acceptance is stale"), nil, nil
		}
		s.phase = PhaseAttaching
		s.connection.Phase = ConnectionAttachPending
		s.connection.AttachmentChallenge = AttachmentChallengeState{Phase: AttachmentChallengeActive, Prepared: s.connection.AttachmentChallenge.Prepared, Promotion: s.connection.AttachmentChallenge.Promotion, Acceptance: value.Receipt}
		var token OperationToken
		s, token = s.nextWire(OpAttach, now.Add(wireOperationDeadline))
		effects = append(effects, AttachEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Candidate: s.connection.HandshakeCandidate, HelloNegotiationWinnerFingerprint: s.connection.HelloWinner.NegotiationWinnerFingerprint, ServerHelloReceiptFingerprint: s.connection.HelloReceipt.ReceiptFingerprint, ActiveAttachmentChallengeHandleID: s.connection.AttachmentChallenge.Prepared.HandleID, AttachmentChallengeAcceptance: value.Receipt, AttachmentChallengeCommitment: s.connection.HelloReceipt.ChallengeCommitment})
	case AttachmentChallengePromotionFailedMsg:
		s.connection.AttachmentChallenge.Phase = AttachmentChallengeRevoked
		return transitionFailure(s, value.Failure, now)
	case AttachAcceptedMsg:
		if value.Attachment.AttachmentAttemptGeneration != s.connection.HandshakeCandidate.AttachmentAttemptGeneration || value.Receipt.CandidateFingerprint != s.connection.HandshakeCandidate.Fingerprint || value.Attachment.ConnectionID != s.connection.ServerConnectionID {
			return fatalState(s, "terminal attach authority is stale"), nil, nil
		}
		s.attachment = AttachmentState{Valid: true, Identity: value.Attachment}
		s.connection.AttachmentChallenge.Phase = AttachmentChallengeConsumed
		s.connection.AttachReceipt = value.Receipt
		s.connection.ReconnectCredentialHandleID = value.ReconnectCredentialHandleID
		s.connection.ReconnectCredentialCarrierFingerprint = value.Receipt.ReconnectCarrierFingerprint
		s.connection.HasReconnectCredentialHandle = true
		s.connection.Phase = ConnectionAttachAckPending
		var token OperationToken
		s, token = s.nextWire(OpAttachAck, now.Add(wireOperationDeadline))
		effects = append(effects, AcknowledgeAttachEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, SemanticWinnerFingerprint: value.Attachment.SemanticWinnerFingerprint, AttachResultReceiptFingerprint: value.Receipt.ReceiptFingerprint})
	case AttachRejectedMsg:
		s.connection.AttachmentChallenge.Phase = AttachmentChallengeRevoked
		return transitionFailure(s, value.Failure, now)
	case AttachAcknowledgedMsg:
		if !s.attachment.Valid || value.Result.AttachmentID != s.attachment.Identity.ID || value.Result.AttachmentGeneration != s.attachment.Identity.Generation || value.Result.SemanticWinnerFingerprint != s.attachment.Identity.SemanticWinnerFingerprint || value.Result.AcknowledgedBindingFingerprint != s.attachment.Identity.BindingFingerprint {
			return fatalState(s, "terminal attachment acknowledgement mismatch"), nil, nil
		}
		s.connection.Phase = ConnectionAttached
		s.connection.HeartbeatSchedule = HeartbeatScheduleState{NextGeneration: 1, NextAt: now.Add(s.attachment.Identity.Heartbeat.Interval), LeaseExpiresAt: s.attachment.Identity.ExpiresAt}
		s.phase = PhaseLoadingSnapshot
		var snapshotEffect RequestSnapshotEffect
		var err error
		s, snapshotEffect, err = requestDurableSnapshot(s, now, nil, true)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		effects = append(effects, snapshotEffect)
	case AttachAckFailedMsg:
		return transitionFailure(s, value.Failure, now)
	case SnapshotAcceptedMsg:
		if value.Snapshot.RuntimeSessionID != s.attachment.Identity.RuntimeSessionID || value.Snapshot.Control.RuntimeSessionID != s.attachment.Identity.RuntimeSessionID {
			return fatalState(s, "terminal durable snapshot crosses runtime sessions"), nil, nil
		}
		operationalRequired := s.snapshotLoading.OperationalRequired
		followLatest := !s.pageCache.Ready() || s.pageCache.ShouldFollowLatest(s.transcript.FollowTail())
		durable, err := s.durable.Install(value.Snapshot)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		pageCache, err := s.pageCache.InstallLatest(value.Snapshot, followLatest, s.attachment.Identity.ID, s.attachment.Identity.Generation)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		viewportSnapshot, err := pageCache.MaterializeCurrent(value.Snapshot)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		transcriptModel := s.transcript
		if s.transcript.Ready() {
			transcriptModel, err = s.transcript.Replace(viewportSnapshot, s.layout.Width, s.layout.TranscriptRows)
		} else {
			transcriptModel, err = s.transcript.Install(viewportSnapshot, s.layout.Width, s.layout.TranscriptRows)
		}
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		control, err := s.control.Install(value.Snapshot.Control, value.Snapshot.RuntimeSessionID)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		targetID := value.Snapshot.Control.PendingInteractionID
		targetGeneration := value.Snapshot.Control.PendingInteractionGeneration
		if !value.Snapshot.Control.PendingInteraction {
			targetID, targetGeneration = "", 0
		}
		interactionState, err := interaction.NewReadOnlyProjectedInteraction(
			value.Snapshot.Control.CursorFingerprint,
			value.Snapshot.Control.PendingInteractionViewFingerprint,
			targetID,
			targetGeneration,
		)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		queueState, err := queue.NewReadOnlyProjectedQueue(
			value.Snapshot.Control.CursorFingerprint,
			value.Snapshot.Control.QueueViewFingerprint,
			queue.S1MaximumActiveItems,
			uint32(len(value.Snapshot.Control.QueueItems)),
		)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		s.durable = durable
		s.transcript = transcriptModel
		s.pageCache = pageCache
		s.control = control
		s.interaction = interactionState
		s.queue = queueState
		s.snapshotLoading.DurableSnapshotFingerprint = value.Snapshot.SnapshotFingerprint
		s.snapshotLoading.DurableControlCursorFingerprint = value.Snapshot.Control.CursorFingerprint
		if operationalRequired || !s.operational.Ready() {
			s.snapshotLoading.OperationalRequired = true
			var operationalEffect RequestOperationalSnapshotEffect
			s, operationalEffect, err = requestOperationalSnapshot(s, now)
			if err != nil {
				return fatalState(s, fmt.Sprintf("terminal operational snapshot preparation: %v", err)), nil, nil
			}
			effects = append(effects, operationalEffect)
			break
		}
		existingOperational := s.operational.Snapshot()
		s.snapshotLoading = SnapshotLoadingState{Phase: SnapshotBaselinesInstalled, AttachmentID: s.attachment.Identity.ID, AttachmentGeneration: s.attachment.Identity.Generation, TransportBindingFingerprint: s.attachment.Identity.BindingFingerprint, DurableSnapshotFingerprint: value.Snapshot.SnapshotFingerprint, DurableControlCursorFingerprint: value.Snapshot.Control.CursorFingerprint, OperationalSnapshotFingerprint: existingOperational.FrameFingerprint, OperationalGeneration: existingOperational.Generation, OperationalCursor: existingOperational.Cursor}
		s.phase = PhaseReady
		s.observation.SnapshotRebaseRounds = 0
		s, liveEffects, liveErr := nextLiveEffect(s, now)
		if liveErr != nil {
			return fatalState(s, liveErr.Error()), nil, nil
		}
		return validatedState(s), liveEffects, nil
	case SnapshotControlRebaseRequiredMsg:
		if !s.control.SnapshotRequired() || !value.Request.HasMinimum || value.Outcome.RequestedMinimumFingerprint != value.Request.MinimumObservedControlCursor.Fingerprint || s.observation.SnapshotRebaseRounds >= 4 {
			return fatalState(s, "terminal control snapshot rebase exceeded its closed contract"), nil, nil
		}
		s.observation.SnapshotRebaseRounds++
		s.phase = PhaseReadOnly
		next, snapshotEffect, err := requestDurableSnapshot(s, now, &value.Outcome.LatestControlCursor, false)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		return validatedState(next), []Effect{snapshotEffect}, nil
	case SnapshotRejectedMsg:
		return transitionFailure(s, value.Failure, now)
	case OperationalSnapshotAcceptedMsg:
		var err error
		s.operational, err = s.operational.Install(value.Snapshot, s.attachment.Identity.RuntimeSessionID)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		s.snapshotLoading.Phase = SnapshotBaselinesInstalled
		s.snapshotLoading.OperationalRequired = false
		if value.Snapshot.AttachmentID != s.attachment.Identity.ID || value.Snapshot.AttachmentGeneration != s.attachment.Identity.Generation || value.Snapshot.AttachmentIdentityFingerprint != s.attachment.Identity.IdentityFingerprint || value.Snapshot.AcknowledgedBindingFingerprint != s.attachment.Identity.BindingFingerprint {
			return fatalState(s, "terminal operational snapshot authority is stale"), nil, nil
		}
		s.snapshotLoading.OperationalSnapshotFingerprint = value.Snapshot.FrameFingerprint
		s.snapshotLoading.OperationalGeneration = value.Snapshot.Generation
		s.snapshotLoading.OperationalCursor = value.Snapshot.Cursor
		s.phase = PhaseReady
		s.observation.SnapshotRebaseRounds = 0
		s, liveEffects, liveErr := nextLiveEffect(s, now)
		if liveErr != nil {
			return fatalState(s, liveErr.Error()), nil, nil
		}
		return validatedState(s), liveEffects, nil
	case OperationalSnapshotRejectedMsg:
		return transitionFailure(s, value.Failure, now)
	case ObservationBatchMsg:
		next, observationEffects, err := applyObservationBatch(s, value.Request, value.Batch, now)
		if err != nil {
			if errors.Is(err, errOperationalObservation) {
				s.operational = s.operational.Invalidate()
				s.phase = PhaseReadOnly
				s.snapshotLoading = SnapshotLoadingState{Phase: SnapshotAwaitingOperationalSnapshot, AttachmentID: s.attachment.Identity.ID, AttachmentGeneration: s.attachment.Identity.Generation, TransportBindingFingerprint: s.attachment.Identity.BindingFingerprint, DurableSnapshotFingerprint: s.durable.SnapshotFingerprint(), DurableControlCursorFingerprint: s.control.ConfirmedCursor().Fingerprint, OperationalRequired: true}
				rebuilt, effect, rebuildErr := requestOperationalSnapshot(s, now)
				if rebuildErr != nil {
					return fatalState(s, rebuildErr.Error()), nil, nil
				}
				return validatedState(rebuilt), []Effect{effect}, nil
			}
			s.durable, s.operational = presentation.Rebuild(s.durable, s.operational)
			s.phase = PhaseReadOnly
			rebuilt, effect, rebuildErr := requestDurableSnapshot(s, now, nil, true)
			if rebuildErr != nil {
				return fatalState(s, rebuildErr.Error()), nil, nil
			}
			return validatedState(rebuilt), []Effect{effect}, nil
		}
		return validatedState(next), observationEffects, nil
	case ObservationNoChangeMsg:
		s.observation.LastResultFingerprint = value.NoChange.Fingerprint
		s.phase = PhaseReady
		next, liveEffects, err := nextLiveEffect(s, now)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		return validatedState(next), liveEffects, nil
	case ObservationRejectedMsg:
		return transitionFailure(s, value.Failure, now)
	case HistoryPageAcceptedMsg:
		next, pageEffects, err := applyHistoryPageResult(s, value.Request, value.Result, now)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		return validatedState(next), pageEffects, nil
	case HistoryPageRejectedMsg:
		s.observation.PendingPage, s.observation.HasPendingPage = protocolvalue.PreparedHistoryPageRequest{}, false
		return transitionFailure(s, value.Failure, now)
	case HeartbeatAcceptedMsg:
		if !s.attachment.Valid || value.Request.HeartbeatGeneration != s.connection.HeartbeatSchedule.NextGeneration || value.Request.PreviousAcceptedGeneration != s.connection.HeartbeatSchedule.LastAcceptedGeneration || value.Receipt.HeartbeatGeneration != value.Request.HeartbeatGeneration || value.Receipt.PreviousAcceptedGeneration != value.Request.PreviousAcceptedGeneration || value.Receipt.AcknowledgedBindingFingerprint != s.attachment.Identity.BindingFingerprint {
			return fatalState(s, "terminal heartbeat receipt is stale"), nil, nil
		}
		if value.Receipt.LivenessDisposition == protocolvalue.HeartbeatSessionClosing {
			s.phase = PhaseReadOnly
			return validatedState(s), nil, nil
		}
		s.connection.HeartbeatSchedule = HeartbeatScheduleState{NextGeneration: value.Receipt.HeartbeatGeneration + 1, LastAcceptedGeneration: value.Receipt.HeartbeatGeneration, LastDisposition: value.Receipt.LivenessDisposition, NextAt: value.Header.ReceivedAt.Add(s.attachment.Identity.Heartbeat.Interval), LeaseExpiresAt: value.Receipt.LeaseExpiresAt, LastReceiptFingerprint: value.Receipt.ReceiptFingerprint}
		s, liveEffects, liveErr := nextLiveEffect(s, value.Header.ReceivedAt)
		if liveErr != nil {
			return fatalState(s, liveErr.Error()), nil, nil
		}
		return validatedState(s), liveEffects, nil
	case HeartbeatRejectedMsg:
		_ = value
		return fatalState(s, "terminal heartbeat was rejected by the runtime"), nil, nil
	case HeartbeatTransportFailedMsg:
		return transitionFailure(s, value.Failure, now)
	case TeardownCompletedMsg:
		if value.Summary.TeardownGeneration != s.teardown.Generation {
			return fatalState(s, "terminal teardown result is stale"), nil, nil
		}
		s.teardown.Phase = TeardownTerminal
		s.teardown.PhysicalOperationCount = value.Summary.CancelledOperationCount + value.Summary.DrainedOperationCount
		s.teardown.SecretRuntimeRevoked = true
		s.teardown.SchedulerDrained = value.Summary.Disposition == TeardownCompleted
		s.teardown.BridgeDrained = true
		s.teardown.TerminalRestoreCompleted = value.Summary.TerminalRestoreCompleted
		if value.Summary.HasFailure {
			s.publicFailure, s.hasPublicFailure = value.Summary.Failure, true
		}
		s.phase = PhaseExited
		s, quit := quitProgramEffect(s, value.Header.ReceivedAt)
		return validatedState(s), []Effect{quit}, nil
	case TeardownFailedMsg:
		s.publicFailure, s.hasPublicFailure = value.Failure, true
		s.teardown.Phase = TeardownTerminal
		s.phase = PhaseExited
		s, quit := quitProgramEffect(s, value.Header.ReceivedAt)
		return validatedState(s), []Effect{quit}, nil
	case ParentRelaunchPreparedMsg:
		if value.CandidateTerminalReceipt.TerminalReceiptFingerprint != s.candidateTerminal.TerminalReceiptFingerprint || value.NegativeOutcomeFingerprint == "" || value.Cause == 0 {
			return fatalState(s, "terminal parent relaunch result is stale"), nil, nil
		}
		s.parentRelaunch = true
		s.parentRelaunchCause = value.Cause
		s.teardown.Phase = TeardownTerminal
		s.teardown.SecretRuntimeRevoked = true
		s.teardown.SchedulerDrained = true
		s.teardown.BridgeDrained = true
		s.teardown.TerminalRestoreCompleted = true
		s.phase = PhaseExited
		s, quit := quitProgramEffect(s, value.Header.ReceivedAt)
		return validatedState(s), []Effect{quit}, nil
	case ParentRelaunchFailedMsg:
		s.publicFailure, s.hasPublicFailure = value.Failure, true
		s.teardown.Phase = TeardownTerminal
		s.phase = PhaseExited
		s, quit := quitProgramEffect(s, value.Header.ReceivedAt)
		return validatedState(s), []Effect{quit}, nil
	}
	return validatedState(s), effects, nil
}

func boundedClipboardText(cells []protocolvalue.HistoryCell) (string, error) {
	var text strings.Builder
	bytes, runes := 0, 0
	for _, cell := range cells {
		cellBytes := len(cell.PublicText) + 1
		cellRunes := len([]rune(cell.PublicText)) + 1
		if bytes > MaximumPublicClipboardBytes-cellBytes || runes > MaximumPublicClipboardRunes-cellRunes {
			return "", errors.New("terminal public clipboard payload exceeds its closed bound")
		}
		text.WriteString(cell.PublicText)
		text.WriteByte('\n')
		bytes += cellBytes
		runes += cellRunes
	}
	return text.String(), nil
}

func appendLocalNotification(state AppState, message string) AppState {
	if len(message) > 256 {
		message = message[:256]
	}
	if len(state.localNotifications.Items) == maximumLocalNotifications {
		state.localNotifications.Items = append([]string(nil), state.localNotifications.Items[1:]...)
		state.localNotifications.Dropped++
	}
	state.localNotifications.Items = append(state.localNotifications.Items, message)
	return state
}

func staleChallengeRevocationEffects(
	s AppState,
	message any,
	observedAt time.Time,
) (AppState, []Effect) {
	if observedAt.IsZero() {
		return s, nil
	}
	switch value := message.(type) {
	case HelloAcceptedMsg:
		if value.PreparedChallenge.Validate() != nil {
			return s, nil
		}
		var token LocalOperationToken
		s, token = s.nextDetachedLocal(
			OpChallengeRevokePrepared,
			observedAt.Add(localOperationDeadline),
		)
		return s, []Effect{RevokePreparedAttachmentChallengeEffect{
			Header:            newLocalHeader(token),
			HandleFingerprint: value.PreparedChallenge.HandleFingerprint,
			Reason:            ChallengeRevokeStaleApplicationResult,
		}}
	case AttachmentChallengePromotedMsg:
		if value.Receipt.Validate() != nil {
			return s, nil
		}
		var token LocalOperationToken
		s, token = s.nextDetachedLocal(
			OpChallengeRevokeActive,
			observedAt.Add(localOperationDeadline),
		)
		return s, []Effect{RevokeActiveAttachmentChallengeEffect{
			Header:               newLocalHeader(token),
			HandleFingerprint:    value.Receipt.PreparedHandleFingerprint,
			PromotionFingerprint: value.Receipt.PromotionReceiptFingerprint,
			Reason:               ChallengeRevokeStaleApplicationResult,
		}}
	case AttachmentChallengePromotionAcceptedMsg:
		if value.Receipt.Validate() != nil {
			return s, nil
		}
		var token LocalOperationToken
		s, token = s.nextDetachedLocal(
			OpChallengeRevokeActive,
			observedAt.Add(localOperationDeadline),
		)
		return s, []Effect{RevokeActiveAttachmentChallengeEffect{
			Header:               newLocalHeader(token),
			HandleFingerprint:    value.Receipt.PreparedHandleFingerprint,
			PromotionFingerprint: value.Receipt.PromotionReceiptFingerprint,
			Reason:               ChallengeRevokeStaleApplicationResult,
		}}
	default:
		return s, nil
	}
}

func validateMessage(message any) error {
	if operation, ok := messageOutstanding(message); ok {
		if !operation.Valid() || messageObservedAt(message).IsZero() {
			return fmt.Errorf("terminal operation result header is invalid")
		}
	}
	switch value := message.(type) {
	case ConnectSucceededMsg:
		return value.validate()
	case ConnectFailedMsg:
		return validateLocalFailure(value.Header, value.Failure, OpConnect)
	case TransportAuthenticatedMsg:
		return value.validate()
	case TransportAuthenticationFailedMsg:
		return validateWireFailure(value.Header, value.Failure, OpTransportAuth)
	case AttachRecoveredMsg:
		return value.validate()
	case HelloAcceptedMsg:
		return value.validate()
	case HelloTransportFailedMsg:
		return validateWireFailure(value.Header, value.Failure, OpHello)
	case HelloNegativeMsg:
		if value.Header.Operation.Kind != OpHello || value.Outcome.OutcomeFingerprint != value.Header.PayloadFingerprint || value.Outcome.RequestID != value.Header.Operation.RequestID || value.Outcome.TerminalReceipt.TerminalReceiptFingerprint == "" {
			return fmt.Errorf("terminal negative Hello result is invalid")
		}
		return nil
	case AttachmentChallengePromotedMsg:
		if value.Header.Operation.Kind != OpChallengePromote || value.Receipt.Validate() != nil || value.Receipt.PromotionReceiptFingerprint != value.Header.PayloadFingerprint || value.Receipt.PromotionOperationID != value.Header.Operation.OperationID || value.Receipt.PromotionOperationGeneration != value.Header.Operation.OperationGeneration {
			return fmt.Errorf("challenge promotion result is invalid")
		}
		return nil
	case AttachmentChallengePromotionAcceptedMsg:
		if value.Header.Operation.Kind != OpChallengePromotionConfirm || value.Receipt.Validate() != nil || value.Receipt.AcceptanceReceiptFingerprint != value.Header.PayloadFingerprint || value.Receipt.ConfirmationOperationID != value.Header.Operation.OperationID || value.Receipt.ConfirmationOperationGeneration != value.Header.Operation.OperationGeneration {
			return fmt.Errorf("challenge acceptance result is invalid")
		}
		return nil
	case AttachmentChallengePromotionFailedMsg:
		if value.Header.Operation.Kind != OpChallengePromote && value.Header.Operation.Kind != OpChallengePromotionConfirm {
			return fmt.Errorf("challenge failure operation kind is invalid")
		}
		return validateLocalFailure(value.Header, value.Failure, value.Header.Operation.Kind)
	case AttachAcceptedMsg:
		return value.validate()
	case AttachRejectedMsg:
		return validateWireFailure(value.Header, value.Failure, OpAttach)
	case AttachAcknowledgedMsg:
		return value.validate()
	case AttachAckFailedMsg:
		return validateWireFailure(value.Header, value.Failure, OpAttachAck)
	case SnapshotAcceptedMsg:
		return value.validate()
	case SnapshotControlRebaseRequiredMsg:
		return value.validate()
	case SnapshotRejectedMsg:
		return validateWireFailure(value.Header, value.Failure, OpProjectionSnapshot)
	case OperationalSnapshotAcceptedMsg:
		return value.validate()
	case OperationalSnapshotRejectedMsg:
		return validateWireFailure(value.Header, value.Failure, OpOperationalSnapshot)
	case ObservationBatchMsg:
		return value.validate()
	case ObservationNoChangeMsg:
		return value.validate()
	case ObservationRejectedMsg:
		return validateWireFailure(value.Header, value.Failure, OpObserve)
	case HistoryPageAcceptedMsg:
		return value.validate()
	case HistoryPageRejectedMsg:
		return validateWireFailure(value.Header, value.Failure, OpHistoryPage)
	case HeartbeatAcceptedMsg:
		return value.validate()
	case HeartbeatRejectedMsg:
		return value.validate()
	case HeartbeatTransportFailedMsg:
		return validateWireFailure(value.Header, value.Failure, OpHeartbeat)
	case TeardownCompletedMsg:
		fingerprint, err := value.Summary.Fingerprint()
		if value.Header.Operation.Kind != OpTeardown || value.Summary.Validate() != nil ||
			err != nil || fingerprint != value.Header.PayloadFingerprint {
			return fmt.Errorf("terminal teardown result is invalid")
		}
		return nil
	case TeardownFailedMsg:
		return validateLocalFailure(value.Header, value.Failure, OpTeardown)
	case ParentRelaunchPreparedMsg:
		if value.Header.Operation.Kind != OpParentRelaunch || value.CandidateTerminalReceipt.TerminalReceiptFingerprint == "" || value.Cause == 0 || value.NegativeOutcomeFingerprint != value.Header.PayloadFingerprint {
			return fmt.Errorf("terminal parent relaunch result is invalid")
		}
		return nil
	case ParentRelaunchFailedMsg:
		return validateLocalFailure(value.Header, value.Failure, OpParentRelaunch)
	case PublicTextCopiedMsg:
		if value.Header.Operation.Kind != OpClipboard || value.Header.PayloadFingerprint == "" {
			return fmt.Errorf("terminal public clipboard result is invalid")
		}
		return nil
	case PublicTextCopyFailedMsg:
		return validateLocalFailure(value.Header, value.Failure, OpClipboard)
	default:
		return fmt.Errorf("unknown terminal application message %T", message)
	}
}

func validateWireFailure(header IOMessageHeader, failure PublicFailure, kind OperationKind) error {
	production := failure.Production()
	requestID, hasRequestID := production.RequestID()
	if header.Operation.Kind != kind || failure.Validate() != nil ||
		header.PayloadFingerprint != failure.EvidenceFingerprint() ||
		production.OperationKind() != kind ||
		production.OperationID() != header.Operation.OperationID ||
		!hasRequestID || requestID != header.Operation.RequestID {
		return fmt.Errorf("terminal wire failure result is invalid")
	}
	return nil
}

func validateLocalFailure(header LocalResultHeader, failure PublicFailure, kind OperationKind) error {
	production := failure.Production()
	requestID, hasRequestID := production.RequestID()
	if header.Operation.Kind != kind || failure.Validate() != nil ||
		header.PayloadFingerprint != failure.EvidenceFingerprint() ||
		production.OperationKind() != kind ||
		production.OperationID() != header.Operation.OperationID ||
		hasRequestID || requestID != "" {
		return fmt.Errorf("terminal local failure result is invalid")
	}
	return nil
}

func validatedState(s AppState) AppState {
	if err := s.Validate(); err != nil {
		return fatalState(s, fmt.Sprintf("client invariant: %v", err))
	}
	return s
}
func fatalState(s AppState, message string) AppState {
	operation := s.connection.Outstanding
	if operation.Carrier == OutstandingNone {
		generation := s.connection.NextOperationGeneration
		if generation == 0 {
			generation = 1
		}
		appGeneration := s.appGeneration
		if appGeneration == 0 {
			appGeneration = 1
		}
		operation = NewOutstandingLocal(LocalOperationToken{
			Kind:                OpTeardown,
			OperationID:         fmt.Sprintf("terminal-client-invariant:%s:%d:%d", s.connection.ClientInstanceID, appGeneration, generation),
			OperationGeneration: generation,
			AppGeneration:       appGeneration,
			Deadline:            time.Unix(1, 0).UTC(),
		})
	}
	receiptFingerprint, fingerprintErr := protocolvalue.CanonicalClientFingerprint(
		"terminal-application-invariant-receipt:v1",
		map[string]any{
			"operation_id": operationIDForFailure(operation),
			"message":      message,
		},
	)
	if fingerprintErr != nil {
		panic(fingerprintErr)
	}
	deliveryPhase := DeliveryLocalOperationStarted
	if operation.Carrier == OutstandingWire {
		deliveryPhase = DeliveryNotStarted
	}
	failure, err := classifyPublicFailure(
		operation,
		deliveryPhase,
		FailureConnectionUsable,
		CauseClientInvariant,
		"",
		false,
		receiptFingerprint,
		message,
	)
	if err != nil {
		panic(err)
	}
	return failedState(s, failure)
}

func operationIDForFailure(operation OutstandingOperation) string {
	if operation.Carrier == OutstandingWire {
		return operation.Wire.OperationID
	}
	return operation.Local.OperationID
}
func failedState(s AppState, failure PublicFailure) AppState {
	s.publicFailure, s.hasPublicFailure = failure, true
	s.phase = PhaseFatal
	s.connection.Outstanding = OutstandingOperation{}
	if s.snapshotLoading.Phase != SnapshotBaselinesInstalled {
		s.snapshotLoading = SnapshotLoadingState{Phase: SnapshotLoadingUninitialized}
	}
	return s
}

func transitionFailure(s AppState, failure PublicFailure, now time.Time) (AppState, []Effect, tea.Cmd) {
	switch failure.Disposition() {
	case FailureReconnect, FailureRetryWithBackoff:
		previous := s.connection
		candidate := previous.HandshakeCandidate
		credentialHandle := previous.TransportCredentialHandleID
		if s.attachment.Valid && previous.Phase == ConnectionAttached {
			if !previous.HasReconnectCredentialHandle {
				return failedState(s, failure), nil, nil
			}
			candidate = protocolvalue.HandshakeCandidate{AttachmentAttemptGeneration: previous.HandshakeCandidate.AttachmentAttemptGeneration + 1}
			credentialHandle = previous.ReconnectCredentialHandleID
			s.durable = s.durable.MarkStale()
			s.operational = s.operational.Invalidate()
			s.observation.Enabled = false
			s.observation.PendingPage, s.observation.HasPendingPage = protocolvalue.PreparedHistoryPageRequest{}, false
		}
		s.phase = PhaseReconnecting
		s.connection = ConnectionState{
			Phase:                                 ConnectionBackoff,
			ClientInstanceID:                      previous.ClientInstanceID,
			BootstrapHandleID:                     previous.BootstrapHandleID,
			TransportCredentialHandleID:           credentialHandle,
			ReconnectCredentialHandleID:           previous.ReconnectCredentialHandleID,
			ReconnectCredentialCarrierFingerprint: previous.ReconnectCredentialCarrierFingerprint,
			HasReconnectCredentialHandle:          previous.HasReconnectCredentialHandle,
			Generation:                            previous.Generation + 1,
			NextOperationGeneration:               previous.NextOperationGeneration,
			HandshakeCandidate:                    candidate,
			HelloWinner:                           previous.HelloWinner,
			AttachReceipt:                         previous.AttachReceipt,
			HeartbeatSchedule:                     previous.HeartbeatSchedule,
			AttachmentChallenge:                   NewNoAttachmentChallenge(),
		}
		s, effects := scheduleTickEffect(s, TickReconnect, s.connection.Generation, now.Add(100*time.Millisecond), now)
		return validatedState(s), effects, nil
	case FailureRebuildDurableSnapshot:
		if !s.attachment.Valid {
			return failedState(s, failure), nil, nil
		}
		s.phase = PhaseLoadingSnapshot
		next, effect, err := requestDurableSnapshot(s, now, nil, true)
		if err != nil {
			return fatalState(s, err.Error()), nil, nil
		}
		return validatedState(next), []Effect{effect}, nil
	case FailureRebuildOperationalSnapshot:
		if !s.attachment.Valid || !s.durable.Ready() || !s.control.Ready() {
			return failedState(s, failure), nil, nil
		}
		s.phase = PhaseLoadingSnapshot
		s.snapshotLoading = SnapshotLoadingState{Phase: SnapshotAwaitingOperationalSnapshot, AttachmentID: s.attachment.Identity.ID, AttachmentGeneration: s.attachment.Identity.Generation, TransportBindingFingerprint: s.attachment.Identity.BindingFingerprint, DurableSnapshotFingerprint: s.durable.SnapshotFingerprint(), DurableControlCursorFingerprint: s.control.ConfirmedCursor().Fingerprint, OperationalRequired: true}
		next, effect, err := requestOperationalSnapshot(s, now)
		if err != nil {
			return fatalState(s, fmt.Sprintf("terminal operational snapshot preparation: %v", err)), nil, nil
		}
		return validatedState(next), []Effect{effect}, nil
	default:
		return failedState(s, failure), nil, nil
	}
}

func scheduleTickEffect(
	s AppState,
	kind TickKind,
	generation uint64,
	dueAt time.Time,
	observedAt time.Time,
) (AppState, []Effect) {
	if kind == 0 || generation == 0 || dueAt.IsZero() || observedAt.IsZero() {
		return s, nil
	}
	deadline := dueAt
	if !deadline.After(observedAt) {
		deadline = observedAt.Add(time.Second)
	}
	var token LocalOperationToken
	s, token = s.nextDetachedLocal(OpTick, deadline)
	return s, []Effect{ScheduleTickEffect{Header: newLocalHeader(token), Kind: kind, TickGeneration: generation, DueAt: dueAt}}
}

func quitProgramEffect(s AppState, observedAt time.Time) (AppState, QuitProgramEffect) {
	var token LocalOperationToken
	s, token = s.nextDetachedLocal(OpTeardown, observedAt.Add(localOperationDeadline))
	return s, QuitProgramEffect{Header: newLocalHeader(token)}
}

func advanceViewportIntent(s *AppState) bool {
	if s.observation.ViewportIntentGeneration == ^uint64(0) {
		return false
	}
	s.observation.ViewportIntentGeneration++
	return true
}

func requestDurableSnapshot(
	s AppState,
	now time.Time,
	minimum *protocolvalue.ControlCursor,
	operationalRequired bool,
) (AppState, RequestSnapshotEffect, error) {
	var token OperationToken
	s, token = s.nextWire(OpProjectionSnapshot, now.Add(wireOperationDeadline))
	request, err := protocolvalue.PrepareProjectionSnapshotRequest(token.RequestID, s.attachment.Identity.RuntimeSessionID, minimum)
	if err != nil {
		return s, RequestSnapshotEffect{}, err
	}
	s.snapshotLoading = SnapshotLoadingState{
		Phase:                       SnapshotAwaitingDurableSnapshot,
		AttachmentID:                s.attachment.Identity.ID,
		AttachmentGeneration:        s.attachment.Identity.Generation,
		TransportBindingFingerprint: s.attachment.Identity.BindingFingerprint,
		DurableOperationID:          token.OperationID,
		DurableOperationGeneration:  token.OperationGeneration,
		OperationalRequired:         operationalRequired,
	}
	return s, RequestSnapshotEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Request: request}, nil
}

func nextLiveEffect(s AppState, now time.Time) (AppState, []Effect, error) {
	if s.phase != PhaseReady || s.connection.Outstanding.Carrier != OutstandingNone || !s.durable.Ready() || !s.operational.Ready() || !s.control.Ready() {
		return s, nil, nil
	}
	if !now.Before(s.connection.HeartbeatSchedule.NextAt) {
		var token OperationToken
		s, token = s.nextWire(OpHeartbeat, now.Add(wireOperationDeadline))
		request, err := protocolvalue.PrepareHeartbeatRequest(token.RequestID, s.attachment.Identity, s.connection.AttachReceipt, s.connection.HeartbeatSchedule.NextGeneration, s.connection.HeartbeatSchedule.LastAcceptedGeneration)
		if err != nil {
			return s, nil, err
		}
		return s, []Effect{HeartbeatEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Request: request}}, nil
	}
	if s.observation.HasPageIntent {
		root, ok := s.pageCache.Current()
		if !ok {
			return s, nil, errors.New("terminal current history page cache is unavailable")
		}
		cursor, hasCursor := root.BeforeCursor, root.HasBeforeCursor
		if s.observation.PageIntentDirection == protocolvalue.HistoryPageAfter {
			cursor, hasCursor = root.AfterCursor, root.HasAfterCursor
		}
		if hasCursor {
			var token OperationToken
			s, token = s.nextWire(OpHistoryPage, now.Add(wireOperationDeadline))
			request, err := protocolvalue.PrepareHistoryPageRequest(token.RequestID, s.attachment.Identity.RuntimeSessionID, cursor, s.observation.PageIntentDirection, protocolvalue.MaximumHistoryPageCells, protocolvalue.MaximumHistoryPageDecodedBytes, s.durable.Durable().ProjectionContractFingerprint, s.observation.ViewportIntentGeneration)
			if err != nil {
				return s, nil, err
			}
			s.observation.PendingPage, s.observation.HasPendingPage = request, true
			s.observation.PageIntentDirection, s.observation.HasPageIntent = 0, false
			return s, []Effect{ReadHistoryPageEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Request: request}}, nil
		}
		s.observation.PageIntentDirection, s.observation.HasPageIntent = 0, false
	}
	wait := s.connection.HeartbeatSchedule.NextAt.Sub(now)
	if wait <= 0 || wait > time.Second {
		wait = time.Second
	}
	maximumWaitMS := uint32(wait / time.Millisecond)
	if maximumWaitMS < 1 {
		maximumWaitMS = 1
	}
	var token OperationToken
	s, token = s.nextWire(OpObserve, now.Add(time.Duration(maximumWaitMS)*time.Millisecond+2*time.Second))
	durable := s.durable.Durable()
	operational := s.operational.Snapshot()
	request, err := protocolvalue.PrepareObserveRequest(token.RequestID, durable.AuthorityHighWater, durable.ProjectionRevision, operational.Generation, operational.Cursor, s.control.ConfirmedCursor(), maximumWaitMS)
	if err != nil {
		return s, nil, err
	}
	s.observation.Enabled = true
	return s, []Effect{ObserveNextEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Request: request}}, nil
}

func requestOperationalSnapshot(s AppState, now time.Time) (AppState, RequestOperationalSnapshotEffect, error) {
	var token OperationToken
	s, token = s.nextWire(OpOperationalSnapshot, now.Add(wireOperationDeadline))
	requestedGeneration, requestedCursor := uint64(0), uint64(0)
	if s.operational.Installed() {
		current := s.operational.Snapshot()
		requestedGeneration, requestedCursor = current.Generation, current.Cursor
	}
	request, err := protocolvalue.PrepareOperationalSnapshotRequest(token.RequestID, s.attachment.Identity, s.connection.AttachReceipt, requestedGeneration, requestedCursor)
	if err != nil {
		return s, RequestOperationalSnapshotEffect{}, err
	}
	s.snapshotLoading.Phase = SnapshotAwaitingOperationalSnapshot
	s.snapshotLoading.OperationalRequired = true
	s.snapshotLoading.OperationalOperationID = token.OperationID
	s.snapshotLoading.OperationalOperationGeneration = token.OperationGeneration
	return s, RequestOperationalSnapshotEffect{Header: newWireHeader(token), ConnectionHandleID: s.connection.HandleID, Request: request}, nil
}
