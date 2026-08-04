package presentation

import "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"

func RequireControlSnapshot(state ControlProjectionState, latest protocolvalue.ControlCursor) (ControlProjectionState, error) {
	return state.RequireSnapshot(latest)
}
