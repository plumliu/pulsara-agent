package app

import (
	"errors"
	"strings"

	"github.com/charmbracelet/x/ansi"
)

const maximumLayoutCells = 256 * 1024

type LayoutMode uint8

const (
	LayoutSingleLine LayoutMode = iota + 1
	LayoutHeaderFooter
	LayoutFullHeight
)

// LayoutPlan is the single renderer-neutral owner of S1 terminal geometry.
// Header and footer never wrap; every accepted plan produces exactly Height
// visual rows and no row wider than Width cells.
type LayoutPlan struct {
	Width          int
	Height         int
	HeaderRows     int
	TranscriptRows int
	ComposerRows   int
	FooterRows     int
	Mode           LayoutMode
}

func NewLayoutPlan(width, height int) (LayoutPlan, error) {
	if width < 1 || height < 1 || width > maximumLayoutCells || height > maximumLayoutCells || width > maximumLayoutCells/height {
		return LayoutPlan{}, errors.New("terminal dimensions exceed the bounded layout contract")
	}
	plan := LayoutPlan{Width: width, Height: height, HeaderRows: 1}
	switch height {
	case 1:
		plan.Mode = LayoutSingleLine
	case 2:
		plan.Mode = LayoutHeaderFooter
		plan.FooterRows = 1
	default:
		plan.Mode = LayoutFullHeight
		plan.TranscriptRows = height - 2
		plan.FooterRows = 1
	}
	return plan, nil
}

func (p LayoutPlan) Validate() error {
	if p.Width < 1 || p.Height < 1 || p.HeaderRows != 1 || p.HeaderRows+p.TranscriptRows+p.ComposerRows+p.FooterRows != p.Height {
		return errors.New("terminal layout row ownership is invalid")
	}
	switch p.Mode {
	case LayoutSingleLine:
		if p.Height != 1 || p.TranscriptRows != 0 || p.ComposerRows != 0 || p.FooterRows != 0 {
			return errors.New("single-line layout is invalid")
		}
	case LayoutHeaderFooter:
		if p.Height != 2 || p.TranscriptRows != 0 || p.ComposerRows != 0 || p.FooterRows != 1 {
			return errors.New("header-footer layout is invalid")
		}
	case LayoutFullHeight:
		if p.Height < 3 || p.TranscriptRows+p.ComposerRows != p.Height-2 || p.FooterRows != 1 || p.ComposerRows < 0 || p.ComposerRows > 6 {
			return errors.New("full-height layout is invalid")
		}
	default:
		return errors.New("terminal layout mode is invalid")
	}
	return nil
}

// NewInteractiveLayoutPlan reserves a bounded composer slice while retaining
// exactly one header and one footer row. Tiny terminals deliberately fall
// back to the S1 read-only geometry instead of hiding authority-bearing text.
func NewInteractiveLayoutPlan(width, height, desiredComposerRows int) (LayoutPlan, error) {
	base, err := NewLayoutPlan(width, height)
	if err != nil || height < 4 {
		return base, err
	}
	if desiredComposerRows < 1 {
		desiredComposerRows = 1
	}
	if desiredComposerRows > 6 {
		desiredComposerRows = 6
	}
	maximum := height - 3 // retain at least one transcript row
	if desiredComposerRows > maximum {
		desiredComposerRows = maximum
	}
	base.ComposerRows = desiredComposerRows
	base.TranscriptRows = height - base.HeaderRows - base.FooterRows - base.ComposerRows
	return base, base.Validate()
}

func fitLayoutLine(value string, width int) string {
	if width < 1 {
		return ""
	}
	value = ansi.Truncate(value, width, "")
	visible := ansi.StringWidth(value)
	if visible < width {
		value += strings.Repeat(" ", width-visible)
	}
	return value
}

func compactFooter(width int) string {
	switch {
	case width >= 52:
		return "observer · wheel/↑/↓ scroll · y copy · q detach"
	case width >= 24:
		return "read-only · ↑↓ · y copy · q detach"
	default:
		return "↑↓·y·q"
	}
}

func interactiveFooter(width int, running bool) string {
	action := "Enter send · Alt+Enter newline"
	if running {
		action += " · Ctrl-C stop"
	}
	switch {
	case width >= 108:
		return action + " · PgUp transcript · ↑↓ prompts · Ctrl-D detach"
	case width >= 72:
		return "Enter send · PgUp transcript · ↑↓ prompts · Ctrl-D detach"
	case width >= 42:
		if running {
			return "↵ send · ^C stop · PgUp transcript · ^D detach"
		}
		return "↵ send · PgUp transcript · ^D detach"
	default:
		if running {
			return "↵ · ^C stop · ^D detach"
		}
		return "↵ send · ^D detach"
	}
}
