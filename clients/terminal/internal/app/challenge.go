package app

import (
	"errors"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

type AttachmentChallengeHandlePhase uint8

const (
	AttachmentChallengeNone AttachmentChallengeHandlePhase = iota + 1
	AttachmentChallengePreparedPromotionPending
	AttachmentChallengeActiveAcceptancePending
	AttachmentChallengeActive
	AttachmentChallengeConsumed
	AttachmentChallengeRevoked
)

type PreparedAttachmentChallengeHandleIdentity struct {
	HandleID                    string
	HandleGeneration            uint64
	HelloOperationID            string
	HelloOperationGeneration    uint64
	ValidatedReceiptFingerprint string
	CandidateFingerprint        string
	ConnectionID                string
	ChallengeCommitment         string
	HandleFingerprint           string
}

func (v PreparedAttachmentChallengeHandleIdentity) Validate() error {
	if v.HandleID == "" || v.HandleGeneration == 0 || v.HelloOperationID == "" || v.HelloOperationGeneration == 0 || v.ValidatedReceiptFingerprint == "" || v.CandidateFingerprint == "" || v.ConnectionID == "" || v.ChallengeCommitment == "" || v.HandleFingerprint == "" {
		return errors.New("prepared terminal attachment challenge identity is incomplete")
	}
	return nil
}

type AttachmentChallengePromotionReceipt struct {
	PreparedHandleFingerprint    string
	PromotionOperationID         string
	PromotionOperationGeneration uint64
	PromotionReceiptFingerprint  string
}

func (v AttachmentChallengePromotionReceipt) Validate() error {
	if v.PreparedHandleFingerprint == "" || v.PromotionOperationID == "" || v.PromotionOperationGeneration == 0 || v.PromotionReceiptFingerprint == "" {
		return errors.New("terminal attachment challenge promotion receipt is incomplete")
	}
	return nil
}

type AttachmentChallengeAcceptanceReceipt struct {
	PreparedHandleFingerprint       string
	PromotionReceiptFingerprint     string
	ConfirmationOperationID         string
	ConfirmationOperationGeneration uint64
	AcceptanceReceiptFingerprint    string
}

type AttachmentChallengeRevocationTarget uint8

const (
	RevokePreparedChallenge AttachmentChallengeRevocationTarget = iota + 1
	RevokeActiveChallenge
)

type AttachmentChallengeRevocationReason uint8

const (
	ChallengeRevokeStaleApplicationResult AttachmentChallengeRevocationReason = iota + 1
	ChallengeRevokeOperationSuperseded
	ChallengeRevokeAuthorityExited
)

type AttachmentChallengeRevocationReceipt struct {
	Target                AttachmentChallengeRevocationTarget
	Reason                AttachmentChallengeRevocationReason
	HandleFingerprint     string
	PromotionFingerprint  string
	RevocationOperationID string
	RevocationGeneration  uint64
	RevocationFingerprint string
}

func NewAttachmentChallengeRevocationReceipt(
	target AttachmentChallengeRevocationTarget,
	reason AttachmentChallengeRevocationReason,
	handleFingerprint string,
	promotionFingerprint string,
	operation LocalOperationToken,
) (AttachmentChallengeRevocationReceipt, error) {
	value := AttachmentChallengeRevocationReceipt{
		Target:                target,
		Reason:                reason,
		HandleFingerprint:     handleFingerprint,
		PromotionFingerprint:  promotionFingerprint,
		RevocationOperationID: operation.OperationID,
		RevocationGeneration:  operation.OperationGeneration,
	}
	fingerprint, err := protocolvalue.CanonicalClientFingerprint(
		"terminal-attachment-challenge-revocation:v1",
		map[string]any{
			"target":                  value.Target,
			"reason":                  value.Reason,
			"handle_fingerprint":      value.HandleFingerprint,
			"promotion_fingerprint":   value.PromotionFingerprint,
			"revocation_operation_id": value.RevocationOperationID,
			"revocation_generation":   value.RevocationGeneration,
		},
	)
	if err != nil {
		return AttachmentChallengeRevocationReceipt{}, err
	}
	value.RevocationFingerprint = fingerprint
	return value, value.Validate()
}

func (v AttachmentChallengeRevocationReceipt) Validate() error {
	if v.Target < RevokePreparedChallenge || v.Target > RevokeActiveChallenge ||
		v.Reason < ChallengeRevokeStaleApplicationResult || v.Reason > ChallengeRevokeAuthorityExited ||
		v.HandleFingerprint == "" || v.RevocationOperationID == "" ||
		v.RevocationGeneration == 0 || v.RevocationFingerprint == "" {
		return errors.New("terminal attachment challenge revocation receipt is incomplete")
	}
	if (v.Target == RevokePreparedChallenge) != (v.PromotionFingerprint == "") {
		return errors.New("terminal attachment challenge revocation target matrix is invalid")
	}
	expected, err := protocolvalue.CanonicalClientFingerprint(
		"terminal-attachment-challenge-revocation:v1",
		map[string]any{
			"target":                  v.Target,
			"reason":                  v.Reason,
			"handle_fingerprint":      v.HandleFingerprint,
			"promotion_fingerprint":   v.PromotionFingerprint,
			"revocation_operation_id": v.RevocationOperationID,
			"revocation_generation":   v.RevocationGeneration,
		},
	)
	if err != nil || expected != v.RevocationFingerprint {
		return errors.New("terminal attachment challenge revocation fingerprint mismatch")
	}
	return nil
}

func (v AttachmentChallengeAcceptanceReceipt) Validate() error {
	if v.PreparedHandleFingerprint == "" || v.PromotionReceiptFingerprint == "" || v.ConfirmationOperationID == "" || v.ConfirmationOperationGeneration == 0 || v.AcceptanceReceiptFingerprint == "" {
		return errors.New("terminal attachment challenge acceptance receipt is incomplete")
	}
	return nil
}

type AttachmentChallengeState struct {
	Phase      AttachmentChallengeHandlePhase
	Prepared   PreparedAttachmentChallengeHandleIdentity
	Promotion  AttachmentChallengePromotionReceipt
	Acceptance AttachmentChallengeAcceptanceReceipt
}

func NewNoAttachmentChallenge() AttachmentChallengeState {
	return AttachmentChallengeState{Phase: AttachmentChallengeNone}
}

func (s AttachmentChallengeState) Validate() error {
	switch s.Phase {
	case AttachmentChallengeNone:
		if s.Prepared.HandleID != "" || s.Promotion.PromotionReceiptFingerprint != "" || s.Acceptance.AcceptanceReceiptFingerprint != "" {
			return errors.New("empty attachment challenge state contains authority")
		}
	case AttachmentChallengePreparedPromotionPending:
		if s.Prepared.Validate() != nil || s.Promotion.PromotionReceiptFingerprint != "" || s.Acceptance.AcceptanceReceiptFingerprint != "" {
			return errors.New("prepared attachment challenge state is invalid")
		}
	case AttachmentChallengeActiveAcceptancePending:
		if s.Prepared.Validate() != nil || s.Promotion.Validate() != nil || s.Promotion.PreparedHandleFingerprint != s.Prepared.HandleFingerprint || s.Acceptance.AcceptanceReceiptFingerprint != "" {
			return errors.New("promoted attachment challenge state is invalid")
		}
	case AttachmentChallengeActive:
		if s.Prepared.Validate() != nil || s.Promotion.Validate() != nil || s.Acceptance.Validate() != nil || s.Acceptance.PreparedHandleFingerprint != s.Prepared.HandleFingerprint || s.Acceptance.PromotionReceiptFingerprint != s.Promotion.PromotionReceiptFingerprint {
			return errors.New("active attachment challenge state is invalid")
		}
	case AttachmentChallengeConsumed, AttachmentChallengeRevoked:
		if s.Prepared.HandleFingerprint == "" {
			return errors.New("terminal attachment challenge terminal state lacks identity")
		}
	default:
		return errors.New("attachment challenge phase is unknown")
	}
	return nil
}
