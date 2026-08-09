package kernelapp

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	tea "charm.land/bubbletea/v2"
	"google.golang.org/protobuf/proto"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/kernelclient"
	protocolv3 "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolv3"
)

type fakeService struct{ session string }

func (s fakeService) SessionID() string {
	if s.session != "" {
		return s.session
	}
	return "session:1"
}
func (fakeService) ResetConnection()                                         {}
func (fakeService) Hello(context.Context) (*protocolv3.HelloAccepted, error) { return nil, nil }
func (fakeService) Snapshot(context.Context, uint32, uint32) (*protocolv3.SnapshotResponse, error) {
	return nil, nil
}
func (fakeService) LiveControlSnapshot(context.Context) (*protocolv3.LiveControlSnapshotResponse, error) {
	return nil, nil
}
func (fakeService) Observe(context.Context, uint64, uint64, uint64, uint64, uint64) (*protocolv3.ObservationResponse, error) {
	return nil, nil
}
func (fakeService) Heartbeat(context.Context) (*protocolv3.HeartbeatResponse, error) {
	return nil, nil
}
func (fakeService) History(context.Context, *protocolv3.HistoryCursor, uint32, uint32) (*protocolv3.HistoryPageResponse, error) {
	return nil, nil
}
func (fakeService) Command(context.Context, string, protocolv3.CommandKind, string, string) (*protocolv3.CommandOutcome, error) {
	return nil, nil
}
func (fakeService) QueryCommand(context.Context, string) (*protocolv3.QueryCommandResponse, error) {
	return nil, nil
}
func (fakeService) ResolveInteraction(context.Context, string, uint64, uint64, uint64, string, protocolv3.InteractionResolutionDecision) (*protocolv3.CommandOutcome, error) {
	return nil, nil
}
func (fakeService) ReadContent(context.Context, string, string, uint64) (*protocolv3.CanonicalContentChunk, error) {
	return nil, nil
}

type interactionService struct {
	fakeService
	commandID, interactionID               string
	writerGeneration, ownerEpoch, revision uint64
	decision                               protocolv3.InteractionResolutionDecision
}

func (s *interactionService) ResolveInteraction(_ context.Context, commandID string, writerGeneration, ownerEpoch, revision uint64, interactionID string, decision protocolv3.InteractionResolutionDecision) (*protocolv3.CommandOutcome, error) {
	s.commandID, s.interactionID, s.writerGeneration = commandID, interactionID, writerGeneration
	s.ownerEpoch, s.revision, s.decision = ownerEpoch, revision, decision
	return &protocolv3.CommandOutcome{
		RequestId: "request:resolve", CommandId: commandID,
		Status: protocolv3.CommandStatus_SUCCEEDED, TargetId: "decision:1",
		PublicCode: "INTERACTION_ALLOW", PublicMessage: "Accepted.",
	}, nil
}

func inline(value string) *protocolv3.CanonicalContentReference {
	data := []byte(value)
	return &protocolv3.CanonicalContentReference{
		Kind: protocolv3.ContentKind_INLINE, InlineContent: data, Digest: digest(data),
		Size: uint64(len(data)), MediaType: "text/plain", Codec: "utf-8",
	}
}

func userEntry(id string, sequence uint64, value string) *protocolv3.CanonicalEntry {
	return &protocolv3.CanonicalEntry{
		EntryId: id, TurnId: "turn:1", EntrySequence: sequence,
		EntryKind: protocolv3.EntryKind_USER_MESSAGE,
		ScopeKind: protocolv3.ConversationScopeKind_ROOT,
		Content:   inline(value), AcceptedAtUtc: "2026-08-09T00:00:00Z",
	}
}

func assistantEntry(id string, sequence uint64, value string) *protocolv3.CanonicalEntry {
	return &protocolv3.CanonicalEntry{
		EntryId: id, TurnId: "turn:1", EntrySequence: sequence,
		EntryKind:                protocolv3.EntryKind_ASSISTANT_MESSAGE,
		ScopeKind:                protocolv3.ConversationScopeKind_ROOT,
		ContextBindingRevisionId: "revision:1", ProviderInputThroughSequence: sequence - 1,
		Content: inline("manifest"), AcceptedAtUtc: "2026-08-09T00:00:01Z",
		Blocks: []*protocolv3.CanonicalAssistantBlock{{
			BlockId: "block:1", Ordinal: 0, BlockKind: "TEXT", Content: inline(value),
		}},
	}
}

func snapshot(entries ...*protocolv3.CanonicalEntry) *protocolv3.SnapshotResponse {
	value := &protocolv3.CanonicalSessionSnapshot{
		SessionId: "session:1", WorkspaceId: "workspace:1", WriterGeneration: 1,
		EntrySequenceCut: uint64(len(entries)), EventSequenceCut: uint64(len(entries)),
		Entries: entries, Control: &protocolv3.CanonicalControl{
			SessionLifecycle: "OPEN",
			MemoryFreshness: []*protocolv3.MemoryFreshnessControl{
				{Channel: "FTS", HandlerContract: "uninitialized@0"},
				{Channel: "VECTOR", HandlerContract: "uninitialized@0"},
			},
		},
	}
	value.SnapshotFingerprint = canonicalSnapshotFingerprint(value)
	return &protocolv3.SnapshotResponse{RequestId: "request:1", Snapshot: value}
}

func TestSnapshotFingerprintAndCanonicalContentIntegrity(t *testing.T) {
	model := New(fakeService{})
	valid := snapshot(userEntry("entry:1", 1, "hello"))
	if err := model.installSnapshot(valid); err != nil {
		t.Fatalf("valid snapshot rejected: %v", err)
	}

	forged := proto.Clone(valid).(*protocolv3.SnapshotResponse)
	forged.Snapshot.Entries[0].Content.InlineContent = []byte("changed")
	forged.Snapshot.SnapshotFingerprint = canonicalSnapshotFingerprint(forged.Snapshot)
	if err := model.installSnapshot(forged); err == nil {
		t.Fatal("snapshot with forged inline payload was accepted")
	}
}

func TestCommittedProjectionContractIsExactAndFailClosed(t *testing.T) {
	if protocolv3.ProjectionContractCount() != 26 {
		t.Fatalf("projection contract count = %d", protocolv3.ProjectionContractCount())
	}
	model := New(fakeService{})
	model.liveEpoch, model.controlEpoch = 1, 1
	invalid := &protocolv3.ObservationResponse{
		ThroughEventSequence: 1, LiveOwnerEpoch: 1, ThroughLiveRevision: 0,
		LiveControlOwnerEpoch: 1, ThroughLiveControlRevision: 0,
		Committed: []*protocolv3.CommittedObservationProjection{{
			EventSequence: 1, EventId: "event:1", EventType: protocolv3.CommittedEventType_ASSISTANT_MESSAGE_ACCEPTED,
			ProjectionKind: protocolv3.ObservationProjectionKind_EVENT_ONLY,
			SubjectSlot:    "subject_entry_id", SubjectId: "entry:1",
		}},
	}
	if err := model.applyObservation(invalid); err == nil {
		t.Fatal("mismatched committed projection branch was accepted")
	}
}

func TestCanonicalProjectionRetiresLiveDraftAndLateDeltaCannotReviveIt(t *testing.T) {
	model := New(fakeService{})
	model.phase = phaseReady
	model.liveEpoch, model.controlEpoch = 1, 1
	start := &protocolv3.ObservationResponse{
		ThroughEventSequence: 0, LiveOwnerEpoch: 1, ThroughLiveRevision: 1,
		LiveControlOwnerEpoch: 1, ThroughLiveControlRevision: 0,
		Live: []*protocolv3.LiveEventProjection{{
			OwnerEpoch: 1, LiveRevision: 1, EventType: protocolv3.LiveEventType_TEXT_DELTA,
			SessionId: "session:1", TurnId: "turn:1", DraftIdentity: "entry:future",
			Payload:      textDeltaPayload("block:1", "draft"),
			ScopeKind:    protocolv3.ConversationScopeKind_ROOT,
			ChannelKind:  protocolv3.LiveChannelKind_LIVE_CHANNEL_MODEL_OUTPUT,
			GenerationId: "model-output:entry:future", ProposedEntryId: "entry:future",
			BlockId: "block:1", BlockKind: protocolv3.LiveBlockKind_LIVE_BLOCK_TEXT,
		}},
	}
	if err := model.applyObservation(start); err != nil {
		t.Fatal(err)
	}
	if model.live["entry:future"] == nil {
		t.Fatal("live draft was not installed")
	}
	committed := &protocolv3.ObservationResponse{
		ThroughEventSequence: 1, LiveOwnerEpoch: 1, ThroughLiveRevision: 1,
		LiveControlOwnerEpoch: 1, ThroughLiveControlRevision: 0,
		Committed: []*protocolv3.CommittedObservationProjection{{
			EventSequence: 1, EventId: "event:1", EventType: protocolv3.CommittedEventType_ASSISTANT_MESSAGE_ACCEPTED,
			ProjectionKind: protocolv3.ObservationProjectionKind_IMMUTABLE_ENTRY,
			SubjectSlot:    "subject_entry_id", SubjectId: "entry:future",
			Entry: assistantEntry("entry:future", 1, "final"),
		}},
	}
	if err := model.applyObservation(committed); err != nil {
		t.Fatal(err)
	}
	if model.live["entry:future"] != nil {
		t.Fatal("canonical entry did not retire matching live draft")
	}
	late := &protocolv3.ObservationResponse{
		ThroughEventSequence: 1, LiveOwnerEpoch: 1, ThroughLiveRevision: 2,
		LiveControlOwnerEpoch: 1, ThroughLiveControlRevision: 0,
		Live: []*protocolv3.LiveEventProjection{{
			OwnerEpoch: 1, LiveRevision: 2, EventType: protocolv3.LiveEventType_TEXT_DELTA,
			SessionId: "session:1", TurnId: "turn:1", DraftIdentity: "entry:future",
			Payload:      textDeltaPayload("block:1", "late"),
			ScopeKind:    protocolv3.ConversationScopeKind_ROOT,
			ChannelKind:  protocolv3.LiveChannelKind_LIVE_CHANNEL_MODEL_OUTPUT,
			GenerationId: "model-output:entry:future", ProposedEntryId: "entry:future",
			BlockId: "block:1", BlockKind: protocolv3.LiveBlockKind_LIVE_BLOCK_TEXT,
		}},
	}
	if err := model.applyObservation(late); err != nil {
		t.Fatal(err)
	}
	if model.live["entry:future"] != nil {
		t.Fatal("late live delta revived a canonical entry")
	}
}

func TestLiveAndControlFramesAreBoundToTheAuthenticatedSession(t *testing.T) {
	model := New(fakeService{})
	model.liveEpoch, model.controlEpoch = 1, 1
	foreignLive := &protocolv3.ObservationResponse{
		ThroughEventSequence: 0, LiveOwnerEpoch: 1, ThroughLiveRevision: 1,
		LiveControlOwnerEpoch: 1, ThroughLiveControlRevision: 0,
		Live: []*protocolv3.LiveEventProjection{{
			OwnerEpoch: 1, LiveRevision: 1, EventType: protocolv3.LiveEventType_TEXT_DELTA,
			SessionId: "session:foreign", TurnId: "turn:1", DraftIdentity: "entry:future",
			Payload:      textDeltaPayload("block:1", "leak"),
			ScopeKind:    protocolv3.ConversationScopeKind_ROOT,
			ChannelKind:  protocolv3.LiveChannelKind_LIVE_CHANNEL_MODEL_OUTPUT,
			GenerationId: "model-output:entry:future", ProposedEntryId: "entry:future",
			BlockId: "block:1", BlockKind: protocolv3.LiveBlockKind_LIVE_BLOCK_TEXT,
		}},
	}
	if err := model.applyObservation(foreignLive); err == nil {
		t.Fatal("cross-session live frame was accepted")
	}
	foreignControl := &protocolv3.LiveControlSnapshotResponse{
		Snapshot: &protocolv3.SessionLiveControlSnapshot{
			SessionId: "session:foreign", OwnerEpoch: 1,
		},
	}
	if err := model.installLiveControlSnapshot(foreignControl); err == nil {
		t.Fatal("cross-session control snapshot was accepted")
	}
}

func textDeltaPayload(blockID, delta string) *protocolv3.LiveEventPayload {
	return &protocolv3.LiveEventPayload{Payload: &protocolv3.LiveEventPayload_TextDelta{
		TextDelta: &protocolv3.LiveTextDeltaPayload{BlockIdentity: blockID, Delta: delta},
	}}
}

func TestHelloLiveSnapshotInstallsRetainedPrefixBeforeObservation(t *testing.T) {
	model := New(fakeService{})
	hello := &protocolv3.HelloAccepted{
		LiveOwnerEpoch: 7, LiveRevision: 2,
		LiveSnapshot: &protocolv3.LiveSnapshotProjection{
			OwnerEpoch: 7, RetainedFromRevision: 1, ThroughRevision: 2,
			Events: []*protocolv3.LiveEventProjection{
				{
					OwnerEpoch: 7, LiveRevision: 1, EventType: protocolv3.LiveEventType_TEXT_START,
					SessionId: "session:1", TurnId: "turn:1", DraftIdentity: "entry:future",
					Payload: &protocolv3.LiveEventPayload{Payload: &protocolv3.LiveEventPayload_TextStart{
						TextStart: &protocolv3.LiveTextStartPayload{BlockIdentity: "block:1"},
					}},
					ScopeKind: protocolv3.ConversationScopeKind_ROOT, ChannelKind: protocolv3.LiveChannelKind_LIVE_CHANNEL_MODEL_OUTPUT,
					GenerationId: "model-output:entry:future", ProposedEntryId: "entry:future",
					BlockId: "block:1", BlockKind: protocolv3.LiveBlockKind_LIVE_BLOCK_TEXT,
				},
				{
					OwnerEpoch: 7, LiveRevision: 2, EventType: protocolv3.LiveEventType_TEXT_DELTA,
					SessionId: "session:1", TurnId: "turn:1", DraftIdentity: "entry:future",
					Payload:   textDeltaPayload("block:1", "draft"),
					ScopeKind: protocolv3.ConversationScopeKind_ROOT, ChannelKind: protocolv3.LiveChannelKind_LIVE_CHANNEL_MODEL_OUTPUT,
					GenerationId: "model-output:entry:future", ProposedEntryId: "entry:future",
					BlockId: "block:1", BlockKind: protocolv3.LiveBlockKind_LIVE_BLOCK_TEXT,
				},
			},
		},
	}
	if err := model.installLiveSnapshot(hello); err != nil {
		t.Fatal(err)
	}
	if model.liveRevision != 2 || model.live["entry:future"] == nil || model.live["entry:future"].blocks["block:1"] != "draft" {
		t.Fatal("live bootstrap prefix was not installed")
	}
	forged := proto.Clone(hello).(*protocolv3.HelloAccepted)
	forged.LiveSnapshot.RetainedFromRevision = 2
	if err := model.installLiveSnapshot(forged); err == nil {
		t.Fatal("incomplete live bootstrap prefix was accepted")
	}
}

func TestContentIsExactOnlyAfterCompleteDigestVerification(t *testing.T) {
	model := New(fakeService{})
	full := []byte("complete content")
	if err := model.applyContent(contentMsg{
		entryID: "entry:1",
		value: &protocolv3.CanonicalContentChunk{
			Digest: digest(full), CompleteSize: uint64(len(full)), Content: full, Complete: true,
		},
	}); err != nil {
		t.Fatal(err)
	}
	state := model.content[contentKey("entry:1", "")]
	if !state.done || !state.verified || state.truncated {
		t.Fatal("complete content did not become verified")
	}
	forged := New(fakeService{})
	if err := forged.applyContent(contentMsg{
		entryID: "entry:2",
		value: &protocolv3.CanonicalContentChunk{
			Digest: digest([]byte("different")), CompleteSize: uint64(len(full)), Content: full, Complete: true,
		},
	}); err == nil {
		t.Fatal("forged terminal content digest was accepted")
	}
}

func TestCommandResponseLossKeepsOneStableCandidateForQuery(t *testing.T) {
	model := New(fakeService{})
	model.phase = phaseReady
	frozen := pendingCommand{id: "command:stable", text: "hello", status: protocolv3.CommandStatus_PENDING}
	model.pending = &frozen
	updated, _ := model.Update(commandMsg{err: errors.New("response lost"), frozen: frozen})
	result := updated.(Model)
	if result.phase != phaseReconnecting || result.pending == nil || result.pending.id != frozen.id {
		t.Fatal("ACK-unknown command candidate was discarded")
	}
	if string(result.draft) != "" {
		t.Fatal("ACK-unknown command was restored as a new draft")
	}
}

func TestEnterUsesExactSteerTargetWhileRootTurnIsActive(t *testing.T) {
	model := New(fakeService{})
	model.phase = phaseReady
	model.role = protocolv3.AttachmentRole_ATTACHMENT_ROLE_CONTROLLER
	model.height = 24
	model.draft = []rune("change direction")
	model.cursor = len(model.draft)
	model.control = &protocolv3.CanonicalControl{
		SessionLifecycle: "OPEN",
		ActiveTurns: []*protocolv3.ActiveTurnControl{{
			TurnId: "turn:active", ScopeKind: protocolv3.ConversationScopeKind_ROOT,
			Status: "RUNNING", AcceptedAtUtc: "2026-08-09T00:00:00Z",
		}},
	}

	updated, command := model.Update(tea.KeyPressMsg(tea.Key{Code: tea.KeyEnter}))
	result := updated.(Model)
	if command == nil || result.pending == nil {
		t.Fatal("active-turn Enter did not install a command")
	}
	if result.pending.target != "turn:active" {
		t.Fatalf("steer target = %q", result.pending.target)
	}
	message, ok := command().(commandMsg)
	if !ok || message.frozen.target != "turn:active" || message.frozen.text != "change direction" {
		t.Fatal("wire command lost the exact steer candidate")
	}
}

func TestEnterUsesNewTurnWhenNoRootTurnIsActive(t *testing.T) {
	model := New(fakeService{})
	model.phase = phaseReady
	model.role = protocolv3.AttachmentRole_ATTACHMENT_ROLE_CONTROLLER
	model.height = 24
	model.draft = []rune("next prompt")
	model.cursor = len(model.draft)
	model.control = &protocolv3.CanonicalControl{SessionLifecycle: "OPEN"}

	updated, command := model.Update(tea.KeyPressMsg(tea.Key{Code: tea.KeyEnter}))
	result := updated.(Model)
	if command == nil || result.pending == nil || result.pending.target != "" {
		t.Fatal("idle Enter did not install an untargeted prompt command")
	}
}

func TestToolInteractionUsesExactSnapshotAndLiveAuthority(t *testing.T) {
	service := &interactionService{}
	model := New(service)
	model.phase = phaseReady
	model.role = protocolv3.AttachmentRole_ATTACHMENT_ROLE_CONTROLLER
	model.height = 24
	model.writerGeneration, model.controlEpoch, model.controlLiveRevision = 9, 9, 3
	model.currentInteraction = &protocolv3.LiveInteractionView{
		InteractionId: "interaction:1", InteractionKind: "TOOL_CONFIRMATION",
		PublicPrompt: "Allow terminal?", PublicOptions: []string{"ALLOW", "DENY"},
		ExpiresAtUtc: "2026-08-09T00:10:00Z",
	}
	updated, command := model.Update(tea.KeyPressMsg(tea.Key{Code: tea.KeyExtended, Text: "y"}))
	result := updated.(Model)
	if command == nil || result.pending == nil || !result.pending.interaction {
		t.Fatal("interaction decision did not install a stable pending command")
	}
	message := command()
	updated, _ = result.Update(message)
	result = updated.(Model)
	if result.pending != nil {
		t.Fatal("accepted interaction command remained pending")
	}
	if service.interactionID != "interaction:1" || service.writerGeneration != 9 || service.ownerEpoch != 9 || service.revision != 3 || service.decision != protocolv3.InteractionResolutionDecision_INTERACTION_ALLOW {
		t.Fatal("interaction command lost its exact authority attribution")
	}
	if len(result.draft) != 0 || len(result.promptHistory) != 0 {
		t.Fatal("interaction decision polluted the prompt composer")
	}
}

func TestThreeGapKindsHaveSeparateRecoveryScopes(t *testing.T) {
	for _, test := range []struct {
		kind      protocolv3.ObservationGapKind
		wantPhase phase
		keepLive  bool
	}{
		{protocolv3.ObservationGapKind_COMMITTED_GAP, phaseLoading, true},
		{protocolv3.ObservationGapKind_LIVE_GAP, phaseReady, false},
		{protocolv3.ObservationGapKind_LIVE_CONTROL_GAP, phaseReady, true},
	} {
		model := New(fakeService{})
		model.phase = phaseReady
		model.liveEpoch, model.controlEpoch = 1, 1
		model.live["entry:draft"] = &liveDraft{entryID: "entry:draft"}
		model.currentInteraction = &protocolv3.LiveInteractionView{InteractionId: "interaction:1"}
		updated, _ := model.Update(observationMsg{value: &protocolv3.ObservationResponse{
			ThroughEventSequence: 0, LiveOwnerEpoch: 1, LiveControlOwnerEpoch: 1,
			Gap: &protocolv3.ObservationGap{Kind: test.kind, LatestSequence: 4},
		}})
		result := updated.(Model)
		if result.phase != test.wantPhase {
			t.Fatalf("GAP %s phase = %v", test.kind, result.phase)
		}
		if (result.live["entry:draft"] != nil) != test.keepLive {
			t.Fatalf("GAP %s used the wrong live recovery scope", test.kind)
		}
		if test.kind == protocolv3.ObservationGapKind_LIVE_CONTROL_GAP && result.currentInteraction != nil {
			t.Fatal("live-control GAP retained stale interaction")
		}
	}
}

func TestPythonSnapshotFixtureCrossesTheGeneratedGoBoundary(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "tests", "fixtures", "stage2_protocol_v3_cross_language.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fixture struct {
		SchemaFingerprint   string `json:"schema_fingerprint"`
		SnapshotHex         string `json:"snapshot_protobuf_hex"`
		SnapshotFingerprint string `json:"snapshot_fingerprint"`
	}
	if err := json.Unmarshal(raw, &fixture); err != nil {
		t.Fatal(err)
	}
	if fixture.SchemaFingerprint != kernelclient.SchemaFingerprint {
		t.Fatal("Python/Go Protocol v3 schema identity drifted")
	}
	payload, err := hex.DecodeString(fixture.SnapshotHex)
	if err != nil {
		t.Fatal(err)
	}
	value := &protocolv3.CanonicalSessionSnapshot{}
	if err := proto.Unmarshal(payload, value); err != nil {
		t.Fatal(err)
	}
	if canonicalSnapshotFingerprint(value) != fixture.SnapshotFingerprint || value.SnapshotFingerprint != fixture.SnapshotFingerprint {
		t.Fatal("Python snapshot failed Go canonical fingerprint validation")
	}
	model := New(fakeService{session: value.SessionId})
	if err := model.installSnapshot(&protocolv3.SnapshotResponse{RequestId: "request:fixture", Snapshot: value}); err != nil {
		t.Fatal(err)
	}
}

func TestScopedCanonicalCacheIsBoundedAndRootViewDoesNotLeakTaskTranscript(t *testing.T) {
	model := New(fakeService{})
	root := userEntry("entry:root", 1, "root text")
	task := userEntry("entry:task", 2, "private task text")
	task.ScopeKind = protocolv3.ConversationScopeKind_SUBAGENT_TASK
	task.ScopeSubagentTaskId = "task:1"
	if err := model.installSnapshot(snapshot(root, task)); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(strings.Join(model.transcriptRows(80), "\n"), "private task text") {
		t.Fatal("default ROOT transcript leaked task-scoped content")
	}
	for sequence := uint64(3); sequence <= uint64(maximumCachedEntries); sequence++ {
		if err := model.installEntry(userEntry(fmt.Sprintf("entry:%d", sequence), sequence, "x")); err != nil {
			t.Fatalf("entry %d rejected before hard cap: %v", sequence, err)
		}
	}
	if err := model.installEntry(userEntry("entry:overflow", uint64(maximumCachedEntries+1), "x")); err == nil {
		t.Fatal("canonical cache accepted an entry beyond its hard cap")
	}
}
