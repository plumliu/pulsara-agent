package client

import (
	"errors"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func validateObserveAttribution(request protocolvalue.PreparedObserveRequest, attachment protocolvalue.Attachment) error {
	if request.RequestID == "" || request.AfterControl.Fingerprint == "" || attachment.ID == "" || attachment.BindingFingerprint == "" {
		return errors.New("terminal observe attribution is incomplete")
	}
	return nil
}
