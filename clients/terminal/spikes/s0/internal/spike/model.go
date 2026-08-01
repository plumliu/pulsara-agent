package spike

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
	"time"

	"charm.land/bubbles/v2/textarea"
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
)

const (
	LargePasteThresholdBytes = 256 * 1024
	maxTranscriptLines       = 24
	maxLatencySamples        = 4096
	maxUndoEntries           = 64
)

type StreamSnapshotMsg struct {
	Revision uint64
	Lines    []string
}

type StreamDeltaMsg struct {
	Sequence      uint64
	Content       string
	SentUnixNanos int64
}

type StreamFailureMsg struct {
	Reason string
}

type Metrics struct {
	Draft                 string `json:"draft"`
	DraftSHA256           string `json:"draft_sha256"`
	DeltaCount            uint64 `json:"delta_count"`
	LastDeltaSequence     uint64 `json:"last_delta_sequence"`
	LargePasteBytes       int    `json:"large_paste_bytes"`
	LargePasteSHA256      string `json:"large_paste_sha256"`
	DeliveryP50Micros     int64  `json:"delivery_p50_micros"`
	DeliveryP95Micros     int64  `json:"delivery_p95_micros"`
	DeliveryP99Micros     int64  `json:"delivery_p99_micros"`
	DeliveryMaxMicros     int64  `json:"delivery_max_micros"`
	DeliverySampleCount   int    `json:"delivery_sample_count"`
	Width                 int    `json:"width"`
	Height                int    `json:"height"`
	KeyboardDisambiguated bool   `json:"keyboard_disambiguated"`
	LastAction            string `json:"last_action"`
	StreamError           string `json:"stream_error,omitempty"`
}

type Model struct {
	composer textarea.Model
	lines    []string

	width  int
	height int

	revision              uint64
	deltaCount            uint64
	lastDeltaSequence     uint64
	deliveryLatencyMicros []int64

	largePasteBytes  int
	largePasteSHA256 string
	pasteActive      bool

	keyboardDisambiguated bool
	lastAction            string
	streamError           string
	undo                  []string
	redo                  []string
}

func NewModel() Model {
	composer := textarea.New()
	composer.Placeholder = "输入中文 / CJK / emoji；Ctrl+J 或 Shift+Enter 换行"
	composer.ShowLineNumbers = false
	composer.Prompt = "> "
	composer.DynamicHeight = true
	composer.MinHeight = 2
	composer.MaxHeight = 6
	composer.MaxContentHeight = 10_000
	composer.SetWidth(80)
	composer.SetHeight(2)
	composer.SetVirtualCursor(false)
	composer.Focus()

	return Model{
		composer: composer,
		lines:    []string{"fake snapshot: waiting for protobuf stream"},
		width:    80,
		height:   24,
	}
}

func (m Model) Init() tea.Cmd {
	return m.composer.Focus()
}

func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = max(msg.Width, 1)
		m.height = max(msg.Height, 1)
		m.composer.SetWidth(max(m.width-2, 1))
		return m, nil

	case tea.KeyboardEnhancementsMsg:
		m.keyboardDisambiguated = msg.SupportsKeyDisambiguation()
		return m, nil

	case tea.PasteStartMsg:
		m.pasteActive = true
		m.lastAction = "paste_started"
		return m, nil

	case tea.PasteEndMsg:
		m.pasteActive = false
		m.lastAction = "paste_completed"
		return m, nil

	case tea.PasteMsg:
		m.pasteActive = false
		if len(msg.Content) >= LargePasteThresholdBytes {
			sum := sha256.Sum256([]byte(msg.Content))
			m.largePasteBytes = len(msg.Content)
			m.largePasteSHA256 = hex.EncodeToString(sum[:])
			m.lastAction = "large_paste_externalized"
			return m, nil
		}
		return m.updateComposer(msg)

	case tea.KeyPressMsg:
		switch msg.Keystroke() {
		case "ctrl+q":
			m.lastAction = "normal_quit"
			return m, tea.Quit
		case "ctrl+g":
			panic("intentional S0 panic probe")
		case "esc":
			m.pasteActive = false
			m.lastAction = "escape"
			return m, nil
		case "enter":
			m.lastAction = "enter_submit_boundary"
			return m, nil
		case "shift+enter", "ctrl+j":
			m.lastAction = "newline"
			return m.updateComposer(tea.KeyPressMsg(tea.Key{Code: tea.KeyEnter}))
		case "ctrl+z":
			return m.undoOnce(), nil
		case "ctrl+shift+z":
			return m.redoOnce(), nil
		default:
			return m.updateComposer(msg)
		}

	case StreamSnapshotMsg:
		m.revision = msg.Revision
		m.lines = append([]string(nil), msg.Lines...)
		m.trimTranscript()
		return m, nil

	case StreamDeltaMsg:
		m.deltaCount++
		m.lastDeltaSequence = msg.Sequence
		m.lines = append(m.lines, msg.Content)
		m.trimTranscript()
		if msg.SentUnixNanos > 0 {
			latency := time.Since(time.Unix(0, msg.SentUnixNanos)).Microseconds()
			if latency >= 0 {
				m.deliveryLatencyMicros = append(m.deliveryLatencyMicros, latency)
				if len(m.deliveryLatencyMicros) > maxLatencySamples {
					m.deliveryLatencyMicros = m.deliveryLatencyMicros[len(m.deliveryLatencyMicros)-maxLatencySamples:]
				}
			}
		}
		return m, nil

	case StreamFailureMsg:
		m.streamError = msg.Reason
		return m, nil
	}

	return m, nil
}

func (m Model) View() tea.View {
	var b strings.Builder
	b.WriteString("Pulsara Bubble Tea S0 · disposable feasibility probe\n")
	b.WriteString(fmt.Sprintf("size=%dx%d revision=%d deltas=%d last=%d keyboard=%t\n", m.width, m.height, m.revision, m.deltaCount, m.lastDeltaSequence, m.keyboardDisambiguated))
	if m.streamError != "" {
		b.WriteString("stream error: " + m.streamError + "\n")
	}

	available := max(m.height-m.composer.Height()-6, 1)
	start := max(len(m.lines)-available, 0)
	for _, line := range m.lines[start:] {
		b.WriteString(line)
		b.WriteByte('\n')
	}

	b.WriteString(strings.Repeat("─", max(min(m.width, 80), 1)))
	b.WriteByte('\n')
	composerOffsetY := strings.Count(b.String(), "\n")
	b.WriteString(m.composer.View())
	b.WriteByte('\n')
	if m.largePasteBytes > 0 {
		b.WriteString(fmt.Sprintf("large paste: %d bytes sha256=%s… (not resident in textarea)\n", m.largePasteBytes, m.largePasteSHA256[:12]))
	}
	b.WriteString("Ctrl+J/Shift+Enter newline · Ctrl+Z undo · Esc cancel · Ctrl+Q quit · Ctrl+G panic")

	view := tea.NewView(b.String())
	view.AltScreen = true
	view.ReportFocus = true
	view.MouseMode = tea.MouseModeCellMotion
	view.KeyboardEnhancements.ReportEventTypes = true
	view.KeyboardEnhancements.ReportAlternateKeys = true
	view.Cursor = m.composer.Cursor()
	if view.Cursor != nil {
		view.Cursor.Position.Y += composerOffsetY
	}
	return view
}

func (m Model) Metrics() Metrics {
	draft := m.composer.Value()
	draftSum := sha256.Sum256([]byte(draft))
	latencies := append([]int64(nil), m.deliveryLatencyMicros...)
	sort.Slice(latencies, func(i, j int) bool { return latencies[i] < latencies[j] })
	return Metrics{
		Draft:                 draft,
		DraftSHA256:           hex.EncodeToString(draftSum[:]),
		DeltaCount:            m.deltaCount,
		LastDeltaSequence:     m.lastDeltaSequence,
		LargePasteBytes:       m.largePasteBytes,
		LargePasteSHA256:      m.largePasteSHA256,
		DeliveryP50Micros:     nearestRank(latencies, 50),
		DeliveryP95Micros:     nearestRank(latencies, 95),
		DeliveryP99Micros:     nearestRank(latencies, 99),
		DeliveryMaxMicros:     nearestRank(latencies, 100),
		DeliverySampleCount:   len(latencies),
		Width:                 m.width,
		Height:                m.height,
		KeyboardDisambiguated: m.keyboardDisambiguated,
		LastAction:            m.lastAction,
		StreamError:           m.streamError,
	}
}

func nearestRank(sortedValues []int64, percentile int) int64 {
	if len(sortedValues) == 0 {
		return 0
	}
	index := (percentile*len(sortedValues) + 99) / 100
	return sortedValues[max(index-1, 0)]
}

func (m Model) ComposerValue() string {
	return m.composer.Value()
}

func (m Model) ComposerHeight() int {
	return m.composer.Height()
}

func (m Model) CursorInfo() textarea.LineInfo {
	return m.composer.LineInfo()
}

func (m Model) updateComposer(msg tea.Msg) (tea.Model, tea.Cmd) {
	before := m.composer.Value()
	next, cmd := m.composer.Update(msg)
	after := next.Value()
	if after != before {
		m.undo = appendBounded(m.undo, before, maxUndoEntries)
		m.redo = nil
	}
	m.composer = next
	return m, cmd
}

func (m Model) undoOnce() Model {
	if len(m.undo) == 0 {
		m.lastAction = "undo_empty"
		return m
	}
	current := m.composer.Value()
	previous := m.undo[len(m.undo)-1]
	m.undo = m.undo[:len(m.undo)-1]
	m.redo = appendBounded(m.redo, current, maxUndoEntries)
	m.composer.SetValue(previous)
	m.lastAction = "undo"
	return m
}

func (m Model) redoOnce() Model {
	if len(m.redo) == 0 {
		m.lastAction = "redo_empty"
		return m
	}
	current := m.composer.Value()
	next := m.redo[len(m.redo)-1]
	m.redo = m.redo[:len(m.redo)-1]
	m.undo = appendBounded(m.undo, current, maxUndoEntries)
	m.composer.SetValue(next)
	m.lastAction = "redo"
	return m
}

func (m *Model) trimTranscript() {
	if len(m.lines) > maxTranscriptLines {
		m.lines = append([]string(nil), m.lines[len(m.lines)-maxTranscriptLines:]...)
	}
}

func appendBounded(values []string, value string, limit int) []string {
	values = append(values, value)
	if len(values) > limit {
		values = append([]string(nil), values[len(values)-limit:]...)
	}
	return values
}

func DisplayWidth(value string) int {
	return lipgloss.Width(value)
}
