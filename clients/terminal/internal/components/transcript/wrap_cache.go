package transcript

import (
	"errors"
	"strings"

	"github.com/charmbracelet/x/ansi"
	"github.com/rivo/uniseg"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/publictext"
)

const tabWidth = 4

type wrappedRow struct {
	cellID       string
	sourceOffset int
	label        bool
	text         string
}

type WrapCache struct {
	snapshotFingerprint string
	width               int
	rows                []wrappedRow
	buildGeneration     uint64
}

func newWrapCache(snapshot protocolvalue.DurableSnapshot, width int) (WrapCache, error) {
	if snapshot.SnapshotFingerprint == "" || width < 1 {
		return WrapCache{}, errors.New("transcript wrap cache identity is invalid")
	}
	cache := WrapCache{snapshotFingerprint: snapshot.SnapshotFingerprint, width: width, buildGeneration: 1}
	for _, cell := range snapshot.Cells {
		if cell.ID == "" || cell.Fingerprint == "" || cell.Kind == "" {
			return WrapCache{}, errors.New("transcript cell is incomplete")
		}
		label := ansi.Truncate(publictext.Transform(cell.Kind), width, "")
		cache.rows = append(cache.rows, wrappedRow{cellID: cell.ID, sourceOffset: -1, label: true, text: label})
		cache.rows = append(cache.rows, wrapPublicText(cell.ID, publictext.Transform(cell.PublicText), width)...)
	}
	return cache, nil
}

func (c WrapCache) validate() error {
	if c.snapshotFingerprint == "" || c.width < 1 || c.buildGeneration == 0 {
		return errors.New("transcript wrap cache is invalid")
	}
	for _, row := range c.rows {
		if row.cellID == "" || row.sourceOffset < -1 || row.label != (row.sourceOffset == -1) || ansi.StringWidth(row.text) > c.width {
			return errors.New("transcript wrapped row attribution is invalid")
		}
	}
	return nil
}

func (c WrapCache) indexForAnchor(anchor viewportAnchor) int {
	best := -1
	for index, row := range c.rows {
		if row.cellID != anchor.cellID || row.label != anchor.label {
			continue
		}
		if row.sourceOffset == anchor.sourceOffset {
			return index
		}
		if !row.label && row.sourceOffset <= anchor.sourceOffset {
			best = index
		}
	}
	if best >= 0 {
		return best
	}
	for index, row := range c.rows {
		if row.cellID == anchor.cellID {
			return index
		}
	}
	return max(len(c.rows)-1, 0)
}

func wrapPublicText(cellID, value string, width int) []wrappedRow {
	rows := make([]wrappedRow, 0, max(len(value)/max(width, 1), 1))
	var line strings.Builder
	lineWidth := 0
	lineStart := 0
	lastWasNewline := false
	flush := func() {
		rows = append(rows, wrappedRow{cellID: cellID, sourceOffset: lineStart, text: line.String()})
		line.Reset()
		lineWidth = 0
	}

	graphemes := uniseg.NewGraphemes(value)
	for graphemes.Next() {
		from, to := graphemes.Positions()
		cluster := graphemes.Str()
		if cluster == "\n" {
			flush()
			lineStart = to
			lastWasNewline = true
			continue
		}
		lastWasNewline = false
		isTab := cluster == "\t"
		clusterWidth := graphemes.Width()
		if isTab {
			clusterWidth = tabWidth - lineWidth%tabWidth
		}
		if line.Len() != 0 && lineWidth+clusterWidth > width {
			flush()
			lineStart = from
			if isTab {
				clusterWidth = tabWidth
			}
		}
		if isTab {
			clusterWidth = min(clusterWidth, width)
			cluster = strings.Repeat(" ", clusterWidth)
		}
		if clusterWidth > width {
			cluster, clusterWidth = "�", 1
		}
		if line.Len() == 0 {
			lineStart = from
		}
		line.WriteString(cluster)
		lineWidth += clusterWidth
	}
	if line.Len() != 0 || len(value) == 0 || lastWasNewline {
		flush()
	}
	return rows
}
