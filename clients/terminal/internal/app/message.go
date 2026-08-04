package app

import (
	"errors"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

type MessageDisposition uint8

const (
	MessageApply MessageDisposition = iota + 1
	MessageCompleteStaleOperation
	MessageDropStaleAuthority
	MessageTriggerGap
	MessageFatalCompatibility
)

type IOMessageHeader struct {
	Operation          OperationToken
	PayloadFingerprint string
	ReceivedAt         time.Time
}
type LocalMessageHeader struct {
	AppGeneration, Sequence uint64
	ProducedAt              time.Time
}

func NewLocalMessageHeader(appGeneration, sequence uint64, producedAt time.Time) (LocalMessageHeader, error) {
	header := LocalMessageHeader{AppGeneration: appGeneration, Sequence: sequence, ProducedAt: producedAt}
	if !header.Valid() {
		return LocalMessageHeader{}, errors.New("terminal local message header is invalid")
	}
	return header, nil
}

func (h LocalMessageHeader) Valid() bool {
	return h.AppGeneration > 0 && h.Sequence > 0 && !h.ProducedAt.IsZero()
}

type LocalResultHeader struct {
	Operation          LocalOperationToken
	PayloadFingerprint string
	ReceivedAt         time.Time
}

const (
	peerCredentialContractID      = "pulsara-terminal-local-peer-credential"
	peerCredentialContractVersion = "1"
)

// ValidatedPeerIdentity can only be created after the client connection owner
// has joined kernel peer credentials to the validated runtime socket path.
// Private fields prevent another package from manufacturing a successful
// connect result with caller-supplied UID data.
type ValidatedPeerIdentity struct {
	effectiveUID              uint64
	peerUID                   uint64
	peerPID                   uint64
	hasPeerPID                bool
	socketOwnerUID            uint64
	runtimePathFingerprint    string
	credentialContractID      string
	credentialContractVersion string
	validationFingerprint     string
}

func NewValidatedPeerIdentity(
	effectiveUID uint64,
	peerUID uint64,
	peerPID uint64,
	hasPeerPID bool,
	socketOwnerUID uint64,
	runtimePathFingerprint string,
) (ValidatedPeerIdentity, error) {
	value := ValidatedPeerIdentity{
		effectiveUID:              effectiveUID,
		peerUID:                   peerUID,
		peerPID:                   peerPID,
		hasPeerPID:                hasPeerPID,
		socketOwnerUID:            socketOwnerUID,
		runtimePathFingerprint:    runtimePathFingerprint,
		credentialContractID:      peerCredentialContractID,
		credentialContractVersion: peerCredentialContractVersion,
	}
	fingerprint, err := value.expectedFingerprint()
	if err != nil {
		return ValidatedPeerIdentity{}, err
	}
	value.validationFingerprint = fingerprint
	if err := value.Validate(); err != nil {
		return ValidatedPeerIdentity{}, err
	}
	return value, nil
}

func (v ValidatedPeerIdentity) Validate() error {
	if v.effectiveUID != v.peerUID || v.peerUID != v.socketOwnerUID ||
		v.runtimePathFingerprint == "" ||
		v.credentialContractID != peerCredentialContractID ||
		v.credentialContractVersion != peerCredentialContractVersion ||
		v.validationFingerprint == "" ||
		(!v.hasPeerPID && v.peerPID != 0) || (v.hasPeerPID && v.peerPID == 0) {
		return errors.New("terminal peer identity matrix is invalid")
	}
	expected, err := v.expectedFingerprint()
	if err != nil || expected != v.validationFingerprint {
		return errors.New("terminal peer identity fingerprint mismatch")
	}
	return nil
}

func (v ValidatedPeerIdentity) expectedFingerprint() (string, error) {
	return protocolvalue.CanonicalClientFingerprint(
		"terminal-validated-peer-identity:v1",
		map[string]any{
			"effective_uid":               v.effectiveUID,
			"peer_uid":                    v.peerUID,
			"peer_pid":                    v.peerPID,
			"has_peer_pid":                v.hasPeerPID,
			"socket_owner_uid":            v.socketOwnerUID,
			"runtime_path_fingerprint":    v.runtimePathFingerprint,
			"credential_contract_id":      v.credentialContractID,
			"credential_contract_version": v.credentialContractVersion,
		},
	)
}

func (v ValidatedPeerIdentity) ValidationFingerprint() string {
	return v.validationFingerprint
}

func ConnectResultFingerprint(
	operation LocalOperationToken,
	connectionHandleID string,
	peer ValidatedPeerIdentity,
) (string, error) {
	if operation.Kind != OpConnect || !operation.Valid() || connectionHandleID == "" ||
		peer.Validate() != nil {
		return "", errors.New("terminal connect result proof is invalid")
	}
	return protocolvalue.CanonicalClientFingerprint(
		"terminal-connect-result:v1",
		map[string]any{
			"operation_id":              operation.OperationID,
			"operation_generation":      operation.OperationGeneration,
			"connection_handle_id":      connectionHandleID,
			"peer_identity_fingerprint": peer.ValidationFingerprint(),
		},
	)
}

type ConnectSucceededMsg struct {
	Header             LocalResultHeader
	ConnectionHandleID string
	Peer               ValidatedPeerIdentity
}
type ConnectFailedMsg struct {
	Header  LocalResultHeader
	Failure PublicFailure
}
type TransportAuthenticatedMsg struct {
	Header             IOMessageHeader
	ConnectionHandleID string
	Candidate          protocolvalue.HandshakeCandidate
	Proof              protocolvalue.TransportAuthResult
}
type TransportAuthenticationFailedMsg struct {
	Header  IOMessageHeader
	Failure PublicFailure
}
type AttachRecoveredMsg struct {
	Header             IOMessageHeader
	ConnectionHandleID string
	Candidate          protocolvalue.HandshakeCandidate
	Proof              protocolvalue.TransportAuthResult
	Recovery           protocolvalue.RecoveredAttachAcknowledgement
	Attachment         protocolvalue.Attachment
}
type HelloAcceptedMsg struct {
	Header            IOMessageHeader
	Winner            protocolvalue.HelloNegotiationWinner
	Receipt           protocolvalue.ValidatedServerHelloReceipt
	PreparedChallenge PreparedAttachmentChallengeHandleIdentity
}
type HelloTransportFailedMsg struct {
	Header  IOMessageHeader
	Failure PublicFailure
}
type HelloNegativeMsg struct {
	Header  IOMessageHeader
	Outcome protocolvalue.HelloNegativeOutcome
}
type AttachmentChallengePromotedMsg struct {
	Header  LocalResultHeader
	Receipt AttachmentChallengePromotionReceipt
}
type AttachmentChallengePromotionAcceptedMsg struct {
	Header  LocalResultHeader
	Receipt AttachmentChallengeAcceptanceReceipt
}
type AttachmentChallengePromotionFailedMsg struct {
	Header  LocalResultHeader
	Failure PublicFailure
}
type AttachmentChallengeRevokedMsg struct {
	Header  LocalMessageHeader
	Receipt AttachmentChallengeRevocationReceipt
}
type AttachAcceptedMsg struct {
	Header     IOMessageHeader
	Attachment protocolvalue.Attachment
	Receipt    protocolvalue.AttachReceipt
}
type AttachRejectedMsg struct {
	Header  IOMessageHeader
	Failure PublicFailure
}
type AttachAcknowledgedMsg struct {
	Header IOMessageHeader
	Result protocolvalue.ValidatedAttachAckResult
}
type AttachAckFailedMsg struct {
	Header  IOMessageHeader
	Failure PublicFailure
}
type HeartbeatAcceptedMsg struct {
	Header  IOMessageHeader
	Request protocolvalue.PreparedHeartbeatRequest
	Receipt protocolvalue.ValidatedHeartbeatAcceptedReceipt
}
type HeartbeatRejectedMsg struct {
	Header  IOMessageHeader
	Request protocolvalue.PreparedHeartbeatRequest
	Receipt protocolvalue.ValidatedHeartbeatRejectedReceipt
}
type HeartbeatTransportFailedMsg struct {
	Header  IOMessageHeader
	Failure PublicFailure
}
type SnapshotAcceptedMsg struct {
	Header   IOMessageHeader
	Snapshot protocolvalue.DurableSnapshot
}
type SnapshotRejectedMsg struct {
	Header  IOMessageHeader
	Failure PublicFailure
}
type OperationalSnapshotAcceptedMsg struct {
	Header   IOMessageHeader
	Snapshot protocolvalue.OperationalSnapshot
}
type OperationalSnapshotRejectedMsg struct {
	Header  IOMessageHeader
	Failure PublicFailure
}

type AppStartedMsg struct {
	Header                      LocalMessageHeader
	BootstrapHandleID           string
	TransportCredentialHandleID string
	HandshakeCandidate          protocolvalue.HandshakeCandidate
}
type KeyInputMsg struct {
	Header LocalMessageHeader
	Key    NormalizedKey
}
type PasteInputMsg struct {
	Header    LocalMessageHeader
	ChunkUTF8 string
	ByteCount uint32
}
type PasteBoundaryMsg struct {
	Header   LocalMessageHeader
	Boundary PasteBoundary
}
type ResizeMsg struct {
	Header        LocalMessageHeader
	Width, Height int
}
type FocusChangedMsg struct {
	Header  LocalMessageHeader
	Focused bool
}
type KeyboardEnhancementsObservedMsg struct {
	Header LocalMessageHeader
	Flags  int
}
type MouseWheelInputMsg struct {
	Header     LocalMessageHeader
	Direction  MouseWheelDirection
	VisualRows uint8
}
type FrameworkInputRejectedMsg struct{ Header LocalMessageHeader }

type FrameworkAdvisoryKind uint8

const (
	FrameworkAdvisoryEnvironment FrameworkAdvisoryKind = iota + 1
	FrameworkAdvisoryColorProfile
	FrameworkAdvisoryKeyRelease
	FrameworkAdvisoryCursorPosition
	FrameworkAdvisoryTerminalVersion
	FrameworkAdvisoryCapability
	FrameworkAdvisoryColorReport
	FrameworkAdvisoryModeReport
	FrameworkAdvisoryClipboard
	FrameworkAdvisoryMousePointer
)

type FrameworkAdvisoryIgnoredMsg struct {
	Header LocalMessageHeader
	Kind   FrameworkAdvisoryKind
}

type TickKind uint8

const (
	TickHeartbeat TickKind = iota + 1
	TickReconnect
	TickCursorBlink
	TickNotificationExpiry
	TickTeardownDeadline
)

type TickMsg struct {
	Header         LocalMessageHeader
	Kind           TickKind
	TickGeneration uint64
}

type ParentShutdownReason uint8

const (
	ParentRequestedShutdown ParentShutdownReason = iota + 1
	ParentPipeClosed
	ParentProcessExited
	ParentProtocolRevoked
)

type ParentShutdownMsg struct {
	Header LocalMessageHeader
	Reason ParentShutdownReason
}
type ReconnectDueMsg struct {
	Header              LocalMessageHeader
	ReconnectGeneration uint64
}
type TeardownCompletedMsg struct {
	Header  LocalResultHeader
	Summary PublicTeardownSummary
}
type TeardownFailedMsg struct {
	Header  LocalResultHeader
	Failure PublicFailure
}
type ParentRelaunchPreparedMsg struct {
	Header                     LocalResultHeader
	CandidateTerminalReceipt   protocolvalue.HandshakeCandidateTerminalReceipt
	Cause                      ParentRelaunchCause
	NegativeOutcomeFingerprint string
}
type ParentRelaunchFailedMsg struct {
	Header  LocalResultHeader
	Failure PublicFailure
}
type PublicTextCopiedMsg struct {
	Header LocalResultHeader
}
type PublicTextCopyFailedMsg struct {
	Header  LocalResultHeader
	Failure PublicFailure
}

type applicationMessage interface{ applicationMessage() }

func (ConnectSucceededMsg) applicationMessage()                     {}
func (ConnectFailedMsg) applicationMessage()                        {}
func (TransportAuthenticatedMsg) applicationMessage()               {}
func (TransportAuthenticationFailedMsg) applicationMessage()        {}
func (AttachRecoveredMsg) applicationMessage()                      {}
func (HelloAcceptedMsg) applicationMessage()                        {}
func (HelloTransportFailedMsg) applicationMessage()                 {}
func (HelloNegativeMsg) applicationMessage()                        {}
func (AttachmentChallengePromotedMsg) applicationMessage()          {}
func (AttachmentChallengePromotionAcceptedMsg) applicationMessage() {}
func (AttachmentChallengePromotionFailedMsg) applicationMessage()   {}
func (AttachmentChallengeRevokedMsg) applicationMessage()           {}
func (AttachAcceptedMsg) applicationMessage()                       {}
func (AttachRejectedMsg) applicationMessage()                       {}
func (AttachAcknowledgedMsg) applicationMessage()                   {}
func (AttachAckFailedMsg) applicationMessage()                      {}
func (HeartbeatAcceptedMsg) applicationMessage()                    {}
func (HeartbeatRejectedMsg) applicationMessage()                    {}
func (HeartbeatTransportFailedMsg) applicationMessage()             {}
func (SnapshotAcceptedMsg) applicationMessage()                     {}
func (SnapshotRejectedMsg) applicationMessage()                     {}
func (OperationalSnapshotAcceptedMsg) applicationMessage()          {}
func (OperationalSnapshotRejectedMsg) applicationMessage()          {}
func (AppStartedMsg) applicationMessage()                           {}
func (KeyInputMsg) applicationMessage()                             {}
func (PasteInputMsg) applicationMessage()                           {}
func (PasteBoundaryMsg) applicationMessage()                        {}
func (ResizeMsg) applicationMessage()                               {}
func (FocusChangedMsg) applicationMessage()                         {}
func (KeyboardEnhancementsObservedMsg) applicationMessage()         {}
func (MouseWheelInputMsg) applicationMessage()                      {}
func (FrameworkInputRejectedMsg) applicationMessage()               {}
func (FrameworkAdvisoryIgnoredMsg) applicationMessage()             {}
func (TickMsg) applicationMessage()                                 {}
func (ParentShutdownMsg) applicationMessage()                       {}
func (ReconnectDueMsg) applicationMessage()                         {}
func (TeardownCompletedMsg) applicationMessage()                    {}
func (TeardownFailedMsg) applicationMessage()                       {}
func (ParentRelaunchPreparedMsg) applicationMessage()               {}
func (ParentRelaunchFailedMsg) applicationMessage()                 {}
func (PublicTextCopiedMsg) applicationMessage()                     {}
func (PublicTextCopyFailedMsg) applicationMessage()                 {}

func NewIOHeader(operation OperationToken, fingerprint string, receivedAt time.Time) (IOMessageHeader, error) {
	if !operation.Valid() || fingerprint == "" || receivedAt.IsZero() {
		return IOMessageHeader{}, errors.New("terminal I/O message header is invalid")
	}
	return IOMessageHeader{Operation: operation, PayloadFingerprint: fingerprint, ReceivedAt: receivedAt}, nil
}
func NewLocalResultHeader(operation LocalOperationToken, fingerprint string, receivedAt time.Time) (LocalResultHeader, error) {
	if !operation.Valid() || fingerprint == "" || receivedAt.IsZero() {
		return LocalResultHeader{}, errors.New("terminal local result header is invalid")
	}
	return LocalResultHeader{Operation: operation, PayloadFingerprint: fingerprint, ReceivedAt: receivedAt}, nil
}

func (m ConnectSucceededMsg) validate() error {
	expected, err := ConnectResultFingerprint(m.Header.Operation, m.ConnectionHandleID, m.Peer)
	if err != nil || expected != m.Header.PayloadFingerprint {
		return errors.New("terminal connect result is invalid")
	}
	return nil
}
func (m TransportAuthenticatedMsg) validate() error {
	if m.Header.Operation.Kind != OpTransportAuth || m.ConnectionHandleID == "" || m.Candidate.ID == "" || m.Candidate.Fingerprint == "" || m.Proof.CandidateFingerprint != m.Candidate.Fingerprint || m.Proof.ResultFingerprint != m.Header.PayloadFingerprint || m.Proof.RequestID != m.Header.Operation.RequestID {
		return errors.New("terminal auth result is invalid")
	}
	return nil
}
func (m AttachRecoveredMsg) validate() error {
	if m.Header.Operation.Kind != OpTransportAuth || m.ConnectionHandleID == "" || m.Proof.Disposition != protocolvalue.TransportAuthAckResultRecovery || m.Proof.ResultFingerprint != m.Header.PayloadFingerprint || m.Proof.RequestID != m.Header.Operation.RequestID || m.Candidate.Fingerprint != m.Proof.CandidateFingerprint || m.Recovery.Ack.AttachmentID != m.Attachment.ID || m.Recovery.Ack.AttachmentGeneration != m.Attachment.Generation || m.Recovery.Ack.SemanticWinnerFingerprint != m.Attachment.SemanticWinnerFingerprint || m.Recovery.Binding.ResultingBinding.Fingerprint != m.Attachment.BindingFingerprint || m.Recovery.Binding.ResultingBinding.ConnectionID != m.Attachment.ConnectionID {
		return errors.New("terminal recovered attachment result is invalid")
	}
	return nil
}
func (m HelloAcceptedMsg) validate() error {
	if m.Header.Operation.Kind != OpHello || m.Winner.NegotiationWinnerFingerprint == "" || m.Receipt.ReceiptFingerprint != m.Header.PayloadFingerprint || m.Receipt.RequestID != m.Header.Operation.RequestID || m.Winner.NegotiationWinnerFingerprint != m.Receipt.NegotiationWinnerFingerprint || m.PreparedChallenge.Validate() != nil || m.PreparedChallenge.HelloOperationID != m.Header.Operation.OperationID || m.PreparedChallenge.HelloOperationGeneration != m.Header.Operation.OperationGeneration || m.PreparedChallenge.ValidatedReceiptFingerprint != m.Receipt.ReceiptFingerprint || m.PreparedChallenge.HandleID != m.Receipt.PreparedChallengeHandleID || m.PreparedChallenge.CandidateFingerprint != m.Winner.CandidateFingerprint || m.PreparedChallenge.ConnectionID != m.Receipt.CurrentConnectionID || m.PreparedChallenge.ChallengeCommitment != m.Receipt.ChallengeCommitment {
		return errors.New("terminal Hello result is invalid")
	}
	return nil
}
func (m AttachAcceptedMsg) validate() error {
	if m.Header.Operation.Kind != OpAttach || m.Receipt.ReceiptFingerprint != m.Header.PayloadFingerprint || m.Receipt.RequestID != m.Header.Operation.RequestID || m.Attachment.SemanticWinnerFingerprint != m.Receipt.SemanticWinnerFingerprint || m.Attachment.BindingFingerprint != m.Receipt.CurrentBinding.Fingerprint {
		return errors.New("terminal attach result is invalid")
	}
	return nil
}
func (m AttachAcknowledgedMsg) validate() error {
	if m.Header.Operation.Kind != OpAttachAck || m.Result.ResultFingerprint != m.Header.PayloadFingerprint || m.Result.RequestID != m.Header.Operation.RequestID {
		return errors.New("terminal attach ACK result is invalid")
	}
	return nil
}
func (m SnapshotAcceptedMsg) validate() error {
	if m.Header.Operation.Kind != OpProjectionSnapshot || m.Snapshot.RequestID != m.Header.Operation.RequestID || m.Snapshot.SnapshotFingerprint != m.Header.PayloadFingerprint {
		return errors.New("terminal durable snapshot result is invalid")
	}
	return nil
}
func (m OperationalSnapshotAcceptedMsg) validate() error {
	if m.Header.Operation.Kind != OpOperationalSnapshot || m.Snapshot.RequestID != m.Header.Operation.RequestID || m.Snapshot.FrameFingerprint != m.Header.PayloadFingerprint {
		return errors.New("terminal operational snapshot result is invalid")
	}
	return nil
}
func (m HeartbeatAcceptedMsg) validate() error {
	if m.Header.Operation.Kind != OpHeartbeat || m.Request.RequestID != m.Header.Operation.RequestID || m.Receipt.RequestID != m.Header.Operation.RequestID || m.Receipt.ReceiptFingerprint != m.Header.PayloadFingerprint || m.Request.CandidateFingerprint != m.Receipt.CandidateFingerprint {
		return errors.New("terminal heartbeat accepted result is invalid")
	}
	return nil
}
func (m HeartbeatRejectedMsg) validate() error {
	if m.Header.Operation.Kind != OpHeartbeat || m.Request.RequestID != m.Header.Operation.RequestID || m.Receipt.RequestID != m.Header.Operation.RequestID || m.Receipt.ReceiptFingerprint != m.Header.PayloadFingerprint || m.Request.CandidateFingerprint != m.Receipt.CandidateFingerprint {
		return errors.New("terminal heartbeat rejected result is invalid")
	}
	return nil
}

func messageOutstanding(message any) (OutstandingOperation, bool) {
	switch value := message.(type) {
	case ConnectSucceededMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case ConnectFailedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case AttachmentChallengePromotedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case AttachmentChallengePromotionAcceptedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case AttachmentChallengePromotionFailedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case TransportAuthenticatedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case TransportAuthenticationFailedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case AttachRecoveredMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case HelloAcceptedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case HelloTransportFailedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case HelloNegativeMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case AttachAcceptedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case AttachRejectedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case AttachAcknowledgedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case AttachAckFailedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case HeartbeatAcceptedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case HeartbeatRejectedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case HeartbeatTransportFailedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case SnapshotAcceptedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case SnapshotRejectedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case OperationalSnapshotAcceptedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case OperationalSnapshotRejectedMsg:
		return NewOutstandingWire(value.Header.Operation), true
	case TeardownCompletedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case TeardownFailedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case ParentRelaunchPreparedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case ParentRelaunchFailedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case PublicTextCopiedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	case PublicTextCopyFailedMsg:
		return NewOutstandingLocal(value.Header.Operation), true
	default:
		return OutstandingOperation{}, false
	}
}

func messageObservedAt(message any) time.Time {
	switch value := message.(type) {
	case ConnectSucceededMsg:
		return value.Header.ReceivedAt
	case ConnectFailedMsg:
		return value.Header.ReceivedAt
	case TransportAuthenticatedMsg:
		return value.Header.ReceivedAt
	case TransportAuthenticationFailedMsg:
		return value.Header.ReceivedAt
	case AttachRecoveredMsg:
		return value.Header.ReceivedAt
	case HelloAcceptedMsg:
		return value.Header.ReceivedAt
	case HelloTransportFailedMsg:
		return value.Header.ReceivedAt
	case HelloNegativeMsg:
		return value.Header.ReceivedAt
	case AttachmentChallengePromotedMsg:
		return value.Header.ReceivedAt
	case AttachmentChallengePromotionAcceptedMsg:
		return value.Header.ReceivedAt
	case AttachmentChallengePromotionFailedMsg:
		return value.Header.ReceivedAt
	case AttachmentChallengeRevokedMsg:
		return value.Header.ProducedAt
	case AttachAcceptedMsg:
		return value.Header.ReceivedAt
	case AttachRejectedMsg:
		return value.Header.ReceivedAt
	case AttachAcknowledgedMsg:
		return value.Header.ReceivedAt
	case AttachAckFailedMsg:
		return value.Header.ReceivedAt
	case HeartbeatAcceptedMsg:
		return value.Header.ReceivedAt
	case HeartbeatRejectedMsg:
		return value.Header.ReceivedAt
	case HeartbeatTransportFailedMsg:
		return value.Header.ReceivedAt
	case SnapshotAcceptedMsg:
		return value.Header.ReceivedAt
	case SnapshotRejectedMsg:
		return value.Header.ReceivedAt
	case OperationalSnapshotAcceptedMsg:
		return value.Header.ReceivedAt
	case OperationalSnapshotRejectedMsg:
		return value.Header.ReceivedAt
	case TeardownCompletedMsg:
		return value.Header.ReceivedAt
	case TeardownFailedMsg:
		return value.Header.ReceivedAt
	case ParentRelaunchPreparedMsg:
		return value.Header.ReceivedAt
	case ParentRelaunchFailedMsg:
		return value.Header.ReceivedAt
	case PublicTextCopiedMsg:
		return value.Header.ReceivedAt
	case PublicTextCopyFailedMsg:
		return value.Header.ReceivedAt
	case KeyInputMsg:
		return value.Header.ProducedAt
	case AppStartedMsg:
		return value.Header.ProducedAt
	case PasteInputMsg:
		return value.Header.ProducedAt
	case PasteBoundaryMsg:
		return value.Header.ProducedAt
	case ResizeMsg:
		return value.Header.ProducedAt
	case FocusChangedMsg:
		return value.Header.ProducedAt
	case KeyboardEnhancementsObservedMsg:
		return value.Header.ProducedAt
	case MouseWheelInputMsg:
		return value.Header.ProducedAt
	case FrameworkInputRejectedMsg:
		return value.Header.ProducedAt
	case FrameworkAdvisoryIgnoredMsg:
		return value.Header.ProducedAt
	case ParentShutdownMsg:
		return value.Header.ProducedAt
	case ReconnectDueMsg:
		return value.Header.ProducedAt
	case TickMsg:
		return value.Header.ProducedAt
	default:
		return time.Time{}
	}
}

func localMessageHeader(message any) (LocalMessageHeader, bool) {
	switch value := message.(type) {
	case KeyInputMsg:
		return value.Header, true
	case AppStartedMsg:
		return value.Header, true
	case PasteInputMsg:
		return value.Header, true
	case PasteBoundaryMsg:
		return value.Header, true
	case ResizeMsg:
		return value.Header, true
	case FocusChangedMsg:
		return value.Header, true
	case KeyboardEnhancementsObservedMsg:
		return value.Header, true
	case MouseWheelInputMsg:
		return value.Header, true
	case FrameworkInputRejectedMsg:
		return value.Header, true
	case FrameworkAdvisoryIgnoredMsg:
		return value.Header, true
	case TickMsg:
		return value.Header, true
	case AttachmentChallengeRevokedMsg:
		return value.Header, true
	case ParentShutdownMsg:
		return value.Header, true
	case ReconnectDueMsg:
		return value.Header, true
	default:
		return LocalMessageHeader{}, false
	}
}

func installLocalMessageHeader(message any, header LocalMessageHeader) (any, bool) {
	switch value := message.(type) {
	case KeyInputMsg:
		value.Header = header
		return value, true
	case AppStartedMsg:
		value.Header = header
		return value, true
	case PasteInputMsg:
		value.Header = header
		return value, true
	case PasteBoundaryMsg:
		value.Header = header
		return value, true
	case ResizeMsg:
		value.Header = header
		return value, true
	case FocusChangedMsg:
		value.Header = header
		return value, true
	case KeyboardEnhancementsObservedMsg:
		value.Header = header
		return value, true
	case MouseWheelInputMsg:
		value.Header = header
		return value, true
	case FrameworkInputRejectedMsg:
		value.Header = header
		return value, true
	case FrameworkAdvisoryIgnoredMsg:
		value.Header = header
		return value, true
	case TickMsg:
		value.Header = header
		return value, true
	case AttachmentChallengeRevokedMsg:
		value.Header = header
		return value, true
	case ParentShutdownMsg:
		value.Header = header
		return value, true
	case ReconnectDueMsg:
		value.Header = header
		return value, true
	default:
		return message, false
	}
}
