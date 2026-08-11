package kernelapp

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"

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

type queuedPromptInteractionService struct {
	interactionService
}

func (*queuedPromptInteractionService) Command(_ context.Context, commandID string, _ protocolv3.CommandKind, _ string, _ string) (*protocolv3.CommandOutcome, error) {
	return &protocolv3.CommandOutcome{
		RequestId: "request:prompt", CommandId: commandID,
		Status: protocolv3.CommandStatus_PENDING, TargetId: "queue:item",
		PublicCode: "PROMPT_QUEUED", PublicMessage: "Queued.",
	}, nil
}

func (*queuedPromptInteractionService) QueryCommand(_ context.Context, commandID string) (*protocolv3.QueryCommandResponse, error) {
	return &protocolv3.QueryCommandResponse{
		RequestId: "request:query", Found: true,
		Outcome: &protocolv3.CommandOutcome{
			CommandId: commandID, Status: protocolv3.CommandStatus_PENDING,
			TargetId: "turn:active", PublicCode: "TURN_RUNNING",
			PublicMessage: "The turn is running.",
		},
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
	if protocolv3.ProjectionContractCount() != 27 {
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

func TestLiveToolCallRendersBoundedUTF8SafeArguments(t *testing.T) {
	model := New(fakeService{})
	start := liveToolCallEvent(1, protocolv3.LiveEventType_TOOL_CALL_START,
		&protocolv3.LiveEventPayload{Payload: &protocolv3.LiveEventPayload_ToolCallStart{
			ToolCallStart: &protocolv3.LiveToolCallStartPayload{
				BlockIdentity: "block:tool", ToolCallId: "call:1", ToolName: "search",
			},
		}})
	if err := model.applyLive(start); err != nil {
		t.Fatal(err)
	}
	delta := liveToolCallEvent(2, protocolv3.LiveEventType_TOOL_CALL_DELTA,
		&protocolv3.LiveEventPayload{Payload: &protocolv3.LiveEventPayload_ToolCallDelta{
			ToolCallDelta: &protocolv3.LiveToolCallDeltaPayload{
				BlockIdentity: "block:tool", ToolCallId: "call:1", Delta: `{"q":"中文"}`,
			},
		}})
	if err := model.applyLive(delta); err != nil {
		t.Fatal(err)
	}
	if got := model.live["entry:tool"].blocks["block:tool"]; got != `search({"q":"中文"})` {
		t.Fatalf("live tool delta = %q", got)
	}

	arguments := `{"q":"` + strings.Repeat("界", maximumLiveToolArgumentBytes) + `"}`
	end := liveToolCallEvent(3, protocolv3.LiveEventType_TOOL_CALL_END,
		&protocolv3.LiveEventPayload{Payload: &protocolv3.LiveEventPayload_ToolCallEnd{
			ToolCallEnd: &protocolv3.LiveToolCallEndPayload{
				BlockIdentity: "block:tool", ToolCallId: "call:1", ToolName: "search",
				ArgumentsJson: arguments, Utf8Bytes: uint64(len([]byte(arguments))), Digest: digest([]byte(arguments)),
			},
		}})
	if err := model.applyLive(end); err != nil {
		t.Fatal(err)
	}
	got := model.live["entry:tool"].blocks["block:tool"]
	if !utf8.ValidString(got) || !strings.Contains(got, liveToolArgumentsTruncated) || len(got) >= len(arguments) {
		t.Fatalf("bounded live tool preview is invalid: bytes=%d", len(got))
	}
}

func liveToolCallEvent(revision uint64, kind protocolv3.LiveEventType, payload *protocolv3.LiveEventPayload) *protocolv3.LiveEventProjection {
	return &protocolv3.LiveEventProjection{
		OwnerEpoch: 1, LiveRevision: revision, EventType: kind,
		SessionId: "session:1", TurnId: "turn:1", DraftIdentity: "entry:tool",
		Payload: payload, ScopeKind: protocolv3.ConversationScopeKind_ROOT,
		ChannelKind:  protocolv3.LiveChannelKind_LIVE_CHANNEL_MODEL_OUTPUT,
		GenerationId: "model-output:entry:tool", ProposedEntryId: "entry:tool",
		BlockId: "block:tool", BlockKind: protocolv3.LiveBlockKind_LIVE_BLOCK_TOOL_CALL,
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
	model.entries["entry:1"] = &protocolv3.CanonicalEntry{
		EntryId: "entry:1", Content: &protocolv3.CanonicalContentReference{
			Kind: protocolv3.ContentKind_CANONICAL_BLOB, Digest: digest(full),
			Size: uint64(len(full)), MediaType: "text/plain", Codec: "utf-8",
		},
	}
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
	forged.entries["entry:2"] = &protocolv3.CanonicalEntry{
		EntryId: "entry:2", Content: &protocolv3.CanonicalContentReference{
			Kind: protocolv3.ContentKind_CANONICAL_BLOB, Digest: digest(full),
			Size: uint64(len(full)), MediaType: "text/plain", Codec: "utf-8",
		},
	}
	if err := forged.applyContent(contentMsg{
		entryID: "entry:2",
		value: &protocolv3.CanonicalContentChunk{
			Digest: digest([]byte("different")), CompleteSize: uint64(len(full)), Content: full, Complete: true,
		},
	}); err == nil {
		t.Fatal("forged terminal content digest was accepted")
	}

	unavailable := New(fakeService{})
	unavailable.entries["entry:3"] = &protocolv3.CanonicalEntry{
		EntryId: "entry:3", Content: &protocolv3.CanonicalContentReference{
			Kind: protocolv3.ContentKind_CANONICAL_BLOB, Digest: digest(full),
			Size: uint64(len(full)), MediaType: "text/plain", Codec: "utf-8",
		},
	}
	updated, _ := unavailable.Update(contentMsg{
		entryID: "entry:3", err: &kernelclient.ProtocolError{
			StableCode: "CONTENT_BLOB_CORRUPT", PublicMessage: "Unavailable.",
		},
	})
	unavailable = updated.(Model)
	state = unavailable.content[contentKey("entry:3", "")]
	if state == nil || !state.done || !state.unavailable || unavailable.phase == phaseFatal {
		t.Fatal("request-local content corruption did not become a stable placeholder")
	}
}

func TestMaximumCanonicalBlobHydratesByRangeAndRendersExactly(t *testing.T) {
	full := append(bytes.Repeat([]byte("a"), maximumHydratedBytes-3), []byte("界")...)
	model := New(fakeService{})
	model.entries["entry:max"] = &protocolv3.CanonicalEntry{
		EntryId: "entry:max", Content: &protocolv3.CanonicalContentReference{
			Kind: protocolv3.ContentKind_CANONICAL_BLOB, Digest: digest(full),
			Size: uint64(len(full)), MediaType: "text/plain", Codec: "utf-8",
		},
	}
	const chunkBytes = 1 << 20
	for offset := 0; offset < len(full); offset += chunkBytes {
		end := min(len(full), offset+chunkBytes)
		if err := model.applyContent(contentMsg{
			entryID: "entry:max",
			value: &protocolv3.CanonicalContentChunk{
				Digest: digest(full), CompleteSize: uint64(len(full)),
				OffsetBytes: uint64(offset), Content: full[offset:end],
				Complete: end == len(full),
			},
		}); err != nil {
			t.Fatalf("range %d rejected: %v", offset, err)
		}
	}
	state := model.content[contentKey("entry:max", "")]
	if state == nil || !state.done || !state.verified || state.unavailable || state.truncated {
		t.Fatal("maximum legal canonical blob did not become exact")
	}
	if got := model.contentText("entry:max", "", model.entries["entry:max"].Content); got != string(full) {
		t.Fatalf("maximum legal canonical blob render drifted: bytes=%d", len(got))
	}
}

func TestFinalCanonicalBlobDigestFailureIsContentLocal(t *testing.T) {
	full := []byte("canonical")
	model := New(fakeService{})
	model.phase = phaseReady
	model.entries["entry:corrupt"] = &protocolv3.CanonicalEntry{
		EntryId: "entry:corrupt", Content: &protocolv3.CanonicalContentReference{
			Kind: protocolv3.ContentKind_CANONICAL_BLOB, Digest: digest(full),
			Size: uint64(len(full)), MediaType: "text/plain", Codec: "utf-8",
		},
	}
	updated, _ := model.Update(contentMsg{
		entryID: "entry:corrupt",
		value: &protocolv3.CanonicalContentChunk{
			Digest: digest(full), CompleteSize: uint64(len(full)),
			Content: []byte("corrupted"), Complete: true,
		},
	})
	model = updated.(Model)
	state := model.content[contentKey("entry:corrupt", "")]
	if model.phase == phaseFatal || state == nil || !state.done || !state.unavailable {
		t.Fatal("final digest mismatch escaped the request-local content boundary")
	}
}

func TestCommandResponseLossKeepsOneStableCandidateForQuery(t *testing.T) {
	model := New(fakeService{})
	model.phase = phaseReady
	frozen := pendingCommand{id: "command:stable", kind: protocolv3.CommandKind_SUBMIT_PROMPT, text: "hello", status: protocolv3.CommandStatus_PENDING}
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

func TestACKUnknownPromptMovesToBackgroundTrackingAfterCanonicalQuery(t *testing.T) {
	model := New(fakeService{})
	model.phase = phaseReady
	frozen := pendingCommand{id: "command:stable", kind: protocolv3.CommandKind_SUBMIT_PROMPT, text: "hello", status: protocolv3.CommandStatus_PENDING}
	model.pending = &frozen
	updated, next := model.Update(queryMsg{
		commandID: frozen.id,
		value: &protocolv3.QueryCommandResponse{
			Found: true,
			Outcome: &protocolv3.CommandOutcome{
				CommandId: frozen.id, Status: protocolv3.CommandStatus_PENDING,
				TargetId: "queue:item", PublicCode: "PROMPT_QUEUED",
				PublicMessage: "Queued.",
			},
		},
	})
	result := updated.(Model)
	if next == nil || result.pending != nil || result.trackedPrompts[frozen.id].id != frozen.id {
		t.Fatal("canonical prompt winner did not release the mutation slot")
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

func TestQueuedPromptDoesNotBlockItsOwnToolInteraction(t *testing.T) {
	service := &queuedPromptInteractionService{}
	model := New(service)
	model.phase = phaseReady
	model.role = protocolv3.AttachmentRole_ATTACHMENT_ROLE_CONTROLLER
	model.height = 24
	model.writerGeneration, model.controlEpoch, model.controlLiveRevision = 9, 9, 3
	model.draft = []rune("use the terminal")
	model.cursor = len(model.draft)

	updated, submit := model.Update(tea.KeyPressMsg(tea.Key{Code: tea.KeyEnter}))
	result := updated.(Model)
	promptID := result.pending.id
	updated, query := result.Update(submit())
	result = updated.(Model)
	if query == nil || result.pending != nil || result.trackedPrompts[promptID].id != promptID {
		t.Fatal("durably queued prompt retained the interactive mutation slot")
	}

	result.currentInteraction = &protocolv3.LiveInteractionView{
		InteractionId: "interaction:prompt-tool", InteractionKind: "TOOL_CONFIRMATION",
		PublicPrompt: "Allow terminal?", PublicOptions: []string{"ALLOW", "DENY"},
		ExpiresAtUtc: "2026-08-09T00:10:00Z",
	}
	updated, resolve := result.Update(tea.KeyPressMsg(tea.Key{Code: tea.KeyExtended, Text: "y"}))
	result = updated.(Model)
	if resolve == nil || result.pending == nil || !result.pending.interaction {
		t.Fatal("queued prompt blocked its own tool confirmation")
	}
	updated, _ = result.Update(resolve())
	result = updated.(Model)
	if result.pending != nil || service.interactionID != "interaction:prompt-tool" {
		t.Fatal("tool confirmation did not complete while prompt outcome was tracked")
	}

	updated, _ = result.Update(queryMsg{
		commandID: promptID,
		value: &protocolv3.QueryCommandResponse{
			Found: true,
			Outcome: &protocolv3.CommandOutcome{
				CommandId: promptID, Status: protocolv3.CommandStatus_PENDING,
				TargetId: "turn:active", PublicCode: "TURN_RUNNING",
				PublicMessage: "The turn is running.",
			},
		},
	})
	result = updated.(Model)
	if _, exists := result.trackedPrompts[promptID]; exists {
		t.Fatal("canonical turn ingress remained in background tracking")
	}
	if len(result.promptHistory) != 1 || result.promptHistory[0] != "use the terminal" {
		t.Fatal("accepted prompt history was not finalized at canonical ingress")
	}
}

func TestTrackedPromptDoesNotBlockSteerStopOrDetach(t *testing.T) {
	for _, test := range []struct {
		name string
		key  tea.Key
		kind protocolv3.CommandKind
	}{
		{"steer", tea.Key{Code: tea.KeyEnter}, protocolv3.CommandKind_STEER_ACTIVE_TURN},
		{"stop", tea.Key{Code: 'c', Mod: tea.ModCtrl}, protocolv3.CommandKind_STOP_ACTIVE_TURN},
		{"detach", tea.Key{Code: 'd', Mod: tea.ModCtrl}, protocolv3.CommandKind_DETACH},
	} {
		t.Run(test.name, func(t *testing.T) {
			model := New(fakeService{})
			model.phase = phaseReady
			model.role = protocolv3.AttachmentRole_ATTACHMENT_ROLE_CONTROLLER
			model.height = 24
			model.trackedPrompts["command:original"] = pendingCommand{
				id: "command:original", kind: protocolv3.CommandKind_SUBMIT_PROMPT,
				text: "original",
			}
			model.control = &protocolv3.CanonicalControl{ActiveTurns: []*protocolv3.ActiveTurnControl{{
				TurnId: "turn:active", ScopeKind: protocolv3.ConversationScopeKind_ROOT,
				Status: "RUNNING", AcceptedAtUtc: "2026-08-09T00:00:00Z",
			}}}
			if test.kind == protocolv3.CommandKind_STEER_ACTIVE_TURN {
				model.draft = []rune("change direction")
				model.cursor = len(model.draft)
			}
			updated, command := model.Update(tea.KeyPressMsg(test.key))
			result := updated.(Model)
			if command == nil || result.pending == nil || result.pending.kind != test.kind {
				t.Fatalf("tracked prompt blocked %s", test.name)
			}
		})
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
