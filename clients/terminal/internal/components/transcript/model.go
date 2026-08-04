package transcript

import (
	"errors"

	"charm.land/lipgloss/v2"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

type viewportAnchor struct {
	cellID       string
	sourceOffset int
	label        bool
}

// Model is the only process-local owner of wrapped rows, scrolling and
// follow-tail state. Durable snapshot semantics remain owned by presentation.
type Model struct {
	cache               WrapCache
	width               int
	height              int
	scrollOffset        int
	followTail          bool
	unseenTerminalCells uint32
	installed           bool
	anchor              viewportAnchor
	hasAnchor           bool
}

func New(width, height int) Model {
	if width < 1 || height < 0 {
		panic("transcript viewport must be created from a validated layout")
	}
	return Model{width: width, height: height, followTail: true}
}

func (m Model) Install(snapshot protocolvalue.DurableSnapshot, width, height int) (Model, error) {
	if m.installed || snapshot.SnapshotFingerprint == "" || width < 1 || height < 0 {
		return Model{}, errors.New("transcript viewport snapshot is invalid or already installed")
	}
	cache, err := newWrapCache(snapshot, width)
	if err != nil {
		return Model{}, err
	}
	m.cache = cache
	m.width, m.height = width, height
	m.scrollOffset = 0
	m.followTail = true
	m.installed = true
	m.hasAnchor = false
	return m, nil
}

// Replace installs a newly confirmed incremental materialization while
// preserving the client-owned viewport anchor. It never merges server
// authority; presentation.State has already validated and applied the frame.
func (m Model) Replace(snapshot protocolvalue.DurableSnapshot, width, height int) (Model, error) {
	if !m.installed || snapshot.SnapshotFingerprint == "" || width < 1 || height < 0 {
		return Model{}, errors.New("transcript replacement is invalid")
	}
	anchor, hasAnchor := m.anchor, m.hasAnchor
	if !m.followTail && !hasAnchor {
		anchor, hasAnchor = m.currentAnchor()
	}
	previousIDs := map[string]bool{}
	for _, row := range m.cache.rows {
		previousIDs[row.cellID] = true
	}
	cache, err := newWrapCache(snapshot, width)
	if err != nil {
		return Model{}, err
	}
	cache.buildGeneration = m.cache.buildGeneration + 1
	newCells := uint32(0)
	seenNew := map[string]bool{}
	for _, row := range cache.rows {
		if !previousIDs[row.cellID] && !seenNew[row.cellID] {
			newCells++
			seenNew[row.cellID] = true
		}
	}
	m.cache, m.width, m.height = cache, width, height
	if m.followTail {
		m.scrollOffset, m.unseenTerminalCells, m.hasAnchor = 0, 0, false
		return m, m.Validate()
	}
	m.unseenTerminalCells += newCells
	if hasAnchor {
		start := m.cache.indexForAnchor(anchor)
		m.scrollOffset = len(m.cache.rows) - effectiveHeight(height) - start
	}
	m.clampScroll()
	m.refreshAnchor()
	return m, m.Validate()
}

func (m Model) Resize(snapshot protocolvalue.DurableSnapshot, width, height int) (Model, error) {
	if width < 1 || height < 0 {
		return Model{}, errors.New("transcript viewport dimensions are invalid")
	}
	if !m.installed {
		m.width, m.height = width, height
		return m, nil
	}
	if snapshot.SnapshotFingerprint == "" || snapshot.SnapshotFingerprint != m.cache.snapshotFingerprint {
		return Model{}, errors.New("transcript resize crosses snapshot identity")
	}
	anchor, hasAnchor := m.anchor, m.hasAnchor
	if !m.followTail && !hasAnchor {
		anchor, hasAnchor = m.currentAnchor()
	}
	if width != m.width {
		previousGeneration := m.cache.buildGeneration
		cache, err := newWrapCache(snapshot, width)
		if err != nil {
			return Model{}, err
		}
		cache.buildGeneration = previousGeneration + 1
		m.cache = cache
	}
	m.width, m.height = width, height
	if m.followTail {
		m.scrollOffset, m.hasAnchor = 0, false
		return m, nil
	}
	if hasAnchor {
		start := m.cache.indexForAnchor(anchor)
		m.scrollOffset = len(m.cache.rows) - effectiveHeight(height) - start
	}
	m.clampScroll()
	m.refreshAnchor()
	return m, nil
}

func (m Model) Scroll(delta int) Model {
	if !m.installed || delta == 0 {
		return m
	}
	m.scrollOffset += delta
	m.clampScroll()
	m.followTail = m.scrollOffset == 0
	m.refreshAnchor()
	return m
}

func (m Model) Page(direction int) Model {
	if direction == 0 {
		return m
	}
	delta := max(effectiveHeight(m.height)-1, 1)
	if direction < 0 {
		delta = -delta
	}
	return m.Scroll(delta)
}

func (m Model) End() Model {
	m.scrollOffset = 0
	m.followTail = true
	m.unseenTerminalCells = 0
	m.hasAnchor = false
	return m
}

// Pin freezes the current content anchor even when the currently materialized
// vector fits entirely in the viewport. History page hydration can then add
// rows before/after that vector without snapping the user back to its tail.
func (m Model) Pin() Model {
	if !m.installed || !m.followTail {
		return m
	}
	m.anchor, m.hasAnchor = m.currentAnchor()
	if m.hasAnchor {
		m.followTail = false
	}
	return m
}

// NoteUnseen records durable cells installed on a newer root while the
// viewport remains pinned to an older immutable root. The pinned viewport is
// necessarily outside follow-tail mode, so this cannot create a hidden unseen
// count on the live tail.
func (m Model) NoteUnseen(count uint32) Model {
	if count == 0 || !m.installed || m.followTail {
		return m
	}
	if ^uint32(0)-m.unseenTerminalCells < count {
		m.unseenTerminalCells = ^uint32(0)
	} else {
		m.unseenTerminalCells += count
	}
	return m
}

func (m Model) RenderLines() []string {
	if !m.installed {
		return nil
	}
	if m.height == 0 {
		return nil
	}
	if len(m.cache.rows) == 0 {
		return []string{lipgloss.NewStyle().Faint(true).Render("No conversation history yet.")}
	}
	end := len(m.cache.rows) - m.scrollOffset
	start := max(end-m.height, 0)
	result := make([]string, 0, end-start)
	for _, row := range m.cache.rows[start:end] {
		if row.label {
			result = append(result, lipgloss.NewStyle().Bold(true).Render(row.text))
		} else {
			result = append(result, row.text)
		}
	}
	return result
}

func (m Model) Validate() error {
	if m.width < 1 || m.height < 0 {
		return errors.New("transcript viewport state is invalid")
	}
	if !m.installed {
		if m.scrollOffset != 0 || !m.followTail || m.hasAnchor || m.cache.snapshotFingerprint != "" {
			return errors.New("uninstalled transcript viewport owns prepared state")
		}
		return nil
	}
	if err := m.cache.validate(); err != nil {
		return err
	}
	maximum := max(len(m.cache.rows)-effectiveHeight(m.height), 0)
	if m.scrollOffset < 0 || m.scrollOffset > maximum || (m.followTail && m.scrollOffset != 0) {
		return errors.New("transcript scroll/follow-tail join is invalid")
	}
	if m.hasAnchor == m.followTail {
		return errors.New("transcript resize anchor ownership is invalid")
	}
	if m.followTail && m.unseenTerminalCells != 0 {
		return errors.New("follow-tail transcript owns unseen cells")
	}
	return nil
}

func (m Model) Ready() bool                 { return m.installed }
func (m Model) Width() int                  { return m.width }
func (m Model) Height() int                 { return m.height }
func (m Model) ScrollOffset() int           { return m.scrollOffset }
func (m Model) FollowTail() bool            { return m.followTail }
func (m Model) UnseenTerminalCount() uint32 { return m.unseenTerminalCells }
func (m Model) SnapshotFingerprint() string { return m.cache.snapshotFingerprint }
func (m Model) WrappedRowCount() int        { return len(m.cache.rows) }
func (m Model) WrapBuildGeneration() uint64 { return m.cache.buildGeneration }
func (m Model) AtTop() bool {
	return m.installed && m.scrollOffset == max(len(m.cache.rows)-effectiveHeight(m.height), 0)
}

func (m *Model) clampScroll() {
	maximum := max(len(m.cache.rows)-effectiveHeight(m.height), 0)
	if m.scrollOffset < 0 {
		m.scrollOffset = 0
	}
	if m.scrollOffset > maximum {
		m.scrollOffset = maximum
	}
	if m.scrollOffset > 0 {
		m.followTail = false
	}
	if m.followTail {
		m.hasAnchor = false
	}
}

func (m *Model) refreshAnchor() {
	if m.followTail {
		m.hasAnchor = false
		return
	}
	m.anchor, m.hasAnchor = m.currentAnchor()
}

func (m Model) currentAnchor() (viewportAnchor, bool) {
	if len(m.cache.rows) == 0 {
		return viewportAnchor{}, false
	}
	end := len(m.cache.rows) - m.scrollOffset
	start := max(end-effectiveHeight(m.height), 0)
	row := m.cache.rows[start]
	return viewportAnchor{cellID: row.cellID, sourceOffset: row.sourceOffset, label: row.label}, true
}

func effectiveHeight(height int) int {
	return max(height, 1)
}
