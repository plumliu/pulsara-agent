package transcript

import (
	"strings"
	"testing"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/presentation"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func TestRenderPreservesCJKEmojiAndServerOrder(t *testing.T) {
	state := presentation.New()
	var err error
	state, err = state.Install(protocolvalue.DurableSnapshot{
		HostSessionID: "host:one", RuntimeSessionID: "runtime:one",
		Control: protocolvalue.ControlProjection{RuntimeSessionID: "runtime:one", CursorFingerprint: "control"}, SnapshotFingerprint: "snapshot",
		Cells: []protocolvalue.HistoryCell{
			{ID: "one", Kind: "user", PublicText: "你好，Pulsara 🌍", Fingerprint: "one"},
			{ID: "two", Kind: "assistant", PublicText: "第二条消息", Fingerprint: "two"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	view := Render(state, 80, 20)
	if !strings.Contains(view, "你好，Pulsara 🌍") || !strings.Contains(view, "第二条消息") {
		t.Fatalf("wide text was lost: %q", view)
	}
	if strings.Index(view, "你好") > strings.Index(view, "第二条") {
		t.Fatal("renderer changed server ordering")
	}
}

func TestRenderIsBoundedByVisualRowsAndScrollsWrappedContent(t *testing.T) {
	state := presentation.New()
	var err error
	state, err = state.Install(protocolvalue.DurableSnapshot{
		HostSessionID: "host:one", RuntimeSessionID: "runtime:one",
		Control: protocolvalue.ControlProjection{RuntimeSessionID: "runtime:one", CursorFingerprint: "control"}, SnapshotFingerprint: "snapshot",
		Cells: []protocolvalue.HistoryCell{{
			ID: "entry:one", Kind: "assistant",
			PublicText:  "第一行中文与 emoji 🌍 followed by a deliberately long ASCII suffix",
			Fingerprint: "cell:one",
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	state = state.Resize(12, 4)
	tail := Render(state, 12, 4)
	if lines := strings.Count(tail, "\n") + 1; lines > 4 {
		t.Fatalf("render escaped viewport height: %d lines\n%s", lines, tail)
	}
	scrolled := Render(state.Scroll(2), 12, 4)
	if scrolled == tail {
		t.Fatal("wrapped transcript did not move when scrolled")
	}
	if lines := strings.Count(scrolled, "\n") + 1; lines > 4 {
		t.Fatalf("scrolled render escaped viewport height: %d lines", lines)
	}
}
