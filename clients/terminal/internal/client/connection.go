package client

import (
	"errors"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	terminalwire "github.com/plumliu/pulsara-agent/clients/terminal/internal/wire"
)

type PhysicalConnectionDrainRecordState uint8

const (
	PhysicalDrainReserved PhysicalConnectionDrainRecordState = iota + 1
	PhysicalDrainStarting
	PhysicalDrainRunning
	PhysicalDrainTerminal
)

type PhysicalConnectionDrainStartDisposition uint8

const (
	PhysicalDrainCreated PhysicalConnectionDrainStartDisposition = iota + 1
	PhysicalDrainCompatibleAlreadyCreated
	PhysicalDrainConflict
)

// PhysicalConnectionDrainHandle is stable process-local identity. Its fields
// remain private so only the connection owner can construct or rebind it.
type PhysicalConnectionDrainHandle struct {
	drainID                    string
	drainGeneration            uint64
	attemptIdentityFingerprint string
	connectionID               string
	connectionGeneration       uint64
	bindingGeneration          uint64
	bindingFingerprint         string
	drainIdentityFingerprint   string
}

type PhysicalConnectionTerminalReceipt struct {
	drainIdentityFingerprint string
	raw                      terminalwire.PhysicalConnectionTerminalReceipt
	receiptFingerprint       string
}

func (r PhysicalConnectionTerminalReceipt) validate(
	handle PhysicalConnectionDrainHandle,
) error {
	if handle.drainIdentityFingerprint == "" ||
		r.drainIdentityFingerprint != handle.drainIdentityFingerprint ||
		r.raw.ConnectionID != handle.connectionID ||
		r.raw.ConnectionGeneration != handle.connectionGeneration ||
		r.receiptFingerprint == "" || r.raw.Validate() != nil {
		return errors.New("terminal physical drain receipt does not join its winner")
	}
	expected, err := protocol.CanonicalJSONFingerprint(
		"terminal-connection-owner-terminal-receipt:v1",
		map[string]any{
			"physical_drain_identity_fingerprint": r.drainIdentityFingerprint,
			"raw_terminal_receipt_fingerprint":    r.raw.ReceiptFingerprint,
		},
	)
	if err != nil || expected != r.receiptFingerprint {
		return errors.New("terminal physical drain receipt fingerprint mismatch")
	}
	return nil
}

type PhysicalConnectionDrainStartResult struct {
	disposition PhysicalConnectionDrainStartDisposition
	handle      PhysicalConnectionDrainHandle
	hasHandle   bool
	state       PhysicalConnectionDrainRecordState
}

type PhysicalConnectionDrainLaunchPermit struct {
	drainIdentityFingerprint string
	launchGeneration         uint64
	launchPermitFingerprint  string
}

type PhysicalConnectionDrainRunnerLease struct {
	launchPermitFingerprint string
	runnerGeneration        uint64
	runnerLeaseFingerprint  string
}

type physicalConnectionDrainRecord struct {
	handle         PhysicalConnectionDrainHandle
	state          PhysicalConnectionDrainRecordState
	stateRevision  uint64
	launchPermit   PhysicalConnectionDrainLaunchPermit
	runnerLease    PhysicalConnectionDrainRunnerLease
	hasRunnerLease bool
	raw            terminalwire.PhysicalConnectionTerminalReceipt
	receipt        PhysicalConnectionTerminalReceipt
	err            error
	done           chan struct{}
	driving        bool
}

// physicalConnectionOwner is the only client-level owner of a socket and of
// the stable drain winner used to settle a fully-started operation.
type physicalConnectionOwner struct {
	raw  *terminalwire.Connection
	peer terminalwire.PeerCredentials

	socketOwnerUID         uint64
	runtimePathFingerprint string

	mu                 sync.Mutex
	drainGeneration    uint64
	drain              *physicalConnectionDrainRecord
	drainWorkers       sync.WaitGroup
	bindingGeneration  uint64
	bindingFingerprint string
}

func openConnection(path string) (*physicalConnectionOwner, error) {
	socketOwnerUID, runtimePathFingerprint, err := inspectLocalSocket(path)
	if err != nil {
		return nil, err
	}
	raw, err := terminalwire.Dial(path, 5*time.Second)
	if err != nil {
		return nil, err
	}
	peer, err := raw.PeerCredentials()
	if err != nil {
		_ = raw.Close()
		return nil, err
	}
	if peer.UID != uint64(os.Geteuid()) || peer.UID != socketOwnerUID {
		_ = raw.Close()
		return nil, errors.New("terminal protocol peer UID mismatch")
	}
	return &physicalConnectionOwner{
		raw:                    raw,
		peer:                   peer,
		socketOwnerUID:         socketOwnerUID,
		runtimePathFingerprint: runtimePathFingerprint,
	}, nil
}

func (c *physicalConnectionOwner) peerIdentityParts() (
	terminalwire.PeerCredentials,
	uint64,
	string,
	error,
) {
	if c == nil || c.raw == nil || c.peer.UID != uint64(os.Geteuid()) ||
		c.socketOwnerUID != c.peer.UID || c.runtimePathFingerprint == "" {
		return terminalwire.PeerCredentials{}, 0, "", errors.New(
			"terminal peer identity proof is unavailable",
		)
	}
	return c.peer, c.socketOwnerUID, c.runtimePathFingerprint, nil
}

func (c *physicalConnectionOwner) Authenticate(
	preface *protocol.TerminalTransportAuthPreface,
	timeout time.Duration,
	operationID string,
	operationGeneration uint64,
) (*protocol.TerminalTransportAuthResult, error) {
	return c.raw.Authenticate(preface, timeout, operationID, operationGeneration)
}

func (c *physicalConnectionOwner) RoundTrip(
	frame *protocol.ClientFrame,
	timeout time.Duration,
	operationID string,
	operationGeneration uint64,
) (*protocol.ServerFrame, error) {
	return c.raw.RoundTrip(frame, timeout, operationID, operationGeneration)
}

func (c *physicalConnectionOwner) SetMaximumFrameBytes(value uint32) error {
	return c.raw.SetMaximumFrameBytes(value)
}

func (c *physicalConnectionOwner) setTransportBinding(
	generation uint64,
	fingerprint string,
) error {
	if generation == 0 || fingerprint == "" {
		return errors.New("terminal transport binding is invalid")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.bindingGeneration > generation ||
		(c.bindingGeneration == generation && c.bindingFingerprint != "" &&
			c.bindingFingerprint != fingerprint) {
		return errors.New("terminal transport binding moved backwards or conflicted")
	}
	c.bindingGeneration = generation
	c.bindingFingerprint = fingerprint
	return nil
}

func (c *physicalConnectionOwner) Close() error { return c.raw.Close() }

func (c *physicalConnectionOwner) CloseAndWait(deadline time.Time) error {
	closeErr := c.raw.CloseAndWait(deadline)
	done := make(chan struct{})
	go func() {
		c.drainWorkers.Wait()
		close(done)
	}()
	duration := time.Until(deadline)
	if duration <= 0 {
		return errors.New("terminal connection-owner drain deadline expired")
	}
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-done:
		return closeErr
	case <-timer.C:
		return errors.New("terminal connection-owner drain deadline expired")
	}
}

func (c *physicalConnectionOwner) startInvalidateClose(
	identity ConnectionTerminalizationAttemptIdentity,
	raw terminalwire.PhysicalConnectionTerminalReceipt,
) (PhysicalConnectionDrainStartResult, error) {
	if identity.attemptIdentityFingerprint == "" || raw.Validate() != nil {
		return PhysicalConnectionDrainStartResult{}, errors.New(
			"terminal physical drain input is invalid",
		)
	}
	connectionID, connectionGeneration := c.raw.Identity()
	if raw.ConnectionID != connectionID || raw.ConnectionGeneration != connectionGeneration {
		return PhysicalConnectionDrainStartResult{}, errors.New(
			"terminal physical drain receipt belongs to another connection",
		)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.drain != nil {
		if c.drain.handle.attemptIdentityFingerprint != identity.attemptIdentityFingerprint {
			return PhysicalConnectionDrainStartResult{
				disposition: PhysicalDrainConflict,
				state:       c.drain.state,
			}, nil
		}
		c.ensureDrainWorkerLocked(c.drain)
		return PhysicalConnectionDrainStartResult{
			disposition: PhysicalDrainCompatibleAlreadyCreated,
			handle:      c.drain.handle,
			hasHandle:   true,
			state:       c.drain.state,
		}, nil
	}
	c.drainGeneration++
	drainFingerprint, err := protocol.CanonicalJSONFingerprint(
		"terminal-physical-connection-drain-identity:v1",
		map[string]any{
			"attempt_identity_fingerprint": identity.attemptIdentityFingerprint,
			"drain_generation":             c.drainGeneration,
			"connection_id":                raw.ConnectionID,
			"connection_generation":        raw.ConnectionGeneration,
			"binding_generation":           c.bindingGeneration,
			"binding_fingerprint":          c.bindingFingerprint,
			"raw_terminal_fingerprint":     raw.ReceiptFingerprint,
		},
	)
	if err != nil {
		return PhysicalConnectionDrainStartResult{}, err
	}
	handle := PhysicalConnectionDrainHandle{
		drainID:                    "terminal-drain:" + drainFingerprint[len("sha256:"):],
		drainGeneration:            c.drainGeneration,
		attemptIdentityFingerprint: identity.attemptIdentityFingerprint,
		connectionID:               raw.ConnectionID,
		connectionGeneration:       raw.ConnectionGeneration,
		bindingGeneration:          c.bindingGeneration,
		bindingFingerprint:         c.bindingFingerprint,
		drainIdentityFingerprint:   drainFingerprint,
	}
	record := &physicalConnectionDrainRecord{
		handle:        handle,
		state:         PhysicalDrainReserved,
		stateRevision: 1,
		raw:           raw,
		done:          make(chan struct{}),
	}
	record.launchPermit = PhysicalConnectionDrainLaunchPermit{
		drainIdentityFingerprint: handle.drainIdentityFingerprint,
		launchGeneration:         1,
	}
	record.launchPermit.launchPermitFingerprint, _ =
		protocol.CanonicalJSONFingerprint(
			"terminal-physical-drain-launch-permit:v1",
			map[string]any{
				"drain_identity_fingerprint": handle.drainIdentityFingerprint,
				"launch_generation":          uint64(1),
			},
		)
	c.drain = record
	c.ensureDrainWorkerLocked(record)
	return PhysicalConnectionDrainStartResult{
		disposition: PhysicalDrainCreated,
		handle:      handle,
		hasHandle:   true,
		state:       PhysicalDrainReserved,
	}, nil
}

func (c *physicalConnectionOwner) ensureDrainWorkerLocked(
	record *physicalConnectionDrainRecord,
) {
	if record.driving || record.state == PhysicalDrainTerminal {
		return
	}
	record.driving = true
	record.runnerLease = PhysicalConnectionDrainRunnerLease{
		launchPermitFingerprint: record.launchPermit.launchPermitFingerprint,
		runnerGeneration:        record.stateRevision,
	}
	record.runnerLease.runnerLeaseFingerprint, _ =
		protocol.CanonicalJSONFingerprint(
			"terminal-physical-drain-runner-lease:v1",
			map[string]any{
				"launch_permit_fingerprint": record.launchPermit.launchPermitFingerprint,
				"runner_generation":         record.runnerLease.runnerGeneration,
			},
		)
	record.hasRunnerLease = true
	c.drainWorkers.Add(1)
	go c.driveDrainRecord(record)
}

func (c *physicalConnectionOwner) driveDrainRecord(
	record *physicalConnectionDrainRecord,
) {
	defer c.drainWorkers.Done()
	defer func() {
		if recovered := recover(); recovered != nil {
			c.mu.Lock()
			record.driving = false
			record.hasRunnerLease = false
			record.stateRevision++
			record.state = PhysicalDrainReserved
			c.ensureDrainWorkerLocked(record)
			c.mu.Unlock()
		}
	}()
	c.mu.Lock()
	record.stateRevision++
	record.state = PhysicalDrainStarting
	c.mu.Unlock()
	// The low-level round trip has already joined its reader/writer before it
	// can yield the raw receipt. This owner still performs the idempotent close
	// and waits through the same physical boundary before publishing terminal.
	c.mu.Lock()
	record.stateRevision++
	record.state = PhysicalDrainRunning
	c.mu.Unlock()
	err := c.raw.CloseAndWait(time.Now().Add(5 * time.Second))
	receipt := PhysicalConnectionTerminalReceipt{
		drainIdentityFingerprint: record.handle.drainIdentityFingerprint,
		raw:                      record.raw,
	}
	receipt.receiptFingerprint, _ = protocol.CanonicalJSONFingerprint(
		"terminal-connection-owner-terminal-receipt:v1",
		map[string]any{
			"physical_drain_identity_fingerprint": receipt.drainIdentityFingerprint,
			"raw_terminal_receipt_fingerprint":    receipt.raw.ReceiptFingerprint,
		},
	)
	if validationErr := receipt.validate(record.handle); validationErr != nil {
		err = validationErr
	}
	c.mu.Lock()
	record.receipt = receipt
	record.err = err
	record.stateRevision++
	record.state = PhysicalDrainTerminal
	record.driving = false
	record.hasRunnerLease = false
	close(record.done)
	c.mu.Unlock()
}

func (c *physicalConnectionOwner) rebindPhysicalDrain(
	identity ConnectionTerminalizationAttemptIdentity,
	drainIdentityFingerprint string,
) (PhysicalConnectionDrainHandle, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.drain == nil ||
		c.drain.handle.attemptIdentityFingerprint != identity.attemptIdentityFingerprint ||
		c.drain.handle.drainIdentityFingerprint != drainIdentityFingerprint {
		return PhysicalConnectionDrainHandle{}, errors.New(
			"terminal physical drain rebind is stale",
		)
	}
	c.ensureDrainWorkerLocked(c.drain)
	return c.drain.handle, nil
}

func (c *physicalConnectionOwner) waitPhysicalDrain(
	handle PhysicalConnectionDrainHandle,
) (PhysicalConnectionTerminalReceipt, error) {
	c.mu.Lock()
	if c.drain == nil || c.drain.handle != handle {
		c.mu.Unlock()
		return PhysicalConnectionTerminalReceipt{}, errors.New(
			"terminal physical drain handle is stale",
		)
	}
	record := c.drain
	c.ensureDrainWorkerLocked(record)
	done := record.done
	c.mu.Unlock()
	<-done
	c.mu.Lock()
	defer c.mu.Unlock()
	if record.err != nil {
		return PhysicalConnectionTerminalReceipt{}, record.err
	}
	if err := record.receipt.validate(handle); err != nil {
		return PhysicalConnectionTerminalReceipt{}, err
	}
	return record.receipt, nil
}

func validateLocalSocket(path string) error {
	_, _, err := inspectLocalSocket(path)
	return err
}

func inspectLocalSocket(path string) (uint64, string, error) {
	if !filepath.IsAbs(path) {
		return 0, "", errors.New("terminal protocol socket path is not absolute")
	}
	parent := filepath.Dir(path)
	parentInfo, err := os.Lstat(parent)
	if err != nil {
		return 0, "", errors.New("terminal protocol runtime directory is unavailable")
	}
	if parentInfo.Mode()&os.ModeSymlink != 0 || !parentInfo.IsDir() || parentInfo.Mode().Perm()&0o077 != 0 || !ownedByCurrentUser(parentInfo) {
		return 0, "", errors.New("terminal protocol runtime directory is unsafe")
	}
	socketInfo, err := os.Lstat(path)
	if err != nil {
		return 0, "", errors.New("terminal protocol socket is unavailable")
	}
	if socketInfo.Mode()&os.ModeSymlink != 0 || socketInfo.Mode()&os.ModeSocket == 0 || socketInfo.Mode().Perm()&0o077 != 0 || !ownedByCurrentUser(socketInfo) {
		return 0, "", errors.New("terminal protocol socket is unsafe")
	}
	metadata, ok := socketInfo.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, "", errors.New("terminal protocol socket ownership is unavailable")
	}
	fingerprint, err := protocol.CanonicalJSONFingerprint(
		"terminal-runtime-socket-path:v1",
		map[string]any{
			"runtime_directory":      filepath.Clean(parent),
			"runtime_directory_mode": uint32(parentInfo.Mode().Perm()),
			"socket_path":            filepath.Clean(path),
			"socket_mode":            uint32(socketInfo.Mode().Perm()),
			"socket_owner_uid":       uint64(metadata.Uid),
		},
	)
	if err != nil {
		return 0, "", err
	}
	return uint64(metadata.Uid), fingerprint, nil
}

func ownedByCurrentUser(info os.FileInfo) bool {
	metadata, ok := info.Sys().(*syscall.Stat_t)
	return ok && metadata.Uid == uint32(os.Geteuid())
}
