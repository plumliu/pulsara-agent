package client

import (
	"crypto/rand"
	"errors"
	"os"
	"sync"
	"time"

	tea "charm.land/bubbletea/v2"
	"google.golang.org/protobuf/proto"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/buildinfo"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/publictext"
	terminalwire "github.com/plumliu/pulsara-agent/clients/terminal/internal/wire"
)

type Service struct {
	mu            sync.Mutex
	executeMu     sync.Mutex
	bootstrap     protocolvalue.Bootstrap
	connection    *physicalConnectionOwner
	runtime       ClientRuntimeOwner
	operations    *operationRegistry
	scheduler     *localScheduler
	candidate     *protocol.HandshakeRecoveryCandidateIdentity
	authResult    *protocol.TerminalTransportAuthResult
	helloWinner   protocolvalue.HelloNegotiationWinner
	helloReceipt  protocolvalue.ValidatedServerHelloReceipt
	helloReady    bool
	attachReceipt *protocol.AttachResultReceipt
	attachment    protocolvalue.Attachment
	closed        bool
	finalized     bool
	closeDone     chan struct{}
	closeErr      error
	buildIdentity buildinfo.BuildIdentity
	bridge        observationBridge
}

type helloOperationResult struct {
	winner   protocolvalue.HelloNegotiationWinner
	receipt  protocolvalue.ValidatedServerHelloReceipt
	prepared app.PreparedAttachmentChallengeHandleIdentity
	negative *protocolvalue.HelloNegativeOutcome
}

type transportAuthOperationResult struct {
	candidate protocolvalue.HandshakeCandidate
	proof     protocolvalue.TransportAuthResult
	recovered *protocolvalue.RecoveredAttachAcknowledgement
}

func NewService(bootstrap protocolvalue.Bootstrap) (*Service, error) {
	// Take the sole long-lived ownership of the launch capability. The caller's
	// decoded bootstrap copy is zeroed before construction returns.
	ownedCapability := append([]byte(nil), bootstrap.LaunchCapability...)
	clear(bootstrap.LaunchCapability)
	bootstrap.LaunchCapability = ownedCapability
	buildIdentity, err := buildinfo.Current()
	if err != nil {
		clear(bootstrap.LaunchCapability)
		return nil, err
	}
	service := &Service{bootstrap: bootstrap, operations: newOperationRegistry(), scheduler: newLocalScheduler(), closeDone: make(chan struct{}), buildIdentity: buildIdentity}
	service.candidate = service.prepareHandshakeCandidate(1)
	if service.candidate == nil {
		_ = service.Close()
		return nil, errors.New("terminal handshake candidate preparation failed")
	}
	return service, nil
}

func (s *Service) ClientInstanceID() string { return s.bootstrap.ClientInstanceID }
func (s *Service) InitialHandshakeCandidate() protocolvalue.HandshakeCandidate {
	if s.candidate == nil {
		return protocolvalue.HandshakeCandidate{}
	}
	return protocolvalue.HandshakeCandidate{ID: s.candidate.CandidateId, ClientInstanceID: s.candidate.ClientInstanceId, AttachmentAttemptGeneration: s.candidate.AttachmentAttemptGeneration, HostSessionID: s.candidate.HostSessionId, RuntimeSessionID: s.candidate.RequestedRuntimeSessionId, Fingerprint: s.candidate.CandidateFingerprint}
}

func (s *Service) prepareHandshakeCandidate(generation uint64) *protocol.HandshakeRecoveryCandidateIdentity {
	if generation == 0 {
		return nil
	}
	candidate := &protocol.HandshakeRecoveryCandidateIdentity{
		CandidateVersion:            1,
		ClientInstanceId:            s.bootstrap.ClientInstanceID,
		AttachmentAttemptGeneration: generation,
		HostSessionId:               s.bootstrap.HostSessionID,
		RequestedRuntimeSessionId:   s.bootstrap.RuntimeSessionID,
		RequestedAttachmentRole:     protocol.AttachmentRole_ATTACHMENT_ROLE_OBSERVER,
		MinimumProtocolMajor:        protocolvalue.ProtocolMajor,
		MinimumProtocolMinor:        protocolvalue.ProtocolMinor,
		MaximumProtocolMajor:        protocolvalue.ProtocolMajor,
		MaximumProtocolMinor:        protocolvalue.ProtocolMinor,
		ClientBuildIdentity:         s.buildIdentity.Fingerprint(),
		SupportedCapabilities:       append([]protocol.TerminalClientCapability(nil), protocolvalue.S2SupportedCapabilities...),
		RequiredCapabilities:        append([]protocol.TerminalClientCapability(nil), protocolvalue.S2RequiredCapabilities...),
		SchemaContractFingerprint:   protocolvalue.SchemaFingerprint,
	}
	fingerprint, err := protocol.InstallFingerprint("terminal-handshake-recovery-candidate:v1", candidate, "candidate_fingerprint", "candidate_id")
	if err != nil {
		return nil
	}
	candidate.CandidateId = "handshake:" + fingerprint[len("sha256:"):]
	return candidate
}

func (s *Service) Execute(effect app.Effect) tea.Cmd {
	switch value := effect.(type) {
	case app.ScheduleTickEffect:
		operation := value.Header.Operation
		if operation.Kind != app.OpTick || !operation.Valid() || value.Kind == 0 ||
			value.TickGeneration == 0 || value.DueAt.IsZero() {
			return func() tea.Msg { return app.FrameworkInputRejectedMsg{} }
		}
		return s.scheduler.schedule(value)
	case app.QuitProgramEffect:
		if value.Header.Operation.Kind != app.OpTeardown || !value.Header.Operation.Valid() {
			return func() tea.Msg { return app.FrameworkInputRejectedMsg{} }
		}
		return tea.Quit
	}
	if value, ok := effect.(app.CopyPublicTextEffect); ok {
		// OSC52 remains a renderer/runtime effect. Update only installs the
		// closed request and consumes the typed completion below.
		return tea.Sequence(tea.SetClipboard(value.PublicUTF8), s.execute(effect))
	}
	return s.execute(effect)
}

func (s *Service) execute(effect app.Effect) tea.Cmd {
	return func() tea.Msg {
		s.executeMu.Lock()
		defer s.executeMu.Unlock()
		operation := effect.Outstanding()
		if err := s.operations.begin(operation); err != nil {
			return s.admissionFailure(operation, err)
		}
		if err := s.validateS2Effect(effect); err != nil {
			return s.failure(operation, err)
		}
		s.mu.Lock()
		if s.closed {
			s.mu.Unlock()
			return s.failure(operation, errors.New("terminal client is closed"))
		}
		s.mu.Unlock()
		var message tea.Msg
		var err error
		switch operation.Carrier {
		case app.OutstandingLocal:
			token := operation.Local
			switch token.Kind {
			case app.OpConnect:
				var peer app.ValidatedPeerIdentity
				connectEffect := effect.(app.ConnectEffect)
				var candidate protocolvalue.HandshakeCandidate
				peer, candidate, err = s.connect(connectEffect.AttachmentAttemptGeneration)
				if err == nil {
					connectionHandleID := "terminal-connection-handle:" + s.bootstrap.ClientInstanceID
					fingerprint, fingerprintErr := app.ConnectResultFingerprint(token, connectionHandleID, peer, candidate)
					if fingerprintErr != nil {
						err = fingerprintErr
						break
					}
					header, _ := app.NewLocalResultHeader(token, fingerprint, time.Now())
					message = app.ConnectSucceededMsg{Header: header, ConnectionHandleID: connectionHandleID, Peer: peer, Candidate: candidate}
				}
			case app.OpChallengePromote:
				value := effect.(app.PromotePreparedAttachmentChallengeEffect)
				var receipt app.AttachmentChallengePromotionReceipt
				receipt, err = s.runtime.PromotePreparedAttachmentChallenge(token, value.Prepared)
				if err == nil {
					header, _ := app.NewLocalResultHeader(token, receipt.PromotionReceiptFingerprint, time.Now())
					message = app.AttachmentChallengePromotedMsg{Header: header, Receipt: receipt}
				}
			case app.OpChallengePromotionConfirm:
				value := effect.(app.ConfirmAttachmentChallengePromotionEffect)
				var receipt app.AttachmentChallengeAcceptanceReceipt
				receipt, err = s.runtime.ConfirmAttachmentChallengePromotion(token, value.Promotion)
				if err == nil {
					header, _ := app.NewLocalResultHeader(token, receipt.AcceptanceReceiptFingerprint, time.Now())
					message = app.AttachmentChallengePromotionAcceptedMsg{Header: header, Receipt: receipt}
				}
			case app.OpChallengeRevokePrepared:
				value := effect.(app.RevokePreparedAttachmentChallengeEffect)
				var receipt app.AttachmentChallengeRevocationReceipt
				receipt, err = s.runtime.RevokePreparedAttachmentChallenge(
					token,
					value.HandleFingerprint,
					value.Reason,
				)
				if err == nil {
					message = app.AttachmentChallengeRevokedMsg{Receipt: receipt}
				}
			case app.OpChallengeRevokeActive:
				value := effect.(app.RevokeActiveAttachmentChallengeEffect)
				var receipt app.AttachmentChallengeRevocationReceipt
				receipt, err = s.runtime.RevokeActiveAttachmentChallenge(
					token,
					value.HandleFingerprint,
					value.PromotionFingerprint,
					value.Reason,
				)
				if err == nil {
					message = app.AttachmentChallengeRevokedMsg{Receipt: receipt}
				}
			case app.OpTeardown:
				var summary app.PublicTeardownSummary
				summary, err = s.beginTeardown(operation, 1, token.Deadline)
				if err == nil {
					fingerprint, fingerprintErr := summary.Fingerprint()
					if fingerprintErr != nil {
						err = fingerprintErr
						break
					}
					header, _ := app.NewLocalResultHeader(token, fingerprint, time.Now())
					message = app.TeardownCompletedMsg{Header: header, Summary: summary}
				}
			case app.OpParentRelaunch:
				value := effect.(app.RequestParentRelaunchEffect)
				var summary app.PublicTeardownSummary
				summary, err = s.beginTeardown(operation, 1, token.Deadline)
				if err == nil && summary.Disposition != app.TeardownCompleted {
					err = errors.New("terminal parent relaunch teardown did not complete")
				}
				if err == nil {
					header, _ := app.NewLocalResultHeader(token, value.NegativeOutcomeFingerprint, time.Now())
					message = app.ParentRelaunchPreparedMsg{Header: header, CandidateTerminalReceipt: value.CandidateTerminalReceipt, Cause: value.Cause, NegativeOutcomeFingerprint: value.NegativeOutcomeFingerprint}
				}
			case app.OpClipboard:
				fingerprint, fingerprintErr := protocol.CanonicalJSONFingerprint("terminal-public-clipboard-result:v1", map[string]any{"operation_id": token.OperationID, "status": "completed"})
				err = fingerprintErr
				if err == nil {
					header, _ := app.NewLocalResultHeader(token, fingerprint, time.Now())
					message = app.PublicTextCopiedMsg{Header: header}
				}
			default:
				err = errors.New("unsupported S2 local terminal effect")
			}
		case app.OutstandingWire:
			token := operation.Wire
			switch token.Kind {
			case app.OpConnect:
				err = errors.New("connect cannot use a wire operation token")
			case app.OpTransportAuth:
				var result transportAuthOperationResult
				result, err = s.authenticate(token)
				if err == nil {
					header, _ := app.NewIOHeader(token, result.proof.ResultFingerprint, time.Now())
					if result.recovered != nil {
						message = app.AttachRecoveredMsg{Header: header, ConnectionHandleID: "terminal-connection-handle:" + s.bootstrap.ClientInstanceID, Candidate: result.candidate, Proof: result.proof, Recovery: *result.recovered, Attachment: s.attachment}
					} else {
						message = app.TransportAuthenticatedMsg{Header: header, ConnectionHandleID: "terminal-connection-handle:" + s.bootstrap.ClientInstanceID, Candidate: result.candidate, Proof: result.proof}
					}
				}
			case app.OpHello:
				var result helloOperationResult
				result, err = s.helloRequest(token)
				if err == nil {
					if result.negative != nil {
						header, _ := app.NewIOHeader(token, result.negative.OutcomeFingerprint, time.Now())
						message = app.HelloNegativeMsg{Header: header, Outcome: *result.negative}
					} else {
						header, _ := app.NewIOHeader(token, result.receipt.ReceiptFingerprint, time.Now())
						message = app.HelloAcceptedMsg{Header: header, Winner: result.winner, Receipt: result.receipt, PreparedChallenge: result.prepared}
					}
				}
			case app.OpAttach:
				value := effect.(app.AttachEffect)
				var attachment protocolvalue.Attachment
				var receipt protocolvalue.AttachReceipt
				attachment, receipt, err = s.attach(token, value.AttachmentChallengeAcceptance)
				if err == nil {
					header, _ := app.NewIOHeader(token, receipt.ReceiptFingerprint, time.Now())
					message = app.AttachAcceptedMsg{Header: header, Attachment: attachment, Receipt: receipt, ReconnectCredentialHandleID: "terminal-reconnect-credential:" + s.bootstrap.ClientInstanceID}
				}
			case app.OpAttachAck:
				var result protocolvalue.ValidatedAttachAckResult
				result, err = s.attachAck(token)
				if err == nil {
					header, _ := app.NewIOHeader(token, result.ResultFingerprint, time.Now())
					message = app.AttachAcknowledgedMsg{Header: header, Result: result}
				}
			case app.OpProjectionSnapshot:
				request := effect.(app.RequestSnapshotEffect).Request
				var value protocolvalue.ProjectionSnapshotOutcome
				value, err = s.durableSnapshot(token, request)
				if err == nil && value.RebaseRequired {
					header, _ := app.NewIOHeader(token, value.Fingerprint, time.Now())
					message = app.SnapshotControlRebaseRequiredMsg{Header: header, Request: request, Outcome: value}
				} else if err == nil {
					header, _ := app.NewIOHeader(token, value.Snapshot.SnapshotFingerprint, time.Now())
					message = app.SnapshotAcceptedMsg{Header: header, Request: request, Snapshot: value.Snapshot}
				}
			case app.OpOperationalSnapshot:
				prepared := effect.(app.RequestOperationalSnapshotEffect).Request
				var value protocolvalue.OperationalSnapshot
				value, err = s.operationalSnapshot(token, prepared)
				if err == nil {
					header, _ := app.NewIOHeader(token, value.FrameFingerprint, time.Now())
					message = app.OperationalSnapshotAcceptedMsg{Header: header, Snapshot: value}
				}
			case app.OpHeartbeat:
				request := effect.(app.HeartbeatEffect).Request
				var accepted *protocolvalue.ValidatedHeartbeatAcceptedReceipt
				var rejected *protocolvalue.ValidatedHeartbeatRejectedReceipt
				accepted, rejected, err = s.heartbeat(token, request)
				if err == nil && accepted != nil {
					header, _ := app.NewIOHeader(token, accepted.ReceiptFingerprint, time.Now())
					message = app.HeartbeatAcceptedMsg{Header: header, Request: request, Receipt: *accepted}
				} else if err == nil && rejected != nil {
					header, _ := app.NewIOHeader(token, rejected.ReceiptFingerprint, time.Now())
					message = app.HeartbeatRejectedMsg{Header: header, Request: request, Receipt: *rejected}
				}
			case app.OpObserve:
				request := effect.(app.ObserveNextEffect).Request
				var value protocolvalue.ObservationResult
				value, err = s.observe(token, request)
				if err == nil && value.IsBatch {
					header, _ := app.NewIOHeader(token, value.Batch.Fingerprint, time.Now())
					message = app.ObservationBatchMsg{Header: header, Request: request, Batch: value.Batch}
				} else if err == nil {
					header, _ := app.NewIOHeader(token, value.NoChange.Fingerprint, time.Now())
					message = app.ObservationNoChangeMsg{Header: header, Request: request, NoChange: value.NoChange}
				}
			case app.OpHistoryPage:
				request := effect.(app.ReadHistoryPageEffect).Request
				var value protocolvalue.HistoryPageResult
				value, err = s.historyPage(token, request)
				if err == nil {
					header, _ := app.NewIOHeader(token, value.Fingerprint, time.Now())
					message = app.HistoryPageAcceptedMsg{Header: header, Request: request, Result: value}
				}
			default:
				err = errors.New("unsupported S2 terminal effect")
			}
		default:
			err = errors.New("terminal effect has no operation carrier")
		}
		if err != nil {
			return s.failure(operation, err)
		}
		if err := s.operations.finishSuccess(operation); err != nil {
			return s.admissionFailure(operation, err)
		}
		return message
	}
}

func (s *Service) validateS2Effect(effect app.Effect) error {
	operation := effect.Outstanding()
	switch value := effect.(type) {
	case app.ConnectEffect:
		if operation.Local.Kind != app.OpConnect || value.Header.Operation != operation.Local || value.BootstrapHandleID != "terminal-bootstrap:"+s.bootstrap.ClientInstanceID || value.AttachmentAttemptGeneration == 0 {
			return errors.New("terminal connect effect is invalid")
		}
	case app.AuthenticateTransportEffect:
		expected := s.InitialHandshakeCandidate()
		expectedHandle := "terminal-launch-credential:" + s.bootstrap.ClientInstanceID
		if expected.AttachmentAttemptGeneration > 1 {
			expectedHandle = "terminal-reconnect-credential:" + s.bootstrap.ClientInstanceID
		}
		if operation.Wire.Kind != app.OpTransportAuth || value.Header.Operation != operation.Wire || value.ConnectionHandleID == "" || value.CredentialHandleID != expectedHandle || value.Candidate != expected {
			return errors.New("terminal transport-auth effect is invalid")
		}
	case app.NegotiateHelloEffect:
		if operation.Wire.Kind != app.OpHello || value.Header.Operation != operation.Wire || value.ConnectionHandleID == "" || value.TransportAuthAttemptID == "" || value.TransportAuthResultFingerprint == "" || value.Candidate.Fingerprint == "" {
			return errors.New("terminal Hello effect is invalid")
		}
	case app.PromotePreparedAttachmentChallengeEffect:
		if operation.Local.Kind != app.OpChallengePromote || value.Header.Operation != operation.Local || value.Prepared.Validate() != nil || value.ExpectedCandidateFingerprint != value.Prepared.CandidateFingerprint || value.ExpectedHelloReceiptFingerprint != value.Prepared.ValidatedReceiptFingerprint || value.ExpectedConnectionID != value.Prepared.ConnectionID {
			return errors.New("terminal challenge-promotion effect is invalid")
		}
	case app.ConfirmAttachmentChallengePromotionEffect:
		if operation.Local.Kind != app.OpChallengePromotionConfirm || value.Header.Operation != operation.Local || value.Promotion.Validate() != nil || value.ExpectedCandidateFingerprint == "" || value.ExpectedHelloReceiptFingerprint == "" || value.ExpectedConnectionID == "" {
			return errors.New("terminal challenge-confirmation effect is invalid")
		}
	case app.RevokePreparedAttachmentChallengeEffect:
		if operation.Local.Kind != app.OpChallengeRevokePrepared ||
			value.Header.Operation != operation.Local || value.HandleFingerprint == "" ||
			value.Reason == 0 {
			return errors.New("terminal prepared-challenge revocation effect is invalid")
		}
	case app.RevokeActiveAttachmentChallengeEffect:
		if operation.Local.Kind != app.OpChallengeRevokeActive ||
			value.Header.Operation != operation.Local || value.HandleFingerprint == "" ||
			value.PromotionFingerprint == "" || value.Reason == 0 {
			return errors.New("terminal active-challenge revocation effect is invalid")
		}
	case app.AttachEffect:
		if operation.Wire.Kind != app.OpAttach || value.Header.Operation != operation.Wire || value.ConnectionHandleID == "" || value.Candidate.Fingerprint == "" || value.HelloNegotiationWinnerFingerprint == "" || value.ServerHelloReceiptFingerprint == "" || value.ActiveAttachmentChallengeHandleID == "" || value.AttachmentChallengeAcceptance.Validate() != nil || value.AttachmentChallengeCommitment == "" {
			return errors.New("terminal attach effect is invalid")
		}
	case app.AcknowledgeAttachEffect:
		if operation.Wire.Kind != app.OpAttachAck || value.Header.Operation != operation.Wire || value.ConnectionHandleID == "" || value.SemanticWinnerFingerprint == "" || value.AttachResultReceiptFingerprint == "" {
			return errors.New("terminal attach-ack effect is invalid")
		}
	case app.HeartbeatEffect:
		request, requestErr := value.Request.ToProto()
		if operation.Wire.Kind != app.OpHeartbeat || value.Header.Operation != operation.Wire || value.ConnectionHandleID == "" || requestErr != nil || request.RequestId != operation.Wire.RequestID {
			return errors.New("terminal heartbeat effect is invalid")
		}
	case app.RequestSnapshotEffect:
		request, requestErr := value.Request.ToProto()
		if operation.Wire.Kind != app.OpProjectionSnapshot || value.Header.Operation != operation.Wire || value.ConnectionHandleID == "" || requestErr != nil || request.RequestId != operation.Wire.RequestID {
			return errors.New("terminal snapshot effect is invalid")
		}
	case app.RequestOperationalSnapshotEffect:
		request, requestErr := value.Request.ToProto()
		if operation.Wire.Kind != app.OpOperationalSnapshot || value.Header.Operation != operation.Wire || value.ConnectionHandleID == "" || requestErr != nil || request.RequestId != operation.Wire.RequestID {
			return errors.New("terminal operational-snapshot effect is invalid")
		}
	case app.ObserveNextEffect:
		request, requestErr := value.Request.ToProto()
		if operation.Wire.Kind != app.OpObserve || value.Header.Operation != operation.Wire || value.ConnectionHandleID == "" || requestErr != nil || request.RequestId != operation.Wire.RequestID {
			return errors.New("terminal observe effect is invalid")
		}
	case app.ReadHistoryPageEffect:
		request, requestErr := value.Request.ToProto()
		if operation.Wire.Kind != app.OpHistoryPage || value.Header.Operation != operation.Wire || value.ConnectionHandleID == "" || requestErr != nil || request.RequestId != operation.Wire.RequestID {
			return errors.New("terminal history page effect is invalid")
		}
	case app.BeginTeardownEffect:
		if operation.Local.Kind != app.OpTeardown || value.Header.Operation != operation.Local || value.Reason == 0 {
			return errors.New("terminal teardown effect is invalid")
		}
	case app.RequestParentRelaunchEffect:
		if operation.Local.Kind != app.OpParentRelaunch || value.Header.Operation != operation.Local || value.CandidateTerminalReceipt.TerminalReceiptFingerprint == "" || value.Cause == 0 || value.NegativeOutcomeFingerprint == "" {
			return errors.New("terminal parent-relaunch effect is invalid")
		}
	case app.CopyPublicTextEffect:
		if operation.Local.Kind != app.OpClipboard || value.Header.Operation != operation.Local || len([]rune(value.PublicUTF8)) > app.MaximumPublicClipboardRunes || len([]byte(value.PublicUTF8)) > app.MaximumPublicClipboardBytes {
			return errors.New("terminal public clipboard effect is invalid")
		}
	default:
		return errors.New("unsupported S2 terminal effect type")
	}
	return nil
}

func (s *Service) beginTeardown(
	operation app.OutstandingOperation,
	generation uint64,
	deadline time.Time,
) (app.PublicTeardownSummary, error) {
	s.mu.Lock()
	s.closed = true
	connection := s.connection
	s.mu.Unlock()
	schedulerErr := s.scheduler.closeAndDrain(deadline)
	var closeErr error
	if connection != nil {
		closeErr = connection.CloseAndWait(deadline)
	}
	drained, drainErr := s.operations.drainConnectionTerminalizationsWithCount(deadline)
	revoked := s.runtime.Close()
	clear(s.bootstrap.LaunchCapability)
	s.bootstrap.LaunchCapability = nil
	terminalErr := closeErr
	if terminalErr == nil {
		terminalErr = schedulerErr
	}
	if terminalErr == nil {
		terminalErr = drainErr
	}
	if terminalErr == nil {
		return app.NewPublicTeardownSummary(
			generation,
			app.TeardownCompleted,
			0,
			drained,
			revoked,
			false,
			false,
			true,
			app.PublicFailure{},
			false,
		)
	}
	cause := app.CauseLocalIntegrationFailed
	disposition := app.TeardownEmergencyRestoreRequired
	if !time.Now().Before(deadline) {
		cause = app.CauseDeadlineExpired
		disposition = app.TeardownDeadlineExceeded
	}
	failure := s.operations.classifyEmbeddedFailure(
		operation,
		cause,
		"The terminal client could not fully drain before exit.",
	)
	return app.NewPublicTeardownSummary(
		generation,
		disposition,
		0,
		drained,
		revoked,
		false,
		false,
		false,
		failure,
		true,
	)
}

func (s *Service) connect(attachmentAttemptGeneration uint64) (app.ValidatedPeerIdentity, protocolvalue.HandshakeCandidate, error) {
	s.mu.Lock()
	if s.connection != nil {
		s.mu.Unlock()
		return app.ValidatedPeerIdentity{}, protocolvalue.HandshakeCandidate{}, errors.New("terminal connection is duplicated")
	}
	if s.candidate == nil || s.candidate.AttachmentAttemptGeneration != attachmentAttemptGeneration {
		s.candidate = s.prepareHandshakeCandidate(attachmentAttemptGeneration)
		s.authResult, s.helloReady, s.attachReceipt = nil, false, nil
	}
	candidate := s.InitialHandshakeCandidate()
	s.mu.Unlock()
	connection, err := openConnection(s.bootstrap.SocketPath)
	if err != nil {
		return app.ValidatedPeerIdentity{}, protocolvalue.HandshakeCandidate{}, err
	}
	peer, socketOwnerUID, runtimePathFingerprint, err := connection.peerIdentityParts()
	if err != nil {
		_ = connection.Close()
		return app.ValidatedPeerIdentity{}, protocolvalue.HandshakeCandidate{}, err
	}
	identity, err := app.NewValidatedPeerIdentity(
		uint64(currentUID()),
		peer.UID,
		peer.PID,
		peer.HasPID,
		socketOwnerUID,
		runtimePathFingerprint,
	)
	if err != nil {
		_ = connection.Close()
		return app.ValidatedPeerIdentity{}, protocolvalue.HandshakeCandidate{}, err
	}
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		_ = connection.Close()
		return app.ValidatedPeerIdentity{}, protocolvalue.HandshakeCandidate{}, errors.New("terminal client closed during connection")
	}
	s.connection = connection
	s.mu.Unlock()
	return identity, candidate, nil
}

func (s *Service) authenticate(token app.OperationToken) (transportAuthOperationResult, error) {
	if s.connection == nil || s.candidate == nil {
		return transportAuthOperationResult{}, errors.New("terminal transport is unavailable")
	}
	candidate := s.candidate
	preface := &protocol.TerminalTransportAuthPreface{
		PrefaceVersion:                1,
		AuthRequestId:                 token.RequestID,
		ClientInstanceId:              s.bootstrap.ClientInstanceID,
		HandshakeCandidateId:          candidate.CandidateId,
		HandshakeCandidateFingerprint: candidate.CandidateFingerprint,
		ConnectionNonce:               randomBytes(32),
	}
	if candidate.AttachmentAttemptGeneration == 1 {
		if len(s.bootstrap.LaunchCapability) == 0 {
			return transportAuthOperationResult{}, errors.New("terminal initial launch credential is unavailable")
		}
		preface.Credential = &protocol.TerminalTransportAuthPreface_InitialLaunch{InitialLaunch: &protocol.InitialLaunchCredential{LaunchId: s.bootstrap.LaunchID, LaunchCapability: append([]byte(nil), s.bootstrap.LaunchCapability...)}}
		defer clear(preface.GetInitialLaunch().LaunchCapability)
	} else {
		identity, capability, err := s.runtime.BorrowReconnectCredential(candidate.AttachmentAttemptGeneration)
		if err != nil {
			return transportAuthOperationResult{}, err
		}
		preface.Credential = &protocol.TerminalTransportAuthPreface_Reconnect{Reconnect: &protocol.ReconnectCredential{ReconnectCredentialId: identity.ID, ReconnectCapability: capability, PreviousAttachmentId: identity.PreviousAttachmentID, PreviousAttachmentGeneration: identity.PreviousAttachmentGeneration}}
		defer clear(preface.GetReconnect().ReconnectCapability)
	}
	if _, err := protocol.InstallFingerprint("terminal-transport-auth-preface:v1", preface, "preface_fingerprint"); err != nil {
		return transportAuthOperationResult{}, err
	}
	result, err := s.connection.Authenticate(preface, 5*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return transportAuthOperationResult{}, err
	}
	proof, err := protocolvalue.TransportAuthResultFromProto(result)
	if err != nil {
		return transportAuthOperationResult{}, err
	}
	if result.Disposition == protocol.TransportAuthDisposition_TRANSPORT_AUTHENTICATION_REJECTED {
		return transportAuthOperationResult{}, errors.New("terminal transport authentication was rejected")
	}
	if result.ClientInstanceId != s.bootstrap.ClientInstanceID || result.AuthenticatedCandidateFingerprint == nil || *result.AuthenticatedCandidateFingerprint != candidate.CandidateFingerprint {
		return transportAuthOperationResult{}, errors.New("terminal transport authentication result is stale")
	}
	s.authResult = result
	validatedCandidate := protocolvalue.HandshakeCandidate{ID: candidate.CandidateId, ClientInstanceID: candidate.ClientInstanceId, AttachmentAttemptGeneration: candidate.AttachmentAttemptGeneration, HostSessionID: candidate.HostSessionId, RuntimeSessionID: candidate.RequestedRuntimeSessionId, Fingerprint: candidate.CandidateFingerprint}
	operationResult := transportAuthOperationResult{candidate: validatedCandidate, proof: proof}
	if result.Disposition == protocol.TransportAuthDisposition_TRANSPORT_ACK_RESULT_RECOVERY {
		recovery, recoveryErr := protocolvalue.RecoveredAttachAcknowledgementFromProto(result)
		if recoveryErr != nil {
			return transportAuthOperationResult{}, recoveryErr
		}
		if !s.attachmentCompatibleWithRecovery(recovery) {
			return transportAuthOperationResult{}, errors.New("terminal recovered attachment proof is stale")
		}
		s.attachment.ConnectionID = recovery.Binding.ResultingBinding.ConnectionID
		s.attachment.BindingGeneration = recovery.Binding.ResultingBinding.Generation
		s.attachment.BindingFingerprint = recovery.Binding.ResultingBinding.Fingerprint
		s.attachment.CurrentReceiptFingerprint = recovery.Ack.ResultFingerprint
		if s.attachReceipt == nil || result.RecoveredTransportBinding == nil {
			return transportAuthOperationResult{}, errors.New("terminal recovered attach receipt is unavailable")
		}
		resultingBinding, ok := proto.Clone(
			result.RecoveredTransportBinding.ResultingTransportBinding,
		).(*protocol.TerminalClientTransportBindingIdentity)
		if !ok || resultingBinding == nil {
			return transportAuthOperationResult{}, errors.New("terminal recovered binding clone failed")
		}
		s.attachReceipt.CurrentTransportBinding = resultingBinding
		if carrier := s.attachReceipt.NextReconnectCredentialCarrier; carrier != nil && carrier.CarrierFingerprint != "" {
			if promoteErr := s.runtime.PromotePendingReconnectCredential(carrier.CarrierFingerprint); promoteErr != nil {
				return transportAuthOperationResult{}, promoteErr
			}
		}
		operationResult.recovered = &recovery
	}
	return operationResult, nil
}

func (s *Service) attachmentCompatibleWithRecovery(value protocolvalue.RecoveredAttachAcknowledgement) bool {
	return s.attachment.ID != "" &&
		s.attachment.ID == value.Ack.AttachmentID &&
		s.attachment.Generation == value.Ack.AttachmentGeneration &&
		s.attachment.SemanticWinnerFingerprint == value.Ack.SemanticWinnerFingerprint &&
		s.attachment.BindingFingerprint == value.Binding.PreviousBindingFingerprint
}

func (s *Service) helloRequest(token app.OperationToken) (helloOperationResult, error) {
	if s.connection == nil || s.candidate == nil || s.authResult == nil {
		return helloOperationResult{}, errors.New("terminal hello prerequisites are missing")
	}
	request := &protocol.HelloRequest{RequestId: token.RequestID, TransportAuthAttemptId: s.authResult.AuthAttemptId, TransportAuthResultFingerprint: s.authResult.ResultFingerprint, HandshakeCandidate: s.candidate}
	response, err := s.connection.RoundTrip(terminalwire.HelloFrame(request), 5*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return helloOperationResult{}, err
	}
	if err := terminalwire.ValidateServerFrame(response, "hello"); err != nil {
		return helloOperationResult{}, err
	}
	outcome := response.GetHello()
	accepted := outcome.GetAccepted()
	if accepted == nil {
		candidate := protocolvalue.HandshakeCandidate{ID: s.candidate.CandidateId, ClientInstanceID: s.candidate.ClientInstanceId, AttachmentAttemptGeneration: s.candidate.AttachmentAttemptGeneration, HostSessionID: s.candidate.HostSessionId, RuntimeSessionID: s.candidate.RequestedRuntimeSessionId, Fingerprint: s.candidate.CandidateFingerprint}
		negative, err := protocolvalue.HelloNegativeFromProto(outcome, request.RequestId, s.authResult.AuthAttemptId, s.authResult.ConnectionId, candidate)
		if err != nil {
			return helloOperationResult{}, err
		}
		return helloOperationResult{negative: &negative}, nil
	}
	winner, receipt := accepted.NegotiationWinner, accepted.Receipt
	if winner == nil || receipt == nil {
		return helloOperationResult{}, errors.New("terminal Hello proof is incomplete")
	}
	if err := terminalwire.ValidateSelectedProtocol(winner.SelectedProtocol); err != nil {
		return helloOperationResult{}, err
	}
	if err := protocolvalue.ValidateCapabilities(winner.SelectedCapabilities); err != nil {
		return helloOperationResult{}, err
	}
	if err := protocol.ValidateFingerprint("terminal-hello-negotiation-winner:v1", winner, "negotiation_winner_fingerprint", winner.NegotiationWinnerFingerprint); err != nil {
		return helloOperationResult{}, err
	}
	if err := protocol.ValidateFingerprint("terminal-server-hello-receipt:v1", receipt, "hello_receipt_fingerprint", receipt.HelloReceiptFingerprint); err != nil {
		return helloOperationResult{}, err
	}
	commitment, err := protocol.AttachmentChallengeCommitment(s.authResult.AuthAttemptId, s.candidate.CandidateFingerprint, s.candidate.CandidateId, receipt.CurrentConnectionId, winner.NegotiationWinnerFingerprint, request.RequestId, receipt.AttachmentChallenge)
	if err != nil || commitment != receipt.AttachmentChallengeCommitment {
		return helloOperationResult{}, errors.New("terminal Hello challenge commitment is invalid")
	}
	if winner.HandshakeCandidateFingerprint != s.candidate.CandidateFingerprint || receipt.HandshakeCandidateFingerprint != s.candidate.CandidateFingerprint || receipt.NegotiationWinnerFingerprint != winner.NegotiationWinnerFingerprint {
		return helloOperationResult{}, errors.New("terminal Hello authority join is invalid")
	}
	if err := s.connection.SetMaximumFrameBytes(winner.NegotiatedLimits.MaximumFrameBytes); err != nil {
		return helloOperationResult{}, err
	}
	handleID := "challenge:" + receipt.HelloReceiptFingerprint[len("sha256:"):]
	validatedWinner, validatedReceipt, err := protocolvalue.HelloFromProto(accepted, handleID)
	if err != nil {
		return helloOperationResult{}, err
	}
	if len(receipt.AttachmentChallenge) != 32 {
		return helloOperationResult{}, errors.New("terminal Hello challenge has an invalid length")
	}
	var challenge [32]byte
	copy(challenge[:], receipt.AttachmentChallenge)
	prepared, err := s.runtime.PrepareAttachmentChallenge(token, challenge, validatedReceipt.ReceiptFingerprint, validatedWinner.CandidateFingerprint, validatedReceipt.CurrentConnectionID, validatedReceipt.ChallengeCommitment, time.Now().Add(30*time.Second))
	clear(challenge[:])
	if err != nil {
		return helloOperationResult{}, err
	}
	if prepared.HandleID != handleID {
		s.runtime.RevokeChallenge()
		return helloOperationResult{}, errors.New("terminal challenge handle identity is inconsistent")
	}
	s.helloWinner, s.helloReceipt, s.helloReady = validatedWinner, validatedReceipt, true
	return helloOperationResult{winner: validatedWinner, receipt: validatedReceipt, prepared: prepared}, nil
}

func (s *Service) attach(token app.OperationToken, acceptance app.AttachmentChallengeAcceptanceReceipt) (protocolvalue.Attachment, protocolvalue.AttachReceipt, error) {
	if s.connection == nil || s.candidate == nil || s.authResult == nil || !s.helloReady {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, errors.New("terminal attach prerequisites are missing")
	}
	challenge, commitment, err := s.runtime.BorrowAttachmentChallengeOnce(acceptance)
	if err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	request := &protocol.AttachRequest{RequestId: token.RequestID, HandshakeCandidateId: s.candidate.CandidateId, HandshakeCandidateFingerprint: s.candidate.CandidateFingerprint, NegotiationWinnerFingerprint: s.helloWinner.NegotiationWinnerFingerprint, CurrentHelloReceiptFingerprint: s.helloReceipt.ReceiptFingerprint, AttachmentChallenge: challenge, AttachmentChallengeCommitment: commitment}
	defer clear(challenge)
	response, err := s.connection.RoundTrip(terminalwire.AttachFrame(request), 5*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	if err := terminalwire.ValidateServerFrame(response, "attach"); err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	receipt := response.GetAttach()
	semantic := receipt.GetAttachSemanticWinner()
	binding := receipt.GetCurrentTransportBinding()
	if semantic == nil || binding == nil || semantic.Attachment == nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, errors.New("terminal attach result is incomplete")
	}
	if err := protocol.ValidateFingerprint("terminal-attachment-identity:v2", semantic.Attachment, "identity_fingerprint", semantic.Attachment.IdentityFingerprint); err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	if err := protocol.ValidateFingerprint("terminal-client-transport-binding:v1", binding, "binding_fingerprint", binding.BindingFingerprint); err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	if err := protocol.ValidateFingerprint("terminal-attach-semantic-winner:v1", semantic, "semantic_winner_fingerprint", semantic.SemanticWinnerFingerprint); err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	if err := protocol.ValidateFingerprint("terminal-attach-result-receipt:v1", receipt, "receipt_fingerprint", receipt.ReceiptFingerprint); err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	if semantic.ControllerDisposition != protocol.ControllerDisposition_OBSERVER_ATTACHED || semantic.BootstrapRequirement != protocol.BootstrapRequirement_PROJECTION_AND_OPERATIONAL_SNAPSHOT_REQUIRED || binding.ConnectionId != s.helloReceipt.CurrentConnectionID {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, errors.New("terminal S1 attach result is incompatible")
	}
	attachment, validatedReceipt, err := protocolvalue.AttachFromProto(receipt, s.bootstrap.ClientInstanceID)
	if err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	reconnectCarrier, err := protocolvalue.ReconnectCredentialCarrierFromProto(receipt.NextReconnectCredentialCarrier)
	if err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	if err := s.runtime.InstallPendingReconnectCredential(reconnectCarrier); err != nil {
		clear(reconnectCarrier.Capability)
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	clear(reconnectCarrier.Capability)
	clear(receipt.NextReconnectCredentialCarrier.ReconnectCapability)
	s.attachReceipt = receipt
	s.attachment = attachment
	if err := s.connection.setTransportBinding(
		attachment.BindingGeneration,
		attachment.BindingFingerprint,
	); err != nil {
		return protocolvalue.Attachment{}, protocolvalue.AttachReceipt{}, err
	}
	return attachment, validatedReceipt, nil
}

func (s *Service) attachAck(token app.OperationToken) (protocolvalue.ValidatedAttachAckResult, error) {
	if s.connection == nil || s.attachReceipt == nil {
		return protocolvalue.ValidatedAttachAckResult{}, errors.New("terminal ACK prerequisites are missing")
	}
	semantic, binding := s.attachReceipt.AttachSemanticWinner, s.attachReceipt.CurrentTransportBinding
	request := &protocol.AttachReceiptAck{RequestId: token.RequestID, AttachmentId: semantic.Attachment.AttachmentId, AttachmentGeneration: semantic.Attachment.AttachmentGeneration, SemanticWinnerFingerprint: semantic.SemanticWinnerFingerprint, CurrentTransportBinding: binding, AttachResultReceiptFingerprint: s.attachReceipt.ReceiptFingerprint}
	if _, err := protocol.InstallFingerprint("terminal-attach-receipt-ack:v1", request, "ack_fingerprint"); err != nil {
		return protocolvalue.ValidatedAttachAckResult{}, err
	}
	response, err := s.connection.RoundTrip(terminalwire.AttachAckFrame(request), 5*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return protocolvalue.ValidatedAttachAckResult{}, err
	}
	if err := terminalwire.ValidateServerFrame(response, "attach_ack"); err != nil {
		return protocolvalue.ValidatedAttachAckResult{}, err
	}
	result := response.GetAttachAck()
	if err := protocol.ValidateFingerprint("terminal-attach-ack-result:v1", result, "ack_result_fingerprint", result.AckResultFingerprint); err != nil {
		return protocolvalue.ValidatedAttachAckResult{}, err
	}
	if result.AttachmentId != s.attachment.ID || result.AttachmentGeneration != s.attachment.Generation || result.SemanticWinnerFingerprint != s.attachment.SemanticWinnerFingerprint || result.AcknowledgedTransportBindingFingerprint != s.attachment.BindingFingerprint {
		return protocolvalue.ValidatedAttachAckResult{}, errors.New("terminal ACK result authority is stale")
	}
	validated, err := protocolvalue.AttachAckFromProto(result)
	if err != nil {
		return protocolvalue.ValidatedAttachAckResult{}, err
	}
	carrierFingerprint := ""
	if s.attachReceipt.NextReconnectCredentialCarrier != nil {
		carrierFingerprint = s.attachReceipt.NextReconnectCredentialCarrier.CarrierFingerprint
	}
	if carrierFingerprint == "" {
		return protocolvalue.ValidatedAttachAckResult{}, errors.New("terminal reconnect credential carrier is missing at ACK")
	}
	if err := s.runtime.PromotePendingReconnectCredential(carrierFingerprint); err != nil {
		return protocolvalue.ValidatedAttachAckResult{}, err
	}
	clear(s.bootstrap.LaunchCapability)
	s.bootstrap.LaunchCapability = nil
	return validated, nil
}

func (s *Service) durableSnapshot(token app.OperationToken, prepared protocolvalue.PreparedProjectionSnapshotRequest) (protocolvalue.ProjectionSnapshotOutcome, error) {
	request, err := prepared.ToProto()
	if err != nil || request.RequestId != token.RequestID || request.RuntimeSessionId != s.attachment.RuntimeSessionID {
		return protocolvalue.ProjectionSnapshotOutcome{}, errors.New("terminal projection snapshot request is stale")
	}
	response, err := s.connection.RoundTrip(terminalwire.SnapshotFrame(request), 10*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return protocolvalue.ProjectionSnapshotOutcome{}, err
	}
	if err := terminalwire.ValidateServerFrame(response, "snapshot"); err != nil {
		return protocolvalue.ProjectionSnapshotOutcome{}, err
	}
	value, err := protocolvalue.ProjectionSnapshotOutcomeFromProto(response.GetSnapshot())
	if err != nil {
		return protocolvalue.ProjectionSnapshotOutcome{}, err
	}
	if value.RebaseRequired {
		if !prepared.HasMinimum || value.RequestedMinimumFingerprint != prepared.MinimumObservedControlCursor.Fingerprint {
			return protocolvalue.ProjectionSnapshotOutcome{}, errors.New("terminal control rebase response is stale")
		}
		return value, nil
	}
	if value.Snapshot.RuntimeSessionID != s.attachment.RuntimeSessionID || value.Snapshot.RequestID != prepared.RequestID {
		return protocolvalue.ProjectionSnapshotOutcome{}, errors.New("terminal durable snapshot crosses sessions")
	}
	if prepared.HasMinimum && (!value.Snapshot.HasValidatedMinimumControlCursor || value.Snapshot.ValidatedMinimumControlCursorFingerprint != prepared.MinimumObservedControlCursor.Fingerprint || value.Snapshot.Control.Generation != prepared.MinimumObservedControlCursor.Generation || value.Snapshot.Control.Revision < prepared.MinimumObservedControlCursor.Revision) {
		return protocolvalue.ProjectionSnapshotOutcome{}, errors.New("terminal durable snapshot did not satisfy its control lower bound")
	}
	return value, nil
}

func (s *Service) operationalSnapshot(token app.OperationToken, prepared protocolvalue.PreparedOperationalSnapshotRequest) (protocolvalue.OperationalSnapshot, error) {
	request, err := prepared.ToProto()
	if err != nil {
		return protocolvalue.OperationalSnapshot{}, err
	}
	if request.RequestId != token.RequestID || request.RuntimeSessionId != s.attachment.RuntimeSessionID || request.AttachmentId != s.attachment.ID || request.AttachmentGeneration != s.attachment.Generation || request.AttachmentIdentityFingerprint != s.attachment.IdentityFingerprint || s.attachReceipt == nil || request.CurrentTransportBinding.BindingFingerprint != s.attachReceipt.CurrentTransportBinding.BindingFingerprint {
		return protocolvalue.OperationalSnapshot{}, errors.New("terminal operational snapshot request is stale")
	}
	response, err := s.connection.RoundTrip(terminalwire.OperationalSnapshotFrame(request), 10*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return protocolvalue.OperationalSnapshot{}, err
	}
	if err := terminalwire.ValidateServerFrame(response, "operational_snapshot"); err != nil {
		return protocolvalue.OperationalSnapshot{}, err
	}
	value, err := protocolvalue.OperationalSnapshotFromWire(response.GetOperationalSnapshot())
	if err != nil {
		return protocolvalue.OperationalSnapshot{}, err
	}
	if value.RuntimeSessionID != s.attachment.RuntimeSessionID || value.RequestID != prepared.RequestID || value.AttachmentID != prepared.AttachmentID || value.AttachmentGeneration != prepared.AttachmentGeneration || value.AttachmentIdentityFingerprint != prepared.AttachmentIdentityFingerprint || value.AcknowledgedBindingFingerprint != prepared.CurrentBinding.Fingerprint {
		return protocolvalue.OperationalSnapshot{}, errors.New("terminal operational snapshot authority is stale")
	}
	return value, nil
}

func (s *Service) observe(token app.OperationToken, prepared protocolvalue.PreparedObserveRequest) (protocolvalue.ObservationResult, error) {
	if err := validateObserveAttribution(prepared, s.attachment); err != nil {
		return protocolvalue.ObservationResult{}, err
	}
	request, err := prepared.ToProto()
	if err != nil || request.RequestId != token.RequestID || token.AttachmentID != s.attachment.ID || token.AttachmentGeneration != s.attachment.Generation || token.TransportBindingFingerprint != s.attachment.BindingFingerprint {
		return protocolvalue.ObservationResult{}, errors.New("terminal observe request is stale")
	}
	response, err := s.connection.RoundTrip(terminalwire.ObserveFrame(request), time.Duration(request.MaximumWaitMs)*time.Millisecond+2*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return protocolvalue.ObservationResult{}, err
	}
	if err := terminalwire.ValidateServerFrame(response, "observation"); err != nil {
		return protocolvalue.ObservationResult{}, err
	}
	value, err := s.bridge.decode(response.GetObservation(), prepared)
	if err != nil {
		return protocolvalue.ObservationResult{}, err
	}
	requestID := value.NoChange.RequestID
	if value.IsBatch {
		requestID = value.Batch.RequestID
	}
	if requestID != prepared.RequestID {
		return protocolvalue.ObservationResult{}, errors.New("terminal observation response crosses requests")
	}
	return value, nil
}

func (s *Service) historyPage(token app.OperationToken, prepared protocolvalue.PreparedHistoryPageRequest) (protocolvalue.HistoryPageResult, error) {
	if err := validateHistoryPageAttribution(prepared, s.attachment); err != nil {
		return protocolvalue.HistoryPageResult{}, err
	}
	request, err := prepared.ToProto()
	if err != nil || request.RequestId != token.RequestID || request.RuntimeSessionId != s.attachment.RuntimeSessionID || token.AttachmentID != s.attachment.ID || token.TransportBindingFingerprint != s.attachment.BindingFingerprint {
		return protocolvalue.HistoryPageResult{}, errors.New("terminal history page request is stale")
	}
	response, err := s.connection.RoundTrip(terminalwire.HistoryPageFrame(request), 10*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return protocolvalue.HistoryPageResult{}, err
	}
	if err := terminalwire.ValidateServerFrame(response, "history_page"); err != nil {
		return protocolvalue.HistoryPageResult{}, err
	}
	value, err := protocolvalue.HistoryPageFromProto(response.GetHistoryPage())
	if err != nil {
		return protocolvalue.HistoryPageResult{}, err
	}
	if value.RequestID != prepared.RequestID || value.RequestedCursorFingerprint != prepared.Cursor.Fingerprint {
		return protocolvalue.HistoryPageResult{}, errors.New("terminal history page response is stale")
	}
	return value, nil
}

func (s *Service) heartbeat(token app.OperationToken, prepared protocolvalue.PreparedHeartbeatRequest) (*protocolvalue.ValidatedHeartbeatAcceptedReceipt, *protocolvalue.ValidatedHeartbeatRejectedReceipt, error) {
	if s.attachReceipt == nil {
		return nil, nil, errors.New("terminal heartbeat has no attachment")
	}
	request, err := prepared.ToProto()
	if err != nil {
		return nil, nil, err
	}
	if request.RequestId != token.RequestID || request.RuntimeSessionId != s.attachment.RuntimeSessionID || request.AttachmentId != s.attachment.ID || request.AttachmentGeneration != s.attachment.Generation || request.AttachmentIdentityFingerprint != s.attachment.IdentityFingerprint || request.AttachSemanticWinnerFingerprint != s.attachment.SemanticWinnerFingerprint || request.CurrentTransportBinding.BindingFingerprint != s.attachReceipt.CurrentTransportBinding.BindingFingerprint {
		return nil, nil, errors.New("terminal heartbeat request is stale")
	}
	response, err := s.connection.RoundTrip(terminalwire.HeartbeatFrame(request), 5*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return nil, nil, err
	}
	if err := terminalwire.ValidateServerFrame(response, "heartbeat"); err != nil {
		return nil, nil, err
	}
	result := response.GetHeartbeat()
	if result == nil {
		return nil, nil, errors.New("terminal heartbeat result is empty")
	}
	if rejected := result.GetRejected(); rejected != nil {
		validated, validationErr := protocolvalue.HeartbeatRejectedFromProto(rejected)
		if validationErr != nil {
			return nil, nil, validationErr
		}
		if validated.RequestID != prepared.RequestID || validated.CandidateFingerprint != prepared.CandidateFingerprint || validated.SubmittedBindingFingerprint != prepared.CurrentBinding.Fingerprint {
			return nil, nil, errors.New("terminal heartbeat rejection is stale")
		}
		return nil, &validated, nil
	}
	accepted := result.GetAccepted()
	if accepted == nil {
		return nil, nil, errors.New("terminal heartbeat outcome is unknown")
	}
	if err := protocol.ValidateFingerprint("terminal-heartbeat-accepted-receipt:v1", accepted, "receipt_fingerprint", accepted.ReceiptFingerprint); err != nil {
		return nil, nil, err
	}
	if accepted.RequestId != prepared.RequestID || accepted.HeartbeatGeneration != prepared.HeartbeatGeneration || accepted.PreviousAcceptedHeartbeatGeneration != prepared.PreviousAcceptedGeneration || accepted.HeartbeatCandidateFingerprint != prepared.CandidateFingerprint || accepted.AcknowledgedTransportBindingFingerprint != prepared.CurrentBinding.Fingerprint {
		return nil, nil, errors.New("terminal heartbeat receipt is stale")
	}
	validated, err := protocolvalue.HeartbeatAcceptedFromProto(accepted)
	if err != nil {
		return nil, nil, err
	}
	return &validated, nil, nil
}

func (s *Service) Close() error {
	deadline := time.Now().Add(5 * time.Second)
	s.mu.Lock()
	s.closed = true
	if s.finalized {
		done := s.closeDone
		s.mu.Unlock()
		timer := time.NewTimer(time.Until(deadline))
		defer timer.Stop()
		select {
		case <-done:
			s.mu.Lock()
			result := s.closeErr
			s.mu.Unlock()
			return result
		case <-timer.C:
			return errors.New("terminal service close owner is still draining")
		}
	}
	s.finalized = true
	connection := s.connection
	s.mu.Unlock()
	var closeErr error
	if connection != nil {
		closeErr = connection.CloseAndWait(deadline)
	}
	s.executeMu.Lock()
	if schedulerErr := s.scheduler.closeAndDrain(deadline); schedulerErr != nil && closeErr == nil {
		closeErr = schedulerErr
	}
	if drainErr := s.operations.drainConnectionTerminalizations(deadline); drainErr != nil && closeErr == nil {
		closeErr = drainErr
	}
	_ = s.runtime.Close()
	clear(s.bootstrap.LaunchCapability)
	s.bootstrap.LaunchCapability = nil
	s.executeMu.Unlock()
	s.mu.Lock()
	s.closeErr = closeErr
	close(s.closeDone)
	s.mu.Unlock()
	return closeErr
}

func (s *Service) failure(operation app.OutstandingOperation, err error) tea.Msg {
	if operation.Carrier == app.OutstandingLocal &&
		(operation.Local.Kind == app.OpChallengePromote ||
			operation.Local.Kind == app.OpChallengePromotionConfirm ||
			operation.Local.Kind == app.OpChallengeRevokePrepared ||
			operation.Local.Kind == app.OpChallengeRevokeActive) {
		s.runtime.RevokeChallenge()
	}
	if operation.Carrier == app.OutstandingWire && operation.Wire.Kind == app.OpAttach {
		s.runtime.RevokeChallenge()
	}
	message := sanitizePublicError(err)
	s.mu.Lock()
	connection := s.connection
	s.mu.Unlock()
	failure := s.operations.classifyFailure(operation, err, message, connection)
	var physical *terminalwire.PhysicalIOError
	if errors.As(err, &physical) && physical.Phase != terminalwire.DeliveryNotStarted {
		s.mu.Lock()
		if s.connection == connection {
			s.connection = nil
		}
		s.mu.Unlock()
	}
	switch operation.Carrier {
	case app.OutstandingLocal:
		header, _ := app.NewLocalResultHeader(operation.Local, failure.EvidenceFingerprint(), time.Now())
		if operation.Local.Kind == app.OpChallengePromote ||
			operation.Local.Kind == app.OpChallengePromotionConfirm ||
			operation.Local.Kind == app.OpChallengeRevokePrepared ||
			operation.Local.Kind == app.OpChallengeRevokeActive {
			return app.AttachmentChallengePromotionFailedMsg{Header: header, Failure: failure}
		}
		if operation.Local.Kind == app.OpTeardown {
			return app.TeardownFailedMsg{Header: header, Failure: failure}
		}
		if operation.Local.Kind == app.OpParentRelaunch {
			return app.ParentRelaunchFailedMsg{Header: header, Failure: failure}
		}
		if operation.Local.Kind == app.OpClipboard {
			return app.PublicTextCopyFailedMsg{Header: header, Failure: failure}
		}
		return app.ConnectFailedMsg{Header: header, Failure: failure}
	case app.OutstandingWire:
		header, _ := app.NewIOHeader(operation.Wire, failure.EvidenceFingerprint(), time.Now())
		switch operation.Wire.Kind {
		case app.OpTransportAuth:
			return app.TransportAuthenticationFailedMsg{Header: header, Failure: failure}
		case app.OpHello:
			return app.HelloTransportFailedMsg{Header: header, Failure: failure}
		case app.OpAttach:
			return app.AttachRejectedMsg{Header: header, Failure: failure}
		case app.OpAttachAck:
			return app.AttachAckFailedMsg{Header: header, Failure: failure}
		case app.OpProjectionSnapshot:
			return app.SnapshotRejectedMsg{Header: header, Failure: failure}
		case app.OpOperationalSnapshot:
			return app.OperationalSnapshotRejectedMsg{Header: header, Failure: failure}
		case app.OpHeartbeat:
			return app.HeartbeatTransportFailedMsg{Header: header, Failure: failure}
		case app.OpObserve:
			return app.ObservationRejectedMsg{Header: header, Failure: failure}
		case app.OpHistoryPage:
			return app.HistoryPageRejectedMsg{Header: header, Failure: failure}
		default:
			return app.TransportAuthenticationFailedMsg{Header: header, Failure: failure}
		}
	default:
		panic("terminal failure classified without an outstanding operation carrier")
	}
}

func (s *Service) admissionFailure(operation app.OutstandingOperation, err error) tea.Msg {
	message := sanitizePublicError(err)
	failure := s.operations.classifyUnadmittedFailure(
		operation,
		message,
		"operation-registry-admission-failed",
	)
	return failureMessageForOperation(operation, failure)
}

func failureMessageForOperation(operation app.OutstandingOperation, failure app.PublicFailure) tea.Msg {
	switch operation.Carrier {
	case app.OutstandingLocal:
		header, _ := app.NewLocalResultHeader(operation.Local, failure.EvidenceFingerprint(), time.Now())
		switch operation.Local.Kind {
		case app.OpChallengePromote, app.OpChallengePromotionConfirm,
			app.OpChallengeRevokePrepared, app.OpChallengeRevokeActive:
			return app.AttachmentChallengePromotionFailedMsg{Header: header, Failure: failure}
		case app.OpClipboard:
			return app.PublicTextCopyFailedMsg{Header: header, Failure: failure}
		case app.OpTeardown:
			return app.TeardownFailedMsg{Header: header, Failure: failure}
		case app.OpParentRelaunch:
			return app.ParentRelaunchFailedMsg{Header: header, Failure: failure}
		default:
			return app.ConnectFailedMsg{Header: header, Failure: failure}
		}
	case app.OutstandingWire:
		header, _ := app.NewIOHeader(operation.Wire, failure.EvidenceFingerprint(), time.Now())
		switch operation.Wire.Kind {
		case app.OpTransportAuth:
			return app.TransportAuthenticationFailedMsg{Header: header, Failure: failure}
		case app.OpHello:
			return app.HelloTransportFailedMsg{Header: header, Failure: failure}
		case app.OpAttach:
			return app.AttachRejectedMsg{Header: header, Failure: failure}
		case app.OpAttachAck:
			return app.AttachAckFailedMsg{Header: header, Failure: failure}
		case app.OpHeartbeat:
			return app.HeartbeatTransportFailedMsg{Header: header, Failure: failure}
		case app.OpProjectionSnapshot:
			return app.SnapshotRejectedMsg{Header: header, Failure: failure}
		case app.OpOperationalSnapshot:
			return app.OperationalSnapshotRejectedMsg{Header: header, Failure: failure}
		case app.OpObserve:
			return app.ObservationRejectedMsg{Header: header, Failure: failure}
		case app.OpHistoryPage:
			return app.HistoryPageRejectedMsg{Header: header, Failure: failure}
		default:
			return app.TransportAuthenticationFailedMsg{Header: header, Failure: failure}
		}
	default:
		panic("terminal admission failure has no outstanding operation carrier")
	}
}
func sanitizePublicError(err error) string {
	if err == nil {
		return "The terminal operation failed."
	}
	value := publictext.Bounded(err.Error(), 512, 2048)
	if value == "" {
		value = "The terminal operation failed."
	}
	return value
}
func randomBytes(size int) []byte {
	value := make([]byte, size)
	if _, err := rand.Read(value); err != nil {
		panic(err)
	}
	return value
}
func currentUID() int { return os.Geteuid() }
