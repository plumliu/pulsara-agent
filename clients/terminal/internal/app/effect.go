package app

import (
	"time"

	tea "charm.land/bubbletea/v2"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

type OperationKind uint8

const (
	OpConnect OperationKind = iota + 1
	OpTransportAuth
	OpHello
	OpChallengePromote
	OpChallengePromotionConfirm
	OpChallengeRevokePrepared
	OpChallengeRevokeActive
	OpAttach
	OpAttachAck
	OpHeartbeat
	OpProjectionSnapshot
	OpOperationalSnapshot
	OpObserve
	OpHistoryPage
	OpMutation
	OpCommandQuery
	OpSecretReveal
	OpSecretEdit
	OpSecretSubmit
	OpClipboard
	OpOpenURL
	OpTick
	OpReconnect
	OpParentRelaunch
	OpTeardown
)

func (k OperationKind) wire() bool {
	switch k {
	case OpTransportAuth, OpHello, OpAttach, OpAttachAck, OpHeartbeat,
		OpProjectionSnapshot, OpOperationalSnapshot, OpObserve, OpHistoryPage,
		OpMutation, OpCommandQuery, OpSecretReveal, OpSecretSubmit:
		return true
	default:
		return false
	}
}

func (k OperationKind) local() bool {
	switch k {
	case OpConnect, OpChallengePromote, OpChallengePromotionConfirm,
		OpChallengeRevokePrepared, OpChallengeRevokeActive, OpSecretEdit,
		OpClipboard, OpOpenURL, OpTick, OpReconnect, OpParentRelaunch, OpTeardown:
		return true
	default:
		return false
	}
}

type OperationToken struct {
	Kind                        OperationKind
	OperationID                 string
	OperationGeneration         uint64
	RequestID                   string
	ConnectionGeneration        uint64
	AttachmentID                string
	AttachmentGeneration        uint64
	TransportBindingGeneration  uint64
	TransportBindingFingerprint string
	ControllerGeneration        uint64
	Deadline                    time.Time
}

func NewOperationToken(kind OperationKind, clientInstanceID string, generation, appGeneration, connectionGeneration uint64, attachment AttachmentState, deadline time.Time) OperationToken {
	operationID, requestID, err := protocolvalue.WireOperationIdentity(clientInstanceID, appGeneration, connectionGeneration, generation, uint8(kind))
	if err != nil {
		return OperationToken{}
	}
	return OperationToken{
		Kind:                        kind,
		OperationID:                 operationID,
		OperationGeneration:         generation,
		RequestID:                   requestID,
		ConnectionGeneration:        connectionGeneration,
		AttachmentID:                attachment.Identity.ID,
		AttachmentGeneration:        attachment.Identity.Generation,
		TransportBindingGeneration:  attachment.Identity.BindingGeneration,
		TransportBindingFingerprint: attachment.Identity.BindingFingerprint,
		ControllerGeneration:        attachment.Identity.ControllerGeneration,
		Deadline:                    deadline,
	}
}

func (t OperationToken) Valid() bool {
	return t.Kind.wire() && t.OperationID != "" && t.RequestID != "" && t.OperationGeneration > 0 && t.ConnectionGeneration > 0 && !t.Deadline.IsZero()
}

type LocalOperationToken struct {
	Kind                OperationKind
	OperationID         string
	OperationGeneration uint64
	AppGeneration       uint64
	Deadline            time.Time
}

func NewLocalOperationToken(kind OperationKind, clientInstanceID string, generation, appGeneration uint64, deadline time.Time) LocalOperationToken {
	operationID, err := protocolvalue.LocalOperationIdentity(clientInstanceID, appGeneration, generation, uint8(kind))
	if err != nil {
		return LocalOperationToken{}
	}
	return LocalOperationToken{Kind: kind, OperationID: operationID, OperationGeneration: generation, AppGeneration: appGeneration, Deadline: deadline}
}

func (t LocalOperationToken) Valid() bool {
	return t.Kind.local() && t.OperationID != "" && t.OperationGeneration > 0 && t.AppGeneration > 0 && !t.Deadline.IsZero()
}

type OutstandingOperationKind uint8

const (
	OutstandingNone OutstandingOperationKind = iota
	OutstandingWire
	OutstandingLocal
)

type OutstandingOperation struct {
	Carrier OutstandingOperationKind
	Wire    OperationToken
	Local   LocalOperationToken
}

func NewOutstandingWire(token OperationToken) OutstandingOperation {
	return OutstandingOperation{Carrier: OutstandingWire, Wire: token}
}
func NewOutstandingLocal(token LocalOperationToken) OutstandingOperation {
	return OutstandingOperation{Carrier: OutstandingLocal, Local: token}
}
func (o OutstandingOperation) Valid() bool {
	switch o.Carrier {
	case OutstandingNone:
		return !o.Wire.Valid() && !o.Local.Valid()
	case OutstandingWire:
		return o.Wire.Valid() && !o.Local.Valid()
	case OutstandingLocal:
		return o.Local.Valid() && !o.Wire.Valid()
	default:
		return false
	}
}

type Effect interface {
	Outstanding() OutstandingOperation
	effect()
}

type WireEffectHeader struct {
	EffectID  string
	Operation OperationToken
}

type LocalEffectHeader struct {
	EffectID  string
	Operation LocalOperationToken
}

func newWireHeader(token OperationToken) WireEffectHeader {
	return WireEffectHeader{EffectID: "terminal-effect:" + token.OperationID, Operation: token}
}
func newLocalHeader(token LocalOperationToken) LocalEffectHeader {
	return LocalEffectHeader{EffectID: "terminal-effect:" + token.OperationID, Operation: token}
}

type ConnectEffect struct {
	Header                      LocalEffectHeader
	BootstrapHandleID           string
	AttachmentAttemptGeneration uint64
}
type AuthenticateTransportEffect struct {
	Header             WireEffectHeader
	ConnectionHandleID string
	CredentialHandleID string
	Candidate          protocolvalue.HandshakeCandidate
}
type NegotiateHelloEffect struct {
	Header                         WireEffectHeader
	ConnectionHandleID             string
	TransportAuthAttemptID         string
	TransportAuthResultFingerprint string
	Candidate                      protocolvalue.HandshakeCandidate
}
type PromotePreparedAttachmentChallengeEffect struct {
	Header                          LocalEffectHeader
	Prepared                        PreparedAttachmentChallengeHandleIdentity
	ExpectedCandidateFingerprint    string
	ExpectedHelloReceiptFingerprint string
	ExpectedConnectionID            string
}
type ConfirmAttachmentChallengePromotionEffect struct {
	Header                          LocalEffectHeader
	Promotion                       AttachmentChallengePromotionReceipt
	ExpectedCandidateFingerprint    string
	ExpectedHelloReceiptFingerprint string
	ExpectedConnectionID            string
}
type RevokePreparedAttachmentChallengeEffect struct {
	Header            LocalEffectHeader
	HandleFingerprint string
	Reason            AttachmentChallengeRevocationReason
}
type RevokeActiveAttachmentChallengeEffect struct {
	Header               LocalEffectHeader
	HandleFingerprint    string
	PromotionFingerprint string
	Reason               AttachmentChallengeRevocationReason
}
type AttachEffect struct {
	Header                            WireEffectHeader
	ConnectionHandleID                string
	Candidate                         protocolvalue.HandshakeCandidate
	HelloNegotiationWinnerFingerprint string
	ServerHelloReceiptFingerprint     string
	ActiveAttachmentChallengeHandleID string
	AttachmentChallengeAcceptance     AttachmentChallengeAcceptanceReceipt
	AttachmentChallengeCommitment     string
}
type AcknowledgeAttachEffect struct {
	Header                         WireEffectHeader
	ConnectionHandleID             string
	SemanticWinnerFingerprint      string
	AttachResultReceiptFingerprint string
}
type HeartbeatEffect struct {
	Header             WireEffectHeader
	ConnectionHandleID string
	Request            protocolvalue.PreparedHeartbeatRequest
}
type RequestSnapshotEffect struct {
	Header             WireEffectHeader
	ConnectionHandleID string
	Request            protocolvalue.PreparedProjectionSnapshotRequest
}
type RequestOperationalSnapshotEffect struct {
	Header             WireEffectHeader
	ConnectionHandleID string
	Request            protocolvalue.PreparedOperationalSnapshotRequest
}
type ObserveNextEffect struct {
	Header             WireEffectHeader
	ConnectionHandleID string
	Request            protocolvalue.PreparedObserveRequest
}
type ReadHistoryPageEffect struct {
	Header             WireEffectHeader
	ConnectionHandleID string
	Request            protocolvalue.PreparedHistoryPageRequest
}
type ScheduleTickEffect struct {
	Header         LocalEffectHeader
	Kind           TickKind
	TickGeneration uint64
	DueAt          time.Time
}
type CopyPublicTextEffect struct {
	Header     LocalEffectHeader
	PublicUTF8 string
}
type BeginTeardownEffect struct {
	Header LocalEffectHeader
	Reason TeardownReason
}
type QuitProgramEffect struct {
	Header LocalEffectHeader
}

type ParentRelaunchCause uint8

const (
	ParentRelaunchNegotiationWinnerUnavailable ParentRelaunchCause = iota + 1
	ParentRelaunchHelloRejected
)

type RequestParentRelaunchEffect struct {
	Header                     LocalEffectHeader
	CandidateTerminalReceipt   protocolvalue.HandshakeCandidateTerminalReceipt
	Cause                      ParentRelaunchCause
	NegativeOutcomeFingerprint string
}

func wireOutstanding(header WireEffectHeader) OutstandingOperation {
	return NewOutstandingWire(header.Operation)
}
func localOutstanding(header LocalEffectHeader) OutstandingOperation {
	return NewOutstandingLocal(header.Operation)
}

func (e ConnectEffect) Outstanding() OutstandingOperation { return localOutstanding(e.Header) }
func (e AuthenticateTransportEffect) Outstanding() OutstandingOperation {
	return wireOutstanding(e.Header)
}
func (e NegotiateHelloEffect) Outstanding() OutstandingOperation { return wireOutstanding(e.Header) }
func (e PromotePreparedAttachmentChallengeEffect) Outstanding() OutstandingOperation {
	return localOutstanding(e.Header)
}
func (e ConfirmAttachmentChallengePromotionEffect) Outstanding() OutstandingOperation {
	return localOutstanding(e.Header)
}
func (e RevokePreparedAttachmentChallengeEffect) Outstanding() OutstandingOperation {
	return localOutstanding(e.Header)
}
func (e RevokeActiveAttachmentChallengeEffect) Outstanding() OutstandingOperation {
	return localOutstanding(e.Header)
}
func (e AttachEffect) Outstanding() OutstandingOperation            { return wireOutstanding(e.Header) }
func (e AcknowledgeAttachEffect) Outstanding() OutstandingOperation { return wireOutstanding(e.Header) }
func (e HeartbeatEffect) Outstanding() OutstandingOperation         { return wireOutstanding(e.Header) }
func (e RequestSnapshotEffect) Outstanding() OutstandingOperation   { return wireOutstanding(e.Header) }
func (e RequestOperationalSnapshotEffect) Outstanding() OutstandingOperation {
	return wireOutstanding(e.Header)
}
func (e ObserveNextEffect) Outstanding() OutstandingOperation     { return wireOutstanding(e.Header) }
func (e ReadHistoryPageEffect) Outstanding() OutstandingOperation { return wireOutstanding(e.Header) }
func (e ScheduleTickEffect) Outstanding() OutstandingOperation    { return localOutstanding(e.Header) }
func (e CopyPublicTextEffect) Outstanding() OutstandingOperation  { return localOutstanding(e.Header) }
func (e BeginTeardownEffect) Outstanding() OutstandingOperation   { return localOutstanding(e.Header) }
func (e QuitProgramEffect) Outstanding() OutstandingOperation     { return localOutstanding(e.Header) }
func (e RequestParentRelaunchEffect) Outstanding() OutstandingOperation {
	return localOutstanding(e.Header)
}

func (ConnectEffect) effect()                             {}
func (AuthenticateTransportEffect) effect()               {}
func (NegotiateHelloEffect) effect()                      {}
func (PromotePreparedAttachmentChallengeEffect) effect()  {}
func (ConfirmAttachmentChallengePromotionEffect) effect() {}
func (RevokePreparedAttachmentChallengeEffect) effect()   {}
func (RevokeActiveAttachmentChallengeEffect) effect()     {}
func (AttachEffect) effect()                              {}
func (AcknowledgeAttachEffect) effect()                   {}
func (HeartbeatEffect) effect()                           {}
func (RequestSnapshotEffect) effect()                     {}
func (RequestOperationalSnapshotEffect) effect()          {}
func (ObserveNextEffect) effect()                         {}
func (ReadHistoryPageEffect) effect()                     {}
func (ScheduleTickEffect) effect()                        {}
func (CopyPublicTextEffect) effect()                      {}
func (BeginTeardownEffect) effect()                       {}
func (QuitProgramEffect) effect()                         {}
func (RequestParentRelaunchEffect) effect()               {}

type Executor interface {
	Execute(Effect) tea.Cmd
	Close() error
}
