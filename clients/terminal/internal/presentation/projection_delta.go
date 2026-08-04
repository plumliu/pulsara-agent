package presentation

import "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"

func ApplyProjectionDelta(state State, delta protocolvalue.ProjectionDelta) (State, error) {
	return state.ApplyProjectionDelta(delta)
}
