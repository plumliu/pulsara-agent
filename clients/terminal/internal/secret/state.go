package secret

import (
	"errors"
	"time"
)

type Phase uint8

const (
	SecretDormant Phase = iota + 1
	SecretHandleActive
	SecretSubmitting
	SecretRevoked
)

// State deliberately stores only opaque process-local identity and masked
// metadata; plaintext remains in ClientRuntimeOwner.
type State struct {
	phase              Phase
	handleID           string
	leaseFingerprint   string
	leaseGeneration    uint64
	expiresAt          time.Time
	interactionID      string
	requestKey         string
	maskedLength       uint32
	validationMetadata string
}

func NewDormantState() State { return State{phase: SecretDormant} }

func (s State) Validate() error {
	switch s.phase {
	case SecretDormant:
		if s.handleID != "" || s.leaseFingerprint != "" || s.leaseGeneration != 0 || !s.expiresAt.IsZero() || s.interactionID != "" || s.requestKey != "" || s.maskedLength != 0 || s.validationMetadata != "" {
			return errors.New("dormant terminal secret state contains a handle")
		}
	case SecretHandleActive, SecretSubmitting:
		if s.handleID == "" || s.leaseFingerprint == "" || s.leaseGeneration == 0 || s.expiresAt.IsZero() || s.interactionID == "" || s.requestKey == "" {
			return errors.New("active terminal secret state is incomplete")
		}
	case SecretRevoked:
		if s.handleID != "" || s.leaseFingerprint != "" || s.leaseGeneration != 0 || !s.expiresAt.IsZero() {
			return errors.New("revoked terminal secret retains live authority")
		}
	default:
		return errors.New("terminal secret phase is unknown")
	}
	return nil
}

func (s State) Dormant() bool { return s.phase == SecretDormant }
