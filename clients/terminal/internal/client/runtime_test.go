package client

import (
	"bytes"
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func TestServiceTakesExclusiveLaunchCapabilityOwnership(t *testing.T) {
	capability := bytes.Repeat([]byte{0x5a}, 32)
	bootstrap := protocolvalue.Bootstrap{
		LaunchID: "launch:one", ClientInstanceID: "terminal-client:one",
		HostSessionID: "host:one", RuntimeSessionID: "runtime:one",
		SocketPath: "/tmp/pulsara-client-owner.sock", LaunchCapability: capability,
		ParentPID: 1, ExpiresAt: time.Now().Add(time.Minute), Fingerprint: "bootstrap",
	}
	service, err := NewService(bootstrap)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(capability, make([]byte, len(capability))) {
		t.Fatal("caller retained a plaintext launch capability copy")
	}
	if !bytes.Equal(service.bootstrap.LaunchCapability, bytes.Repeat([]byte{0x5a}, 32)) {
		t.Fatal("service did not install the launch capability in its owner")
	}
	service.runtime.Close()
	clear(service.bootstrap.LaunchCapability)
}

func TestAttachmentChallengeIsOneShotAndRevocable(t *testing.T) {
	owner := &ClientRuntimeOwner{}
	hello := app.NewOperationToken(app.OpHello, "terminal-client:test", 1, 1, 1, app.AttachmentState{}, time.Now().Add(time.Second))
	var raw [32]byte
	for index := range raw {
		raw[index] = byte(index + 1)
	}
	prepared, err := owner.PrepareAttachmentChallenge(hello, raw, "hello-receipt", "candidate", "connection", "challenge-commitment", time.Now().Add(time.Second))
	if err != nil {
		t.Fatal(err)
	}
	promote := app.NewLocalOperationToken(app.OpChallengePromote, "terminal-client:test", 2, 1, time.Now().Add(time.Second))
	promotion, err := owner.PromotePreparedAttachmentChallenge(promote, prepared)
	if err != nil {
		t.Fatal(err)
	}
	confirm := app.NewLocalOperationToken(app.OpChallengePromotionConfirm, "terminal-client:test", 3, 1, time.Now().Add(time.Second))
	acceptance, err := owner.ConfirmAttachmentChallengePromotion(confirm, promotion)
	if err != nil {
		t.Fatal(err)
	}
	value, commitment, err := owner.BorrowAttachmentChallengeOnce(acceptance)
	if err != nil {
		t.Fatal(err)
	}
	if len(value) != 32 || commitment != "challenge-commitment" {
		t.Fatalf("unexpected challenge borrow: len=%d commitment=%q", len(value), commitment)
	}
	if _, _, err := owner.BorrowAttachmentChallengeOnce(acceptance); err == nil {
		t.Fatal("one-shot challenge was borrowed twice")
	}
	owner.Close()
}

func TestStaleChallengeRevocationConsumesTheExactRuntimeOwner(t *testing.T) {
	owner := &ClientRuntimeOwner{}
	hello := app.NewOperationToken(
		app.OpHello,
		"terminal-client:revoke",
		1,
		1,
		1,
		app.AttachmentState{},
		time.Now().Add(time.Second),
	)
	var raw [32]byte
	for index := range raw {
		raw[index] = byte(index + 1)
	}
	prepared, err := owner.PrepareAttachmentChallenge(
		hello,
		raw,
		"hello-receipt",
		"candidate",
		"connection",
		"challenge-commitment",
		time.Now().Add(time.Second),
	)
	if err != nil {
		t.Fatal(err)
	}
	revokePrepared := app.NewLocalOperationToken(
		app.OpChallengeRevokePrepared,
		"terminal-client:revoke",
		2,
		1,
		time.Now().Add(time.Second),
	)
	receipt, err := owner.RevokePreparedAttachmentChallenge(
		revokePrepared,
		prepared.HandleFingerprint,
		app.ChallengeRevokeStaleApplicationResult,
	)
	if err != nil || receipt.Validate() != nil || receipt.Target != app.RevokePreparedChallenge {
		t.Fatalf("prepared challenge was not exactly revoked: receipt=%#v err=%v", receipt, err)
	}
	promote := app.NewLocalOperationToken(
		app.OpChallengePromote,
		"terminal-client:revoke",
		3,
		1,
		time.Now().Add(time.Second),
	)
	if _, err := owner.PromotePreparedAttachmentChallenge(promote, prepared); err == nil {
		t.Fatal("revoked prepared challenge remained promotable")
	}
}

func TestServiceCloseSharesTheSinglePhysicalDrainOwner(t *testing.T) {
	bootstrap := protocolvalue.Bootstrap{
		LaunchID: "launch:close", ClientInstanceID: "terminal-client:close",
		HostSessionID: "host:close", RuntimeSessionID: "runtime:close",
		SocketPath: "/tmp/pulsara-client-close.sock", LaunchCapability: bytes.Repeat([]byte{0x33}, 32),
		ParentPID: 1, ExpiresAt: time.Now().Add(time.Minute), Fingerprint: "bootstrap:close",
	}
	service, err := NewService(bootstrap)
	if err != nil {
		t.Fatal(err)
	}
	results := make(chan error, 2)
	go func() { results <- service.Close() }()
	go func() { results <- service.Close() }()
	for range 2 {
		if closeErr := <-results; closeErr != nil {
			t.Fatal(closeErr)
		}
	}
}

func TestOperationFailureMessagePreservesOperationFamily(t *testing.T) {
	attachment := app.AttachmentState{}
	wireToken := app.NewOperationToken(app.OpProjectionSnapshot, "terminal-client:test", 1, 1, 1, attachment, time.Now().Add(time.Second))
	wireOperation := app.NewOutstandingWire(wireToken)
	wireFailure := newOperationRegistry().classifyUnadmittedFailure(
		wireOperation,
		"invalid operation",
		"failure-proof",
	)
	if _, ok := failureMessageForOperation(wireOperation, wireFailure).(app.SnapshotRejectedMsg); !ok {
		t.Fatal("snapshot failure was lowered into the wrong message family")
	}
	localToken := app.NewLocalOperationToken(app.OpClipboard, "terminal-client:test", 2, 1, time.Now().Add(time.Second))
	localOperation := app.NewOutstandingLocal(localToken)
	localFailure := newOperationRegistry().classifyUnadmittedFailure(
		localOperation,
		"invalid operation",
		"failure-proof",
	)
	if _, ok := failureMessageForOperation(localOperation, localFailure).(app.PublicTextCopyFailedMsg); !ok {
		t.Fatal("clipboard failure was lowered into the wrong message family")
	}
}
