package transcript

import (
	"charm.land/bubbles/v2/viewport"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/presentation"
)

type Model struct {
	state    presentation.State
	viewport viewport.Model
}

func New(state presentation.State) Model {
	return Model{
		state: state,
		viewport: viewport.New(
			viewport.WithWidth(state.Width()),
			viewport.WithHeight(state.Height()),
		),
	}
}
