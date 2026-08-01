package spike

import (
	"encoding/binary"
	"fmt"
	"io"

	tea "charm.land/bubbletea/v2"
	"google.golang.org/protobuf/proto"

	"pulsara.local/terminal-s0/internal/probeproto"
)

const maxProbeFrameBytes = 2 * 1024 * 1024

func ReadProbeStream(reader io.Reader, send func(tea.Msg)) error {
	var lengthBytes [4]byte
	for {
		if _, err := io.ReadFull(reader, lengthBytes[:]); err != nil {
			return err
		}
		length := binary.BigEndian.Uint32(lengthBytes[:])
		if length == 0 || length > maxProbeFrameBytes {
			return fmt.Errorf("invalid probe frame length: %d", length)
		}
		payload := make([]byte, length)
		if _, err := io.ReadFull(reader, payload); err != nil {
			return err
		}
		var frame probeproto.ProbeFrame
		if err := proto.Unmarshal(payload, &frame); err != nil {
			return fmt.Errorf("decode probe frame: %w", err)
		}
		switch body := frame.Body.(type) {
		case *probeproto.ProbeFrame_Snapshot:
			send(StreamSnapshotMsg{
				Revision: body.Snapshot.Revision,
				Lines:    append([]string(nil), body.Snapshot.Lines...),
			})
		case *probeproto.ProbeFrame_Delta:
			send(StreamDeltaMsg{
				Sequence:      body.Delta.Sequence,
				Content:       body.Delta.Content,
				SentUnixNanos: body.Delta.SentUnixNanos,
			})
		default:
			return fmt.Errorf("probe frame has no body")
		}
	}
}
