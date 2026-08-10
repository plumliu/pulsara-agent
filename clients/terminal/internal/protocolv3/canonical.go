package protocolv3

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
)

// CanonicalFingerprint is the sole Protocol-v3 JSON fingerprint helper.
// encoding/json sorts string map keys and rejects unsupported values.
func CanonicalFingerprint(namespace string, value any) (string, error) {
	if namespace == "" {
		return "", errors.New("fingerprint namespace is empty")
	}
	payload, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256(append(append([]byte(namespace), 0), payload...))
	return fmt.Sprintf("sha256:%x", digest), nil
}
