package bootstrap

import (
	"encoding/binary"
	"os"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
)

func TestReadConsumesOneBoundedCarrierAndEOF(t *testing.T) {
	now := time.Date(2026, 8, 3, 1, 2, 3, 0, time.UTC)
	carrier := validCarrier(t, now)
	read, write, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(carrier)
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		defer write.Close()
		var header [4]byte
		binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
		_, _ = write.Write(header[:])
		_, _ = write.Write(payload)
	}()
	value, err := Read(Options{FD: int(read.Fd()), Now: func() time.Time { return now }})
	if err != nil {
		t.Fatal(err)
	}
	if value.RuntimeSessionID != "runtime:one" || value.HostSessionID != "host:one" || len(value.LaunchCapability) != 32 {
		t.Fatalf("unexpected bootstrap value: %#v", value)
	}
}

func TestReadRejectsTrailingBytes(t *testing.T) {
	now := time.Date(2026, 8, 3, 1, 2, 3, 0, time.UTC)
	carrier := validCarrier(t, now)
	read, write, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(carrier)
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		defer write.Close()
		var header [4]byte
		binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
		_, _ = write.Write(header[:])
		_, _ = write.Write(payload)
		_, _ = write.Write([]byte{1})
	}()
	if _, err := Read(Options{FD: int(read.Fd()), Now: func() time.Time { return now }}); err == nil {
		t.Fatal("trailing bootstrap byte was accepted")
	}
}

func validCarrier(t *testing.T, now time.Time) *protocol.TerminalClientBootstrapCarrier {
	t.Helper()
	carrier := &protocol.TerminalClientBootstrapCarrier{
		CarrierVersion:          1,
		LaunchId:                "launch:one",
		ClientInstanceId:        "client:one",
		HostSessionId:           "host:one",
		RuntimeSessionId:        "runtime:one",
		UnixSocketPath:          "/tmp/pulsara-terminal-bootstrap-test.sock",
		LaunchCapability:        make([]byte, 32),
		RequestedAttachmentRole: protocol.AttachmentRole_ATTACHMENT_ROLE_OBSERVER,
		ParentPid:               123,
		IssuedAtUtc:             now.Format(time.RFC3339Nano),
		ExpiresAtUtc:            now.Add(time.Minute).Format(time.RFC3339Nano),
		CarrierNonce:            make([]byte, 32),
	}
	if _, err := protocol.InstallFingerprint("terminal-client-bootstrap:v1", carrier, "bootstrap_fingerprint"); err != nil {
		t.Fatal(err)
	}
	return carrier
}
