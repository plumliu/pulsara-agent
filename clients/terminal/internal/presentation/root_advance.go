package presentation

import "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"

func ApplyRootAdvance(state State, advance protocolvalue.RootAdvance) (State, bool, error) {
	return state.ApplyRootAdvance(advance)
}
