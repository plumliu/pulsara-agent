package wire

import (
	"bytes"
	"errors"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
)

func TestFramingRoundTripAndBound(t *testing.T) {
	value := &protocol.OperationalSnapshotRequest{RequestId: "request:one", RuntimeSessionId: "runtime:one", RequestFingerprint: wireSHA('a')}
	var buffer bytes.Buffer
	if err := writeMessage(&buffer, value, 1024); err != nil {
		t.Fatal(err)
	}
	result := &protocol.OperationalSnapshotRequest{}
	if err := readMessage(&buffer, result, 1024); err != nil {
		t.Fatal(err)
	}
	if result.RequestId != value.RequestId || result.RuntimeSessionId != value.RuntimeSessionId {
		t.Fatalf("round trip changed request: %#v", result)
	}
	if err := writeMessage(&bytes.Buffer{}, value, 1); err == nil {
		t.Fatal("oversized frame was accepted")
	}
}

type shortWriter struct{ buffer bytes.Buffer }

func (w *shortWriter) Write(payload []byte) (int, error) {
	if len(payload) > 2 {
		payload = payload[:2]
	}
	return w.buffer.Write(payload)
}

func TestFramingCompletesShortWritesAndRejectsNestedUnknownFields(t *testing.T) {
	value := &protocol.OperationalSnapshotRequest{RequestId: "request:short", RuntimeSessionId: "runtime:one", RequestFingerprint: wireSHA('a')}
	writer := &shortWriter{}
	if err := writeMessage(writer, value, 1024); err != nil {
		t.Fatal(err)
	}
	decoded := &protocol.OperationalSnapshotRequest{}
	if err := readMessage(&writer.buffer, decoded, 1024); err != nil {
		t.Fatal(err)
	}
	if decoded.RequestId != value.RequestId {
		t.Fatalf("short writer lost payload: %#v", decoded)
	}

	request := &protocol.OperationalSnapshotRequest{RequestId: "request:unknown", RuntimeSessionId: "runtime:one", RequestFingerprint: wireSHA('a')}
	request.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 0x01})
	frame := &protocol.ClientFrame{Request: &protocol.ClientFrame_OperationalSnapshot{OperationalSnapshot: request}}
	var buffer bytes.Buffer
	if err := writeMessage(&buffer, frame, 1024); err != nil {
		t.Fatal(err)
	}
	if err := readMessage(&buffer, &protocol.ClientFrame{}, 1024); err == nil {
		t.Fatal("nested protocol 2.0 unknown field was accepted")
	}
}

func TestFramingRejectsMalformedOpaqueFingerprint(t *testing.T) {
	request := &protocol.OperationalSnapshotRequest{
		RequestId:                     "request:opaque",
		RuntimeSessionId:              "runtime:one",
		AttachmentIdentityFingerprint: "not-a-fingerprint",
		RequestFingerprint:            wireSHA('a'),
	}
	frame := &protocol.ClientFrame{Request: &protocol.ClientFrame_OperationalSnapshot{OperationalSnapshot: request}}
	var buffer bytes.Buffer
	if err := writeMessage(&buffer, frame, 4096); err != nil {
		t.Fatal(err)
	}
	if err := readMessage(&buffer, &protocol.ClientFrame{}, 4096); err == nil {
		t.Fatal("malformed nested opaque fingerprint was accepted")
	}
}

func TestFullySentReadTimeoutInstallsDrainBoundTerminalReceipt(t *testing.T) {
	root, err := os.MkdirTemp("/tmp", "pulsara-wire-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	path := filepath.Join(root, "terminal.sock")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	requestRead := make(chan struct{})
	serverDone := make(chan struct{})
	go func() {
		defer close(serverDone)
		connection, acceptErr := listener.AcceptUnix()
		if acceptErr != nil {
			return
		}
		defer connection.Close()
		request := &protocol.ClientFrame{}
		if readMessage(connection, request, CompiledMaximumFrameBytes) == nil {
			close(requestRead)
		}
		var sink [1]byte
		_, _ = connection.Read(sink[:])
	}()

	connection, err := Dial(path, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	request := &protocol.ClientFrame{Request: &protocol.ClientFrame_OperationalSnapshot{OperationalSnapshot: &protocol.OperationalSnapshotRequest{RequestId: "request:timeout", RuntimeSessionId: "runtime:one", RequestFingerprint: wireSHA('a')}}}
	_, err = connection.RoundTrip(request, 50*time.Millisecond, "operation:timeout", 7)
	var physical *PhysicalIOError
	if !errors.As(err, &physical) || physical.Phase != DeliveryResponseReadStarted {
		t.Fatalf("unexpected physical error: %#v", err)
	}
	receipt, ok := physical.TerminalReceipt()
	if !ok {
		t.Fatal("read timeout lost terminal receipt")
	}
	if err := receipt.Validate(); err != nil {
		t.Fatal(err)
	}
	if receipt.TerminalReason != ConnectionTerminalReadDeadline || receipt.WriterExit != PhysicalIOJoined || receipt.ReaderExit != PhysicalIOJoined || receipt.WriterOperationID != "operation:timeout:writer" || receipt.ReaderOperationID != "operation:timeout:reader" {
		t.Fatalf("unexpected terminal receipt: %#v", receipt)
	}
	<-requestRead
	<-serverDone
}

func TestTerminalReceiptRejectsDrainAndPhysicalOwnerDrift(t *testing.T) {
	receipt := PhysicalConnectionTerminalReceipt{
		ConnectionID: "connection:one", ConnectionGeneration: 1,
		TerminalReason:    ConnectionTerminalReadDeadline,
		WriterOperationID: "operation:one:writer", WriterOperationGeneration: 7, WriterExit: PhysicalIOJoined,
		ReaderOperationID: "operation:one:reader", ReaderOperationGeneration: 7, ReaderExit: PhysicalIOJoined,
	}
	receipt.PhysicalDrainIdentityFingerprint = drainFingerprint(receipt)
	receipt.ReceiptFingerprint = receiptFingerprint(receipt, false)
	if err := receipt.Validate(); err != nil {
		t.Fatal(err)
	}

	mutations := []func(*PhysicalConnectionTerminalReceipt){
		func(value *PhysicalConnectionTerminalReceipt) {
			value.PhysicalDrainIdentityFingerprint = "sha256:forged"
		},
		func(value *PhysicalConnectionTerminalReceipt) { value.WriterOperationID = "operation:other:writer" },
		func(value *PhysicalConnectionTerminalReceipt) { value.ReaderOperationGeneration++ },
		func(value *PhysicalConnectionTerminalReceipt) { value.WriterExit = PhysicalIONotStarted },
	}
	for index, mutate := range mutations {
		forged := receipt
		mutate(&forged)
		if err := forged.Validate(); err == nil {
			t.Fatalf("forged terminal receipt %d was accepted", index)
		}
	}
}

func TestCloseAndWaitPhysicallyDrainsBlockedRoundTrip(t *testing.T) {
	root, err := os.MkdirTemp("/tmp", "pulsara-wire-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	path := filepath.Join(root, "terminal.sock")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	requestRead := make(chan struct{})
	go func() {
		connection, acceptErr := listener.AcceptUnix()
		if acceptErr != nil {
			return
		}
		defer connection.Close()
		request := &protocol.ClientFrame{}
		if readMessage(connection, request, CompiledMaximumFrameBytes) == nil {
			close(requestRead)
		}
		var sink [1]byte
		_, _ = connection.Read(sink[:])
	}()
	connection, err := Dial(path, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	request := &protocol.ClientFrame{Request: &protocol.ClientFrame_OperationalSnapshot{OperationalSnapshot: &protocol.OperationalSnapshotRequest{RequestId: "request:close", RuntimeSessionId: "runtime:one", RequestFingerprint: wireSHA('a')}}}
	done := make(chan error, 1)
	go func() {
		_, roundTripErr := connection.RoundTrip(request, time.Minute, "operation:close", 9)
		done <- roundTripErr
	}()
	<-requestRead
	if err := connection.CloseAndWait(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		var physical *PhysicalIOError
		if !errors.As(err, &physical) {
			t.Fatalf("blocked operation did not terminate physically: %v", err)
		}
		if receipt, ok := physical.TerminalReceipt(); !ok || receipt.Validate() != nil {
			t.Fatalf("blocked operation lost terminal receipt: %#v", physical)
		}
	case <-time.After(time.Second):
		t.Fatal("blocked physical operation survived close drain")
	}
}

func wireSHA(character byte) string {
	value := make([]byte, 64)
	for index := range value {
		value[index] = character
	}
	return "sha256:" + string(value)
}
