package testkit

import (
	tea "charm.land/bubbletea/v2"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
)

type FakeExecutor struct{ Handler func(app.Effect) tea.Msg }

func (f FakeExecutor) Execute(effect app.Effect) tea.Cmd {
	return func() tea.Msg { return f.Handler(effect) }
}
func (FakeExecutor) Close() error { return nil }
