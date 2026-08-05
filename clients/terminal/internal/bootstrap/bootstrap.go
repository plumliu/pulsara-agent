package bootstrap

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"time"

	"google.golang.org/protobuf/proto"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/buildinfo"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/client"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/config"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/supervision"
)

const ParentRelaunchExitCode = 75

func Run(arguments []string) int {
	flags := flag.NewFlagSet("pulsara-tui", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	bootstrapFD := flags.Int("bootstrap-fd", config.ParseBootstrapFD(os.Getenv("PULSARA_TUI_BOOTSTRAP_FD"), 3), "inherited one-shot bootstrap fd")
	showVersion := flags.Bool("version", false, "print version")
	showVersionJSON := flags.Bool("version-json", false, "print version metadata as JSON")
	selfTest := flags.Bool("self-test", false, "run local compatibility checks")
	printRange := flags.Bool("print-protocol-range", false, "print supported protocol range")
	if err := flags.Parse(arguments); err != nil {
		return 2
	}
	if *showVersion {
		fmt.Printf("pulsara-tui %s\n", buildinfo.Version)
		return 0
	}
	if *showVersionJSON {
		if err := emitVersionJSON(); err != nil {
			return fail("version JSON: %v", err)
		}
		return 0
	}
	if *printRange {
		fmt.Printf("%d.%d schema=%s\n", protocolvalue.ProtocolMajor, protocolvalue.ProtocolMinor, protocolvalue.SchemaFingerprint)
		return 0
	}
	if *selfTest {
		if err := selfTestRuntime(); err != nil {
			return fail("self-test: %v", err)
		}
		fmt.Println("pulsara-tui self-test: ok")
		return 0
	}

	carrier, err := Read(Options{FD: *bootstrapFD})
	if err != nil {
		return fail("bootstrap: %v", err)
	}
	service, err := client.NewService(carrier)
	if err != nil {
		return fail("client composition: %v", err)
	}
	model := app.NewModel(service)
	program := app.NewProductionProgram(
		model,
		context.Background(),
		config.SanitizedEnvironment(os.Environ(), *bootstrapFD),
	)
	stopSupervision := supervision.Start(program, carrier.ParentPID)
	finalModel, runErr := program.Run()
	stopSupervision()
	closeErr := supervision.Close(service)
	if runErr != nil {
		return fail("terminal client: %v", runErr)
	}
	if closeErr != nil {
		return fail("terminal teardown: %v", closeErr)
	}
	if value, ok := finalModel.(interface{ State() app.AppState }); ok {
		state := value.State()
		if failure := state.Failure(); failure != "" {
			return fail("terminal application: %s", failure)
		}
		if _, requested := state.ParentRelaunchRequested(); requested {
			return ParentRelaunchExitCode
		}
	}
	return 0
}

func emitVersionJSON() error {
	value := map[string]any{
		"version": buildinfo.Version, "commit": buildinfo.Commit,
		"protocol_major": buildinfo.ProtocolMajor, "protocol_minor": buildinfo.ProtocolMinor,
		"schema_fingerprint":          buildinfo.SchemaFingerprint,
		"dependency_lock_fingerprint": buildinfo.DependencyLockFingerprint,
		"go_version":                  runtime.Version(), "goos": runtime.GOOS, "goarch": runtime.GOARCH,
	}
	return json.NewEncoder(os.Stdout).Encode(value)
}

func selfTestRuntime() error {
	if protocolvalue.ProtocolMajor != 2 || protocolvalue.SchemaFingerprint == "" {
		return fmt.Errorf("invalid protocol identity")
	}
	info, ok := debug.ReadBuildInfo()
	if !ok || info.GoVersion == "" {
		return fmt.Errorf("build info unavailable")
	}
	return nil
}

func fail(format string, arguments ...any) int {
	fmt.Fprintf(os.Stderr, format+"\n", arguments...)
	return 2
}

func Read(options Options) (protocolvalue.Bootstrap, error) {
	options = options.withDefaults()
	file := os.NewFile(uintptr(options.FD), "pulsara-terminal-bootstrap")
	if file == nil {
		return protocolvalue.Bootstrap{}, errors.New("terminal bootstrap fd is invalid")
	}
	defer file.Close()
	type readResult struct {
		payload []byte
		err     error
	}
	done := make(chan readResult, 1)
	go func() {
		var header [4]byte
		if _, err := io.ReadFull(file, header[:]); err != nil {
			done <- readResult{err: err}
			return
		}
		size := binary.BigEndian.Uint32(header[:])
		if size == 0 || size > MaximumCarrierBytes {
			done <- readResult{err: fmt.Errorf("terminal bootstrap size %d is invalid", size)}
			return
		}
		payload := make([]byte, size)
		if _, err := io.ReadFull(file, payload); err != nil {
			done <- readResult{err: err}
			return
		}
		var trailing [1]byte
		count, err := file.Read(trailing[:])
		if count != 0 || (err != nil && !errors.Is(err, io.EOF)) {
			done <- readResult{err: errors.New("terminal bootstrap contains trailing bytes")}
			return
		}
		done <- readResult{payload: payload}
	}()
	var payload []byte
	select {
	case result := <-done:
		if result.err != nil {
			return protocolvalue.Bootstrap{}, result.err
		}
		payload = result.payload
	case <-time.After(ReadDeadline):
		_ = file.Close()
		return protocolvalue.Bootstrap{}, errors.New("terminal bootstrap read timed out")
	}
	defer clear(payload)
	var carrier protocol.TerminalClientBootstrapCarrier
	if err := proto.Unmarshal(payload, &carrier); err != nil {
		return protocolvalue.Bootstrap{}, fmt.Errorf("decode terminal bootstrap: %w", err)
	}
	defer clear(carrier.LaunchCapability)
	defer clear(carrier.CarrierNonce)
	if err := protocol.ValidateFingerprint("terminal-client-bootstrap:v1", &carrier, "bootstrap_fingerprint", carrier.BootstrapFingerprint); err != nil {
		return protocolvalue.Bootstrap{}, err
	}
	expires, err := time.Parse(time.RFC3339Nano, carrier.ExpiresAtUtc)
	if err != nil || !options.Now().Before(expires) {
		return protocolvalue.Bootstrap{}, errors.New("terminal bootstrap is expired")
	}
	if carrier.CarrierVersion != 1 || carrier.LaunchId == "" || carrier.ClientInstanceId == "" || carrier.HostSessionId == "" || carrier.RuntimeSessionId == "" || carrier.ParentPid == 0 || len(carrier.LaunchCapability) < 32 || len(carrier.CarrierNonce) != 32 || (carrier.RequestedAttachmentRole != protocol.AttachmentRole_ATTACHMENT_ROLE_OBSERVER && carrier.RequestedAttachmentRole != protocol.AttachmentRole_ATTACHMENT_ROLE_CONTROLLER) {
		return protocolvalue.Bootstrap{}, errors.New("terminal bootstrap fields are invalid")
	}
	if !filepath.IsAbs(carrier.UnixSocketPath) || len([]byte(carrier.UnixSocketPath)) > 103 {
		return protocolvalue.Bootstrap{}, errors.New("terminal bootstrap socket path is invalid")
	}
	return protocolvalue.Bootstrap{
		LaunchID:         carrier.LaunchId,
		ClientInstanceID: carrier.ClientInstanceId,
		HostSessionID:    carrier.HostSessionId,
		RuntimeSessionID: carrier.RuntimeSessionId,
		SocketPath:       carrier.UnixSocketPath,
		LaunchCapability: append([]byte(nil), carrier.LaunchCapability...),
		ParentPID:        carrier.ParentPid,
		ExpiresAt:        expires,
		Fingerprint:      carrier.BootstrapFingerprint,
		RequestedRole:    carrier.RequestedAttachmentRole,
	}, nil
}
