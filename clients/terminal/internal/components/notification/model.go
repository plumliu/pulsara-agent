package notification

import (
	"errors"
	"time"
	"unicode/utf8"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/publictext"
)

const (
	MaximumItems     = 16
	MaximumRunes     = 512
	MaximumUTF8Bytes = 2048
	DefaultLifetime  = 8 * time.Second
)

type Kind uint8

const (
	Information Kind = iota + 1
	Warning
	Failure
	CommandOutcome
)

type Item struct {
	id                string
	kind              Kind
	publicText        string
	sourceFingerprint string
	createdAt         time.Time
	expiresAt         time.Time
	sticky            bool
	fingerprint       string
}

type Model struct {
	items                    []Item
	dropped                  uint64
	nextOrdinal              uint64
	nextTimerGeneration      uint64
	scheduledTimerGeneration uint64
	scheduledDueAt           time.Time
}

func New() Model {
	return Model{nextOrdinal: 1, nextTimerGeneration: 1}
}

func (m Model) Validate() error {
	if m.nextOrdinal == 0 || m.nextTimerGeneration == 0 || len(m.items) > MaximumItems ||
		((m.scheduledTimerGeneration == 0) != m.scheduledDueAt.IsZero()) {
		return errors.New("terminal local notification model is invalid")
	}
	seen := make(map[string]struct{}, len(m.items))
	for _, item := range m.items {
		if err := item.Validate(); err != nil {
			return err
		}
		if _, exists := seen[item.id]; exists {
			return errors.New("terminal local notification identity is duplicated")
		}
		seen[item.id] = struct{}{}
	}
	if m.scheduledTimerGeneration != 0 {
		if m.scheduledTimerGeneration >= m.nextTimerGeneration {
			return errors.New("terminal notification timer generation is invalid")
		}
		matched := false
		for _, item := range m.items {
			matched = matched || (!item.sticky && item.expiresAt.Equal(m.scheduledDueAt))
		}
		if !matched {
			return errors.New("terminal notification timer does not own an expiry")
		}
	}
	return nil
}

func (i Item) Validate() error {
	if i.id == "" || i.kind < Information || i.kind > CommandOutcome ||
		i.publicText == "" || !utf8.ValidString(i.publicText) ||
		!publictext.IsSafe(i.publicText) || len([]rune(i.publicText)) > MaximumRunes ||
		len([]byte(i.publicText)) > MaximumUTF8Bytes || i.sourceFingerprint == "" ||
		i.createdAt.IsZero() || i.expiresAt.Before(i.createdAt) || i.fingerprint == "" {
		return errors.New("terminal local notification is invalid")
	}
	expected, err := itemFingerprint(i)
	if err != nil || expected != i.fingerprint {
		return errors.New("terminal local notification fingerprint mismatch")
	}
	return nil
}

func (m Model) Add(kind Kind, text, sourceFingerprint string, createdAt time.Time, sticky bool) (Model, error) {
	if m.Validate() != nil || kind < Information || kind > CommandOutcome ||
		sourceFingerprint == "" || createdAt.IsZero() || m.nextOrdinal == ^uint64(0) {
		return Model{}, errors.New("terminal local notification admission is invalid")
	}
	text = boundedPublicText(text)
	if text == "" {
		return Model{}, errors.New("terminal local notification text is empty")
	}
	expiresAt := createdAt.Add(DefaultLifetime)
	if sticky {
		expiresAt = createdAt
	}
	idFingerprint, err := protocolvalue.CanonicalClientFingerprint("terminal-local-notification-id:v1", map[string]any{
		"created_at_utc":     createdAt.UTC().Format(time.RFC3339Nano),
		"next_ordinal":       m.nextOrdinal,
		"source_fingerprint": sourceFingerprint,
	})
	if err != nil {
		return Model{}, err
	}
	item := Item{
		id: "terminal-notification:" + idFingerprint[len("sha256:"):], kind: kind,
		publicText: text, sourceFingerprint: sourceFingerprint, createdAt: createdAt,
		expiresAt: expiresAt, sticky: sticky,
	}
	item.fingerprint, err = itemFingerprint(item)
	if err != nil {
		return Model{}, err
	}
	if len(m.items) == MaximumItems {
		retired := m.items[0]
		m.items = append([]Item(nil), m.items[1:]...)
		m.dropped++
		if !retired.sticky && m.scheduledTimerGeneration != 0 && retired.expiresAt.Equal(m.scheduledDueAt) {
			m.scheduledTimerGeneration = 0
			m.scheduledDueAt = time.Time{}
		}
	}
	m.items = append(append([]Item(nil), m.items...), item)
	m.nextOrdinal++
	if !item.sticky && m.scheduledTimerGeneration != 0 && item.expiresAt.Before(m.scheduledDueAt) {
		m.scheduledTimerGeneration = 0
		m.scheduledDueAt = time.Time{}
	}
	return m, m.Validate()
}

func (m Model) LatestSticky() bool {
	return len(m.items) > 0 && m.items[len(m.items)-1].sticky
}

func (m Model) DismissLatestSticky() Model {
	if !m.LatestSticky() {
		return m
	}
	m.items = append([]Item(nil), m.items[:len(m.items)-1]...)
	return m
}

func (m Model) Expire(now time.Time) Model {
	if now.IsZero() {
		return m
	}
	retained := make([]Item, 0, len(m.items))
	for _, item := range m.items {
		if item.sticky || now.Before(item.expiresAt) {
			retained = append(retained, item)
		}
	}
	if len(retained) != len(m.items) {
		m.items = retained
		m.scheduledTimerGeneration = 0
		m.scheduledDueAt = time.Time{}
	}
	return m
}

// PlanExpiry installs one generation-bound timer intent for the earliest
// transient item.  A later model revision makes an already-scheduled tick a
// harmless stale delivery.
func (m Model) PlanExpiry() (Model, time.Time, uint64, bool) {
	if m.scheduledTimerGeneration != 0 {
		return m, time.Time{}, 0, false
	}
	var due time.Time
	for _, item := range m.items {
		if item.sticky {
			continue
		}
		if due.IsZero() || item.expiresAt.Before(due) {
			due = item.expiresAt
		}
	}
	if due.IsZero() {
		return m, time.Time{}, 0, false
	}
	if m.nextTimerGeneration == ^uint64(0) {
		return m, time.Time{}, 0, false
	}
	generation := m.nextTimerGeneration
	m.nextTimerGeneration++
	m.scheduledTimerGeneration = generation
	m.scheduledDueAt = due
	return m, due, generation, true
}

func (m Model) ApplyExpiryTick(generation uint64, now time.Time) (Model, bool) {
	if generation == 0 || generation != m.scheduledTimerGeneration {
		return m, false
	}
	m.scheduledTimerGeneration = 0
	m.scheduledDueAt = time.Time{}
	return m.Expire(now), true
}

func (m Model) Latest(now time.Time) (Item, bool) {
	m = m.Expire(now)
	if len(m.items) == 0 {
		return Item{}, false
	}
	return m.items[len(m.items)-1], true
}

func (m Model) Count() int      { return len(m.items) }
func (m Model) Dropped() uint64 { return m.dropped }

func (i Item) PublicText() string { return i.publicText }
func (i Item) Kind() Kind         { return i.kind }
func (i Item) Sticky() bool       { return i.sticky }

func itemFingerprint(item Item) (string, error) {
	return protocolvalue.CanonicalClientFingerprint("terminal-local-notification:v1", map[string]any{
		"created_at_utc":     item.createdAt.UTC().Format(time.RFC3339Nano),
		"expires_at_utc":     item.expiresAt.UTC().Format(time.RFC3339Nano),
		"kind":               item.kind,
		"notification_id":    item.id,
		"public_text":        item.publicText,
		"source_fingerprint": item.sourceFingerprint,
		"sticky":             item.sticky,
	})
}

func boundedPublicText(value string) string {
	value = publictext.Transform(value)
	for len([]rune(value)) > MaximumRunes || len([]byte(value)) > MaximumUTF8Bytes {
		_, size := utf8.DecodeLastRuneInString(value)
		if size == 0 {
			return ""
		}
		value = value[:len(value)-size]
	}
	if value == "" {
		return ""
	}
	return value
}
