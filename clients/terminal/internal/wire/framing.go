package wire

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
)

const CompiledMaximumFrameBytes = 8 * 1024 * 1024

type Connection struct {
	requestMu                 sync.Mutex
	mu                        sync.Mutex
	conn                      *net.UnixConn
	maximumFrameBytes         uint32
	closed                    bool
	active                    bool
	activeDone                chan struct{}
	connectionID              string
	generation                uint64
	activeOperationID         string
	activeOperationGeneration uint64
	terminalReceipt           PhysicalConnectionTerminalReceipt
}

// PeerCredentials is the process-local result of asking the connected Unix
// socket for its kernel-authenticated peer. It is deliberately not a protocol
// carrier; the client application binds it to the socket-path proof before the
// value can cross into Update.
type PeerCredentials struct {
	UID    uint64
	PID    uint64
	HasPID bool
}

var connectionGeneration atomic.Uint64

type DeliveryPhase uint8

const (
	DeliveryNotStarted DeliveryPhase = iota + 1
	DeliveryWriteStarted
	DeliveryRequestFullySent
	DeliveryResponseReadStarted
)

type PhysicalIOError struct {
	Phase      DeliveryPhase
	Cause      error
	receipt    PhysicalConnectionTerminalReceipt
	hasReceipt bool
}

func (e *PhysicalIOError) Error() string { return e.Cause.Error() }
func (e *PhysicalIOError) Unwrap() error { return e.Cause }
func (e *PhysicalIOError) TerminalReceipt() (PhysicalConnectionTerminalReceipt, bool) {
	return e.receipt, e.hasReceipt
}

type PhysicalIOExitDisposition uint8

const (
	PhysicalIOJoined PhysicalIOExitDisposition = iota + 1
	PhysicalIONotStarted
)

type PhysicalConnectionTerminalReason uint8

const (
	ConnectionTerminalReadDeadline PhysicalConnectionTerminalReason = iota + 1
	ConnectionTerminalEOF
	ConnectionTerminalReadFailure
	ConnectionTerminalWriteFailure
	ConnectionTerminalCallerTeardown
)

type PhysicalConnectionTerminalReceipt struct {
	ConnectionID                     string
	ConnectionGeneration             uint64
	PhysicalDrainIdentityFingerprint string
	TerminalReason                   PhysicalConnectionTerminalReason
	ReaderOperationID                string
	ReaderOperationGeneration        uint64
	ReaderExit                       PhysicalIOExitDisposition
	WriterOperationID                string
	WriterOperationGeneration        uint64
	WriterExit                       PhysicalIOExitDisposition
	ReceiptFingerprint               string
}

func (r PhysicalConnectionTerminalReceipt) Validate() error {
	if r.ConnectionID == "" || r.ConnectionGeneration == 0 || r.PhysicalDrainIdentityFingerprint == "" || r.TerminalReason == 0 || r.ReceiptFingerprint == "" {
		return errors.New("terminal physical connection receipt is incomplete")
	}
	if r.WriterExit == PhysicalIOJoined {
		if r.WriterOperationID == "" || r.WriterOperationGeneration == 0 {
			return errors.New("joined terminal writer identity is incomplete")
		}
	} else if r.WriterExit != PhysicalIONotStarted || r.WriterOperationID != "" || r.WriterOperationGeneration != 0 {
		return errors.New("terminal writer exit matrix is invalid")
	}
	if r.ReaderExit == PhysicalIOJoined {
		if r.ReaderOperationID == "" || r.ReaderOperationGeneration == 0 {
			return errors.New("joined terminal reader identity is incomplete")
		}
	} else if r.ReaderExit != PhysicalIONotStarted || r.ReaderOperationID != "" || r.ReaderOperationGeneration != 0 {
		return errors.New("terminal reader exit matrix is invalid")
	}
	if receiptFingerprint(r, false) != r.ReceiptFingerprint {
		return errors.New("terminal physical connection receipt fingerprint mismatch")
	}
	return nil
}

func Dial(path string, timeout time.Duration) (*Connection, error) {
	dialer := net.Dialer{Timeout: timeout}
	raw, err := dialer.Dial("unix", path)
	if err != nil {
		return nil, err
	}
	connection, ok := raw.(*net.UnixConn)
	if !ok {
		raw.Close()
		return nil, errors.New("terminal transport is not a Unix socket")
	}
	generation := connectionGeneration.Add(1)
	return &Connection{conn: connection, maximumFrameBytes: CompiledMaximumFrameBytes, connectionID: connectionIdentity(path, generation), generation: generation}, nil
}

func (c *Connection) SetMaximumFrameBytes(value uint32) error {
	if value == 0 || value > CompiledMaximumFrameBytes {
		return errors.New("terminal negotiated frame bound is invalid")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	c.maximumFrameBytes = value
	return nil
}

func (c *Connection) Identity() (string, uint64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.connectionID, c.generation
}

func (c *Connection) PeerCredentials() (PeerCredentials, error) {
	c.mu.Lock()
	connection := c.conn
	closed := c.closed
	c.mu.Unlock()
	if connection == nil || closed {
		return PeerCredentials{}, errors.New("terminal peer credentials are unavailable")
	}
	raw, err := connection.SyscallConn()
	if err != nil {
		return PeerCredentials{}, err
	}
	var credentials PeerCredentials
	var controlErr error
	if err := raw.Control(func(fd uintptr) {
		credentials, controlErr = peerCredentialsFromFD(int(fd))
	}); err != nil {
		return PeerCredentials{}, err
	}
	if controlErr != nil {
		return PeerCredentials{}, controlErr
	}
	return credentials, nil
}

func (c *Connection) Authenticate(preface *protocol.TerminalTransportAuthPreface, timeout time.Duration, operationID string, operationGeneration uint64) (*protocol.TerminalTransportAuthResult, error) {
	result := &protocol.TerminalTransportAuthResult{}
	if err := c.roundTrip(preface, result, timeout, 16*1024, operationID, operationGeneration); err != nil {
		return nil, err
	}
	return result, nil
}

func (c *Connection) RoundTrip(frame *protocol.ClientFrame, timeout time.Duration, operationID string, operationGeneration uint64) (*protocol.ServerFrame, error) {
	result := &protocol.ServerFrame{}
	if err := c.roundTrip(frame, result, timeout, c.maximumFrameBytes, operationID, operationGeneration); err != nil {
		return nil, err
	}
	return result, nil
}

func (c *Connection) roundTrip(request, response proto.Message, timeout time.Duration, maximum uint32, operationID string, operationGeneration uint64) error {
	c.requestMu.Lock()
	defer c.requestMu.Unlock()
	if operationID == "" || operationGeneration == 0 {
		return &PhysicalIOError{Phase: DeliveryNotStarted, Cause: errors.New("terminal physical operation identity is missing")}
	}
	c.mu.Lock()
	if c.closed {
		receipt := c.terminalReceipt
		c.mu.Unlock()
		return &PhysicalIOError{Phase: DeliveryNotStarted, Cause: net.ErrClosed, receipt: receipt, hasReceipt: receipt.ReceiptFingerprint != ""}
	}
	done := make(chan struct{})
	c.active, c.activeDone = true, done
	c.activeOperationID, c.activeOperationGeneration = operationID, operationGeneration
	connection := c.conn
	c.mu.Unlock()
	defer func() {
		c.mu.Lock()
		if c.activeDone == done {
			c.active = false
			c.activeDone = nil
			c.activeOperationID = ""
			c.activeOperationGeneration = 0
			close(done)
		}
		c.mu.Unlock()
	}()
	deadline := time.Now().Add(timeout)
	if err := connection.SetDeadline(deadline); err != nil {
		return &PhysicalIOError{Phase: DeliveryNotStarted, Cause: err}
	}
	defer connection.SetDeadline(time.Time{})
	if err := writeMessage(connection, request, maximum); err != nil {
		receipt := c.terminate(connection, operationID, operationGeneration, ConnectionTerminalWriteFailure, false)
		return &PhysicalIOError{Phase: DeliveryWriteStarted, Cause: err, receipt: receipt, hasReceipt: true}
	}
	if err := readMessage(connection, response, maximum); err != nil {
		// V1 has no multiplexing or cancellation. Once a request is fully sent,
		// any read failure invalidates this physical stream.
		reason := ConnectionTerminalReadFailure
		if timeoutError, ok := err.(net.Error); ok && timeoutError.Timeout() {
			reason = ConnectionTerminalReadDeadline
		} else if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			reason = ConnectionTerminalEOF
		}
		receipt := c.terminate(connection, operationID, operationGeneration, reason, true)
		return &PhysicalIOError{Phase: DeliveryResponseReadStarted, Cause: err, receipt: receipt, hasReceipt: true}
	}
	return nil
}

func (c *Connection) invalidate(connection *net.UnixConn) {
	c.mu.Lock()
	if c.conn == connection {
		c.closed = true
	}
	c.mu.Unlock()
	_ = connection.Close()
}

func (c *Connection) terminate(connection *net.UnixConn, operationID string, operationGeneration uint64, reason PhysicalConnectionTerminalReason, readerStarted bool) PhysicalConnectionTerminalReceipt {
	c.invalidate(connection)
	receipt := PhysicalConnectionTerminalReceipt{
		ConnectionID: c.connectionID, ConnectionGeneration: c.generation,
		TerminalReason:    reason,
		WriterOperationID: operationID + ":writer", WriterOperationGeneration: operationGeneration, WriterExit: PhysicalIOJoined,
		ReaderExit: PhysicalIONotStarted,
	}
	if readerStarted {
		receipt.ReaderOperationID = operationID + ":reader"
		receipt.ReaderOperationGeneration = operationGeneration
		receipt.ReaderExit = PhysicalIOJoined
	}
	receipt.PhysicalDrainIdentityFingerprint = drainFingerprint(receipt)
	receipt.ReceiptFingerprint = receiptFingerprint(receipt, false)
	c.mu.Lock()
	if c.terminalReceipt.ReceiptFingerprint == "" {
		c.terminalReceipt = receipt
	} else {
		receipt = c.terminalReceipt
	}
	c.mu.Unlock()
	return receipt
}

func (c *Connection) Close() error {
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return nil
	}
	c.closed = true
	connection := c.conn
	c.mu.Unlock()
	return connection.Close()
}

func (c *Connection) CloseAndWait(deadline time.Time) error {
	c.mu.Lock()
	c.closed = true
	connection := c.conn
	done := c.activeDone
	c.mu.Unlock()
	closeErr := connection.Close()
	if errors.Is(closeErr, net.ErrClosed) {
		closeErr = nil
	}
	if done == nil {
		return closeErr
	}
	duration := time.Until(deadline)
	if duration <= 0 {
		return errors.New("terminal physical connection drain deadline expired")
	}
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-done:
		return closeErr
	case <-timer.C:
		return errors.New("terminal physical connection drain deadline expired")
	}
}

func connectionIdentity(path string, generation uint64) string {
	digest := sha256.Sum256([]byte(fmt.Sprintf("terminal-connection:v1\x00%s\x00%d", path, generation)))
	return "terminal-connection:" + hex.EncodeToString(digest[:])
}

func drainFingerprint(receipt PhysicalConnectionTerminalReceipt) string {
	digest := sha256.Sum256([]byte(fmt.Sprintf("terminal-physical-drain:v1\x00%s\x00%d\x00%s\x00%d\x00%d", receipt.ConnectionID, receipt.ConnectionGeneration, receipt.WriterOperationID, receipt.WriterOperationGeneration, receipt.TerminalReason)))
	return "sha256:" + hex.EncodeToString(digest[:])
}

func receiptFingerprint(receipt PhysicalConnectionTerminalReceipt, _ bool) string {
	digest := sha256.Sum256([]byte(fmt.Sprintf("terminal-physical-terminal-receipt:v1\x00%s\x00%d\x00%s\x00%d\x00%s\x00%d\x00%d\x00%s\x00%d\x00%d", receipt.ConnectionID, receipt.ConnectionGeneration, receipt.PhysicalDrainIdentityFingerprint, receipt.TerminalReason, receipt.ReaderOperationID, receipt.ReaderOperationGeneration, receipt.ReaderExit, receipt.WriterOperationID, receipt.WriterOperationGeneration, receipt.WriterExit)))
	return "sha256:" + hex.EncodeToString(digest[:])
}

func writeMessage(writer io.Writer, message proto.Message, maximum uint32) error {
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(message)
	if err != nil {
		return err
	}
	if len(payload) == 0 || uint64(len(payload)) > uint64(maximum) {
		return errors.New("terminal output frame is outside its bound")
	}
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
	if err := writeAll(writer, header[:]); err != nil {
		return err
	}
	return writeAll(writer, payload)
}

func readMessage(reader io.Reader, message proto.Message, maximum uint32) error {
	var header [4]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil {
		return err
	}
	size := binary.BigEndian.Uint32(header[:])
	if size == 0 || size > maximum {
		return fmt.Errorf("terminal input frame size %d is invalid", size)
	}
	payload := make([]byte, size)
	if _, err := io.ReadFull(reader, payload); err != nil {
		return err
	}
	if err := proto.Unmarshal(payload, message); err != nil {
		return fmt.Errorf("decode terminal frame: %w", err)
	}
	if err := rejectUnknownFields(message.ProtoReflect()); err != nil {
		return err
	}
	if err := protocol.ValidateFingerprintFormats(message); err != nil {
		return err
	}
	return nil
}

func writeAll(writer io.Writer, payload []byte) error {
	for len(payload) > 0 {
		written, err := writer.Write(payload)
		if written < 0 || written > len(payload) {
			return errors.New("terminal writer returned an invalid byte count")
		}
		payload = payload[written:]
		if err != nil {
			return err
		}
		if written == 0 {
			return io.ErrShortWrite
		}
	}
	return nil
}

func rejectUnknownFields(message protoreflect.Message) error {
	if len(message.GetUnknown()) != 0 {
		return errors.New("terminal frame contains fields outside protocol 2.0")
	}
	var nestedErr error
	message.Range(func(field protoreflect.FieldDescriptor, value protoreflect.Value) bool {
		if field.IsMap() {
			if field.MapValue().Kind() != protoreflect.MessageKind && field.MapValue().Kind() != protoreflect.GroupKind {
				return true
			}
			value.Map().Range(func(_ protoreflect.MapKey, item protoreflect.Value) bool {
				nestedErr = rejectUnknownFields(item.Message())
				return nestedErr == nil
			})
			return nestedErr == nil
		}
		if field.IsList() {
			if field.Kind() != protoreflect.MessageKind && field.Kind() != protoreflect.GroupKind {
				return true
			}
			items := value.List()
			for index := 0; index < items.Len(); index++ {
				if nestedErr = rejectUnknownFields(items.Get(index).Message()); nestedErr != nil {
					return false
				}
			}
			return true
		}
		if field.Kind() == protoreflect.MessageKind || field.Kind() == protoreflect.GroupKind {
			nestedErr = rejectUnknownFields(value.Message())
			return nestedErr == nil
		}
		return true
	})
	return nestedErr
}
