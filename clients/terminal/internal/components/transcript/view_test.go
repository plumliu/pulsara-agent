package transcript

import (
	"strings"
	"testing"

	"github.com/charmbracelet/x/ansi"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func transcriptSnapshot(text string) protocolvalue.DurableSnapshot {
	return protocolvalue.DurableSnapshot{
		HostSessionID: "host:one", RuntimeSessionID: "runtime:one",
		Control:             protocolvalue.ControlProjection{RuntimeSessionID: "runtime:one", CursorFingerprint: "control"},
		SnapshotFingerprint: "snapshot",
		Cells: []protocolvalue.HistoryCell{
			{ID: "one", Kind: "user", PublicText: text, Fingerprint: "one"},
			{ID: "two", Kind: "assistant", PublicText: "第二条消息 🌍", Fingerprint: "two"},
		},
	}
}

func TestRenderPreservesCJKEmojiAndServerOrder(t *testing.T) {
	model, err := New(80, 20).Install(transcriptSnapshot("你好，Pulsara 🌍"), 80, 20)
	if err != nil {
		t.Fatal(err)
	}
	view := strings.Join(Render(model), "\n")
	if !strings.Contains(view, "你好，Pulsara 🌍") || !strings.Contains(view, "第二条消息") {
		t.Fatalf("wide text was lost: %q", view)
	}
	if strings.Index(view, "你好") > strings.Index(view, "第二条") {
		t.Fatal("renderer changed server ordering")
	}
}

func TestViewportScrollPageEndAndBoundsUseVisualRows(t *testing.T) {
	model, err := New(12, 4).Install(
		transcriptSnapshot("第一行中文与 emoji 🌍 followed by a deliberately long ASCII suffix that wraps many times"),
		12,
		4,
	)
	if err != nil {
		t.Fatal(err)
	}
	tail := strings.Join(Render(model), "\n")
	if len(Render(model)) > 4 {
		t.Fatalf("render escaped viewport height: %d lines\n%s", len(Render(model)), tail)
	}

	model = model.Scroll(1)
	if model.ScrollOffset() != 1 || model.FollowTail() {
		t.Fatalf("single-row scroll invariant failed: offset=%d follow=%v", model.ScrollOffset(), model.FollowTail())
	}
	model = model.Page(1)
	if model.ScrollOffset() != 4 {
		t.Fatalf("page-up did not move viewportRows-1: %d", model.ScrollOffset())
	}
	model = model.Page(-1)
	if model.ScrollOffset() != 1 {
		t.Fatalf("page-down did not move viewportRows-1: %d", model.ScrollOffset())
	}
	model = model.Scroll(-1000)
	if model.ScrollOffset() != 0 || !model.FollowTail() {
		t.Fatal("scroll lower bound did not restore follow-tail")
	}
	model = model.Scroll(1000)
	if model.ScrollOffset() <= 0 {
		t.Fatal("scroll upper bound was not reached")
	}
	model = model.End()
	if model.ScrollOffset() != 0 || !model.FollowTail() {
		t.Fatal("End did not restore tail")
	}
	if err := model.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestResizePreservesScrolledContentAnchorAndTail(t *testing.T) {
	snapshot := transcriptSnapshot(strings.Repeat("甲乙丙丁戊己庚辛壬癸", 8))
	model, err := New(12, 4).Install(snapshot, 12, 4)
	if err != nil {
		t.Fatal(err)
	}
	tailGeneration := model.WrapBuildGeneration()
	tail, err := model.Resize(snapshot, 8, 6)
	if err != nil {
		t.Fatal(err)
	}
	if !tail.FollowTail() || tail.ScrollOffset() != 0 || tail.WrapBuildGeneration() != tailGeneration+1 {
		t.Fatal("follow-tail resize did not remain at the tail or rebuild once")
	}

	scrolled := model.Scroll(5)
	oldAnchor := scrolled.anchor
	oldGeneration := scrolled.WrapBuildGeneration()
	scrolled, err = scrolled.Resize(snapshot, 9, 5)
	if err != nil {
		t.Fatal(err)
	}
	if scrolled.FollowTail() || !scrolled.hasAnchor || scrolled.anchor.cellID != oldAnchor.cellID || scrolled.anchor.sourceOffset > oldAnchor.sourceOffset {
		t.Fatalf("scrolled resize lost its content anchor: before=%#v after=%#v", oldAnchor, scrolled.anchor)
	}
	if scrolled.WrapBuildGeneration() != oldGeneration+1 {
		t.Fatal("body width change did not rebuild the immutable wrap cache exactly once")
	}
	sameWidthGeneration := scrolled.WrapBuildGeneration()
	scrolled, err = scrolled.Resize(snapshot, 9, 3)
	if err != nil {
		t.Fatal(err)
	}
	if scrolled.WrapBuildGeneration() != sameWidthGeneration {
		t.Fatal("height-only resize rebuilt the wrap cache")
	}
}

func TestWrappedRowsAreDisplayWidthBoundedForNarrowWideAndLongText(t *testing.T) {
	for _, width := range []int{1, 2, 7, 80} {
		model, err := New(width, 20).Install(
			transcriptSnapshot("中🌍\t"+strings.Repeat("unbreakable", 20)),
			width,
			20,
		)
		if err != nil {
			t.Fatalf("width %d: %v", width, err)
		}
		for _, row := range model.cache.rows {
			if got := ansi.StringWidth(row.text); got > width {
				t.Fatalf("width %d row escaped bound: visual=%d text=%q", width, got, row.text)
			}
		}
	}
}
