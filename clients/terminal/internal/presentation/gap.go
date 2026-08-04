package presentation

// Rebuild preserves the last confirmed durable screen while invalidating the
// two server-driven live planes. Snapshot installation is the only recovery.
func Rebuild(durable State, operational OperationalState) (State, OperationalState) {
	return durable.MarkStale(), operational.Invalidate()
}
