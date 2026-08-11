package kernelclient

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"google.golang.org/protobuf/proto"

	protocolv3 "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolv3"
)

const (
	ProtocolMajor     = 3
	ProtocolMinor     = 0
	SchemaFingerprint = "sha256:c8571a6124c4b02f6d4b10911fbd11aa46517f05b84408b6606fa8c85866dbbe"
	maximumFrameBytes = 8 << 20
)

// ProtocolError is the closed server disposition for one physical request.
// It does not imply that the serial connection is invalid.
type ProtocolError struct {
	StableCode    string
	PublicMessage string
}

func (e *ProtocolError) Error() string {
	return fmt.Sprintf("Protocol v3 %s: %s", e.StableCode, e.PublicMessage)
}

func (e *ProtocolError) StableProtocolCode() string { return e.StableCode }

func IsCanonicalContentUnavailable(err error) bool {
	var protocolError *ProtocolError
	if !errors.As(err, &protocolError) {
		return false
	}
	switch protocolError.StableCode {
	case "CONTENT_REFERENCE_MISSING", "CONTENT_REFERENCE_CORRUPT", "CONTENT_BLOB_MISSING", "CONTENT_BLOB_CORRUPT":
		return true
	default:
		return false
	}
}

type Bootstrap struct {
	LaunchID         string
	LaunchCapability []byte
	ClientInstanceID string
	HostSessionID    string
	SessionID        string
	SocketPath       string
	RequestedRole    protocolv3.AttachmentRole
	ParentPID        uint64
}

type Service struct {
	bootstrap Bootstrap
	mu        sync.Mutex
	conn      net.Conn
	request   atomic.Uint64
	maximum   uint32
	attachID  string
	attachGen uint64
}

// SessionID is the immutable semantic session scope from the authenticated
// bootstrap carrier.  UI reducers use it to reject otherwise well-formed
// cross-session live/control frames.
func (s *Service) SessionID() string { return s.bootstrap.SessionID }

func New(bootstrap Bootstrap) (*Service, error) {
	if bootstrap.LaunchID == "" || len(bootstrap.LaunchCapability) < 32 || bootstrap.ClientInstanceID == "" || bootstrap.HostSessionID == "" || bootstrap.SessionID == "" || bootstrap.SocketPath == "" || bootstrap.ParentPID == 0 {
		return nil, errors.New("Protocol v3 bootstrap is incomplete")
	}
	return &Service{bootstrap: bootstrap, maximum: maximumFrameBytes}, nil
}

func (s *Service) Attachment() (string, uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.attachID, s.attachGen
}

func (s *Service) ResetConnection() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn != nil {
		_ = s.conn.Close()
	}
	s.conn = nil
	s.attachID = ""
	s.attachGen = 0
}

func (s *Service) Hello(ctx context.Context) (*protocolv3.HelloAccepted, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.connectLocked(ctx); err != nil {
		return nil, err
	}
	requestID := s.nextRequestID("hello")
	response, err := s.roundTripLocked(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_Hello{Hello: &protocolv3.HelloRequest{
		RequestId: requestID,
		Protocol:  &protocolv3.ProtocolIdentity{Major: ProtocolMajor, Minor: ProtocolMinor, SchemaFingerprint: SchemaFingerprint},
		LaunchId:  s.bootstrap.LaunchID, LaunchCapability: append([]byte(nil), s.bootstrap.LaunchCapability...), ClientInstanceId: s.bootstrap.ClientInstanceID,
		HostSessionId: s.bootstrap.HostSessionID, SessionId: s.bootstrap.SessionID, RequestedRole: s.bootstrap.RequestedRole,
	}}})
	if err != nil {
		return nil, err
	}
	accepted := response.GetHello()
	if accepted == nil || accepted.RequestId != requestID || accepted.Protocol == nil || accepted.Protocol.Major != ProtocolMajor || accepted.Protocol.Minor != ProtocolMinor || accepted.Protocol.SchemaFingerprint != SchemaFingerprint || accepted.AttachmentId == "" || accepted.AttachmentGeneration == 0 {
		return nil, errors.New("Protocol v3 Hello result failed validation")
	}
	if accepted.GrantedRole != s.bootstrap.RequestedRole || accepted.MaximumFrameBytes < 1024 || accepted.MaximumFrameBytes > maximumFrameBytes {
		return nil, errors.New("Protocol v3 Hello limits or role are invalid")
	}
	if accepted.LiveOwnerEpoch == 0 || helloFingerprint(accepted) != accepted.ResultFingerprint {
		return nil, errors.New("Protocol v3 Hello authority proof is invalid")
	}
	s.attachID, s.attachGen, s.maximum = accepted.AttachmentId, accepted.AttachmentGeneration, accepted.MaximumFrameBytes
	return proto.Clone(accepted).(*protocolv3.HelloAccepted), nil
}

func (s *Service) Snapshot(ctx context.Context, maximumEntries, maximumControl uint32) (*protocolv3.SnapshotResponse, error) {
	requestID := s.nextRequestID("snapshot")
	response, err := s.attachedRoundTrip(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_Snapshot{Snapshot: &protocolv3.SnapshotRequest{
		RequestId: requestID, MaximumEntries: maximumEntries, MaximumControlItems: maximumControl,
	}}})
	if err != nil {
		return nil, err
	}
	value := response.GetSnapshot()
	if value == nil || value.RequestId != requestID || value.Snapshot == nil || value.Snapshot.SessionId != s.bootstrap.SessionID {
		return nil, errors.New("Protocol v3 snapshot failed validation")
	}
	return proto.Clone(value).(*protocolv3.SnapshotResponse), nil
}

func (s *Service) LiveControlSnapshot(ctx context.Context) (*protocolv3.LiveControlSnapshotResponse, error) {
	requestID := s.nextRequestID("live-control-snapshot")
	response, err := s.attachedRoundTrip(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_LiveControlSnapshot{LiveControlSnapshot: &protocolv3.LiveControlSnapshotRequest{
		RequestId: requestID,
	}}})
	if err != nil {
		return nil, err
	}
	value := response.GetLiveControlSnapshot()
	if value == nil || value.RequestId != requestID || value.Snapshot == nil || value.Snapshot.SessionId != s.bootstrap.SessionID || value.Snapshot.OwnerEpoch == 0 {
		return nil, errors.New("Protocol v3 live-control snapshot failed validation")
	}
	return proto.Clone(value).(*protocolv3.LiveControlSnapshotResponse), nil
}

func (s *Service) Observe(ctx context.Context, afterEvent, liveEpoch, afterLive, controlEpoch, afterControl uint64) (*protocolv3.ObservationResponse, error) {
	requestID := s.nextRequestID("observe")
	response, err := s.attachedRoundTrip(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_Observe{Observe: &protocolv3.ObserveRequest{
		RequestId: requestID, AfterEventSequence: afterEvent, LiveOwnerEpoch: liveEpoch, AfterLiveRevision: afterLive,
		LiveControlOwnerEpoch: controlEpoch, AfterLiveControlRevision: afterControl,
		MaximumEvents: 256, MaximumBytes: 4 << 20, WaitMs: 1000,
	}}})
	if err != nil {
		return nil, err
	}
	value := response.GetObservation()
	if value == nil || value.RequestId != requestID || value.ThroughEventSequence < afterEvent {
		return nil, errors.New("Protocol v3 observation failed validation")
	}
	return proto.Clone(value).(*protocolv3.ObservationResponse), nil
}

func (s *Service) Heartbeat(ctx context.Context) (*protocolv3.HeartbeatResponse, error) {
	requestID := s.nextRequestID("heartbeat")
	response, err := s.attachedRoundTrip(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_Heartbeat{Heartbeat: &protocolv3.HeartbeatRequest{RequestId: requestID}}})
	if err != nil {
		return nil, err
	}
	value := response.GetHeartbeat()
	if value == nil || value.RequestId != requestID || !value.Active {
		return nil, errors.New("Protocol v3 heartbeat failed validation")
	}
	return proto.Clone(value).(*protocolv3.HeartbeatResponse), nil
}

func (s *Service) History(ctx context.Context, cursor *protocolv3.HistoryCursor, maximumEntries, maximumBytes uint32) (*protocolv3.HistoryPageResponse, error) {
	if cursor == nil || cursor.SessionId != s.bootstrap.SessionID {
		return nil, errors.New("Protocol v3 history cursor is invalid")
	}
	if maximumEntries == 0 || maximumEntries > 128 || maximumBytes == 0 || maximumBytes > 4<<20 {
		return nil, errors.New("Protocol v3 history bounds are invalid")
	}
	requestID := s.nextRequestID("history")
	response, err := s.attachedRoundTrip(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_HistoryPage{HistoryPage: &protocolv3.HistoryPageRequest{
		RequestId: requestID, Cursor: proto.Clone(cursor).(*protocolv3.HistoryCursor), MaximumEntries: maximumEntries, MaximumSerializedBytes: maximumBytes,
	}}})
	if err != nil {
		return nil, err
	}
	value := response.GetHistoryPage()
	if value == nil || value.RequestId != requestID || len(value.Entries) > int(maximumEntries) || proto.Size(value) > int(maximumBytes) {
		return nil, errors.New("Protocol v3 history page failed validation")
	}
	for _, entry := range value.Entries {
		if entry == nil || entry.EntrySequence >= cursor.EntrySequence {
			return nil, errors.New("Protocol v3 history page crossed its cursor")
		}
	}
	if value.OlderHistoryCursor != nil && (value.OlderHistoryCursor.SessionId != cursor.SessionId || value.OlderHistoryCursor.CutSequence != cursor.CutSequence || value.OlderHistoryCursor.EntrySequence >= cursor.EntrySequence) {
		return nil, errors.New("Protocol v3 history continuation cursor is invalid")
	}
	if value.HasMore != (value.OlderHistoryCursor != nil) {
		return nil, errors.New("Protocol v3 history continuation union is invalid")
	}
	return proto.Clone(value).(*protocolv3.HistoryPageResponse), nil
}

func (s *Service) Command(ctx context.Context, commandID string, kind protocolv3.CommandKind, text string, targetTurnID string) (*protocolv3.CommandOutcome, error) {
	return s.command(ctx, commandID, kind, text, targetTurnID, "", "")
}

// AcceptSubagentResult installs an already durable child result into the ROOT
// conversation.  It never resumes or replays child execution.
func (s *Service) AcceptSubagentResult(ctx context.Context, commandID, targetTurnID, sourceResultID string) (*protocolv3.CommandOutcome, error) {
	if sourceResultID == "" {
		return nil, errors.New("Protocol v3 subagent result identity is empty")
	}
	return s.command(ctx, commandID, protocolv3.CommandKind_ACCEPT_SUBAGENT_RESULT, "", targetTurnID, sourceResultID, "")
}

// AcceptJobResult installs an already durable job result into an existing
// safe ROOT or, when targetTurnID is empty, an explicit new ROOT turn.
func (s *Service) AcceptJobResult(ctx context.Context, commandID, targetTurnID, sourceJobID string) (*protocolv3.CommandOutcome, error) {
	if sourceJobID == "" {
		return nil, errors.New("Protocol v3 job result identity is empty")
	}
	return s.command(ctx, commandID, protocolv3.CommandKind_ACCEPT_JOB_RESULT, "", targetTurnID, "", sourceJobID)
}

func (s *Service) command(ctx context.Context, commandID string, kind protocolv3.CommandKind, text, targetTurnID, sourceResultID, sourceJobID string) (*protocolv3.CommandOutcome, error) {
	if commandID == "" {
		return nil, errors.New("Protocol v3 command identity is empty")
	}
	requestID := s.nextRequestID("command")
	response, err := s.attachedRoundTrip(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_Command{Command: &protocolv3.CommandRequest{
		RequestId: requestID, CommandId: commandID, CommandKind: kind, ClientSubmissionId: commandID, Text: text, TargetTurnId: targetTurnID, SourceSubagentResultId: sourceResultID, SourceJobId: sourceJobID,
	}}})
	if err != nil {
		return nil, err
	}
	value := response.GetCommandOutcome()
	if value == nil || value.RequestId != requestID || value.CommandId != commandID || value.Status == protocolv3.CommandStatus_COMMAND_STATUS_UNSPECIFIED {
		return nil, errors.New("Protocol v3 command outcome failed validation")
	}
	return proto.Clone(value).(*protocolv3.CommandOutcome), nil
}

func (s *Service) QueryCommand(ctx context.Context, commandID string) (*protocolv3.QueryCommandResponse, error) {
	requestID := s.nextRequestID("query-command")
	response, err := s.attachedRoundTrip(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_QueryCommand{QueryCommand: &protocolv3.QueryCommandRequest{RequestId: requestID, CommandId: commandID}}})
	if err != nil {
		return nil, err
	}
	value := response.GetQueryCommand()
	if value == nil || value.RequestId != requestID || (value.Found && (value.Outcome == nil || value.Outcome.CommandId != commandID)) {
		return nil, errors.New("Protocol v3 command query failed validation")
	}
	return proto.Clone(value).(*protocolv3.QueryCommandResponse), nil
}

func (s *Service) ResolveInteraction(
	ctx context.Context,
	commandID string,
	writerGeneration, ownerEpoch, liveRevision uint64,
	interactionID string,
	decision protocolv3.InteractionResolutionDecision,
) (*protocolv3.CommandOutcome, error) {
	if commandID == "" || interactionID == "" || writerGeneration == 0 || ownerEpoch == 0 || liveRevision == 0 || (decision != protocolv3.InteractionResolutionDecision_INTERACTION_ALLOW && decision != protocolv3.InteractionResolutionDecision_INTERACTION_DENY) {
		return nil, errors.New("Protocol v3 interaction resolution is invalid")
	}
	requestID := s.nextRequestID("resolve-interaction")
	response, err := s.attachedRoundTrip(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_ResolveInteraction{ResolveInteraction: &protocolv3.ResolveInteractionRequest{
		RequestId: requestID, CommandId: commandID,
		ExpectedWriterGeneration: writerGeneration,
		ExpectedOwnerEpoch:       ownerEpoch, ExpectedLiveRevision: liveRevision,
		InteractionId: interactionID, Decision: decision,
	}}})
	if err != nil {
		return nil, err
	}
	value := response.GetCommandOutcome()
	if value == nil || value.RequestId != requestID || value.CommandId != commandID || value.Status == protocolv3.CommandStatus_COMMAND_STATUS_UNSPECIFIED {
		return nil, errors.New("Protocol v3 interaction outcome failed validation")
	}
	return proto.Clone(value).(*protocolv3.CommandOutcome), nil
}

func (s *Service) ReadContent(ctx context.Context, entryID, blockID string, offset uint64) (*protocolv3.CanonicalContentChunk, error) {
	requestID := s.nextRequestID("content")
	response, err := s.attachedRoundTrip(ctx, &protocolv3.ClientFrame{Request: &protocolv3.ClientFrame_ReadContent{ReadContent: &protocolv3.ReadContentRequest{
		RequestId: requestID, EntryId: entryID, BlockId: blockID, OffsetBytes: offset, LimitBytes: 1 << 20,
	}}})
	if err != nil {
		return nil, err
	}
	value := response.GetContent()
	if value == nil || value.RequestId != requestID || value.OffsetBytes != offset || len(value.Content) > 1<<20 || value.Digest == "" || value.CompleteSize < offset+uint64(len(value.Content)) || (value.Complete && value.CompleteSize != offset+uint64(len(value.Content))) {
		return nil, errors.New("Protocol v3 content chunk failed validation")
	}
	return proto.Clone(value).(*protocolv3.CanonicalContentChunk), nil
}

func (s *Service) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn == nil {
		return nil
	}
	err := s.conn.Close()
	s.conn = nil
	clear(s.bootstrap.LaunchCapability)
	return err
}

func (s *Service) attachedRoundTrip(ctx context.Context, frame *protocolv3.ClientFrame) (*protocolv3.ServerFrame, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.conn == nil || s.attachID == "" || s.attachGen == 0 {
		return nil, errors.New("Protocol v3 attachment is unavailable")
	}
	installAttachment(frame, s.attachID, s.attachGen)
	return s.roundTripLocked(ctx, frame)
}

func (s *Service) connectLocked(ctx context.Context) error {
	if s.conn != nil {
		return nil
	}
	dialer := net.Dialer{}
	conn, err := dialer.DialContext(ctx, "unix", s.bootstrap.SocketPath)
	if err != nil {
		return fmt.Errorf("connect Protocol v3 socket: %w", err)
	}
	s.conn = conn
	return nil
}

func (s *Service) roundTripLocked(ctx context.Context, frame *protocolv3.ClientFrame) (*protocolv3.ServerFrame, error) {
	if s.conn == nil {
		return nil, errors.New("Protocol v3 connection is closed")
	}
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(12 * time.Second)
	}
	if err := s.conn.SetDeadline(deadline); err != nil {
		return nil, err
	}
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(frame)
	if err != nil || len(payload) == 0 || uint32(len(payload)) > s.maximum {
		return nil, errors.New("Protocol v3 request frame is invalid")
	}
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
	if _, err := s.conn.Write(append(header[:], payload...)); err != nil {
		_ = s.conn.Close()
		s.conn = nil
		return nil, err
	}
	if _, err := io.ReadFull(s.conn, header[:]); err != nil {
		_ = s.conn.Close()
		s.conn = nil
		return nil, err
	}
	size := binary.BigEndian.Uint32(header[:])
	if size == 0 || size > s.maximum {
		return nil, errors.New("Protocol v3 response frame is out of bounds")
	}
	responsePayload := make([]byte, size)
	if _, err := io.ReadFull(s.conn, responsePayload); err != nil {
		_ = s.conn.Close()
		s.conn = nil
		return nil, err
	}
	response := &protocolv3.ServerFrame{}
	if err := proto.Unmarshal(responsePayload, response); err != nil {
		return nil, fmt.Errorf("decode Protocol v3 response: %w", err)
	}
	if failure := response.GetError(); failure != nil {
		return nil, &ProtocolError{
			StableCode: failure.StableCode, PublicMessage: failure.PublicMessage,
		}
	}
	return response, nil
}

func (s *Service) nextRequestID(kind string) string {
	return fmt.Sprintf("terminal-v3-request:%s:%d", kind, s.request.Add(1))
}

func installAttachment(frame *protocolv3.ClientFrame, id string, generation uint64) {
	switch value := frame.Request.(type) {
	case *protocolv3.ClientFrame_Snapshot:
		value.Snapshot.AttachmentId, value.Snapshot.AttachmentGeneration = id, generation
	case *protocolv3.ClientFrame_HistoryPage:
		value.HistoryPage.AttachmentId, value.HistoryPage.AttachmentGeneration = id, generation
	case *protocolv3.ClientFrame_Observe:
		value.Observe.AttachmentId, value.Observe.AttachmentGeneration = id, generation
	case *protocolv3.ClientFrame_Command:
		value.Command.AttachmentId, value.Command.AttachmentGeneration = id, generation
	case *protocolv3.ClientFrame_QueryCommand:
		value.QueryCommand.AttachmentId, value.QueryCommand.AttachmentGeneration = id, generation
	case *protocolv3.ClientFrame_ReadContent:
		value.ReadContent.AttachmentId, value.ReadContent.AttachmentGeneration = id, generation
	case *protocolv3.ClientFrame_Heartbeat:
		value.Heartbeat.AttachmentId, value.Heartbeat.AttachmentGeneration = id, generation
	case *protocolv3.ClientFrame_LiveControlSnapshot:
		value.LiveControlSnapshot.AttachmentId, value.LiveControlSnapshot.AttachmentGeneration = id, generation
	case *protocolv3.ClientFrame_ResolveInteraction:
		value.ResolveInteraction.AttachmentId, value.ResolveInteraction.AttachmentGeneration = id, generation
	}
}

func helloFingerprint(value *protocolv3.HelloAccepted) string {
	clone := proto.Clone(value).(*protocolv3.HelloAccepted)
	clone.ResultFingerprint = ""
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(clone)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(append([]byte("terminal-v3-hello-accepted:v1\x00"), payload...))
	return fmt.Sprintf("sha256:%x", sum[:])
}
