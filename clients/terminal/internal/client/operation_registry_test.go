package client

import (
	"encoding/binary"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	terminalwire "github.com/plumliu/pulsara-agent/clients/terminal/internal/wire"
)

func TestOperationRegistryOwnsAdmissionAndSingleSettlement(t *testing.T) {
	registry := newOperationRegistry()
	token := app.NewOperationToken(
		app.OpProjectionSnapshot,
		"terminal-client:registry",
		1,
		1,
		1,
		app.AttachmentState{},
		time.Now().Add(time.Second),
	)
	operation := app.NewOutstandingWire(token)
	if err := registry.begin(operation); err != nil {
		t.Fatal(err)
	}
	if err := registry.begin(operation); err == nil {
		t.Fatal("duplicate physical operation was admitted")
	}
	if err := registry.finishSuccess(operation); err != nil {
		t.Fatal(err)
	}
	if err := registry.finishSuccess(operation); err == nil {
		t.Fatal("operation was settled twice")
	}
	if registry.activeCount() != 0 {
		t.Fatal("settled operation remained resident")
	}
}

func TestOperationRegistryRejectsCallerInventedNonPhysicalFailurePhase(t *testing.T) {
	registry := newOperationRegistry()
	token := app.NewOperationToken(
		app.OpProjectionSnapshot,
		"terminal-client:registry",
		1,
		1,
		1,
		app.AttachmentState{},
		time.Now().Add(time.Second),
	)
	operation := app.NewOutstandingWire(token)
	if err := registry.begin(operation); err != nil {
		t.Fatal(err)
	}
	failure := registry.classifyFailure(
		operation,
		errors.New("snapshot failed"),
		"snapshot failed",
		nil,
	)
	if failure.Code() != app.FailureProjectionSnapshot || failure.Disposition() != app.FailureRebuildDurableSnapshot {
		t.Fatalf("unexpected closed failure classification: code=%d disposition=%d", failure.Code(), failure.Disposition())
	}
	production := failure.Production()
	requestID, hasRequestID := production.RequestID()
	if production.OperationKind() != app.OpProjectionSnapshot ||
		production.OperationID() != token.OperationID || !hasRequestID ||
		requestID != token.RequestID ||
		production.DeliveryPhase() != app.DeliveryResponseFullyValidated ||
		production.ConnectionState() != app.FailureConnectionUsable ||
		production.PhysicalCause() != app.CauseProjectionValidationFailed ||
		production.PhysicalReceiptFingerprint() == "" ||
		production.EvidenceFingerprint() != failure.EvidenceFingerprint() {
		t.Fatalf("failure production proof drifted from the exact operation: %#v", production)
	}
	if registry.activeCount() != 0 {
		t.Fatal("failed operation remained resident")
	}
}

func TestOperationRegistryOwnsStartedReadFailureThroughExactDrain(t *testing.T) {
	root, err := os.MkdirTemp("/tmp", "pulsara-terminal-drain-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	path := filepath.Join(root, "terminal.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	serverDone := make(chan struct{})
	go func() {
		defer close(serverDone)
		connection, acceptErr := listener.Accept()
		if acceptErr != nil {
			return
		}
		defer connection.Close()
		var header [4]byte
		if _, readErr := io.ReadFull(connection, header[:]); readErr != nil {
			return
		}
		payload := make([]byte, binary.BigEndian.Uint32(header[:]))
		_, _ = io.ReadFull(connection, payload)
		// Keep the response absent until the client-owned read deadline closes
		// the stream and its drain owner joins both physical I/O owners.
		_, _ = io.Copy(io.Discard, connection)
	}()

	raw, err := terminalwire.Dial(path, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	owner := &physicalConnectionOwner{raw: raw}
	registry := newOperationRegistry()
	token := app.NewOperationToken(
		app.OpProjectionSnapshot,
		"terminal-client:drain",
		1,
		1,
		1,
		app.AttachmentState{},
		time.Now().Add(time.Second),
	)
	operation := app.NewOutstandingWire(token)
	if err := registry.begin(operation); err != nil {
		t.Fatal(err)
	}
	_, roundTripErr := owner.RoundTrip(
		&protocol.ClientFrame{
			Request: &protocol.ClientFrame_Snapshot{
				Snapshot: &protocol.ProjectionSnapshotRequest{
					RequestId: token.RequestID,
				},
			},
		},
		30*time.Millisecond,
		token.OperationID,
		token.OperationGeneration,
	)
	if roundTripErr == nil {
		t.Fatal("blocked read unexpectedly succeeded")
	}
	failure := registry.classifyFailure(
		operation,
		roundTripErr,
		"snapshot read failed",
		owner,
	)
	if failure.Code() != app.FailureReadTimeout || registry.activeCount() != 0 {
		t.Fatalf("started read did not settle through terminalization: code=%d active=%d", failure.Code(), registry.activeCount())
	}
	production := failure.Production()
	terminalFingerprint, hasTerminalFingerprint := production.ConnectionTerminalReceiptFingerprint()
	if production.OperationID() != token.OperationID ||
		production.DeliveryPhase() != app.DeliveryResponseReadStarted ||
		production.ConnectionState() != app.FailureConnectionInvalidated ||
		production.PhysicalCause() != app.CauseDeadlineExpired ||
		!hasTerminalFingerprint || terminalFingerprint == "" {
		t.Fatalf("terminalized failure lost its drain-bound production proof: %#v", production)
	}
	registry.mu.Lock()
	attempt := registry.attempts[token.OperationID]
	registry.mu.Unlock()
	if attempt == nil || attempt.completion == "" ||
		attempt.failure.connectionTerminal.validate(
			owner.drain.handle,
		) != nil ||
		attempt.failure.connectionTerminal.drainIdentityFingerprint !=
			owner.drain.handle.drainIdentityFingerprint {
		t.Fatal("terminalization completion did not bind the exact drain winner")
	}
	if err := registry.drainConnectionTerminalizations(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if err := owner.CloseAndWait(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	<-serverDone
}

func TestTerminalizationWaitDeadlineOnlyDetachesWaiter(t *testing.T) {
	registry := newOperationRegistry()
	handle := ConnectionTerminalizationAttemptHandle{
		attemptID:                  "attempt:wait",
		attemptGeneration:          1,
		operationID:                "operation:wait",
		operationGeneration:        1,
		attemptIdentityFingerprint: "attempt-fingerprint:wait",
	}
	attempt := &connectionTerminalizationAttempt{
		handle: handle,
		done:   make(chan struct{}),
	}
	registry.attempts[handle.operationID] = attempt
	result, err := registry.waitConnectionTerminalization(
		handle,
		nil,
		time.Now().Add(5*time.Millisecond),
	)
	if err != nil || result.disposition != TerminalizationWaiterDeadline {
		t.Fatalf("deadline did not detach waiter: result=%#v err=%v", result, err)
	}
	registry.mu.Lock()
	_, stillOwned := registry.attempts[handle.operationID]
	registry.mu.Unlock()
	if !stillOwned {
		t.Fatal("waiter deadline deleted the service-owned attempt")
	}
}
