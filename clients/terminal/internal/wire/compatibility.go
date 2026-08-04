package wire

import (
	"errors"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func ValidateSelectedProtocol(version *protocol.ProtocolVersion) error {
	if version == nil || version.Major != protocolvalue.ProtocolMajor || version.Minor != protocolvalue.ProtocolMinor || version.SchemaContractFingerprint != protocolvalue.SchemaFingerprint || version.MinimumCompatibleMinor > protocolvalue.ProtocolMinor {
		return errors.New("terminal protocol 2.0 compatibility check failed")
	}
	return nil
}
