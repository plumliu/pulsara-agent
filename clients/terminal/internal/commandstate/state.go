package commandstate

import (
	"errors"
	"time"
)

const MaximumRecords uint32 = 64

type Phase uint8

const (
	Frozen Phase = iota + 1
	Sending
	AwaitingOutcome
	QueryRequired
	Querying
	Accepted
	Rejected
	PendingConfirmation
	Reconciliation
	CompatibleWinner
)

type Record struct {
	candidate   Candidate
	phase       Phase
	queryToken  string
	lastOutcome Outcome
	hasOutcome  bool
	attempts    uint8
	nextAttempt time.Time
}

type Registry struct {
	maximum     uint32
	generation  uint64
	nextOrdinal uint64
	enabled     bool
	records     []Record
}

func NewDormantRegistry(maximum uint32) (Registry, error) {
	if maximum != MaximumRecords {
		return Registry{}, errors.New("terminal command registry bound is incompatible")
	}
	return Registry{maximum: maximum, generation: 1, nextOrdinal: 1}, nil
}

func (r Registry) Activate() (Registry, error) {
	if r.Validate() != nil || r.enabled || len(r.records) != 0 {
		return Registry{}, errors.New("terminal command registry activation is invalid")
	}
	r.enabled = true
	return r, r.Validate()
}

func (r Registry) Validate() error {
	if r.maximum != MaximumRecords || r.generation == 0 || r.nextOrdinal == 0 || uint32(len(r.records)) > r.maximum {
		return errors.New("terminal command registry is invalid")
	}
	if !r.enabled && len(r.records) != 0 {
		return errors.New("dormant command registry contains records")
	}
	seen := map[string]bool{}
	for _, record := range r.records {
		if record.candidate.Validate() != nil || record.phase < Frozen || record.phase > CompatibleWinner || seen[record.candidate.ID()] || record.hasOutcome != (record.lastOutcome.CommandID != "") || record.attempts > 8 {
			return errors.New("terminal command registry record is invalid")
		}
		if !record.nextAttempt.IsZero() && record.phase != Frozen && record.phase != QueryRequired && record.phase != PendingConfirmation {
			return errors.New("terminal command retry deadline is attached to an incompatible phase")
		}
		seen[record.candidate.ID()] = true
		if record.hasOutcome {
			if record.lastOutcome.Validate() != nil || record.lastOutcome.CommandID != record.candidate.ID() || record.lastOutcome.TargetID != record.candidate.Binding().ExpectedTargetID || record.lastOutcome.TargetGeneration != record.candidate.Binding().ExpectedTargetGeneration || record.queryToken != record.lastOutcome.QueryToken {
				return errors.New("terminal command outcome/candidate join is invalid")
			}
		}
	}
	return nil
}

func (r Registry) Dormant() bool       { return !r.enabled }
func (r Registry) Enabled() bool       { return r.enabled }
func (r Registry) Count() int          { return len(r.records) }
func (r Registry) NextOrdinal() uint64 { return r.nextOrdinal }

func (r Registry) Install(candidate Candidate) (Registry, error) {
	if !r.enabled || candidate.Validate() != nil {
		return Registry{}, errors.New("terminal command candidate admission is unavailable")
	}
	for _, record := range r.records {
		if record.candidate.ID() == candidate.ID() {
			if record.candidate.Fingerprint() == candidate.Fingerprint() {
				return r, nil
			}
			return Registry{}, errors.New("terminal command candidate identity conflicts")
		}
	}
	if uint32(len(r.records)) == r.maximum {
		retired := -1
		for index, record := range r.records {
			if record.phase == Accepted || record.phase == Rejected || record.phase == Reconciliation || record.phase == CompatibleWinner {
				retired = index
				break
			}
		}
		if retired < 0 {
			return Registry{}, errors.New("terminal command registry has no retireable record")
		}
		r.records = append(append([]Record(nil), r.records[:retired]...), r.records[retired+1:]...)
	}
	r.records = append(r.records, Record{candidate: candidate, phase: Frozen})
	r.nextOrdinal++
	return r, r.Validate()
}

func (r Registry) NextAction(now time.Time) (Candidate, bool, bool) {
	for _, record := range r.records {
		if (record.phase == QueryRequired || record.phase == PendingConfirmation) && (record.nextAttempt.IsZero() || !now.Before(record.nextAttempt)) {
			return record.candidate, true, true
		}
	}
	for _, record := range r.records {
		if record.phase == Frozen && (record.nextAttempt.IsZero() || !now.Before(record.nextAttempt)) {
			return record.candidate, false, true
		}
	}
	return Candidate{}, false, false
}

func (r Registry) MarkMutationSending(commandID string) (Registry, error) {
	return r.transition(commandID, []Phase{Frozen}, Sending)
}
func (r Registry) MarkAwaitingOutcome(commandID string) (Registry, error) {
	return r.transition(commandID, []Phase{Sending}, AwaitingOutcome)
}
func (r Registry) MarkQuerying(commandID string) (Registry, error) {
	return r.transition(commandID, []Phase{QueryRequired, PendingConfirmation}, Querying)
}
func (r Registry) MarkQueryRequired(commandID string, now time.Time) (Registry, error) {
	return r.retryTransition(commandID, []Phase{Sending, AwaitingOutcome, Querying}, QueryRequired, now)
}
func (r Registry) MarkResendSameCandidate(commandID string, now time.Time) (Registry, error) {
	return r.retryTransition(commandID, []Phase{Querying}, Frozen, now)
}
func (r Registry) MarkPredispatchRetry(commandID string, now time.Time) (Registry, error) {
	return r.retryTransition(commandID, []Phase{Sending}, Frozen, now)
}

func (r Registry) MarkMissingAfterAuthorityChange(commandID string) (Registry, error) {
	return r.transition(commandID, []Phase{Querying}, Reconciliation)
}

// RequireQueryAfterAttachmentChange preserves stable command identity across a
// reconnect without ever replaying a mutation that was bound to the retired
// semantic attachment. The successor attachment may only query the old durable
// winner; a missing result is resolved by the application as reconciliation.
func (r Registry) RequireQueryAfterAttachmentChange(now time.Time) (Registry, error) {
	if !r.enabled || now.IsZero() {
		return Registry{}, errors.New("terminal command attachment replacement is invalid")
	}
	for index := range r.records {
		switch r.records[index].phase {
		case Accepted, Rejected, Reconciliation, CompatibleWinner:
			continue
		default:
			r.records[index].phase = QueryRequired
			r.records[index].nextAttempt = now
		}
	}
	return r, r.Validate()
}

func (r Registry) ApplyOutcome(commandID string, outcome Outcome, now time.Time) (Registry, error) {
	index := r.index(commandID)
	if index < 0 || outcome.Validate() != nil || outcome.CommandID != commandID || outcome.TargetID != r.records[index].candidate.Binding().ExpectedTargetID || outcome.TargetGeneration != r.records[index].candidate.Binding().ExpectedTargetGeneration {
		return Registry{}, errors.New("terminal command outcome attribution is invalid")
	}
	record := r.records[index]
	if record.hasOutcome && record.lastOutcome.Fingerprint == outcome.Fingerprint {
		if outcome.Status != OutcomePendingConfirmation {
			return r, nil
		}
		record.phase = PendingConfirmation
		record.attempts, record.nextAttempt = nextRetry(record.attempts, now)
		r.records[index] = record
		return r, r.Validate()
	}
	switch record.phase {
	case Sending, AwaitingOutcome, Querying, PendingConfirmation:
	default:
		return Registry{}, errors.New("terminal command outcome predecessor is invalid")
	}
	switch outcome.Status {
	case OutcomeSucceeded:
		record.phase = Accepted
	case OutcomeRejected:
		record.phase = Rejected
	case OutcomePendingConfirmation:
		record.phase = PendingConfirmation
		record.attempts, record.nextAttempt = nextRetry(record.attempts, now)
	case OutcomeReconciliationRequired:
		record.phase = Reconciliation
	case OutcomeCompatibleWinner:
		record.phase = CompatibleWinner
	default:
		return Registry{}, errors.New("terminal command outcome status is unknown")
	}
	record.queryToken, record.lastOutcome, record.hasOutcome = outcome.QueryToken, outcome, true
	if record.phase != PendingConfirmation {
		record.attempts, record.nextAttempt = 0, time.Time{}
	}
	r.records[index] = record
	return r, r.Validate()
}

func (r Registry) Candidate(commandID string) (Candidate, bool) {
	if index := r.index(commandID); index >= 0 {
		return r.records[index].candidate, true
	}
	return Candidate{}, false
}
func (r Registry) OwnsExact(candidate Candidate) bool {
	installed, ok := r.Candidate(candidate.ID())
	return ok && installed.Fingerprint() == candidate.Fingerprint()
}
func (r Registry) Pending(kind Kind, targetID string) (Candidate, bool) {
	for _, record := range r.records {
		if record.candidate.Kind() == kind && record.candidate.Binding().ExpectedTargetID == targetID &&
			record.phase != Accepted && record.phase != Rejected && record.phase != Reconciliation && record.phase != CompatibleWinner {
			return record.candidate, true
		}
	}
	return Candidate{}, false
}

// ExactSubmission returns the newest command authority frozen from the exact
// composer content revision. Caret and viewport movements are deliberately
// excluded so they cannot turn one unchanged draft into multiple durable
// prompt submissions. A rejected terminal outcome remains visible to the
// caller, which may deliberately freeze a new retry with a new ordinal.
func (r Registry) ExactSubmission(revision uint64, contentFingerprint, text string) (Candidate, Phase, bool) {
	for index := len(r.records) - 1; index >= 0; index-- {
		record := r.records[index]
		if record.candidate.Kind() == SubmitPrompt &&
			record.candidate.ComposerRevision() == revision &&
			record.candidate.ComposerContentFingerprint() == contentFingerprint &&
			record.candidate.Text() == text {
			return record.candidate, record.phase, true
		}
	}
	return Candidate{}, 0, false
}

func (r Registry) Record(commandID string) (Record, bool) {
	if index := r.index(commandID); index >= 0 {
		return r.records[index], true
	}
	return Record{}, false
}

// LatestPendingRecord returns the newest command whose physical or durable
// outcome is still unresolved. Terminal records must not hide an older pending
// command from the persistent command-status view.
func (r Registry) LatestPendingRecord() (Record, bool) {
	for index := len(r.records) - 1; index >= 0; index-- {
		switch r.records[index].phase {
		case Frozen, Sending, AwaitingOutcome, QueryRequired, Querying, PendingConfirmation:
			return r.records[index], true
		}
	}
	return Record{}, false
}
func (r Record) Candidate() Candidate     { return r.candidate }
func (r Record) Phase() Phase             { return r.phase }
func (r Record) Outcome() (Outcome, bool) { return r.lastOutcome, r.hasOutcome }

func (r Registry) transition(commandID string, from []Phase, to Phase) (Registry, error) {
	index := r.index(commandID)
	if index < 0 {
		return Registry{}, errors.New("terminal command record is missing")
	}
	allowed := false
	for _, phase := range from {
		allowed = allowed || r.records[index].phase == phase
	}
	if !allowed {
		return Registry{}, errors.New("terminal command transition predecessor is invalid")
	}
	r.records[index].phase = to
	if to == Sending || to == AwaitingOutcome || to == Querying || to == Accepted || to == Rejected || to == Reconciliation || to == CompatibleWinner {
		r.records[index].nextAttempt = time.Time{}
	}
	return r, r.Validate()
}

func (r Registry) retryTransition(commandID string, from []Phase, to Phase, now time.Time) (Registry, error) {
	if now.IsZero() {
		return Registry{}, errors.New("terminal command retry time is absent")
	}
	next, err := r.transition(commandID, from, to)
	if err != nil {
		return Registry{}, err
	}
	index := next.index(commandID)
	next.records[index].attempts, next.records[index].nextAttempt = nextRetry(next.records[index].attempts, now)
	return next, next.Validate()
}

func nextRetry(attempts uint8, now time.Time) (uint8, time.Time) {
	if attempts < 8 {
		attempts++
	}
	shift := attempts - 1
	if shift > 4 {
		shift = 4
	}
	return attempts, now.Add(250 * time.Millisecond * time.Duration(1<<shift))
}
func (r Registry) index(commandID string) int {
	for index := range r.records {
		if r.records[index].candidate.ID() == commandID {
			return index
		}
	}
	return -1
}
