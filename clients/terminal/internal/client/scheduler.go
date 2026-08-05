package client

import (
	"errors"
	"sync"
	"time"

	tea "charm.land/bubbletea/v2"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
)

// localScheduler is the single owner of client-local timer workers. Scheduling
// is non-blocking, while teardown closes admission and physically drains every
// already-admitted timer before reporting a completed teardown summary.
type localScheduler struct {
	mu      sync.Mutex
	closed  bool
	done    chan struct{}
	workers sync.WaitGroup
}

func newLocalScheduler() *localScheduler {
	return &localScheduler{done: make(chan struct{})}
}

func (s *localScheduler) schedule(effect app.ScheduleTickEffect) tea.Cmd {
	return func() tea.Msg {
		s.mu.Lock()
		if s.closed {
			s.mu.Unlock()
			return nil
		}
		s.workers.Add(1)
		done := s.done
		s.mu.Unlock()
		defer s.workers.Done()
		delay := time.Until(effect.DueAt)
		if delay < 0 {
			delay = 0
		}
		timer := time.NewTimer(delay)
		defer timer.Stop()
		select {
		case <-done:
			return nil
		case <-timer.C:
			s.mu.Lock()
			closed := s.closed
			s.mu.Unlock()
			if closed {
				return nil
			}
			switch effect.Kind {
			case app.TickHeartbeat, app.TickSnapshotRetry, app.TickNotificationExpiry:
				return app.TickMsg{Kind: effect.Kind, TickGeneration: effect.TickGeneration}
			case app.TickReconnect:
				return app.ReconnectDueMsg{ReconnectGeneration: effect.TickGeneration}
			default:
				return app.FrameworkInputRejectedMsg{}
			}
		}
	}
}

func (s *localScheduler) closeAndDrain(deadline time.Time) error {
	s.mu.Lock()
	if !s.closed {
		s.closed = true
		close(s.done)
	}
	s.mu.Unlock()
	drained := make(chan struct{})
	go func() {
		s.workers.Wait()
		close(drained)
	}()
	duration := time.Until(deadline)
	if duration <= 0 {
		return errors.New("terminal local scheduler drain deadline expired")
	}
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-drained:
		return nil
	case <-timer.C:
		return errors.New("terminal local scheduler drain deadline expired")
	}
}
