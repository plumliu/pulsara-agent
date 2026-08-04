package app

import (
	tea "charm.land/bubbletea/v2"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

type Model struct {
	state    AppState
	executor Executor
	startup  AppStartedMsg
}

func NewModel(executor Executor) Model {
	clientInstanceID := "terminal-client:unknown"
	if owner, ok := executor.(interface{ ClientInstanceID() string }); ok && owner.ClientInstanceID() != "" {
		clientInstanceID = owner.ClientInstanceID()
	}
	state := NewInitialAppState(clientInstanceID)
	startup := AppStartedMsg{
		BootstrapHandleID:           "terminal-bootstrap:" + clientInstanceID,
		TransportCredentialHandleID: "terminal-launch-credential:" + clientInstanceID,
	}
	if owner, ok := executor.(interface {
		InitialHandshakeCandidate() protocolvalue.HandshakeCandidate
	}); ok {
		startup.HandshakeCandidate = owner.InitialHandshakeCandidate()
	}
	return Model{state: state, executor: executor, startup: startup}
}

func (m Model) Init() tea.Cmd { return func() tea.Msg { return m.startup } }

func (m Model) Update(message tea.Msg) (tea.Model, tea.Cmd) {
	state, effects, local := m.state.update(message)
	m.state = state
	commands := make([]tea.Cmd, 0, len(effects)+1)
	if local != nil {
		commands = append(commands, local)
	}
	for _, effect := range effects {
		commands = append(commands, m.executor.Execute(effect))
	}
	return m, tea.Batch(commands...)
}

func (m Model) View() tea.View  { return render(m.state) }
func (m Model) State() AppState { return m.state }
