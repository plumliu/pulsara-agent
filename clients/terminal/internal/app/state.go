package app

import (
	"errors"
	"fmt"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/commandstate"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/components/transcript"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/interaction"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/presentation"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/queue"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/secret"
)

type AppPhase uint8

const (
	PhaseBooting AppPhase = iota + 1
	PhaseConnecting
	PhaseNegotiating
	PhaseAttaching
	PhaseLoadingSnapshot
	PhaseReady
	PhaseReconnecting
	PhaseReadOnly
	PhaseFatal
	PhaseDetaching
	PhaseExited
)

type ConnectionPhase uint8

const (
	ConnectionDisconnected ConnectionPhase = iota + 1
	ConnectionDialing
	ConnectionAuthPending
	ConnectionHelloPending
	ConnectionAttachPending
	ConnectionAttachAckPending
	ConnectionAttached
	ConnectionBackoff
	ConnectionClosing
)

type SnapshotLoadingPhase uint8

const (
	SnapshotLoadingUninitialized SnapshotLoadingPhase = iota + 1
	SnapshotAwaitingDurableSnapshot
	SnapshotAwaitingOperationalSnapshot
	SnapshotBaselinesInstalled
)

type SnapshotLoadingState struct {
	Phase                           SnapshotLoadingPhase
	AttachmentID                    string
	AttachmentGeneration            uint64
	TransportBindingFingerprint     string
	DurableOperationID              string
	DurableOperationGeneration      uint64
	DurableSnapshotFingerprint      string
	DurableControlCursorFingerprint string
	OperationalOperationID          string
	OperationalOperationGeneration  uint64
	OperationalSnapshotFingerprint  string
	OperationalGeneration           uint64
	OperationalCursor               uint64
	OperationalRequired             bool
}

func (s SnapshotLoadingState) Validate(appPhase AppPhase) error {
	switch s.Phase {
	case SnapshotLoadingUninitialized:
		if s.AttachmentID != "" || s.AttachmentGeneration != 0 || s.TransportBindingFingerprint != "" || s.DurableOperationID != "" || s.DurableSnapshotFingerprint != "" || s.OperationalOperationID != "" || s.OperationalSnapshotFingerprint != "" || s.OperationalRequired {
			return errors.New("uninitialized snapshot state contains authority")
		}
	case SnapshotAwaitingDurableSnapshot:
		if (appPhase != PhaseLoadingSnapshot && appPhase != PhaseReadOnly) || s.AttachmentID == "" || s.AttachmentGeneration == 0 || s.TransportBindingFingerprint == "" || s.DurableOperationID == "" || s.DurableOperationGeneration == 0 || s.DurableSnapshotFingerprint != "" || s.OperationalOperationID != "" {
			return errors.New("durable snapshot wait state is invalid")
		}
	case SnapshotAwaitingOperationalSnapshot:
		if (appPhase != PhaseLoadingSnapshot && appPhase != PhaseReadOnly) || !s.OperationalRequired || s.DurableSnapshotFingerprint == "" || s.DurableControlCursorFingerprint == "" || s.OperationalOperationID == "" || s.OperationalOperationGeneration == 0 || s.OperationalSnapshotFingerprint != "" {
			return errors.New("operational snapshot wait state is invalid")
		}
	case SnapshotBaselinesInstalled:
		// A Ready reconnect keeps the last confirmed baselines for display while
		// the next physical connection and semantic attachment are negotiated.
		// They remain stale/read-only and are replaced by the mandatory snapshots
		// after the successor Attach ACK.
		if appPhase != PhaseReady && appPhase != PhaseReadOnly &&
			appPhase != PhaseReconnecting && appPhase != PhaseConnecting &&
			appPhase != PhaseNegotiating && appPhase != PhaseAttaching &&
			appPhase != PhaseDetaching && appPhase != PhaseFatal && appPhase != PhaseExited {
			return errors.New("installed snapshot baseline has an invalid app phase")
		}
		if s.DurableSnapshotFingerprint == "" || s.DurableControlCursorFingerprint == "" || s.OperationalSnapshotFingerprint == "" || s.OperationalGeneration == 0 {
			return errors.New("installed snapshot baseline proof is incomplete")
		}
		if s.OperationalRequired {
			return errors.New("installed snapshot baseline retains rebuild intent")
		}
	default:
		return errors.New("snapshot loading phase is unknown")
	}
	return nil
}

type HeartbeatScheduleState struct {
	NextGeneration         uint64
	LastAcceptedGeneration uint64
	LastDisposition        protocolvalue.HeartbeatLivenessDisposition
	NextAt                 time.Time
	LeaseExpiresAt         time.Time
	Missed                 uint32
	LastReceiptFingerprint string
}

func (s HeartbeatScheduleState) Validate(attached bool) error {
	if !attached {
		if s.NextGeneration != 0 || s.LastAcceptedGeneration != 0 || !s.NextAt.IsZero() || !s.LeaseExpiresAt.IsZero() || s.LastReceiptFingerprint != "" {
			return errors.New("heartbeat schedule exists without attachment")
		}
		return nil
	}
	if s.NextGeneration == 0 || s.LastAcceptedGeneration >= s.NextGeneration || s.NextAt.IsZero() || s.LeaseExpiresAt.IsZero() {
		return errors.New("heartbeat generation state is invalid")
	}
	if s.LastAcceptedGeneration == 0 && (s.LastReceiptFingerprint != "" || s.LastDisposition != protocolvalue.HeartbeatLivenessUnspecified) {
		return errors.New("heartbeat receipt exists before first acceptance")
	}
	return nil
}

type ConnectionState struct {
	Phase                                 ConnectionPhase
	ClientInstanceID                      string
	BootstrapHandleID                     string
	TransportCredentialHandleID           string
	ReconnectCredentialHandleID           string
	ReconnectCredentialCarrierFingerprint string
	HasReconnectCredentialHandle          bool
	Generation                            uint64
	HandleID                              string
	ServerConnectionID                    string
	NextOperationGeneration               uint64
	Outstanding                           OutstandingOperation
	HandshakeCandidate                    protocolvalue.HandshakeCandidate
	TransportAuth                         protocolvalue.TransportAuthResult
	HelloWinner                           protocolvalue.HelloNegotiationWinner
	HelloReceipt                          protocolvalue.ValidatedServerHelloReceipt
	AttachmentChallenge                   AttachmentChallengeState
	AttachReceipt                         protocolvalue.AttachReceipt
	HeartbeatSchedule                     HeartbeatScheduleState
}

type AttachmentState struct {
	Valid    bool
	Identity protocolvalue.Attachment
}

type ComposerState struct{ Enabled bool }

type ClientMouseMode uint8

const (
	MouseDisabled ClientMouseMode = iota + 1
	MouseCellMotion
	MouseAllMotion
)

type LocalNotificationState struct {
	Items   []string
	Dropped uint64
}

type ClipboardOperationState struct {
	Pending bool
	Token   LocalOperationToken
}

func (s ClipboardOperationState) Validate() error {
	if s.Pending {
		if s.Token.Kind != OpClipboard || !s.Token.Valid() {
			return errors.New("terminal clipboard operation owner is invalid")
		}
		return nil
	}
	if s.Token != (LocalOperationToken{}) {
		return errors.New("terminal idle clipboard owner retains an operation")
	}
	return nil
}

type ObservationLoopState struct {
	Enabled                  bool
	LastResultFingerprint    string
	ViewportIntentGeneration uint64
	SnapshotRebaseRounds     uint8
	PendingPage              protocolvalue.PreparedHistoryPageRequest
	HasPendingPage           bool
	PageIntentDirection      protocolvalue.HistoryPageDirection
	HasPageIntent            bool
}

func (s ObservationLoopState) Validate() error {
	if s.ViewportIntentGeneration == 0 {
		return errors.New("terminal viewport intent generation is zero")
	}
	if s.SnapshotRebaseRounds > 4 || s.HasPendingPage != (s.PendingPage.RequestID != "") || s.HasPageIntent != (s.PageIntentDirection != 0) {
		return errors.New("terminal observation loop state is invalid")
	}
	return nil
}

type TeardownPhase uint8

const (
	TeardownIdle TeardownPhase = iota + 1
	TeardownStoppingEffects
	TeardownDetaching
	TeardownDraining
	TeardownRestoringTerminal
	TeardownTerminal
)

type TeardownReason uint8

const (
	TeardownUserQuit TeardownReason = iota + 1
	TeardownParentShutdown
	TeardownServerClosing
	TeardownSignal
	TeardownFatalCompatibility
	TeardownClientInvariant
	TeardownParentRelaunch
)

type PublicTeardownDisposition uint8

const (
	TeardownCompleted PublicTeardownDisposition = iota + 1
	TeardownDeadlineExceeded
	TeardownEmergencyRestoreRequired
)

type PublicTeardownSummary struct {
	TeardownGeneration       uint64
	Disposition              PublicTeardownDisposition
	CancelledOperationCount  uint32
	DrainedOperationCount    uint32
	RevokedSecretHandleCount uint32
	DetachAttempted          bool
	DetachConfirmed          bool
	TerminalRestoreCompleted bool
	Failure                  PublicFailure
	HasFailure               bool
}

func NewPublicTeardownSummary(
	teardownGeneration uint64,
	disposition PublicTeardownDisposition,
	cancelledOperationCount uint32,
	drainedOperationCount uint32,
	revokedSecretHandleCount uint32,
	detachAttempted bool,
	detachConfirmed bool,
	terminalRestoreCompleted bool,
	failure PublicFailure,
	hasFailure bool,
) (PublicTeardownSummary, error) {
	value := PublicTeardownSummary{
		TeardownGeneration:       teardownGeneration,
		Disposition:              disposition,
		CancelledOperationCount:  cancelledOperationCount,
		DrainedOperationCount:    drainedOperationCount,
		RevokedSecretHandleCount: revokedSecretHandleCount,
		DetachAttempted:          detachAttempted,
		DetachConfirmed:          detachConfirmed,
		TerminalRestoreCompleted: terminalRestoreCompleted,
		Failure:                  failure,
		HasFailure:               hasFailure,
	}
	if err := value.Validate(); err != nil {
		return PublicTeardownSummary{}, err
	}
	return value, nil
}

func (s PublicTeardownSummary) Validate() error {
	if s.TeardownGeneration == 0 || (s.DetachConfirmed && !s.DetachAttempted) {
		return errors.New("terminal teardown summary identity matrix is invalid")
	}
	switch s.Disposition {
	case TeardownCompleted:
		if s.HasFailure || s.Failure != (PublicFailure{}) || !s.TerminalRestoreCompleted {
			return errors.New("completed terminal teardown contains failure authority")
		}
	case TeardownDeadlineExceeded, TeardownEmergencyRestoreRequired:
		if !s.HasFailure || s.Failure.Validate() != nil ||
			(s.Failure.Code() != FailureTeardown &&
				s.Failure.Code() != FailureTeardownDeadline) {
			return errors.New("incomplete terminal teardown lacks its closed failure")
		}
	default:
		return errors.New("terminal teardown disposition is unknown")
	}
	return nil
}

func (s PublicTeardownSummary) Fingerprint() (string, error) {
	failureCode := PublicFailureCode(0)
	failureDisposition := FailureDisposition(0)
	failureEvidence := ""
	failureMessage := ""
	if s.HasFailure {
		failureCode = s.Failure.Code()
		failureDisposition = s.Failure.Disposition()
		failureEvidence = s.Failure.EvidenceFingerprint()
		failureMessage = s.Failure.Message()
	}
	return protocolvalue.CanonicalClientFingerprint(
		"terminal-public-teardown-summary:v1",
		map[string]any{
			"teardown_generation":         s.TeardownGeneration,
			"disposition":                 s.Disposition,
			"cancelled_operation_count":   s.CancelledOperationCount,
			"drained_operation_count":     s.DrainedOperationCount,
			"revoked_secret_handle_count": s.RevokedSecretHandleCount,
			"detach_attempted":            s.DetachAttempted,
			"detach_confirmed":            s.DetachConfirmed,
			"terminal_restore_completed":  s.TerminalRestoreCompleted,
			"has_failure":                 s.HasFailure,
			"failure_code":                failureCode,
			"failure_disposition":         failureDisposition,
			"failure_evidence":            failureEvidence,
			"failure_message":             failureMessage,
		},
	)
}

type TeardownState struct {
	Phase                    TeardownPhase
	Reason                   TeardownReason
	Generation               uint64
	Deadline                 time.Time
	DetachCommandID          string
	StopAcceptingEffects     bool
	PhysicalOperationCount   uint32
	SecretRuntimeRevoked     bool
	SchedulerDrained         bool
	BridgeDrained            bool
	TerminalRestoreCompleted bool
}

func NewIdleTeardownState() TeardownState { return TeardownState{Phase: TeardownIdle} }

func NewActiveTeardownState(reason TeardownReason, generation uint64, deadline time.Time) TeardownState {
	return TeardownState{
		Phase:                TeardownStoppingEffects,
		Reason:               reason,
		Generation:           generation,
		Deadline:             deadline,
		StopAcceptingEffects: true,
	}
}

func (s TeardownState) Validate() error {
	switch s.Phase {
	case TeardownIdle:
		if s.Reason != 0 || s.Generation != 0 || !s.Deadline.IsZero() ||
			s.DetachCommandID != "" || s.StopAcceptingEffects ||
			s.PhysicalOperationCount != 0 || s.SecretRuntimeRevoked ||
			s.SchedulerDrained || s.BridgeDrained || s.TerminalRestoreCompleted {
			return errors.New("idle teardown state contains terminalization authority")
		}
	case TeardownStoppingEffects, TeardownDetaching, TeardownDraining, TeardownRestoringTerminal:
		if s.Reason == 0 || s.Generation == 0 || s.Deadline.IsZero() || !s.StopAcceptingEffects {
			return errors.New("active teardown state is incomplete")
		}
	case TeardownTerminal:
		if s.Reason == 0 || s.Generation == 0 || !s.StopAcceptingEffects {
			return errors.New("terminal teardown state is incomplete")
		}
	default:
		return errors.New("teardown phase is unknown")
	}
	if s.TerminalRestoreCompleted &&
		(!s.SecretRuntimeRevoked || !s.SchedulerDrained || !s.BridgeDrained) {
		return errors.New("terminal restore completed before its teardown dependencies")
	}
	return nil
}

type PublicFailureCode uint16

const (
	FailureConnect PublicFailureCode = iota + 1
	FailurePeerIdentity
	FailureBootstrap
	FailureTransportAuthentication
	FailureTransportIO
	FailureProtocolVersion
	FailureProtocolSchema
	FailureRequiredCapability
	FailureAttach
	FailureHeartbeat
	FailureReadTimeout
	FailureCommandOutcomeTimeout
	FailureCancelled
	FailureProjectionSnapshot
	FailureOperationalSnapshot
	FailureHistoryPage
	FailureCommandPreDispatch
	FailureCommandDeliveryUnknown
	FailureSecretTransport
	FailureSecretSubmitDeliveryUnknown
	FailureClipboard
	FailureOpenURL
	FailureTeardown
	FailureTeardownDeadline
	FailureClientInvariant
)

type FailureDisposition uint8

const (
	FailureFatal FailureDisposition = iota + 1
	FailureRetryWithBackoff
	FailureReconnect
	FailureRetryRead
	FailureQueryCommand
	FailureRebuildDurableSnapshot
	FailureRebuildOperationalSnapshot
	FailureRevokeSecret
	FailureRevokeSecretAndQuery
	FailureContinueTeardown
	FailureNoRetry
)

type FailureDeliveryPhase uint8

const (
	DeliveryNotStarted FailureDeliveryPhase = iota + 1
	DeliveryWriteStarted
	DeliveryRequestFullySent
	DeliveryResponseReadStarted
	DeliveryResponseFullyValidated
	DeliveryLocalOperationStarted
)

type FailureConnectionState uint8

const (
	FailureConnectionNotEstablished FailureConnectionState = iota + 1
	FailureConnectionUsable
	FailureConnectionInvalidated
	FailureConnectionClosing
)

type PhysicalFailureCause uint8

const (
	CauseDialFailed PhysicalFailureCause = iota + 1
	CausePeerRejected
	CauseBootstrapRejected
	CauseAuthenticationRejected
	CauseProtocolVersionRejected
	CauseProtocolSchemaRejected
	CauseRequiredCapabilityMissing
	CauseAttachRejected
	CauseDeadlineExpired
	CauseCallerCancelled
	CauseEOF
	CauseReadFailed
	CauseWriteFailed
	CauseMalformedResponse
	CauseProjectionValidationFailed
	CauseLocalIntegrationFailed
	CauseClientInvariant
)

// FailureProductionFact is the immutable proof produced by the physical
// operation registry before a failure may cross into Update. Its fields stay
// private so message producers cannot alter a classified delivery phase or
// retry disposition with a struct literal.
type FailureProductionFact struct {
	operationKind                        OperationKind
	operationID                          string
	requestID                            string
	hasRequestID                         bool
	deliveryPhase                        FailureDeliveryPhase
	connectionState                      FailureConnectionState
	physicalCause                        PhysicalFailureCause
	connectionTerminalReceiptFingerprint string
	hasConnectionTerminalReceipt         bool
	physicalReceiptFingerprint           string
	evidenceFingerprint                  string
}

type PublicFailure struct {
	code        PublicFailureCode
	message     string
	disposition FailureDisposition
	production  FailureProductionFact
}

func (f PublicFailure) Validate() error {
	if f.code == 0 || f.disposition == 0 || f.message == "" || len([]rune(f.message)) > 512 || len([]byte(f.message)) > 2048 {
		return errors.New("public terminal failure is invalid")
	}
	if err := f.production.Validate(); err != nil {
		return err
	}
	expectedCode, err := classifyFailureCode(f.production)
	if err != nil || expectedCode != f.code {
		return errors.New("public terminal failure code does not match its production fact")
	}
	if expectedFailureDisposition(f.code) != f.disposition {
		return errors.New("public terminal failure disposition does not match its code")
	}
	return nil
}

// ClassifyPublicFailure is the sole cross-package construction seam. Production
// architecture gates restrict it to the physical operation registry; callers
// provide a sealed receipt fingerprint, never a failure code or disposition.
func ClassifyPublicFailure(
	operation OutstandingOperation,
	deliveryPhase FailureDeliveryPhase,
	connectionState FailureConnectionState,
	physicalCause PhysicalFailureCause,
	connectionTerminalReceiptFingerprint string,
	hasConnectionTerminalReceipt bool,
	physicalReceiptFingerprint string,
	message string,
) (PublicFailure, error) {
	return classifyPublicFailure(
		operation,
		deliveryPhase,
		connectionState,
		physicalCause,
		connectionTerminalReceiptFingerprint,
		hasConnectionTerminalReceipt,
		physicalReceiptFingerprint,
		message,
	)
}

func classifyPublicFailure(
	operation OutstandingOperation,
	deliveryPhase FailureDeliveryPhase,
	connectionState FailureConnectionState,
	physicalCause PhysicalFailureCause,
	connectionTerminalReceiptFingerprint string,
	hasConnectionTerminalReceipt bool,
	physicalReceiptFingerprint string,
	message string,
) (PublicFailure, error) {
	kind, operationID, requestID, hasRequestID, err := failureOperationIdentity(operation)
	if err != nil {
		return PublicFailure{}, err
	}
	production := FailureProductionFact{
		operationKind:                        kind,
		operationID:                          operationID,
		requestID:                            requestID,
		hasRequestID:                         hasRequestID,
		deliveryPhase:                        deliveryPhase,
		connectionState:                      connectionState,
		physicalCause:                        physicalCause,
		connectionTerminalReceiptFingerprint: connectionTerminalReceiptFingerprint,
		hasConnectionTerminalReceipt:         hasConnectionTerminalReceipt,
		physicalReceiptFingerprint:           physicalReceiptFingerprint,
	}
	evidence, err := production.expectedEvidenceFingerprint()
	if err != nil {
		return PublicFailure{}, err
	}
	production.evidenceFingerprint = evidence
	code, err := classifyFailureCode(production)
	if err != nil {
		return PublicFailure{}, err
	}
	value := PublicFailure{code: code, message: message, disposition: expectedFailureDisposition(code), production: production}
	return value, value.Validate()
}

func failureOperationIdentity(operation OutstandingOperation) (OperationKind, string, string, bool, error) {
	if !operation.Valid() || operation.Carrier == OutstandingNone {
		return 0, "", "", false, errors.New("failure production operation is invalid")
	}
	if operation.Carrier == OutstandingWire {
		return operation.Wire.Kind, operation.Wire.OperationID, operation.Wire.RequestID, true, nil
	}
	return operation.Local.Kind, operation.Local.OperationID, "", false, nil
}

func (f FailureProductionFact) Validate() error {
	if f.operationKind == 0 || f.operationID == "" || f.deliveryPhase == 0 ||
		f.connectionState == 0 || f.physicalCause == 0 ||
		f.physicalReceiptFingerprint == "" || f.evidenceFingerprint == "" {
		return errors.New("terminal failure production fact is incomplete")
	}
	if f.operationKind.wire() != f.hasRequestID ||
		(f.hasRequestID && f.requestID == "") || (!f.hasRequestID && f.requestID != "") {
		return errors.New("terminal failure request identity matrix is invalid")
	}
	if f.hasConnectionTerminalReceipt != (f.connectionTerminalReceiptFingerprint != "") {
		return errors.New("terminal failure connection receipt matrix is invalid")
	}
	if f.hasConnectionTerminalReceipt {
		if f.connectionState != FailureConnectionInvalidated ||
			(f.deliveryPhase != DeliveryWriteStarted && f.deliveryPhase != DeliveryResponseReadStarted && f.deliveryPhase != DeliveryRequestFullySent) {
			return errors.New("terminal failure connection receipt is incompatible with its physical phase")
		}
	} else if f.connectionState == FailureConnectionInvalidated &&
		f.physicalCause != CauseClientInvariant {
		return errors.New("invalidated terminal failure lacks a terminal connection receipt")
	}
	expected, err := f.expectedEvidenceFingerprint()
	if err != nil || expected != f.evidenceFingerprint {
		return errors.New("terminal failure production fingerprint mismatch")
	}
	return nil
}

func (f FailureProductionFact) expectedEvidenceFingerprint() (string, error) {
	return protocolvalue.CanonicalClientFingerprint(
		"terminal-failure-production-fact:v1",
		map[string]any{
			"operation_kind":   f.operationKind,
			"operation_id":     f.operationID,
			"request_id":       f.requestID,
			"has_request_id":   f.hasRequestID,
			"delivery_phase":   f.deliveryPhase,
			"connection_state": f.connectionState,
			"physical_cause":   f.physicalCause,
			"connection_terminal_receipt_fingerprint": f.connectionTerminalReceiptFingerprint,
			"has_connection_terminal_receipt":         f.hasConnectionTerminalReceipt,
			"physical_receipt_fingerprint":            f.physicalReceiptFingerprint,
		},
	)
}

func classifyFailureCode(f FailureProductionFact) (PublicFailureCode, error) {
	if f.physicalCause == CauseClientInvariant {
		return FailureClientInvariant, nil
	}
	if f.physicalCause == CauseCallerCancelled {
		return FailureCancelled, nil
	}
	if f.deliveryPhase == DeliveryResponseReadStarted && f.physicalCause == CauseDeadlineExpired {
		if f.operationKind == OpMutation {
			return FailureCommandOutcomeTimeout, nil
		}
		return FailureReadTimeout, nil
	}
	if f.operationKind == OpMutation {
		if f.deliveryPhase == DeliveryNotStarted {
			return FailureCommandPreDispatch, nil
		}
		return FailureCommandDeliveryUnknown, nil
	}
	switch f.operationKind {
	case OpConnect:
		if f.physicalCause == CausePeerRejected {
			return FailurePeerIdentity, nil
		}
		if f.physicalCause == CauseBootstrapRejected {
			return FailureBootstrap, nil
		}
		return FailureConnect, nil
	case OpTransportAuth:
		return FailureTransportAuthentication, nil
	case OpHello:
		switch f.physicalCause {
		case CauseProtocolVersionRejected:
			return FailureProtocolVersion, nil
		case CauseRequiredCapabilityMissing:
			return FailureRequiredCapability, nil
		default:
			return FailureProtocolSchema, nil
		}
	case OpAttach, OpAttachAck:
		return FailureAttach, nil
	case OpHeartbeat:
		if f.physicalCause == CauseEOF || f.physicalCause == CauseReadFailed || f.physicalCause == CauseWriteFailed {
			return FailureHeartbeat, nil
		}
		return FailureHeartbeat, nil
	case OpProjectionSnapshot:
		if f.physicalCause == CauseProjectionValidationFailed || f.deliveryPhase == DeliveryResponseFullyValidated {
			return FailureProjectionSnapshot, nil
		}
	case OpOperationalSnapshot:
		if f.physicalCause == CauseProjectionValidationFailed || f.deliveryPhase == DeliveryResponseFullyValidated {
			return FailureOperationalSnapshot, nil
		}
	case OpHistoryPage:
		if f.deliveryPhase == DeliveryResponseFullyValidated {
			return FailureHistoryPage, nil
		}
	case OpSecretReveal, OpSecretSubmit:
		if f.operationKind == OpSecretSubmit && f.deliveryPhase != DeliveryNotStarted {
			return FailureSecretSubmitDeliveryUnknown, nil
		}
		return FailureSecretTransport, nil
	case OpClipboard:
		return FailureClipboard, nil
	case OpOpenURL:
		return FailureOpenURL, nil
	case OpTeardown, OpParentRelaunch:
		if f.physicalCause == CauseDeadlineExpired {
			return FailureTeardownDeadline, nil
		}
		return FailureTeardown, nil
	}
	if f.physicalCause == CauseEOF || f.physicalCause == CauseReadFailed || f.physicalCause == CauseWriteFailed || f.physicalCause == CauseMalformedResponse {
		return FailureTransportIO, nil
	}
	return FailureClientInvariant, nil
}

func expectedFailureDisposition(code PublicFailureCode) FailureDisposition {
	switch code {
	case FailureConnect:
		return FailureRetryWithBackoff
	case FailurePeerIdentity, FailureBootstrap, FailureTransportAuthentication, FailureProtocolVersion, FailureProtocolSchema, FailureRequiredCapability, FailureAttach, FailureClientInvariant:
		return FailureFatal
	case FailureTransportIO, FailureHeartbeat, FailureReadTimeout:
		return FailureReconnect
	case FailureHistoryPage:
		return FailureRetryRead
	case FailureCommandPreDispatch:
		return FailureRetryWithBackoff
	case FailureCommandOutcomeTimeout, FailureCommandDeliveryUnknown:
		return FailureQueryCommand
	case FailureProjectionSnapshot:
		return FailureRebuildDurableSnapshot
	case FailureOperationalSnapshot:
		return FailureRebuildOperationalSnapshot
	case FailureSecretTransport:
		return FailureRevokeSecret
	case FailureSecretSubmitDeliveryUnknown:
		return FailureRevokeSecretAndQuery
	case FailureTeardown, FailureTeardownDeadline:
		return FailureContinueTeardown
	case FailureCancelled, FailureClipboard, FailureOpenURL:
		return FailureNoRetry
	default:
		return 0
	}
}

func (f PublicFailure) Code() PublicFailureCode                         { return f.code }
func (f PublicFailure) Message() string                                 { return f.message }
func (f PublicFailure) Disposition() FailureDisposition                 { return f.disposition }
func (f PublicFailure) Production() FailureProductionFact               { return f.production }
func (f PublicFailure) EvidenceFingerprint() string                     { return f.production.evidenceFingerprint }
func (f FailureProductionFact) OperationKind() OperationKind            { return f.operationKind }
func (f FailureProductionFact) OperationID() string                     { return f.operationID }
func (f FailureProductionFact) RequestID() (string, bool)               { return f.requestID, f.hasRequestID }
func (f FailureProductionFact) DeliveryPhase() FailureDeliveryPhase     { return f.deliveryPhase }
func (f FailureProductionFact) ConnectionState() FailureConnectionState { return f.connectionState }
func (f FailureProductionFact) PhysicalCause() PhysicalFailureCause     { return f.physicalCause }
func (f FailureProductionFact) ConnectionTerminalReceiptFingerprint() (string, bool) {
	return f.connectionTerminalReceiptFingerprint, f.hasConnectionTerminalReceipt
}
func (f FailureProductionFact) PhysicalReceiptFingerprint() string {
	return f.physicalReceiptFingerprint
}
func (f FailureProductionFact) EvidenceFingerprint() string { return f.evidenceFingerprint }

type AppState struct {
	phase               AppPhase
	appGeneration       uint64
	connection          ConnectionState
	attachment          AttachmentState
	snapshotLoading     SnapshotLoadingState
	durable             presentation.State
	operational         presentation.OperationalState
	control             presentation.ControlProjectionState
	pageCache           presentation.PageCache
	observation         ObservationLoopState
	transcript          transcript.Model
	composer            ComposerState
	commands            commandstate.Registry
	interaction         interaction.State
	queue               queue.State
	secret              secret.State
	layout              LayoutPlan
	mouseMode           ClientMouseMode
	localNotifications  LocalNotificationState
	clipboard           ClipboardOperationState
	teardown            TeardownState
	publicFailure       PublicFailure
	hasPublicFailure    bool
	parentRelaunch      bool
	parentRelaunchCause ParentRelaunchCause
	candidateTerminal   protocolvalue.HandshakeCandidateTerminalReceipt
	frameworkAdvisories uint64
	lastLocalSequence   uint64
}

func NewInitialAppState(clientInstanceID string) AppState {
	commands, commandErr := commandstate.NewDormantRegistry(commandstate.S1MaximumRecords)
	queueState, queueErr := queue.NewDormantState(queue.S1MaximumActiveItems)
	layout, layoutErr := NewLayoutPlan(80, 24)
	if commandErr != nil || queueErr != nil || layoutErr != nil {
		panic(fmt.Sprintf("invalid compiled S1 bounds: command=%v queue=%v layout=%v", commandErr, queueErr, layoutErr))
	}
	return AppState{
		phase: PhaseBooting, appGeneration: 1,
		connection:      ConnectionState{Phase: ConnectionDisconnected, ClientInstanceID: clientInstanceID, Generation: 1, NextOperationGeneration: 1, AttachmentChallenge: NewNoAttachmentChallenge()},
		snapshotLoading: SnapshotLoadingState{Phase: SnapshotLoadingUninitialized},
		durable:         presentation.New(), operational: presentation.NewOperational(), control: presentation.NewControlProjection(),
		pageCache: presentation.NewPageCache(), observation: ObservationLoopState{ViewportIntentGeneration: 1},
		transcript: transcript.New(layout.Width, layout.TranscriptRows),
		commands:   commands, interaction: interaction.NewDormantState(), queue: queueState, secret: secret.NewDormantState(),
		layout: layout, mouseMode: MouseCellMotion, teardown: NewIdleTeardownState(),
	}
}

func (s AppState) Validate() error {
	if s.appGeneration == 0 || s.connection.Generation == 0 || s.connection.NextOperationGeneration == 0 {
		return errors.New("terminal application generation is invalid")
	}
	if err := s.layout.Validate(); err != nil {
		return err
	}
	if s.mouseMode < MouseDisabled || s.mouseMode > MouseAllMotion {
		return errors.New("terminal client mouse mode is invalid")
	}
	if len(s.localNotifications.Items) > maximumLocalNotifications {
		return errors.New("terminal local notification window exceeds its closed bound")
	}
	for _, notification := range s.localNotifications.Items {
		if notification == "" || len(notification) > 256 {
			return errors.New("terminal local notification is invalid")
		}
	}
	if err := s.clipboard.Validate(); err != nil {
		return err
	}
	if !s.connection.Outstanding.Valid() {
		return errors.New("terminal outstanding operation union is invalid")
	}
	if err := s.snapshotLoading.Validate(s.phase); err != nil {
		return err
	}
	if err := s.connection.AttachmentChallenge.Validate(); err != nil {
		return err
	}
	if s.connection.HasReconnectCredentialHandle != (s.connection.ReconnectCredentialHandleID != "" && s.connection.ReconnectCredentialCarrierFingerprint != "") {
		return errors.New("terminal reconnect credential handle matrix is invalid")
	}
	if err := s.durable.Validate(); err != nil {
		return err
	}
	if err := s.transcript.Validate(); err != nil {
		return err
	}
	if s.transcript.Width() != s.layout.Width || s.transcript.Height() != s.layout.TranscriptRows {
		return errors.New("terminal viewport geometry diverges from the validated layout")
	}
	if s.durable.Installed() != s.transcript.Ready() {
		return errors.New("terminal durable snapshot and viewport readiness diverge")
	}
	if err := s.operational.Validate(); err != nil {
		return err
	}
	if err := s.control.Validate(); err != nil {
		return err
	}
	if err := s.pageCache.Validate(); err != nil {
		return err
	}
	if s.durable.Ready() {
		if err := s.pageCache.ValidateAgainstDurable(s.durable.Durable()); err != nil {
			return err
		}
		if s.transcript.SnapshotFingerprint() != s.pageCache.CurrentMaterializationFingerprint() {
			return errors.New("terminal viewport materialization diverges from the current pinned root")
		}
	} else if s.durable.Installed() && s.pageCache.Ready() {
		if err := s.pageCache.ValidateAgainstDurable(s.durable.Durable()); err != nil {
			return err
		}
		if s.transcript.SnapshotFingerprint() != s.pageCache.CurrentMaterializationFingerprint() {
			return errors.New("terminal stale viewport materialization diverges from the current pinned root")
		}
	} else if s.pageCache.Ready() {
		return errors.New("terminal page cache owns roots before durable installation")
	}
	if err := s.observation.Validate(); err != nil {
		return err
	}
	if err := s.commands.Validate(); err != nil {
		return err
	}
	if err := s.interaction.Validate(); err != nil {
		return err
	}
	if err := s.queue.Validate(); err != nil {
		return err
	}
	if err := s.secret.Validate(); err != nil {
		return err
	}
	if err := s.teardown.Validate(); err != nil {
		return err
	}
	heartbeatOwned := s.connection.Phase == ConnectionAttached || s.connection.HeartbeatSchedule.NextGeneration > 0
	if err := s.connection.HeartbeatSchedule.Validate(heartbeatOwned); err != nil {
		return err
	}
	if s.phase == PhaseReady {
		if !s.attachment.Valid || !s.durable.Ready() || !s.transcript.Ready() || !s.operational.Ready() || !s.control.Ready() || s.snapshotLoading.Phase != SnapshotBaselinesInstalled {
			return errors.New("ready terminal state lacks required baselines")
		}
	} else if s.phase == PhaseReadOnly {
		// ReadOnly deliberately preserves the last confirmed screen while a
		// control/durable/operational rebuild is in flight.  The loading union
		// owns that exact replacement operation, so requiring
		// BaselinesInstalled here would turn every legitimate GAP or control
		// invalidation into a client invariant failure before the snapshot can
		// complete.
		if !s.attachment.Valid || !s.durable.Installed() || !s.transcript.Ready() || !s.operational.Installed() || !s.control.Installed() ||
			(s.snapshotLoading.Phase != SnapshotBaselinesInstalled &&
				s.snapshotLoading.Phase != SnapshotAwaitingDurableSnapshot &&
				s.snapshotLoading.Phase != SnapshotAwaitingOperationalSnapshot) {
			return errors.New("read-only terminal state lacks preserved baselines")
		}
	}
	if s.hasPublicFailure {
		return s.publicFailure.Validate()
	}
	return nil
}

func (s AppState) nextLocal(kind OperationKind, deadline time.Time) (AppState, LocalOperationToken) {
	token := NewLocalOperationToken(kind, s.connection.ClientInstanceID, s.connection.NextOperationGeneration, s.appGeneration, deadline)
	s.connection.NextOperationGeneration++
	s.connection.Outstanding = NewOutstandingLocal(token)
	return s, token
}

func (s AppState) nextDetachedLocal(kind OperationKind, deadline time.Time) (AppState, LocalOperationToken) {
	token := NewLocalOperationToken(kind, s.connection.ClientInstanceID, s.connection.NextOperationGeneration, s.appGeneration, deadline)
	s.connection.NextOperationGeneration++
	return s, token
}

func (s AppState) nextWire(kind OperationKind, deadline time.Time) (AppState, OperationToken) {
	attachment := s.attachment
	if kind == OpTransportAuth || kind == OpHello || kind == OpAttach {
		attachment = AttachmentState{}
	}
	token := NewOperationToken(kind, s.connection.ClientInstanceID, s.connection.NextOperationGeneration, s.appGeneration, s.connection.Generation, attachment, deadline)
	s.connection.NextOperationGeneration++
	s.connection.Outstanding = NewOutstandingWire(token)
	return s, token
}

func (s AppState) clearOutstanding() AppState {
	s.connection.Outstanding = OutstandingOperation{}
	return s
}
func (s AppState) Phase() AppPhase                  { return s.phase }
func (s AppState) Presentation() presentation.State { return s.durable }
func (s AppState) Failure() string {
	if s.hasPublicFailure {
		return s.publicFailure.message
	}
	return ""
}
func (s AppState) LastHeartbeatGeneration() uint64 {
	return s.connection.HeartbeatSchedule.LastAcceptedGeneration
}
func (s AppState) ParentRelaunchRequested() (ParentRelaunchCause, bool) {
	return s.parentRelaunchCause, s.parentRelaunch
}
