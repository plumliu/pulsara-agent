package app

import (
	"testing"

	"github.com/charmbracelet/x/ansi"
)

func TestLayoutPlanOwnsExactS1Rows(t *testing.T) {
	tests := []struct {
		width, height int
		mode          LayoutMode
		body          int
	}{
		{1, 1, LayoutSingleLine, 0},
		{8, 2, LayoutHeaderFooter, 0},
		{80, 3, LayoutFullHeight, 1},
		{80, 24, LayoutFullHeight, 22},
		{120, 40, LayoutFullHeight, 38},
		{160, 59, LayoutFullHeight, 57},
	}
	for _, test := range tests {
		plan, err := NewLayoutPlan(test.width, test.height)
		if err != nil {
			t.Fatalf("%dx%d: %v", test.width, test.height, err)
		}
		if err := plan.Validate(); err != nil {
			t.Fatalf("%dx%d: %v", test.width, test.height, err)
		}
		if plan.Mode != test.mode || plan.TranscriptRows != test.body || plan.HeaderRows+plan.TranscriptRows+plan.FooterRows != test.height {
			t.Fatalf("%dx%d unexpected plan: %#v", test.width, test.height, plan)
		}
	}
}

func TestLayoutPlanRejectsInvalidOrUnboundedDimensions(t *testing.T) {
	for _, dimensions := range [][2]int{{0, 1}, {1, 0}, {-1, 24}, {maximumLayoutCells, 2}, {1024, 1024}} {
		if _, err := NewLayoutPlan(dimensions[0], dimensions[1]); err == nil {
			t.Fatalf("accepted invalid layout %dx%d", dimensions[0], dimensions[1])
		}
	}
}

func TestFitLayoutLineUsesANSIDisplayWidth(t *testing.T) {
	for _, value := range []string{"中🌍abcdef", "\x1b[31m红色文本\x1b[0m", "long-unbreakable-value"} {
		line := fitLayoutLine(value, 7)
		if width := ansi.StringWidth(line); width != 7 {
			t.Fatalf("line width=%d want=7: %q", width, line)
		}
	}
}

func TestFooterResponsiveVocabulary(t *testing.T) {
	if got := compactFooter(80); got != "observer · wheel/↑/↓ scroll · y copy · q detach" {
		t.Fatalf("unexpected wide footer: %q", got)
	}
	if got := compactFooter(30); got != "read-only · ↑↓ · y copy · q detach" {
		t.Fatalf("unexpected compact footer: %q", got)
	}
	if got := compactFooter(8); got != "↑↓·y·q" {
		t.Fatalf("unexpected narrow footer: %q", got)
	}
	if got := interactiveFooter(120, true); got != "Enter send · Alt+Enter newline · Ctrl-C stop · PgUp transcript · ↑↓ prompts · Ctrl-D detach" {
		t.Fatalf("interactive footer semantic vocabulary drifted: %q", got)
	}
	if got := interactiveFooter(30, false); got != "↵ send · ^D detach" {
		t.Fatalf("narrow interactive footer changed detach into quit: %q", got)
	}
}
