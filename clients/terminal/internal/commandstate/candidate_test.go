package commandstate

import (
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func testSubmitCandidate(t *testing.T, ordinal uint64) Candidate {
	t.Helper()
	candidate, err := NewSubmitCandidate(CandidateInput{
		ClientInstanceID: "client:registry", AttachmentID: "attachment:registry",
		AttachmentGeneration: 1, RuntimeSessionID: "runtime:registry",
		ExpectedTargetID: "runtime:registry", ExpectedTargetGeneration: 1,
		ExpectedControllerGeneration: 1, CandidateOrdinal: ordinal,
		ComposerRevision: ordinal, ComposerContentFingerprint: "sha256:composer",
		Text: "candidate", DeliveryMode: DeliveryAuto,
	})
	if err != nil {
		t.Fatal(err)
	}
	return candidate
}

func testTerminalOutcome(t *testing.T, candidate Candidate) Outcome {
	t.Helper()
	fingerprint, err := protocolvalue.CanonicalClientFingerprint("terminal-command-outcome:v1", map[string]any{
		"command_id": candidate.ID(), "durable_reference_ids": []string{},
		"public_result_code": "DONE", "public_result_text": "Done.",
		"query_token": "query:" + candidate.ID(), "status": "succeeded",
		"target_generation": candidate.Binding().ExpectedTargetGeneration,
		"target_id":         candidate.Binding().ExpectedTargetID,
	})
	if err != nil {
		t.Fatal(err)
	}
	return Outcome{
		RequestID: "request:" + candidate.ID(), Status: OutcomeSucceeded,
		CommandID: candidate.ID(), TargetID: candidate.Binding().ExpectedTargetID,
		TargetGeneration: candidate.Binding().ExpectedTargetGeneration,
		PublicResultCode: "DONE", PublicResultText: "Done.",
		DurableReferenceIDs: []string{}, QueryToken: "query:" + candidate.ID(), Fingerprint: fingerprint,
	}
}

func TestRequestSemanticFingerprintMatchesPythonGolden(t *testing.T) {
	binding := Binding{
		ClientInstanceID: "client:one", AttachmentID: "attachment:one",
		AttachmentGeneration: 2, CommandID: "terminal-command:golden",
		RuntimeSessionID: "runtime:one", ExpectedTargetID: "runtime:one",
		ExpectedTargetGeneration: 1, ExpectedControllerGeneration: 3,
	}
	submit, err := requestSemanticFingerprint("submit_prompt", binding, map[string]any{
		"client_submission_id": "terminal-submission:one", "command_kind": "submit_prompt",
		"requested_delivery_mode": "auto", "text": "你好\nworld",
	})
	if err != nil {
		t.Fatal(err)
	}
	if submit != "sha256:37f3cf611e521741e4d3c32c4765d04a2cd31181fa179aa82a0015cdf5362fe4" {
		t.Fatalf("submit request fingerprint drifted: %s", submit)
	}
	stop, err := requestSemanticFingerprint("stop_run", binding, map[string]any{
		"command_kind": "stop_run", "reason": "user_stop",
	})
	if err != nil {
		t.Fatal(err)
	}
	if stop != "sha256:26b0c85f6f295507bf22c3c709b62390c817bb96cfd83a099d17431baaaf6e3f" {
		t.Fatalf("stop request fingerprint drifted: %s", stop)
	}
}

func TestOutcomeFingerprintMatchesPythonGolden(t *testing.T) {
	outcome := Outcome{
		RequestID: "request:one", Status: OutcomeSucceeded,
		CommandID: "terminal-command:golden", TargetID: "runtime:one", TargetGeneration: 1,
		PublicResultCode: "RUN_COMPLETED", PublicResultText: "The submitted prompt completed.",
		DurableReferenceIDs: []string{}, QueryToken: "query:receipt",
		Fingerprint: "sha256:c53d99dc1dd94a56ead5cc8de012147e7c63f58be96e42efb7b4ccf81987a07e",
	}
	if err := outcome.Validate(); err != nil {
		t.Fatalf("Python outcome golden rejected: %v", err)
	}
}

func TestMutationPayloadAdmissionMatchesPhysicalWriterAccounting(t *testing.T) {
	candidate := testSubmitCandidate(t, 1)
	_, requestID, err := protocolvalue.WireOperationIdentity("client:registry", 1, 1, 1, 17)
	if err != nil {
		t.Fatal(err)
	}
	if len(requestID) != len(protocol.CanonicalWireRequestIDForPayloadSizing()) {
		t.Fatalf("payload-size request shape drifted: actual=%d canonical=%d", len(requestID), len(protocol.CanonicalWireRequestIDForPayloadSizing()))
	}
	request, err := candidate.ToProto(requestID)
	if err != nil {
		t.Fatal(err)
	}
	payload, err := protocol.MarshalBoundedDeterministicPayload(
		&protocol.ClientFrame{Request: &protocol.ClientFrame_Mutation{Mutation: request}},
		protocolvalue.MaximumFrameBytes,
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := candidate.ValidateMutationPayloadBound(uint32(len(payload))); err != nil {
		t.Fatalf("exact physical payload bound was rejected: %v", err)
	}
	if err := candidate.ValidateMutationPayloadBound(uint32(len(payload) - 1)); err == nil {
		t.Fatal("candidate escaped the exact physical payload bound")
	}
}

func TestEmptyProtoDurableReferencesNormalizeToCanonicalArray(t *testing.T) {
	outcome, err := OutcomeFromProto(&protocol.CommandOutcome{
		RequestId: "request:one", OutcomeStatus: protocol.CommandOutcomeStatus_SUCCEEDED,
		CommandId: "terminal-command:golden", TargetId: "runtime:one", TargetGeneration: 1,
		PublicResultCode: "RUN_COMPLETED", PublicResultText: "The submitted prompt completed.",
		QueryToken:         "query:receipt",
		OutcomeFingerprint: "sha256:c53d99dc1dd94a56ead5cc8de012147e7c63f58be96e42efb7b4ccf81987a07e",
	})
	if err != nil {
		t.Fatal(err)
	}
	if outcome.DurableReferenceIDs == nil || len(outcome.DurableReferenceIDs) != 0 {
		t.Fatal("empty protobuf repeated field was not normalized to a canonical array")
	}
}

func TestRegistryRetiresOnlyTerminalRecordsAtItsHardBound(t *testing.T) {
	registry, err := NewDormantRegistry(MaximumRecords)
	if err != nil {
		t.Fatal(err)
	}
	registry, err = registry.Activate()
	if err != nil {
		t.Fatal(err)
	}
	firstID := ""
	for ordinal := uint64(1); ordinal <= uint64(MaximumRecords); ordinal++ {
		candidate := testSubmitCandidate(t, ordinal)
		if ordinal == 1 {
			firstID = candidate.ID()
		}
		registry, err = registry.Install(candidate)
		if err != nil {
			t.Fatal(err)
		}
		registry, err = registry.MarkMutationSending(candidate.ID())
		if err != nil {
			t.Fatal(err)
		}
		registry, err = registry.ApplyOutcome(candidate.ID(), testTerminalOutcome(t, candidate), time.Now())
		if err != nil {
			t.Fatal(err)
		}
	}
	if registry.Count() != int(MaximumRecords) {
		t.Fatalf("registry count=%d, want %d", registry.Count(), MaximumRecords)
	}
	replacement := testSubmitCandidate(t, uint64(MaximumRecords)+1)
	registry, err = registry.Install(replacement)
	if err != nil {
		t.Fatal(err)
	}
	if registry.Count() != int(MaximumRecords) {
		t.Fatalf("terminal eviction changed the hard bound: %d", registry.Count())
	}
	if _, found := registry.Record(firstID); found {
		t.Fatal("oldest terminal record was not retired")
	}
	if !registry.OwnsExact(replacement) {
		t.Fatal("replacement candidate was not installed exactly")
	}
}

func TestAttachmentReplacementQueriesOldCandidateInsteadOfResending(t *testing.T) {
	registry, err := NewDormantRegistry(MaximumRecords)
	if err != nil {
		t.Fatal(err)
	}
	registry, err = registry.Activate()
	if err != nil {
		t.Fatal(err)
	}
	candidate := testSubmitCandidate(t, 1)
	registry, err = registry.Install(candidate)
	if err != nil {
		t.Fatal(err)
	}
	registry, err = registry.MarkMutationSending(candidate.ID())
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now().UTC()
	registry, err = registry.RequireQueryAfterAttachmentChange(now)
	if err != nil {
		t.Fatal(err)
	}
	got, query, ready := registry.NextAction(now)
	if !ready || !query || got.Fingerprint() != candidate.Fingerprint() {
		t.Fatal("retired-attachment command was not preserved as an exact query-only candidate")
	}
}

func TestLatestPendingRecordIsNotHiddenByNewerTerminalRecord(t *testing.T) {
	registry, err := NewDormantRegistry(MaximumRecords)
	if err != nil {
		t.Fatal(err)
	}
	registry, err = registry.Activate()
	if err != nil {
		t.Fatal(err)
	}
	pending := testSubmitCandidate(t, 1)
	terminal := testSubmitCandidate(t, 2)
	for _, candidate := range []Candidate{pending, terminal} {
		registry, err = registry.Install(candidate)
		if err != nil {
			t.Fatal(err)
		}
		registry, err = registry.MarkMutationSending(candidate.ID())
		if err != nil {
			t.Fatal(err)
		}
	}
	registry, err = registry.ApplyOutcome(terminal.ID(), testTerminalOutcome(t, terminal), time.Now())
	if err != nil {
		t.Fatal(err)
	}
	record, ok := registry.LatestPendingRecord()
	if !ok || record.Candidate().ID() != pending.ID() {
		t.Fatal("newer terminal command hid an older unresolved command")
	}
}
