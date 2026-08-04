package app

import (
	"context"
	"os"
	"time"

	tea "charm.land/bubbletea/v2"
)

// productionInputBoundary is the only owner allowed to translate framework
// messages into the closed App message vocabulary. The inner Model.Update
// remains deterministic and sees neither framework input nor a wall clock.
type productionInputBoundary struct {
	inner             Model
	headerGeneration  uint64
	nextLocalSequence uint64
}

func (m productionInputBoundary) Init() tea.Cmd { return m.inner.Init() }

func (m productionInputBoundary) Update(message tea.Msg) (tea.Model, tea.Cmd) {
	if m.headerGeneration != m.inner.state.appGeneration {
		m.headerGeneration = m.inner.state.appGeneration
		m.nextLocalSequence = 1
	}
	if _, isApplicationMessage := message.(applicationMessage); !isApplicationMessage {
		header := m.nextHeader(time.Now())
		if normalized, ok := normalizeFrameworkMessage(message, header); ok {
			message = normalized
		} else {
			message = FrameworkInputRejectedMsg{Header: header}
		}
	} else if _, isLocalMessage := localMessageHeader(message); isLocalMessage {
		header := m.nextHeader(time.Now())
		message, _ = installLocalMessageHeader(message, header)
	}
	next, command := m.inner.Update(message)
	inner, ok := next.(Model)
	if !ok {
		panic("terminal application model changed concrete type")
	}
	m.inner = inner
	return m, command
}

func (m *productionInputBoundary) nextHeader(producedAt time.Time) LocalMessageHeader {
	if m.nextLocalSequence == 0 {
		m.nextLocalSequence = 1
	}
	header, err := NewLocalMessageHeader(m.headerGeneration, m.nextLocalSequence, producedAt)
	if err != nil {
		panic(err)
	}
	m.nextLocalSequence++
	return header
}

func (m productionInputBoundary) View() tea.View  { return m.inner.View() }
func (m productionInputBoundary) State() AppState { return m.inner.State() }

func NewProductionProgram(model Model, programContext context.Context, sanitizedEnvironment []string) *tea.Program {
	return tea.NewProgram(
		productionInputBoundary{inner: model, headerGeneration: model.state.appGeneration, nextLocalSequence: 1},
		tea.WithContext(programContext),
		tea.WithInput(os.Stdin),
		tea.WithOutput(os.Stdout),
		tea.WithEnvironment(append([]string(nil), sanitizedEnvironment...)),
		tea.WithoutSignalHandler(),
		tea.WithFPS(60),
	)
}
