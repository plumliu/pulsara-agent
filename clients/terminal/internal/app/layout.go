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
	if p.Width < 1 || p.Height < 1 || p.HeaderRows != 1 || p.HeaderRows+p.TranscriptRows+p.FooterRows != p.Height {
		return errors.New("terminal layout row ownership is invalid")
	}
	switch p.Mode {
	case LayoutSingleLine:
		if p.Height != 1 || p.TranscriptRows != 0 || p.FooterRows != 0 {
			return errors.New("single-line layout is invalid")
		}
	case LayoutHeaderFooter:
		if p.Height != 2 || p.TranscriptRows != 0 || p.FooterRows != 1 {
			return errors.New("header-footer layout is invalid")
		}
	case LayoutFullHeight:
		if p.Height < 3 || p.TranscriptRows != p.Height-2 || p.FooterRows != 1 {
			return errors.New("full-height layout is invalid")
		}
	default:
		return errors.New("terminal layout mode is invalid")
	}
	return nil
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
		return "observer · wheel/↑/↓ scroll · y copy · q quit"
	case width >= 24:
		return "read-only · ↑↓ · y copy · q quit"
	default:
		return "↑↓·y·q"
	}
}
