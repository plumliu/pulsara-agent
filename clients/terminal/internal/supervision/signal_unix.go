package supervision

import (
	"os"
	"os/signal"
	"syscall"
	"time"

	tea "charm.land/bubbletea/v2"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
)

func Start(program *tea.Program, parentPID uint64) func() {
	done := make(chan struct{})
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	go func() {
		select {
		case value := <-signals:
			_ = value
			program.Send(app.ParentShutdownMsg{Reason: app.ParentRequestedShutdown})
		case <-done:
		}
	}()
	if parentPID > 0 {
		go func() {
			ticker := time.NewTicker(time.Second)
			defer ticker.Stop()
			for {
				select {
				case <-ticker.C:
					if syscall.Kill(int(parentPID), 0) != nil {
						program.Send(app.ParentShutdownMsg{Reason: app.ParentProcessExited})
						return
					}
				case <-done:
					return
				}
			}
		}()
	}
	return func() { signal.Stop(signals); close(done) }
}
