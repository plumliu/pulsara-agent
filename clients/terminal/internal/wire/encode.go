package wire

import "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"

func HelloFrame(value *protocol.HelloRequest) *protocol.ClientFrame {
	return &protocol.ClientFrame{Request: &protocol.ClientFrame_Hello{Hello: value}}
}

func AttachFrame(value *protocol.AttachRequest) *protocol.ClientFrame {
	return &protocol.ClientFrame{Request: &protocol.ClientFrame_Attach{Attach: value}}
}

func AttachAckFrame(value *protocol.AttachReceiptAck) *protocol.ClientFrame {
	return &protocol.ClientFrame{Request: &protocol.ClientFrame_AttachAck{AttachAck: value}}
}

func SnapshotFrame(value *protocol.ProjectionSnapshotRequest) *protocol.ClientFrame {
	return &protocol.ClientFrame{Request: &protocol.ClientFrame_Snapshot{Snapshot: value}}
}

func OperationalSnapshotFrame(value *protocol.OperationalSnapshotRequest) *protocol.ClientFrame {
	return &protocol.ClientFrame{Request: &protocol.ClientFrame_OperationalSnapshot{OperationalSnapshot: value}}
}

func HeartbeatFrame(value *protocol.HeartbeatRequest) *protocol.ClientFrame {
	return &protocol.ClientFrame{Request: &protocol.ClientFrame_Heartbeat{Heartbeat: value}}
}

func ObserveFrame(value *protocol.ObserveNextRequest) *protocol.ClientFrame {
	return &protocol.ClientFrame{Request: &protocol.ClientFrame_ObserveNext{ObserveNext: value}}
}

func HistoryPageFrame(value *protocol.HistoryPageRequest) *protocol.ClientFrame {
	return &protocol.ClientFrame{Request: &protocol.ClientFrame_HistoryPage{HistoryPage: value}}
}
