package presentation

import "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"

func InstallDurableSnapshot(state State, snapshot protocolvalue.DurableSnapshot) (State, error) {
	return state.Install(snapshot)
}
