package client

import (
	"errors"
	"strings"
	"sync"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

type challengePhase uint8

const (
	challengePrepared challengePhase = iota + 1
	challengePendingAcceptance
	challengeActive
	challengeConsumed
	challengeRevoked
)

type challengeRecord struct {
	identity   app.PreparedAttachmentChallengeHandleIdentity
	value      []byte
	phase      challengePhase
	expiresAt  time.Time
	promotion  app.AttachmentChallengePromotionReceipt
	acceptance app.AttachmentChallengeAcceptanceReceipt
}

type ClientRuntimeOwner struct {
	mu               sync.Mutex
	challenge        *challengeRecord
	reconnectCurrent *reconnectCredentialRecord
	reconnectPending *reconnectCredentialRecord
	closed           bool
}

type reconnectCredentialRecord struct {
	public             protocolvalue.ReconnectCredentialPublicIdentity
	capability         []byte
	carrierFingerprint string
	expiresAt          time.Time
}

func (o *ClientRuntimeOwner) InstallPendingReconnectCredential(value protocolvalue.ReconnectCredentialCarrier) error {
	expiresAt, err := time.Parse(time.RFC3339Nano, value.PublicIdentity.ExpiresAtUTC)
	if err != nil || !time.Now().Before(expiresAt) || len(value.Capability) != 32 || value.Fingerprint == "" {
		return errors.New("terminal reconnect credential is invalid")
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closed {
		return errors.New("terminal runtime owner is closed")
	}
	if o.reconnectPending != nil {
		if o.reconnectPending.carrierFingerprint == value.Fingerprint {
			return nil
		}
		return errors.New("terminal pending reconnect credential conflicts")
	}
	o.reconnectPending = &reconnectCredentialRecord{public: value.PublicIdentity, capability: append([]byte(nil), value.Capability...), carrierFingerprint: value.Fingerprint, expiresAt: expiresAt}
	return nil
}

func (o *ClientRuntimeOwner) PromotePendingReconnectCredential(carrierFingerprint string) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closed || o.reconnectPending == nil || o.reconnectPending.carrierFingerprint != carrierFingerprint || !time.Now().Before(o.reconnectPending.expiresAt) {
		return errors.New("terminal pending reconnect credential is unavailable")
	}
	if o.reconnectCurrent != nil {
		clear(o.reconnectCurrent.capability)
	}
	o.reconnectCurrent, o.reconnectPending = o.reconnectPending, nil
	return nil
}

func (o *ClientRuntimeOwner) BorrowReconnectCredential(expectedAttemptGeneration uint64) (protocolvalue.ReconnectCredentialPublicIdentity, []byte, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closed || o.reconnectCurrent == nil || !time.Now().Before(o.reconnectCurrent.expiresAt) || o.reconnectCurrent.public.ExpectedNextAttachmentAttemptGeneration != expectedAttemptGeneration {
		return protocolvalue.ReconnectCredentialPublicIdentity{}, nil, errors.New("terminal reconnect credential is unavailable")
	}
	return o.reconnectCurrent.public, append([]byte(nil), o.reconnectCurrent.capability...), nil
}

func (o *ClientRuntimeOwner) RevokeReconnectCredentials() uint32 {
	o.mu.Lock()
	defer o.mu.Unlock()
	var count uint32
	if o.reconnectCurrent != nil {
		clear(o.reconnectCurrent.capability)
		o.reconnectCurrent = nil
		count++
	}
	if o.reconnectPending != nil {
		clear(o.reconnectPending.capability)
		o.reconnectPending = nil
		count++
	}
	return count
}

func (o *ClientRuntimeOwner) PrepareAttachmentChallenge(helloOperation app.OperationToken, challenge [32]byte, validatedReceiptFingerprint, candidateFingerprint, connectionID, commitment string, expiresAt time.Time) (app.PreparedAttachmentChallengeHandleIdentity, error) {
	if helloOperation.Kind != app.OpHello || !helloOperation.Valid() || validatedReceiptFingerprint == "" || candidateFingerprint == "" || connectionID == "" || commitment == "" || !time.Now().Before(expiresAt) {
		return app.PreparedAttachmentChallengeHandleIdentity{}, errors.New("prepared terminal challenge is invalid")
	}
	identity := app.PreparedAttachmentChallengeHandleIdentity{
		HandleID:                    "challenge:" + strings.TrimPrefix(validatedReceiptFingerprint, "sha256:"),
		HandleGeneration:            1,
		HelloOperationID:            helloOperation.OperationID,
		HelloOperationGeneration:    helloOperation.OperationGeneration,
		ValidatedReceiptFingerprint: validatedReceiptFingerprint,
		CandidateFingerprint:        candidateFingerprint,
		ConnectionID:                connectionID,
		ChallengeCommitment:         commitment,
	}
	fingerprint, err := protocol.CanonicalJSONFingerprint("terminal-prepared-attachment-challenge:v1", map[string]any{
		"handle_id": identity.HandleID, "handle_generation": identity.HandleGeneration,
		"hello_operation_id": identity.HelloOperationID, "hello_operation_generation": identity.HelloOperationGeneration,
		"validated_receipt_fingerprint": identity.ValidatedReceiptFingerprint, "candidate_fingerprint": identity.CandidateFingerprint,
		"connection_id": identity.ConnectionID, "challenge_commitment": identity.ChallengeCommitment,
	})
	if err != nil {
		return app.PreparedAttachmentChallengeHandleIdentity{}, err
	}
	identity.HandleFingerprint = fingerprint
	if err := identity.Validate(); err != nil {
		return app.PreparedAttachmentChallengeHandleIdentity{}, err
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closed {
		return app.PreparedAttachmentChallengeHandleIdentity{}, errors.New("terminal runtime owner is closed")
	}
	if o.challenge != nil && o.challenge.phase != challengeConsumed && o.challenge.phase != challengeRevoked {
		return app.PreparedAttachmentChallengeHandleIdentity{}, errors.New("terminal challenge owner already has a live record")
	}
	o.challenge = &challengeRecord{identity: identity, value: append([]byte(nil), challenge[:]...), phase: challengePrepared, expiresAt: expiresAt}
	return identity, nil
}

func (o *ClientRuntimeOwner) PromotePreparedAttachmentChallenge(operation app.LocalOperationToken, prepared app.PreparedAttachmentChallengeHandleIdentity) (app.AttachmentChallengePromotionReceipt, error) {
	if operation.Kind != app.OpChallengePromote || !operation.Valid() || prepared.Validate() != nil {
		return app.AttachmentChallengePromotionReceipt{}, errors.New("terminal challenge promotion input is invalid")
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.challenge == nil || o.challenge.phase != challengePrepared || o.challenge.identity != prepared || !time.Now().Before(o.challenge.expiresAt) {
		return app.AttachmentChallengePromotionReceipt{}, errors.New("terminal challenge promotion is stale")
	}
	receipt := app.AttachmentChallengePromotionReceipt{PreparedHandleFingerprint: prepared.HandleFingerprint, PromotionOperationID: operation.OperationID, PromotionOperationGeneration: operation.OperationGeneration}
	fingerprint, err := protocol.CanonicalJSONFingerprint("terminal-attachment-challenge-promotion:v1", map[string]any{"prepared_handle_fingerprint": receipt.PreparedHandleFingerprint, "operation_id": receipt.PromotionOperationID, "operation_generation": receipt.PromotionOperationGeneration})
	if err != nil {
		return app.AttachmentChallengePromotionReceipt{}, err
	}
	receipt.PromotionReceiptFingerprint = fingerprint
	o.challenge.phase, o.challenge.promotion = challengePendingAcceptance, receipt
	return receipt, nil
}

func (o *ClientRuntimeOwner) ConfirmAttachmentChallengePromotion(operation app.LocalOperationToken, promotion app.AttachmentChallengePromotionReceipt) (app.AttachmentChallengeAcceptanceReceipt, error) {
	if operation.Kind != app.OpChallengePromotionConfirm || !operation.Valid() || promotion.Validate() != nil {
		return app.AttachmentChallengeAcceptanceReceipt{}, errors.New("terminal challenge confirmation input is invalid")
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.challenge == nil || o.challenge.phase != challengePendingAcceptance || o.challenge.promotion != promotion || !time.Now().Before(o.challenge.expiresAt) {
		return app.AttachmentChallengeAcceptanceReceipt{}, errors.New("terminal challenge confirmation is stale")
	}
	receipt := app.AttachmentChallengeAcceptanceReceipt{PreparedHandleFingerprint: o.challenge.identity.HandleFingerprint, PromotionReceiptFingerprint: promotion.PromotionReceiptFingerprint, ConfirmationOperationID: operation.OperationID, ConfirmationOperationGeneration: operation.OperationGeneration}
	fingerprint, err := protocol.CanonicalJSONFingerprint("terminal-attachment-challenge-acceptance:v1", map[string]any{"prepared_handle_fingerprint": receipt.PreparedHandleFingerprint, "promotion_receipt_fingerprint": receipt.PromotionReceiptFingerprint, "operation_id": receipt.ConfirmationOperationID, "operation_generation": receipt.ConfirmationOperationGeneration})
	if err != nil {
		return app.AttachmentChallengeAcceptanceReceipt{}, err
	}
	receipt.AcceptanceReceiptFingerprint = fingerprint
	o.challenge.phase, o.challenge.acceptance = challengeActive, receipt
	return receipt, nil
}

func (o *ClientRuntimeOwner) BorrowAttachmentChallengeOnce(acceptance app.AttachmentChallengeAcceptanceReceipt) ([]byte, string, error) {
	if acceptance.Validate() != nil {
		return nil, "", errors.New("terminal challenge borrow proof is invalid")
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.challenge == nil || o.challenge.phase != challengeActive || o.challenge.acceptance != acceptance || !time.Now().Before(o.challenge.expiresAt) {
		return nil, "", errors.New("active terminal challenge is unavailable")
	}
	value := append([]byte(nil), o.challenge.value...)
	commitment := o.challenge.identity.ChallengeCommitment
	clear(o.challenge.value)
	o.challenge.phase = challengeConsumed
	return value, commitment, nil
}

func (o *ClientRuntimeOwner) RevokePreparedAttachmentChallenge(
	operation app.LocalOperationToken,
	handleFingerprint string,
	reason app.AttachmentChallengeRevocationReason,
) (app.AttachmentChallengeRevocationReceipt, error) {
	if operation.Kind != app.OpChallengeRevokePrepared || !operation.Valid() ||
		handleFingerprint == "" || reason == 0 {
		return app.AttachmentChallengeRevocationReceipt{}, errors.New(
			"prepared terminal challenge revocation input is invalid",
		)
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.challenge == nil || o.challenge.identity.HandleFingerprint != handleFingerprint {
		return app.AttachmentChallengeRevocationReceipt{}, errors.New(
			"prepared terminal challenge revocation is stale",
		)
	}
	if o.challenge.phase != challengePrepared && o.challenge.phase != challengeRevoked {
		return app.AttachmentChallengeRevocationReceipt{}, errors.New(
			"prepared terminal challenge was already promoted",
		)
	}
	receipt, err := app.NewAttachmentChallengeRevocationReceipt(
		app.RevokePreparedChallenge,
		reason,
		handleFingerprint,
		"",
		operation,
	)
	if err != nil {
		return app.AttachmentChallengeRevocationReceipt{}, err
	}
	clear(o.challenge.value)
	o.challenge.phase = challengeRevoked
	return receipt, nil
}

func (o *ClientRuntimeOwner) RevokeActiveAttachmentChallenge(
	operation app.LocalOperationToken,
	handleFingerprint string,
	promotionFingerprint string,
	reason app.AttachmentChallengeRevocationReason,
) (app.AttachmentChallengeRevocationReceipt, error) {
	if operation.Kind != app.OpChallengeRevokeActive || !operation.Valid() ||
		handleFingerprint == "" || promotionFingerprint == "" || reason == 0 {
		return app.AttachmentChallengeRevocationReceipt{}, errors.New(
			"active terminal challenge revocation input is invalid",
		)
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.challenge == nil || o.challenge.identity.HandleFingerprint != handleFingerprint ||
		o.challenge.promotion.PromotionReceiptFingerprint != promotionFingerprint {
		return app.AttachmentChallengeRevocationReceipt{}, errors.New(
			"active terminal challenge revocation is stale",
		)
	}
	if o.challenge.phase != challengePendingAcceptance &&
		o.challenge.phase != challengeActive && o.challenge.phase != challengeRevoked {
		return app.AttachmentChallengeRevocationReceipt{}, errors.New(
			"active terminal challenge has no revocable value",
		)
	}
	receipt, err := app.NewAttachmentChallengeRevocationReceipt(
		app.RevokeActiveChallenge,
		reason,
		handleFingerprint,
		promotionFingerprint,
		operation,
	)
	if err != nil {
		return app.AttachmentChallengeRevocationReceipt{}, err
	}
	clear(o.challenge.value)
	o.challenge.phase = challengeRevoked
	return receipt, nil
}

func (o *ClientRuntimeOwner) RevokeChallenge() {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.challenge != nil {
		clear(o.challenge.value)
		o.challenge.phase = challengeRevoked
	}
}

func (o *ClientRuntimeOwner) Close() uint32 {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closed {
		return 0
	}
	o.closed = true
	var revoked uint32
	if o.challenge != nil {
		if o.challenge.phase != challengeConsumed && o.challenge.phase != challengeRevoked {
			revoked = 1
		}
		clear(o.challenge.value)
		o.challenge.phase = challengeRevoked
	}
	if o.reconnectCurrent != nil {
		clear(o.reconnectCurrent.capability)
		o.reconnectCurrent = nil
		revoked++
	}
	if o.reconnectPending != nil {
		clear(o.reconnectPending.capability)
		o.reconnectPending = nil
		revoked++
	}
	return revoked
}
