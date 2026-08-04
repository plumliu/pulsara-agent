package wire

import (
	"errors"
	"strings"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/publictext"
)

func ValidateServerFrame(frame *protocol.ServerFrame, expected string) error {
	if frame == nil || frame.Response == nil {
		return errors.New("terminal server frame has no response")
	}
	if frame.GetError() != nil {
		if !publictext.IsSafe(frame.GetError().PublicMessage) {
			return errors.New("terminal server error contains unsafe public text")
		}
		return errors.New(strings.TrimSpace(frame.GetError().PublicMessage))
	}
	actual := frame.ProtoReflect().WhichOneof(frame.ProtoReflect().Descriptor().Oneofs().ByName("response"))
	if actual == nil || string(actual.Name()) != expected {
		return errors.New("terminal server response branch is unexpected")
	}
	return nil
}
