package main

import (
	"os"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/bootstrap"
)

func main() {
	os.Exit(bootstrap.Run(os.Args[1:]))
}
