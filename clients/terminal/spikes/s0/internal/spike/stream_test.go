package spike

import (
	"bytes"
	"encoding/binary"
	"testing"

	tea "charm.land/bubbletea/v2"
	"google.golang.org/protobuf/proto"

	"github.com/plumliu/pulsara-agent/clients/terminal-s0/internal/probeproto"
)

func TestReadProbeStreamPreservesTypedSnapshotAndDelta(t *testing.T) {
	frames := []*probeproto.ProbeFrame{
		{
			Body: &probeproto.ProbeFrame_Snapshot{Snapshot: &probeproto.Snapshot{
				Revision: 7,
				Lines:    []string{"你好", "snapshot🙂"},
			}},
		},
		{
			Body: &probeproto.ProbeFrame_Delta{Delta: &probeproto.Delta{
				Sequence:      8,
				Content:       "delta界",
				SentUnixNanos: 123,
			}},
		},
	}
	var wire bytes.Buffer
	for _, frame := range frames {
		payload, err := proto.Marshal(frame)
		if err != nil {
			t.Fatal(err)
		}
		if err := binary.Write(&wire, binary.BigEndian, uint32(len(payload))); err != nil {
			t.Fatal(err)
		}
		wire.Write(payload)
	}

	var messages []tea.Msg
	err := ReadProbeStream(&wire, func(msg tea.Msg) { messages = append(messages, msg) })
	if err == nil {
		t.Fatal("expected EOF after complete frames")
	}
	if len(messages) != 2 {
		t.Fatalf("messages = %d, want 2", len(messages))
	}
	snapshot, ok := messages[0].(StreamSnapshotMsg)
	if !ok || snapshot.Revision != 7 || len(snapshot.Lines) != 2 {
		t.Fatalf("snapshot = %#v", messages[0])
	}
	delta, ok := messages[1].(StreamDeltaMsg)
	if !ok || delta.Sequence != 8 || delta.Content != "delta界" || delta.SentUnixNanos != 123 {
		t.Fatalf("delta = %#v", messages[1])
	}
}

func TestReadProbeStreamRejectsOversizedFrameBeforeAllocation(t *testing.T) {
	var wire bytes.Buffer
	if err := binary.Write(&wire, binary.BigEndian, uint32(maxProbeFrameBytes+1)); err != nil {
		t.Fatal(err)
	}
	if err := ReadProbeStream(&wire, func(tea.Msg) {}); err == nil {
		t.Fatal("expected oversized frame rejection")
	}
}
