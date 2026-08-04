package client

import (
	"testing"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
)

func TestLocalSchedulerDrainsStartedAndNeverStartedTicks(t *testing.T) {
	scheduler := newLocalScheduler()
	operation := app.NewLocalOperationToken(app.OpTick, "terminal-client:scheduler", 1, 1, time.Now().Add(time.Hour))
	effect := app.ScheduleTickEffect{
		Header:         app.LocalEffectHeader{EffectID: "effect:tick", Operation: operation},
		Kind:           app.TickHeartbeat,
		TickGeneration: 1,
		DueAt:          time.Now().Add(time.Hour),
	}
	neverStarted := scheduler.schedule(effect)
	started := scheduler.schedule(effect)
	result := make(chan any, 1)
	go func() { result <- started() }()
	if err := scheduler.closeAndDrain(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	if message := <-result; message != nil {
		t.Fatalf("cancelled scheduler produced a late message: %#v", message)
	}
	if message := neverStarted(); message != nil {
		t.Fatalf("post-close scheduler command produced a message: %#v", message)
	}
}
