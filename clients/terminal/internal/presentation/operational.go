package presentation

import "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"

func InstallOperationalSnapshot(state OperationalState, snapshot protocolvalue.OperationalSnapshot, runtimeSessionID string) (OperationalState, error) {
	return state.Install(snapshot, runtimeSessionID)
}
