package main

import (
	"os"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/kernelbootstrap"
)

func main() {
	os.Exit(kernelbootstrap.Run(os.Args[1:]))
}
