package client

import (
	"errors"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func validateHistoryPageAttribution(request protocolvalue.PreparedHistoryPageRequest, attachment protocolvalue.Attachment) error {
	if request.RequestID == "" || request.RuntimeSessionID != attachment.RuntimeSessionID || request.Cursor.Root.RuntimeSessionID != attachment.RuntimeSessionID {
		return errors.New("terminal history-page attribution is incomplete")
	}
	return nil
}
