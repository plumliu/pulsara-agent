package commandstate

import (
	"errors"
	"fmt"
	"strings"
	"unicode/utf8"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

type Kind uint8

const (
	SubmitPrompt Kind = iota + 1
	StopRun
)

type DeliveryMode uint8

const (
	DeliveryAuto DeliveryMode = iota + 1
	DeliverySteer
	DeliveryFollowUp
)

func (m DeliveryMode) semanticValue() (string, error) {
	switch m {
	case DeliveryAuto:
		return "auto", nil
	case DeliverySteer:
		return "steer", nil
	case DeliveryFollowUp:
		return "follow_up", nil
	default:
		return "", errors.New("terminal delivery mode is unknown")
	}
}

func (m DeliveryMode) wireValue() protocol.SubmitPromptCommand_DeliveryMode {
	switch m {
	case DeliveryAuto:
		return protocol.SubmitPromptCommand_AUTO
	case DeliverySteer:
		return protocol.SubmitPromptCommand_STEER
	case DeliveryFollowUp:
		return protocol.SubmitPromptCommand_FOLLOW_UP
	default:
		return protocol.SubmitPromptCommand_DELIVERY_MODE_UNSPECIFIED
	}
}

type Binding struct {
	ClientInstanceID             string
	AttachmentID                 string
	AttachmentGeneration         uint64
	CommandID                    string
	RuntimeSessionID             string
	ExpectedTargetID             string
	ExpectedTargetGeneration     uint64
	ExpectedControllerGeneration uint64
	RequestSemanticFingerprint   string
}

func (b Binding) Validate() error {
	if b.ClientInstanceID == "" || b.AttachmentID == "" || b.AttachmentGeneration == 0 ||
		b.CommandID == "" || b.RuntimeSessionID == "" || b.ExpectedTargetID == "" ||
		b.ExpectedTargetGeneration == 0 || b.ExpectedControllerGeneration == 0 ||
		b.RequestSemanticFingerprint == "" {
		return errors.New("terminal command binding is incomplete")
	}
	return nil
}

func (b Binding) toProto() *protocol.CommandBinding {
	return &protocol.CommandBinding{
		ClientInstanceId:             b.ClientInstanceID,
		AttachmentId:                 b.AttachmentID,
		AttachmentGeneration:         b.AttachmentGeneration,
		CommandId:                    b.CommandID,
		RuntimeSessionId:             b.RuntimeSessionID,
		ExpectedTargetId:             b.ExpectedTargetID,
		ExpectedTargetGeneration:     b.ExpectedTargetGeneration,
		ExpectedControllerGeneration: b.ExpectedControllerGeneration,
		RequestSemanticFingerprint:   b.RequestSemanticFingerprint,
	}
}

// Candidate is the immutable semantic command. Physical request IDs are
// deliberately absent and are added only by ToProto/QueryToProto.
type Candidate struct {
	kind                       Kind
	binding                    Binding
	clientSubmissionID         string
	text                       string
	deliveryMode               DeliveryMode
	reason                     string
	composerRevision           uint64
	composerContentFingerprint string
	candidateFingerprint       string
}

type CandidateInput struct {
	ClientInstanceID             string
	AttachmentID                 string
	AttachmentGeneration         uint64
	RuntimeSessionID             string
	ExpectedTargetID             string
	ExpectedTargetGeneration     uint64
	ExpectedControllerGeneration uint64
	CandidateOrdinal             uint64
	ComposerRevision             uint64
	ComposerContentFingerprint   string
	Text                         string
	DeliveryMode                 DeliveryMode
}

func NewSubmitCandidate(input CandidateInput) (Candidate, error) {
	if input.CandidateOrdinal == 0 || input.ComposerRevision == 0 || input.ComposerContentFingerprint == "" ||
		input.Text == "" || !utf8.ValidString(input.Text) || len([]byte(input.Text)) > 1024*1024+32*1024 {
		return Candidate{}, errors.New("terminal submit candidate input is invalid")
	}
	mode, err := input.DeliveryMode.semanticValue()
	if err != nil {
		return Candidate{}, err
	}
	seed := map[string]any{
		"attachment_generation":          input.AttachmentGeneration,
		"attachment_id":                  input.AttachmentID,
		"candidate_ordinal":              input.CandidateOrdinal,
		"client_instance_id":             input.ClientInstanceID,
		"composer_content_fingerprint":   input.ComposerContentFingerprint,
		"composer_revision":              input.ComposerRevision,
		"expected_controller_generation": input.ExpectedControllerGeneration,
		"expected_target_generation":     input.ExpectedTargetGeneration,
		"expected_target_id":             input.ExpectedTargetID,
		"kind":                           "submit_prompt",
		"runtime_session_id":             input.RuntimeSessionID,
	}
	seedFingerprint, err := protocolvalue.CanonicalClientFingerprint("terminal-command-client-candidate-seed:v1", seed)
	if err != nil {
		return Candidate{}, err
	}
	commandID := "terminal-command:" + strings.TrimPrefix(seedFingerprint, "sha256:")
	submissionFingerprint, err := protocolvalue.CanonicalClientFingerprint("terminal-client-submission-id:v1", map[string]any{
		"command_id":                   commandID,
		"composer_content_fingerprint": input.ComposerContentFingerprint,
		"composer_revision":            input.ComposerRevision,
	})
	if err != nil {
		return Candidate{}, err
	}
	submissionID := "terminal-submission:" + strings.TrimPrefix(submissionFingerprint, "sha256:")
	binding := Binding{
		ClientInstanceID: input.ClientInstanceID, AttachmentID: input.AttachmentID,
		AttachmentGeneration: input.AttachmentGeneration, CommandID: commandID,
		RuntimeSessionID: input.RuntimeSessionID, ExpectedTargetID: input.ExpectedTargetID,
		ExpectedTargetGeneration:     input.ExpectedTargetGeneration,
		ExpectedControllerGeneration: input.ExpectedControllerGeneration,
	}
	requestFingerprint, err := requestSemanticFingerprint("submit_prompt", binding, map[string]any{
		"client_submission_id":    submissionID,
		"command_kind":            "submit_prompt",
		"requested_delivery_mode": mode,
		"text":                    input.Text,
	})
	if err != nil {
		return Candidate{}, err
	}
	binding.RequestSemanticFingerprint = requestFingerprint
	value := Candidate{kind: SubmitPrompt, binding: binding, clientSubmissionID: submissionID, text: input.Text, deliveryMode: input.DeliveryMode, composerRevision: input.ComposerRevision, composerContentFingerprint: input.ComposerContentFingerprint}
	value.candidateFingerprint, err = value.expectedFingerprint()
	if err != nil {
		return Candidate{}, err
	}
	return value, value.Validate()
}

func NewStopCandidate(input CandidateInput) (Candidate, error) {
	if input.CandidateOrdinal == 0 {
		return Candidate{}, errors.New("terminal stop candidate ordinal is zero")
	}
	seedFingerprint, err := protocolvalue.CanonicalClientFingerprint("terminal-command-client-candidate-seed:v1", map[string]any{
		"attachment_generation":          input.AttachmentGeneration,
		"attachment_id":                  input.AttachmentID,
		"candidate_ordinal":              input.CandidateOrdinal,
		"client_instance_id":             input.ClientInstanceID,
		"expected_controller_generation": input.ExpectedControllerGeneration,
		"expected_target_generation":     input.ExpectedTargetGeneration,
		"expected_target_id":             input.ExpectedTargetID,
		"kind":                           "stop_run",
		"runtime_session_id":             input.RuntimeSessionID,
	})
	if err != nil {
		return Candidate{}, err
	}
	binding := Binding{ClientInstanceID: input.ClientInstanceID, AttachmentID: input.AttachmentID, AttachmentGeneration: input.AttachmentGeneration, CommandID: "terminal-command:" + strings.TrimPrefix(seedFingerprint, "sha256:"), RuntimeSessionID: input.RuntimeSessionID, ExpectedTargetID: input.ExpectedTargetID, ExpectedTargetGeneration: input.ExpectedTargetGeneration, ExpectedControllerGeneration: input.ExpectedControllerGeneration}
	binding.RequestSemanticFingerprint, err = requestSemanticFingerprint("stop_run", binding, map[string]any{"command_kind": "stop_run", "reason": "user_stop"})
	if err != nil {
		return Candidate{}, err
	}
	value := Candidate{kind: StopRun, binding: binding, reason: "user_stop"}
	value.candidateFingerprint, err = value.expectedFingerprint()
	if err != nil {
		return Candidate{}, err
	}
	return value, value.Validate()
}

func requestSemanticFingerprint(kind string, binding Binding, payload map[string]any) (string, error) {
	return protocolvalue.CanonicalClientFingerprint("terminal-command-request:"+kind+":v1", map[string]any{
		"binding": map[string]any{
			"attachment_generation":          binding.AttachmentGeneration,
			"attachment_id":                  binding.AttachmentID,
			"client_instance_id":             binding.ClientInstanceID,
			"command_id":                     binding.CommandID,
			"expected_controller_generation": binding.ExpectedControllerGeneration,
			"expected_target_generation":     binding.ExpectedTargetGeneration,
			"expected_target_id":             binding.ExpectedTargetID,
			"runtime_session_id":             binding.RuntimeSessionID,
		},
		"payload": payload,
	})
}

func (c Candidate) expectedFingerprint() (string, error) {
	return protocolvalue.CanonicalClientFingerprint("terminal-command-client-candidate:v1", map[string]any{
		"binding_semantic_fingerprint": c.binding.RequestSemanticFingerprint,
		"client_submission_id":         c.clientSubmissionID,
		"command_id":                   c.binding.CommandID,
		"composer_content_fingerprint": c.composerContentFingerprint,
		"composer_revision":            c.composerRevision,
		"delivery_mode":                c.deliveryMode,
		"kind":                         c.kind,
		"reason":                       c.reason,
		"text":                         c.text,
	})
}

func (c Candidate) Validate() error {
	if c.kind < SubmitPrompt || c.kind > StopRun || c.binding.Validate() != nil || c.candidateFingerprint == "" {
		return errors.New("terminal command candidate is invalid")
	}
	switch c.kind {
	case SubmitPrompt:
		if c.clientSubmissionID == "" || c.text == "" || c.deliveryMode.wireValue() == protocol.SubmitPromptCommand_DELIVERY_MODE_UNSPECIFIED || c.reason != "" || c.composerRevision == 0 || c.composerContentFingerprint == "" {
			return errors.New("terminal submit candidate matrix is invalid")
		}
	case StopRun:
		if c.reason != "user_stop" || c.clientSubmissionID != "" || c.text != "" || c.deliveryMode != 0 || c.composerRevision != 0 || c.composerContentFingerprint != "" {
			return errors.New("terminal stop candidate matrix is invalid")
		}
	}
	expected, err := c.expectedFingerprint()
	if err != nil || expected != c.candidateFingerprint {
		return errors.New("terminal command candidate fingerprint mismatch")
	}
	return nil
}

func (c Candidate) ToProto(requestID string) (*protocol.MutationCommand, error) {
	if requestID == "" || c.Validate() != nil {
		return nil, errors.New("terminal command candidate cannot be lowered")
	}
	result := &protocol.MutationCommand{RequestId: requestID}
	switch c.kind {
	case SubmitPrompt:
		result.Command = &protocol.MutationCommand_SubmitPrompt{SubmitPrompt: &protocol.SubmitPromptCommand{Binding: c.binding.toProto(), ClientSubmissionId: c.clientSubmissionID, Text: c.text, RequestedDeliveryMode: c.deliveryMode.wireValue()}}
	case StopRun:
		result.Command = &protocol.MutationCommand_StopRun{StopRun: &protocol.StopRunCommand{Binding: c.binding.toProto()}}
	default:
		return nil, errors.New("terminal command kind cannot be lowered")
	}
	return result, nil
}

func (c Candidate) QueryToProto(requestID string) (*protocol.QueryCommandRequest, error) {
	if requestID == "" || c.Validate() != nil {
		return nil, errors.New("terminal command query cannot be lowered")
	}
	return &protocol.QueryCommandRequest{RequestId: requestID, RuntimeSessionId: c.binding.RuntimeSessionID, OriginalClientInstanceId: c.binding.ClientInstanceID, CommandId: c.binding.CommandID}, nil
}

func (c Candidate) ValidateMutationPayloadBound(maximum uint32) error {
	request, err := c.ToProto(protocol.CanonicalWireRequestIDForPayloadSizing())
	if err != nil {
		return err
	}
	frame := &protocol.ClientFrame{Request: &protocol.ClientFrame_Mutation{Mutation: request}}
	_, err = protocol.MarshalBoundedDeterministicPayload(frame, maximum)
	return err
}

func (c Candidate) ID() string                         { return c.binding.CommandID }
func (c Candidate) Kind() Kind                         { return c.kind }
func (c Candidate) Binding() Binding                   { return c.binding }
func (c Candidate) Fingerprint() string                { return c.candidateFingerprint }
func (c Candidate) ComposerRevision() uint64           { return c.composerRevision }
func (c Candidate) ComposerContentFingerprint() string { return c.composerContentFingerprint }
func (c Candidate) Text() string                       { return c.text }
func (c Candidate) ClientSubmissionID() string         { return c.clientSubmissionID }
func (c Candidate) String() string                     { return fmt.Sprintf("command(%s)", c.binding.CommandID) }
