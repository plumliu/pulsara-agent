package commandstate

import (
	"errors"
	"fmt"
	"unicode/utf8"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/publictext"
)

type OutcomeStatus uint8

const (
	OutcomeSucceeded OutcomeStatus = iota + 1
	OutcomeRejected
	OutcomePendingConfirmation
	OutcomeReconciliationRequired
	OutcomeCompatibleWinner
)

type Outcome struct {
	RequestID           string
	Status              OutcomeStatus
	CommandID           string
	TargetID            string
	TargetGeneration    uint64
	PublicResultCode    string
	PublicResultText    string
	DurableReferenceIDs []string
	QueryToken          string
	Fingerprint         string
}

func (o Outcome) semanticStatus() string {
	switch o.Status {
	case OutcomeSucceeded:
		return "succeeded"
	case OutcomeRejected:
		return "rejected"
	case OutcomePendingConfirmation:
		return "pending_confirmation"
	case OutcomeReconciliationRequired:
		return "reconciliation_required"
	case OutcomeCompatibleWinner:
		return "superseded_by_compatible_winner"
	default:
		return ""
	}
}

func (o Outcome) Validate() error {
	if o.RequestID == "" || o.semanticStatus() == "" || o.CommandID == "" || o.TargetID == "" || o.TargetGeneration == 0 || o.PublicResultCode == "" || o.QueryToken == "" || !utf8.ValidString(o.PublicResultText) || !publictext.IsSafe(o.PublicResultText) || len([]rune(o.PublicResultText)) > 512 || len([]byte(o.PublicResultText)) > 2048 || len(o.DurableReferenceIDs) > 64 || o.Fingerprint == "" {
		return errors.New("terminal command outcome is malformed")
	}
	for _, item := range o.DurableReferenceIDs {
		if item == "" {
			return errors.New("terminal command durable reference is empty")
		}
	}
	expected, err := protocolvalue.CanonicalClientFingerprint("terminal-command-outcome:v1", map[string]any{
		"command_id":            o.CommandID,
		"durable_reference_ids": o.DurableReferenceIDs,
		"public_result_code":    o.PublicResultCode,
		"public_result_text":    o.PublicResultText,
		"query_token":           o.QueryToken,
		"status":                o.semanticStatus(),
		"target_generation":     o.TargetGeneration,
		"target_id":             o.TargetID,
	})
	if err != nil {
		return errors.New("terminal command outcome fingerprint could not be recomputed")
	}
	if expected != o.Fingerprint {
		return fmt.Errorf("terminal command outcome fingerprint mismatch: expected %s, received %s", expected, o.Fingerprint)
	}
	return nil
}

func OutcomeFromProto(value *protocol.CommandOutcome) (Outcome, error) {
	if value == nil {
		return Outcome{}, errors.New("terminal command outcome is absent")
	}
	status := map[protocol.CommandOutcomeStatus]OutcomeStatus{
		protocol.CommandOutcomeStatus_SUCCEEDED:                       OutcomeSucceeded,
		protocol.CommandOutcomeStatus_REJECTED:                        OutcomeRejected,
		protocol.CommandOutcomeStatus_PENDING_CONFIRMATION:            OutcomePendingConfirmation,
		protocol.CommandOutcomeStatus_RECONCILIATION_REQUIRED:         OutcomeReconciliationRequired,
		protocol.CommandOutcomeStatus_SUPERSEDED_BY_COMPATIBLE_WINNER: OutcomeCompatibleWinner,
	}[value.OutcomeStatus]
	// Protobuf decodes an empty repeated field as nil, while Protocol canonical
	// JSON owns the semantic value as an empty array. Normalize before
	// recomputing the cross-language fingerprint.
	result := Outcome{RequestID: value.RequestId, Status: status, CommandID: value.CommandId, TargetID: value.TargetId, TargetGeneration: value.TargetGeneration, PublicResultCode: value.PublicResultCode, PublicResultText: value.PublicResultText, DurableReferenceIDs: append([]string{}, value.DurableReferenceIds...), QueryToken: value.QueryToken, Fingerprint: value.OutcomeFingerprint}
	return result, result.Validate()
}

type QueryResult struct {
	RequestID string
	Found     bool
	Outcome   Outcome
}

func QueryResultFromProto(value *protocol.QueryCommandResponse) (QueryResult, error) {
	if value == nil || value.RequestId == "" || value.Found != (value.Outcome != nil) {
		return QueryResult{}, errors.New("terminal command query response matrix is invalid")
	}
	result := QueryResult{RequestID: value.RequestId, Found: value.Found}
	if value.Outcome != nil {
		outcome, err := OutcomeFromProto(value.Outcome)
		if err != nil || outcome.RequestID != value.RequestId {
			return QueryResult{}, errors.New("terminal command query outcome is invalid")
		}
		result.Outcome = outcome
	}
	return result, nil
}
