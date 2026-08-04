package client

import (
	"errors"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

// observationBridge enforces the S2 one-response/one-message boundary. It has
// no worker queue: the serial operation owner returns exactly one immutable
// batch to Bubble Tea, so a slow renderer can never backpressure Python's
// committed event writer.
type observationBridge struct{}

func (observationBridge) decode(response *protocol.ObservationResponse, request protocolvalue.PreparedObserveRequest) (protocolvalue.ObservationResult, error) {
	value, err := protocolvalue.ObservationFromProto(response)
	if err != nil {
		return protocolvalue.ObservationResult{}, err
	}
	requestID := value.NoChange.RequestID
	if value.IsBatch {
		requestID = value.Batch.RequestID
	}
	if requestID != request.RequestID {
		return protocolvalue.ObservationResult{}, errors.New("terminal observation bridge crossed requests")
	}
	return value, nil
}
