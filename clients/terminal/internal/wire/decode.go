package wire

import "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"

func ResponseBranch(frame *protocol.ServerFrame) string {
	if frame == nil || frame.Response == nil {
		return ""
	}
	field := frame.ProtoReflect().WhichOneof(frame.ProtoReflect().Descriptor().Oneofs().ByName("response"))
	if field == nil {
		return ""
	}
	return string(field.Name())
}
