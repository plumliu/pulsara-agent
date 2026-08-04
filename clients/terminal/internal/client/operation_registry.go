package client

import (
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	terminalwire "github.com/plumliu/pulsara-agent/clients/terminal/internal/wire"
)

type physicalOperationStage uint8

const (
	physicalInstalled physicalOperationStage = iota + 1
	physicalWriteStarted
	physicalRequestFullySent
	physicalResponseReadStarted
	physicalResponseFullyValidated
	physicalTerminalizing
	physicalTerminal
)

type physicalOperationRecord struct {
	operation            app.OutstandingOperation
	stage                physicalOperationStage
	settled              bool
	settlementCapability OperationSettlementCapability
}

type OperationSettlementCapability struct {
	operationID           string
	operationGeneration   uint64
	capabilityFingerprint string
	consumed              bool
}

type PhysicalFailureCause = app.PhysicalFailureCause

const (
	PhysicalCauseWriteFailed  = app.CauseWriteFailed
	PhysicalCauseReadDeadline = app.CauseDeadlineExpired
	PhysicalCauseEOF          = app.CauseEOF
	PhysicalCauseReadFailed   = app.CauseReadFailed
)

type PostJoinOperationSettlementCapability struct {
	operationID                          string
	operationGeneration                  uint64
	preparedTerminalizationFingerprint   string
	terminalizationCapabilityFingerprint string
	frozenPhysicalCause                  PhysicalFailureCause
	capabilityFingerprint                string
	consumed                             bool
}

type PhysicalFailureSignal struct {
	cause            PhysicalFailureCause
	causeFingerprint string
}

type PreparedConnectionTerminalization struct {
	postJoinSettlementCapability       PostJoinOperationSettlementCapability
	frozenFailureSignalFingerprint     string
	preparedTerminalizationFingerprint string
}

type ConnectionTerminalizationAttemptIdentity struct {
	attemptID                          string
	attemptGeneration                  uint64
	operationID                        string
	operationGeneration                uint64
	frozenFailureSignalFingerprint     string
	preparedTerminalizationFingerprint string
	attemptIdentityFingerprint         string
}

type ConnectionTerminalizationAttemptHandle struct {
	attemptID                  string
	attemptGeneration          uint64
	operationID                string
	operationGeneration        uint64
	attemptIdentityFingerprint string
}

type ConnectionTerminalizationAttemptState uint8

const (
	TerminalizationAttemptInstalled ConnectionTerminalizationAttemptState = iota + 1
	TerminalizationAttemptInvalidating
	TerminalizationAttemptPhysicalDraining
	TerminalizationAttemptReceiptReady
	TerminalizationAttemptSettling
	TerminalizationAttemptTerminal
)

type ConnectionTerminalizationWaitDisposition uint8

const (
	TerminalizationWaitCompleted ConnectionTerminalizationWaitDisposition = iota + 1
	TerminalizationWaiterCancelled
	TerminalizationWaiterDeadline
)

type PhysicalOperationFailureReceipt struct {
	operation                  app.OutstandingOperation
	cause                      PhysicalFailureCause
	connectionTerminal         PhysicalConnectionTerminalReceipt
	physicalReceiptFingerprint string
}

type ConnectionTerminalizationWaitResult struct {
	disposition           ConnectionTerminalizationWaitDisposition
	attemptHandle         ConnectionTerminalizationAttemptHandle
	completionFingerprint string
	failureReceipt        PhysicalOperationFailureReceipt
	hasFailureReceipt     bool
}

type connectionTerminalizationAttempt struct {
	identity      ConnectionTerminalizationAttemptIdentity
	handle        ConnectionTerminalizationAttemptHandle
	prepared      PreparedConnectionTerminalization
	state         ConnectionTerminalizationAttemptState
	stateRevision uint64
	operation     app.OutstandingOperation
	physical      *terminalwire.PhysicalIOError
	connection    *physicalConnectionOwner
	done          chan struct{}
	failure       PhysicalOperationFailureReceipt
	completion    string
	err           error
}

type operationRegistry struct {
	mu                    sync.Mutex
	active                map[string]physicalOperationRecord
	attempts              map[string]*connectionTerminalizationAttempt
	nextAttemptGeneration uint64
	attemptWorkers        sync.WaitGroup
	closing               bool
}

func newOperationRegistry() *operationRegistry {
	return &operationRegistry{
		active:   map[string]physicalOperationRecord{},
		attempts: map[string]*connectionTerminalizationAttempt{},
	}
}

func operationID(value app.OutstandingOperation) string {
	switch value.Carrier {
	case app.OutstandingWire:
		return value.Wire.OperationID
	case app.OutstandingLocal:
		return value.Local.OperationID
	default:
		return ""
	}
}

func operationGeneration(value app.OutstandingOperation) uint64 {
	switch value.Carrier {
	case app.OutstandingWire:
		return value.Wire.OperationGeneration
	case app.OutstandingLocal:
		return value.Local.OperationGeneration
	default:
		return 0
	}
}

func (r *operationRegistry) begin(operation app.OutstandingOperation) error {
	if !operation.Valid() || operation.Carrier == app.OutstandingNone {
		return errors.New("terminal operation token is invalid")
	}
	id := operationID(operation)
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing {
		return errors.New("terminal operation registry is closing")
	}
	if _, exists := r.active[id]; exists {
		return errors.New("terminal operation is duplicated")
	}
	capabilityFingerprint, err := protocol.CanonicalJSONFingerprint(
		"terminal-operation-settlement-capability:v1",
		map[string]any{
			"operation_id":         id,
			"operation_generation": operationGeneration(operation),
		},
	)
	if err != nil {
		return err
	}
	r.active[id] = physicalOperationRecord{
		operation: operation,
		stage:     physicalInstalled,
		settlementCapability: OperationSettlementCapability{
			operationID:           id,
			operationGeneration:   operationGeneration(operation),
			capabilityFingerprint: capabilityFingerprint,
		},
	}
	return nil
}

func (r *operationRegistry) finishSuccess(operation app.OutstandingOperation) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	id := operationID(operation)
	record, ok := r.active[id]
	if !ok || record.operation != operation || record.settled {
		return errors.New("terminal operation success settlement is stale")
	}
	if record.settlementCapability.consumed {
		return errors.New("terminal operation settlement capability was consumed")
	}
	record.settlementCapability.consumed = true
	record.stage, record.settled = physicalResponseFullyValidated, true
	delete(r.active, id)
	return nil
}

func (r *operationRegistry) classifyFailure(
	operation app.OutstandingOperation,
	operationErr error,
	publicMessage string,
	connection *physicalConnectionOwner,
) app.PublicFailure {
	var physical *terminalwire.PhysicalIOError
	if errors.As(operationErr, &physical) &&
		(physical.Phase == terminalwire.DeliveryWriteStarted ||
			physical.Phase == terminalwire.DeliveryResponseReadStarted) {
		handle, err := r.beginConnectionTerminalization(
			operation,
			physical,
			connection,
		)
		if err != nil {
			return r.classifyUnadmittedFailure(
				operation,
				publicMessage,
				"terminalization-install-failed",
			)
		}
		result, err := r.waitConnectionTerminalization(
			handle,
			nil,
			time.Now().Add(5*time.Second),
		)
		if err != nil || result.disposition != TerminalizationWaitCompleted ||
			!result.hasFailureReceipt {
			return r.classifyUnadmittedFailure(
				operation,
				publicMessage,
				"terminalization-wait-incomplete",
			)
		}
		return classifySealedPublicFailure(
			operation,
			failureDeliveryPhase(physical),
			app.FailureConnectionInvalidated,
			physicalCause(physical),
			result.failureReceipt.connectionTerminal.receiptFingerprint,
			true,
			result.failureReceipt.physicalReceiptFingerprint,
			publicMessage,
		)
	}

	r.mu.Lock()
	defer r.mu.Unlock()
	id := operationID(operation)
	record, ok := r.active[id]
	if !ok || record.operation != operation || record.settled {
		return r.classifyUnadmittedFailure(operation, publicMessage, "missing-operation-record")
	}
	if record.settlementCapability.consumed {
		return r.classifyUnadmittedFailure(
			operation,
			publicMessage,
			"settlement-capability-already-consumed",
		)
	}
	record.settlementCapability.consumed = true
	deliveryPhase := app.DeliveryLocalOperationStarted
	connectionState := app.FailureConnectionUsable
	cause := nonPhysicalFailureCause(operation)
	terminalReceiptFingerprint := ""
	hasTerminalReceipt := false
	physicalEvidence := map[string]any{
		"operation_id":      id,
		"operation_carrier": operation.Carrier,
		"delivery_phase":    deliveryPhase,
		"physical_cause":    cause,
	}
	if operation.Carrier == app.OutstandingLocal {
		if operation.Local.Kind == app.OpConnect {
			connectionState = app.FailureConnectionNotEstablished
		} else if operation.Local.Kind == app.OpTeardown || operation.Local.Kind == app.OpParentRelaunch {
			connectionState = app.FailureConnectionClosing
		}
	} else {
		deliveryPhase = app.DeliveryResponseFullyValidated
	}
	if errors.As(operationErr, &physical) {
		deliveryPhase = failureDeliveryPhase(physical)
		cause = physicalCause(physical)
		physicalEvidence["delivery_phase"] = deliveryPhase
		physicalEvidence["physical_cause"] = cause
		receipt, hasReceipt := physical.TerminalReceipt()
		if physical.Phase == terminalwire.DeliveryWriteStarted || physical.Phase == terminalwire.DeliveryResponseReadStarted {
			if !hasReceipt || receipt.Validate() != nil || operation.Carrier != app.OutstandingWire || receipt.WriterOperationID != operation.Wire.OperationID+":writer" || receipt.WriterOperationGeneration != operation.Wire.OperationGeneration || (physical.Phase == terminalwire.DeliveryResponseReadStarted && (receipt.ReaderOperationID != operation.Wire.OperationID+":reader" || receipt.ReaderOperationGeneration != operation.Wire.OperationGeneration || receipt.WriterExit != terminalwire.PhysicalIOJoined || receipt.ReaderExit != terminalwire.PhysicalIOJoined)) {
				cause = app.CauseClientInvariant
				connectionState = app.FailureConnectionUsable
				physicalEvidence["terminal_receipt_invalid"] = true
			} else {
				connectionState = app.FailureConnectionInvalidated
				terminalReceiptFingerprint = receipt.ReceiptFingerprint
				hasTerminalReceipt = true
				physicalEvidence["terminal_receipt_fingerprint"] = receipt.ReceiptFingerprint
				physicalEvidence["physical_drain_identity_fingerprint"] = receipt.PhysicalDrainIdentityFingerprint
			}
		} else if hasReceipt && receipt.Validate() != nil {
			cause = app.CauseClientInvariant
			physicalEvidence["unexpected_terminal_receipt"] = true
		}
	}
	physicalEvidence["delivery_phase"] = deliveryPhase
	physicalEvidence["physical_cause"] = cause
	physicalEvidence["connection_state"] = connectionState
	fingerprint, err := protocol.CanonicalJSONFingerprint("terminal-physical-operation-failure-receipt:v1", physicalEvidence)
	if err != nil {
		fingerprint = "physical-operation-failure-fallback"
		cause = app.CauseClientInvariant
		connectionState = app.FailureConnectionUsable
		terminalReceiptFingerprint = ""
		hasTerminalReceipt = false
	}
	record.stage, record.settled = physicalTerminal, true
	delete(r.active, id)
	return classifySealedPublicFailure(
		operation,
		deliveryPhase,
		connectionState,
		cause,
		terminalReceiptFingerprint,
		hasTerminalReceipt,
		fingerprint,
		publicMessage,
	)
}

func (r *operationRegistry) beginConnectionTerminalization(
	operation app.OutstandingOperation,
	physical *terminalwire.PhysicalIOError,
	connection *physicalConnectionOwner,
) (ConnectionTerminalizationAttemptHandle, error) {
	if operation.Carrier != app.OutstandingWire || physical == nil || connection == nil {
		return ConnectionTerminalizationAttemptHandle{}, errors.New(
			"terminalization prerequisites are missing",
		)
	}
	raw, ok := physical.TerminalReceipt()
	if !ok || raw.Validate() != nil {
		return ConnectionTerminalizationAttemptHandle{}, errors.New(
			"terminalization lacks a joined physical receipt",
		)
	}
	cause := physicalCause(physical)
	signalFingerprint, err := protocol.CanonicalJSONFingerprint(
		"terminal-physical-failure-signal:v1",
		map[string]any{
			"operation_id": operation.Wire.OperationID,
			"cause":        cause,
			"raw_receipt":  raw.ReceiptFingerprint,
		},
	)
	if err != nil {
		return ConnectionTerminalizationAttemptHandle{}, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing {
		return ConnectionTerminalizationAttemptHandle{}, errors.New(
			"terminalization registry is closing",
		)
	}
	if existing := r.attempts[operation.Wire.OperationID]; existing != nil {
		if existing.identity.frozenFailureSignalFingerprint != signalFingerprint {
			return ConnectionTerminalizationAttemptHandle{}, errors.New(
				"terminalization compatible winner conflicts",
			)
		}
		return existing.handle, nil
	}
	record, found := r.active[operation.Wire.OperationID]
	if !found || record.operation != operation || record.settled {
		return ConnectionTerminalizationAttemptHandle{}, errors.New(
			"terminalization operation authority is stale",
		)
	}
	if record.settlementCapability.consumed ||
		record.settlementCapability.operationID != operation.Wire.OperationID ||
		record.settlementCapability.operationGeneration != operation.Wire.OperationGeneration {
		return ConnectionTerminalizationAttemptHandle{}, errors.New(
			"terminalization settlement capability is stale",
		)
	}
	record.settlementCapability.consumed = true
	r.nextAttemptGeneration++
	preparedFingerprint, err := protocol.CanonicalJSONFingerprint(
		"terminal-prepared-connection-terminalization:v1",
		map[string]any{
			"operation_id":               operation.Wire.OperationID,
			"operation_generation":       operation.Wire.OperationGeneration,
			"failure_signal_fingerprint": signalFingerprint,
		},
	)
	if err != nil {
		return ConnectionTerminalizationAttemptHandle{}, err
	}
	identityPayload := map[string]any{
		"attempt_generation":                   r.nextAttemptGeneration,
		"operation_id":                         operation.Wire.OperationID,
		"operation_generation":                 operation.Wire.OperationGeneration,
		"failure_signal_fingerprint":           signalFingerprint,
		"prepared_terminalization_fingerprint": preparedFingerprint,
	}
	identityFingerprint, err := protocol.CanonicalJSONFingerprint(
		"terminal-connection-terminalization-attempt-identity:v1",
		identityPayload,
	)
	if err != nil {
		return ConnectionTerminalizationAttemptHandle{}, err
	}
	identity := ConnectionTerminalizationAttemptIdentity{
		attemptID: "terminalization-attempt:" +
			identityFingerprint[len("sha256:"):],
		attemptGeneration:                  r.nextAttemptGeneration,
		operationID:                        operation.Wire.OperationID,
		operationGeneration:                operation.Wire.OperationGeneration,
		frozenFailureSignalFingerprint:     signalFingerprint,
		preparedTerminalizationFingerprint: preparedFingerprint,
		attemptIdentityFingerprint:         identityFingerprint,
	}
	postJoinFingerprint, err := protocol.CanonicalJSONFingerprint(
		"terminal-post-join-operation-settlement-capability:v1",
		map[string]any{
			"operation_id":                         operation.Wire.OperationID,
			"operation_generation":                 operation.Wire.OperationGeneration,
			"prepared_terminalization_fingerprint": preparedFingerprint,
			"physical_cause":                       cause,
		},
	)
	if err != nil {
		return ConnectionTerminalizationAttemptHandle{}, err
	}
	prepared := PreparedConnectionTerminalization{
		postJoinSettlementCapability: PostJoinOperationSettlementCapability{
			operationID:                          operation.Wire.OperationID,
			operationGeneration:                  operation.Wire.OperationGeneration,
			preparedTerminalizationFingerprint:   preparedFingerprint,
			terminalizationCapabilityFingerprint: identityFingerprint,
			frozenPhysicalCause:                  cause,
			capabilityFingerprint:                postJoinFingerprint,
		},
		frozenFailureSignalFingerprint:     signalFingerprint,
		preparedTerminalizationFingerprint: preparedFingerprint,
	}
	handle := ConnectionTerminalizationAttemptHandle{
		attemptID:                  identity.attemptID,
		attemptGeneration:          identity.attemptGeneration,
		operationID:                identity.operationID,
		operationGeneration:        identity.operationGeneration,
		attemptIdentityFingerprint: identity.attemptIdentityFingerprint,
	}
	attempt := &connectionTerminalizationAttempt{
		identity:      identity,
		handle:        handle,
		prepared:      prepared,
		state:         TerminalizationAttemptInstalled,
		stateRevision: 1,
		operation:     operation,
		physical:      physical,
		connection:    connection,
		done:          make(chan struct{}),
	}
	if physical.Phase == terminalwire.DeliveryWriteStarted {
		record.stage = physicalWriteStarted
	} else {
		record.stage = physicalResponseReadStarted
	}
	record.stage = physicalTerminalizing
	r.active[operation.Wire.OperationID] = record
	r.attempts[operation.Wire.OperationID] = attempt
	r.attemptWorkers.Add(1)
	go r.driveConnectionTerminalization(attempt, raw, cause)
	return handle, nil
}

func (r *operationRegistry) driveConnectionTerminalization(
	attempt *connectionTerminalizationAttempt,
	raw terminalwire.PhysicalConnectionTerminalReceipt,
	cause PhysicalFailureCause,
) {
	defer r.attemptWorkers.Done()
	r.transitionAttempt(attempt, TerminalizationAttemptInvalidating)
	start, err := attempt.connection.startInvalidateClose(attempt.identity, raw)
	if err == nil && (!start.hasHandle || start.disposition == PhysicalDrainConflict) {
		err = errors.New("terminal physical drain winner conflicts")
	}
	var terminal PhysicalConnectionTerminalReceipt
	if err == nil {
		r.transitionAttempt(attempt, TerminalizationAttemptPhysicalDraining)
		terminal, err = attempt.connection.waitPhysicalDrain(start.handle)
	}
	var receipt PhysicalOperationFailureReceipt
	var completion string
	if err == nil {
		r.transitionAttempt(attempt, TerminalizationAttemptReceiptReady)
		r.transitionAttempt(attempt, TerminalizationAttemptSettling)
		if attempt.prepared.postJoinSettlementCapability.consumed ||
			attempt.prepared.postJoinSettlementCapability.frozenPhysicalCause != cause {
			err = errors.New("terminal post-join settlement capability is stale")
		} else {
			attempt.prepared.postJoinSettlementCapability.consumed = true
		}
	}
	if err == nil {
		receipt = PhysicalOperationFailureReceipt{
			operation:          attempt.operation,
			cause:              cause,
			connectionTerminal: terminal,
		}
		receipt.physicalReceiptFingerprint, err = protocol.CanonicalJSONFingerprint(
			"terminal-physical-operation-failure-receipt:v1",
			map[string]any{
				"operation_id":                attempt.operation.Wire.OperationID,
				"operation_generation":        attempt.operation.Wire.OperationGeneration,
				"cause":                       cause,
				"connection_terminal_receipt": terminal.receiptFingerprint,
			},
		)
	}
	if err == nil {
		completion, err = protocol.CanonicalJSONFingerprint(
			"terminal-connection-terminalization-completion:v1",
			map[string]any{
				"attempt_identity_fingerprint": attempt.identity.attemptIdentityFingerprint,
				"failure_receipt_fingerprint":  receipt.physicalReceiptFingerprint,
			},
		)
	}
	r.mu.Lock()
	attempt.failure = receipt
	attempt.completion = completion
	attempt.err = err
	attempt.stateRevision++
	attempt.state = TerminalizationAttemptTerminal
	if record, ok := r.active[attempt.operation.Wire.OperationID]; ok {
		record.settled = true
		r.active[attempt.operation.Wire.OperationID] = record
		delete(r.active, attempt.operation.Wire.OperationID)
	}
	close(attempt.done)
	r.mu.Unlock()
}

func (r *operationRegistry) transitionAttempt(
	attempt *connectionTerminalizationAttempt,
	state ConnectionTerminalizationAttemptState,
) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if state <= attempt.state || attempt.state == TerminalizationAttemptTerminal {
		return
	}
	attempt.stateRevision++
	attempt.state = state
}

func (r *operationRegistry) waitConnectionTerminalization(
	handle ConnectionTerminalizationAttemptHandle,
	waiterCancellation <-chan struct{},
	waiterDeadline time.Time,
) (ConnectionTerminalizationWaitResult, error) {
	r.mu.Lock()
	attempt := r.attempts[handle.operationID]
	if attempt == nil || attempt.handle != handle {
		r.mu.Unlock()
		return ConnectionTerminalizationWaitResult{}, errors.New(
			"terminalization waiter handle is stale",
		)
	}
	done := attempt.done
	r.mu.Unlock()
	duration := time.Until(waiterDeadline)
	if duration <= 0 {
		return ConnectionTerminalizationWaitResult{
			disposition:   TerminalizationWaiterDeadline,
			attemptHandle: handle,
		}, nil
	}
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-done:
		r.mu.Lock()
		defer r.mu.Unlock()
		if attempt.err != nil {
			return ConnectionTerminalizationWaitResult{}, attempt.err
		}
		return ConnectionTerminalizationWaitResult{
			disposition:           TerminalizationWaitCompleted,
			attemptHandle:         handle,
			completionFingerprint: attempt.completion,
			failureReceipt:        attempt.failure,
			hasFailureReceipt:     true,
		}, nil
	case <-waiterCancellation:
		// Completion wins once installed under the registry lock.
		r.mu.Lock()
		defer r.mu.Unlock()
		select {
		case <-done:
			if attempt.err != nil {
				return ConnectionTerminalizationWaitResult{}, attempt.err
			}
			return ConnectionTerminalizationWaitResult{
				disposition:           TerminalizationWaitCompleted,
				attemptHandle:         handle,
				completionFingerprint: attempt.completion,
				failureReceipt:        attempt.failure,
				hasFailureReceipt:     true,
			}, nil
		default:
			return ConnectionTerminalizationWaitResult{
				disposition:   TerminalizationWaiterCancelled,
				attemptHandle: handle,
			}, nil
		}
	case <-timer.C:
		r.mu.Lock()
		defer r.mu.Unlock()
		select {
		case <-done:
			if attempt.err != nil {
				return ConnectionTerminalizationWaitResult{}, attempt.err
			}
			return ConnectionTerminalizationWaitResult{
				disposition:           TerminalizationWaitCompleted,
				attemptHandle:         handle,
				completionFingerprint: attempt.completion,
				failureReceipt:        attempt.failure,
				hasFailureReceipt:     true,
			}, nil
		default:
			return ConnectionTerminalizationWaitResult{
				disposition:   TerminalizationWaiterDeadline,
				attemptHandle: handle,
			}, nil
		}
	}
}

func (r *operationRegistry) drainConnectionTerminalizations(
	closeDeadline time.Time,
) error {
	_, err := r.drainConnectionTerminalizationsWithCount(closeDeadline)
	return err
}

func (r *operationRegistry) drainConnectionTerminalizationsWithCount(
	closeDeadline time.Time,
) (uint32, error) {
	r.mu.Lock()
	r.closing = true
	var pending uint32
	for _, attempt := range r.attempts {
		if attempt.state != TerminalizationAttemptTerminal {
			pending++
		}
	}
	r.mu.Unlock()
	done := make(chan struct{})
	go func() {
		r.attemptWorkers.Wait()
		close(done)
	}()
	duration := time.Until(closeDeadline)
	if duration <= 0 {
		return 0, errors.New("terminalization drain deadline expired")
	}
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-done:
		return pending, nil
	case <-timer.C:
		return 0, errors.New("terminalization drain deadline expired")
	}
}

func physicalCause(failure *terminalwire.PhysicalIOError) PhysicalFailureCause {
	if failure.Phase == terminalwire.DeliveryWriteStarted {
		return PhysicalCauseWriteFailed
	}
	if timeout, ok := failure.Cause.(net.Error); ok && timeout.Timeout() {
		return PhysicalCauseReadDeadline
	}
	if errors.Is(failure, io.EOF) || errors.Is(failure, io.ErrUnexpectedEOF) {
		return PhysicalCauseEOF
	}
	return PhysicalCauseReadFailed
}

func nonPhysicalFailureCause(operation app.OutstandingOperation) app.PhysicalFailureCause {
	if operation.Carrier == app.OutstandingLocal {
		switch operation.Local.Kind {
		case app.OpConnect:
			return app.CauseDialFailed
		case app.OpTeardown, app.OpParentRelaunch:
			return app.CauseLocalIntegrationFailed
		case app.OpClipboard, app.OpOpenURL:
			return app.CauseLocalIntegrationFailed
		default:
			return app.CauseClientInvariant
		}
	}
	switch operation.Wire.Kind {
	case app.OpTransportAuth:
		return app.CauseAuthenticationRejected
	case app.OpHello:
		return app.CauseProtocolSchemaRejected
	case app.OpAttach, app.OpAttachAck:
		return app.CauseAttachRejected
	case app.OpHeartbeat, app.OpProjectionSnapshot, app.OpOperationalSnapshot,
		app.OpHistoryPage:
		return app.CauseProjectionValidationFailed
	default:
		return app.CauseClientInvariant
	}
}

func failureDeliveryPhase(failure *terminalwire.PhysicalIOError) app.FailureDeliveryPhase {
	if failure == nil {
		return app.DeliveryResponseFullyValidated
	}
	switch failure.Phase {
	case terminalwire.DeliveryNotStarted:
		return app.DeliveryNotStarted
	case terminalwire.DeliveryWriteStarted:
		return app.DeliveryWriteStarted
	case terminalwire.DeliveryRequestFullySent:
		return app.DeliveryRequestFullySent
	case terminalwire.DeliveryResponseReadStarted:
		return app.DeliveryResponseReadStarted
	default:
		return app.DeliveryNotStarted
	}
}

func classifySealedPublicFailure(
	operation app.OutstandingOperation,
	deliveryPhase app.FailureDeliveryPhase,
	connectionState app.FailureConnectionState,
	cause app.PhysicalFailureCause,
	terminalReceiptFingerprint string,
	hasTerminalReceipt bool,
	physicalReceiptFingerprint string,
	message string,
) app.PublicFailure {
	value, err := app.ClassifyPublicFailure(
		operation,
		deliveryPhase,
		connectionState,
		cause,
		terminalReceiptFingerprint,
		hasTerminalReceipt,
		physicalReceiptFingerprint,
		message,
	)
	if err == nil {
		return value
	}
	fallbackReceipt, fingerprintErr := protocol.CanonicalJSONFingerprint(
		"terminal-failure-classifier-fallback:v1",
		map[string]any{
			"operation_id":               operationID(operation),
			"operation_generation":       operationGeneration(operation),
			"classification_error":       err.Error(),
			"source_receipt_fingerprint": physicalReceiptFingerprint,
		},
	)
	if fingerprintErr != nil {
		fallbackReceipt = fmt.Sprintf("terminal-failure-classifier-fallback:%s:%d", operationID(operation), operationGeneration(operation))
	}
	fallback, fallbackErr := app.ClassifyPublicFailure(
		operation,
		deliveryPhase,
		app.FailureConnectionUsable,
		app.CauseClientInvariant,
		"",
		false,
		fallbackReceipt,
		"terminal physical failure classification was invalid",
	)
	if fallbackErr != nil {
		panic(fallbackErr)
	}
	return fallback
}

func (r *operationRegistry) classifyUnadmittedFailure(
	operation app.OutstandingOperation,
	message string,
	reason string,
) app.PublicFailure {
	deliveryPhase := app.DeliveryLocalOperationStarted
	connectionState := app.FailureConnectionUsable
	if operation.Carrier == app.OutstandingWire {
		deliveryPhase = app.DeliveryNotStarted
	} else if operation.Carrier == app.OutstandingLocal && operation.Local.Kind == app.OpConnect {
		connectionState = app.FailureConnectionNotEstablished
	} else if operation.Carrier == app.OutstandingLocal &&
		(operation.Local.Kind == app.OpTeardown || operation.Local.Kind == app.OpParentRelaunch) {
		connectionState = app.FailureConnectionClosing
	}
	fingerprint, err := protocol.CanonicalJSONFingerprint(
		"terminal-operation-admission-failure-receipt:v1",
		map[string]any{
			"operation_id":         operationID(operation),
			"operation_generation": operationGeneration(operation),
			"reason":               reason,
		},
	)
	if err != nil {
		fingerprint = "terminal-operation-admission-failure-receipt"
	}
	return classifySealedPublicFailure(
		operation,
		deliveryPhase,
		connectionState,
		app.CauseClientInvariant,
		"",
		false,
		fingerprint,
		message,
	)
}

func (r *operationRegistry) classifyEmbeddedFailure(
	operation app.OutstandingOperation,
	cause app.PhysicalFailureCause,
	message string,
) app.PublicFailure {
	r.mu.Lock()
	record, ok := r.active[operationID(operation)]
	r.mu.Unlock()
	if !ok || record.operation != operation || record.settled {
		return r.classifyUnadmittedFailure(operation, message, "embedded-failure-operation-stale")
	}
	fingerprint, err := protocol.CanonicalJSONFingerprint(
		"terminal-embedded-operation-failure-receipt:v1",
		map[string]any{
			"operation_id":         operationID(operation),
			"operation_generation": operationGeneration(operation),
			"physical_cause":       cause,
			"stage":                record.stage,
		},
	)
	if err != nil {
		fingerprint = "terminal-embedded-operation-failure-receipt"
	}
	return classifySealedPublicFailure(
		operation,
		app.DeliveryLocalOperationStarted,
		app.FailureConnectionClosing,
		cause,
		"",
		false,
		fingerprint,
		message,
	)
}

func (r *operationRegistry) activeCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.active)
}
