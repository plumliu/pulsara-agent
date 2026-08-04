package supervision

import "github.com/plumliu/pulsara-agent/clients/terminal/internal/app"

func Close(executor app.Executor) error { return executor.Close() }
