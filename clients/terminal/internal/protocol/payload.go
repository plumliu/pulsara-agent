package protocol

import (
	"errors"
	"strings"

	"google.golang.org/protobuf/proto"
)

const wireRequestIDDigestHexLength = 64

// CanonicalWireRequestIDForPayloadSizing has the exact UTF-8 shape of every
// request ID emitted by WireOperationIdentity. Protobuf string size depends on
// byte length, so this carrier permits pre-dispatch sizing without inventing a
// shorter request identity or consuming a real operation generation.
func CanonicalWireRequestIDForPayloadSizing() string {
	return "terminal-request:" + strings.Repeat("0", wireRequestIDDigestHexLength)
}

// MarshalBoundedDeterministicPayload is the single Protocol-owned payload
// accounting seam used by both pre-dispatch admission and physical framing.
// The four-byte transport length header is deliberately outside this bound.
func MarshalBoundedDeterministicPayload(message proto.Message, maximum uint32) ([]byte, error) {
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(message)
	if err != nil {
		return nil, err
	}
	if len(payload) == 0 || uint64(len(payload)) > uint64(maximum) {
		return nil, errors.New("terminal output frame is outside its bound")
	}
	return payload, nil
}
