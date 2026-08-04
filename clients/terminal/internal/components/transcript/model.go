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
	m.hasAnchor = false
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
	if m.width < 1 || m.height < 0 || m.unseenTerminalCells != 0 {
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
	if m.scrollOffset < 0 || m.scrollOffset > maximum || m.followTail != (m.scrollOffset == 0) {
		return errors.New("transcript scroll/follow-tail join is invalid")
	}
	if m.hasAnchor == m.followTail {
		return errors.New("transcript resize anchor ownership is invalid")
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

func (m *Model) clampScroll() {
	maximum := max(len(m.cache.rows)-effectiveHeight(m.height), 0)
	if m.scrollOffset < 0 {
		m.scrollOffset = 0
	}
	if m.scrollOffset > maximum {
		m.scrollOffset = maximum
	}
	m.followTail = m.scrollOffset == 0
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
