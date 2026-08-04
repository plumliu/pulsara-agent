package presentation

import (
	"errors"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

// State is the immutable durable transcript projection consumed by the S1
// renderer. Snapshot sequencing is owned by app.SnapshotLoadingState.
type State struct {
	snapshot     protocolvalue.DurableSnapshot
	installed    bool
	scrollOffset int
	width        int
	height       int
}

func New() State { return State{width: 80, height: 24} }

func (s State) Install(value protocolvalue.DurableSnapshot) (State, error) {
	if s.installed || value.RuntimeSessionID == "" || value.Control.CursorFingerprint == "" || value.SnapshotFingerprint == "" {
		return State{}, errors.New("durable snapshot is invalid or already installed")
	}
	value.Cells = cloneHistoryCells(value.Cells)
	// The wire snapshot atomically carries both planes, but durable transcript
	// state must not retain a second copy of the control authority. Update
	// installs the validated control value into ControlProjectionState.
	value.Control = protocolvalue.ControlProjection{}
	s.snapshot = value
	s.installed = true
	return s, nil
}

func (s State) Resize(width, height int) State {
	if width < 1 {
		width = 1
	}
	if height < 1 {
		height = 1
	}
	s.width, s.height = width, height
	return s
}

func (s State) Scroll(delta int) State {
	// Scrolling is measured in visual rows. Decoded UTF-8 bytes are a safe
	// upper bound for the number of rows at the narrowest legal viewport; the
	// renderer clamps this conservative bound to the exact wrapped content.
	maximum := 0
	for _, cell := range s.snapshot.Cells {
		maximum += len([]byte(cell.PublicText)) + 2
	}
	maximum -= s.height
	if maximum < 0 {
		maximum = 0
	}
	s.scrollOffset += delta
	if s.scrollOffset < 0 {
		s.scrollOffset = 0
	}
	if s.scrollOffset > maximum {
		s.scrollOffset = maximum
	}
	return s
}

func (s State) Validate() error {
	if s.width < 1 || s.height < 1 {
		return errors.New("terminal viewport dimensions are invalid")
	}
	if s.installed && (s.snapshot.RuntimeSessionID == "" || s.snapshot.SnapshotFingerprint == "") {
		return errors.New("durable presentation baseline is invalid")
	}
	return nil
}

func (s State) Ready() bool { return s.installed }
func (s State) Durable() protocolvalue.DurableSnapshot {
	value := s.snapshot
	value.Cells = cloneHistoryCells(value.Cells)
	return value
}
func (s State) Width() int        { return s.width }
func (s State) Height() int       { return s.height }
func (s State) ScrollOffset() int { return s.scrollOffset }

type OperationalState struct {
	snapshot  protocolvalue.OperationalSnapshot
	installed bool
}

func NewOperational() OperationalState { return OperationalState{} }
func (s OperationalState) Install(value protocolvalue.OperationalSnapshot, runtimeSessionID string) (OperationalState, error) {
	if s.installed || value.RuntimeSessionID == "" || value.RuntimeSessionID != runtimeSessionID || value.FrameFingerprint == "" {
		return OperationalState{}, errors.New("operational snapshot is invalid or already installed")
	}
	value.Cells = cloneOperationalCells(value.Cells)
	s.snapshot, s.installed = value, true
	return s, nil
}
func (s OperationalState) Validate() error {
	if s.installed && (s.snapshot.RuntimeSessionID == "" || s.snapshot.FrameFingerprint == "") {
		return errors.New("operational presentation baseline is invalid")
	}
	return nil
}
func (s OperationalState) Ready() bool { return s.installed }
func (s OperationalState) Snapshot() protocolvalue.OperationalSnapshot {
	value := s.snapshot
	value.Cells = cloneOperationalCells(value.Cells)
	return value
}

type ControlProjectionState struct {
	projection protocolvalue.ControlProjection
	installed  bool
}

func NewControlProjection() ControlProjectionState { return ControlProjectionState{} }
func (s ControlProjectionState) Install(value protocolvalue.ControlProjection, runtimeSessionID string) (ControlProjectionState, error) {
	if s.installed || value.RuntimeSessionID != runtimeSessionID || value.CursorFingerprint == "" || value.ViewFingerprint == "" || value.ProjectionFingerprint != value.ViewFingerprint {
		return ControlProjectionState{}, errors.New("control projection baseline is invalid or already installed")
	}
	value.QueueItems = append([]protocolvalue.QueueItem(nil), value.QueueItems...)
	value.ServerNotifications = append([]protocolvalue.ServerNotification(nil), value.ServerNotifications...)
	s.projection, s.installed = value, true
	return s, nil
}
func (s ControlProjectionState) Validate() error {
	if s.installed && (s.projection.CursorFingerprint == "" || s.projection.ProjectionFingerprint != s.projection.ViewFingerprint) {
		return errors.New("control projection join is invalid")
	}
	return nil
}
func (s ControlProjectionState) Ready() bool { return s.installed }
func (s ControlProjectionState) Projection() protocolvalue.ControlProjection {
	value := s.projection
	value.QueueItems = append([]protocolvalue.QueueItem(nil), value.QueueItems...)
	value.ServerNotifications = append([]protocolvalue.ServerNotification(nil), value.ServerNotifications...)
	return value
}
