package protocol

import (
	"encoding/hex"
	"testing"

	"google.golang.org/protobuf/proto"
)

func TestProtocolFingerprintGolden(t *testing.T) {
	candidate := &HandshakeRecoveryCandidateIdentity{
		CandidateVersion:            1,
		ClientInstanceId:            "client:golden",
		AttachmentAttemptGeneration: 1,
		HostSessionId:               "host:golden",
		RequestedRuntimeSessionId:   "runtime:golden",
		RequestedAttachmentRole:     AttachmentRole_ATTACHMENT_ROLE_OBSERVER,
		MinimumProtocolMajor:        2,
		MinimumProtocolMinor:        0,
		MaximumProtocolMajor:        2,
		MaximumProtocolMinor:        0,
		ClientBuildIdentity:         "pulsara-tui:golden",
		SupportedCapabilities: []TerminalClientCapability{
			TerminalClientCapability_PRESENTATION_SNAPSHOT_V1,
			TerminalClientCapability_OPERATIONAL_SNAPSHOT_V1,
			TerminalClientCapability_BOOTSTRAP_CARRIER_V1,
			TerminalClientCapability_LAUNCH_AUTH_PREFACE_V1,
			TerminalClientCapability_ATTACH_ACK_V1,
		},
		RequiredCapabilities: []TerminalClientCapability{
			TerminalClientCapability_PRESENTATION_SNAPSHOT_V1,
			TerminalClientCapability_OPERATIONAL_SNAPSHOT_V1,
			TerminalClientCapability_BOOTSTRAP_CARRIER_V1,
			TerminalClientCapability_LAUNCH_AUTH_PREFACE_V1,
			TerminalClientCapability_ATTACH_ACK_V1,
		},
		SchemaContractFingerprint: "sha256:8d46525581d6d7b1ed9dbf7fa8b975edda6eb409d5c389841bdf2609f0cd3e60",
	}
	fingerprint, err := InstallFingerprint("terminal-handshake-recovery-candidate:v1", candidate, "candidate_fingerprint", "candidate_id")
	if err != nil {
		t.Fatal(err)
	}
	const expectedFingerprint = "sha256:c72f01fe8931c94c8b271fddb5ebd0904ab6d95e331e0b3d4b9f81e8a40b59d7"
	if fingerprint != expectedFingerprint {
		t.Fatalf("candidate fingerprint = %s", fingerprint)
	}
	candidate.CandidateId = "handshake:" + fingerprint[len("sha256:"):]
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(candidate)
	if err != nil {
		t.Fatal(err)
	}
	const expectedHex = "0801124a68616e647368616b653a633732663031666538393331633934633862323731666464623565626430393034616236643935653333316530623364346239663831653861343062353964371a0d636c69656e743a676f6c64656e20012a0b686f73743a676f6c64656e320e72756e74696d653a676f6c64656e380140025002621270756c736172612d7475693a676f6c64656e6a050102030405720501020304057a477368613235363a633732663031666538393331633934633862323731666464623565626430393034616236643935653333316530623364346239663831653861343062353964378201477368613235363a38643436353235353831643664376231656439646266376661386239373565646461366562343039643563333839383431626466323630396630636433653630"
	if hex.EncodeToString(payload) != expectedHex {
		t.Fatalf("candidate deterministic protobuf drifted")
	}
}

func TestAttachmentChallengeCommitmentGolden(t *testing.T) {
	challenge, err := hex.DecodeString("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
	if err != nil {
		t.Fatal(err)
	}
	value, err := AttachmentChallengeCommitment(
		"auth:golden",
		"sha256:c72f01fe8931c94c8b271fddb5ebd0904ab6d95e331e0b3d4b9f81e8a40b59d7",
		"handshake:c72f01fe8931c94c8b271fddb5ebd0904ab6d95e331e0b3d4b9f81e8a40b59d7",
		"connection:golden",
		"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"request:golden",
		challenge,
	)
	if err != nil {
		t.Fatal(err)
	}
	if value != "sha256:9267871f39881959d9a2ea29b2a754220c4a167cc7340dd74703ccec8a70f7a0" {
		t.Fatalf("challenge commitment = %s", value)
	}
}
