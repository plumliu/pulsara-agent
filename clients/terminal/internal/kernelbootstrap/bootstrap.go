package kernelbootstrap

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"syscall"
	"time"

	tea "charm.land/bubbletea/v2"
	"google.golang.org/protobuf/proto"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/buildinfo"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/config"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/kernelapp"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/kernelclient"
	protocolv3 "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolv3"
)

const (
	maximumCarrierBytes = 64 << 10
	readDeadline        = 10 * time.Second
)

func Run(arguments []string) int {
	flags := flag.NewFlagSet("pulsara-tui", flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	bootstrapFD := flags.Int("bootstrap-fd", config.ParseBootstrapFD(os.Getenv("PULSARA_TUI_BOOTSTRAP_FD"), 3), "inherited one-shot bootstrap fd")
	showVersion := flags.Bool("version", false, "print version")
	showVersionJSON := flags.Bool("version-json", false, "print version metadata as JSON")
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
		fmt.Printf("%d.%d schema=%s\n", kernelclient.ProtocolMajor, kernelclient.ProtocolMinor, kernelclient.SchemaFingerprint)
		return 0
	}
	carrier, err := Read(*bootstrapFD, time.Now)
	if err != nil {
		return fail("bootstrap: %v", err)
	}
	service, err := kernelclient.New(carrier)
	if err != nil {
		return fail("client composition: %v", err)
	}
	model := kernelapp.New(service)
	program := tea.NewProgram(
		model,
		tea.WithContext(context.Background()),
		tea.WithInput(os.Stdin), tea.WithOutput(os.Stdout),
		tea.WithEnvironment(config.SanitizedEnvironment(os.Environ(), *bootstrapFD)),
		tea.WithoutSignalHandler(), tea.WithFPS(60),
	)
	stop := supervise(program, carrier.ParentPID)
	_, runErr := program.Run()
	stop()
	closeErr := service.Close()
	if runErr != nil {
		return fail("terminal client: %v", runErr)
	}
	if closeErr != nil {
		return fail("terminal teardown: %v", closeErr)
	}
	return 0
}

func emitVersionJSON() error {
	value := map[string]any{
		"version": buildinfo.Version, "commit": buildinfo.Commit,
		"protocol_major": kernelclient.ProtocolMajor, "protocol_minor": kernelclient.ProtocolMinor,
		"schema_fingerprint":          kernelclient.SchemaFingerprint,
		"dependency_lock_fingerprint": buildinfo.DependencyLockFingerprint,
		"go_version":                  runtime.Version(), "goos": runtime.GOOS, "goarch": runtime.GOARCH,
	}
	return json.NewEncoder(os.Stdout).Encode(value)
}

func Read(fd int, now func() time.Time) (kernelclient.Bootstrap, error) {
	file := os.NewFile(uintptr(fd), "pulsara-terminal-v3-bootstrap")
	if file == nil {
		return kernelclient.Bootstrap{}, errors.New("Protocol v3 bootstrap fd is invalid")
	}
	defer file.Close()
	type result struct {
		payload []byte
		err     error
	}
	done := make(chan result, 1)
	go func() {
		var header [4]byte
		if _, err := io.ReadFull(file, header[:]); err != nil {
			done <- result{err: err}
			return
		}
		size := binary.BigEndian.Uint32(header[:])
		if size == 0 || size > maximumCarrierBytes {
			done <- result{err: errors.New("Protocol v3 bootstrap size is invalid")}
			return
		}
		payload := make([]byte, size)
		if _, err := io.ReadFull(file, payload); err != nil {
			done <- result{err: err}
			return
		}
		var trailing [1]byte
		count, err := file.Read(trailing[:])
		if count != 0 || (err != nil && !errors.Is(err, io.EOF)) {
			done <- result{err: errors.New("Protocol v3 bootstrap contains trailing bytes")}
			return
		}
		done <- result{payload: payload}
	}()
	var payload []byte
	select {
	case value := <-done:
		if value.err != nil {
			return kernelclient.Bootstrap{}, value.err
		}
		payload = value.payload
	case <-time.After(readDeadline):
		_ = file.Close()
		return kernelclient.Bootstrap{}, errors.New("Protocol v3 bootstrap read timed out")
	}
	defer clear(payload)
	carrier := &protocolv3.TerminalKernelBootstrapCarrier{}
	if err := proto.Unmarshal(payload, carrier); err != nil {
		return kernelclient.Bootstrap{}, err
	}
	defer clear(carrier.LaunchCapability)
	defer clear(carrier.CarrierNonce)
	if err := validateFingerprint("terminal-kernel-bootstrap:v3", carrier, carrier.CarrierFingerprint); err != nil {
		return kernelclient.Bootstrap{}, err
	}
	expires, err := time.Parse(time.RFC3339Nano, carrier.ExpiresAtUtc)
	if err != nil || !now().Before(expires) {
		return kernelclient.Bootstrap{}, errors.New("Protocol v3 bootstrap is expired")
	}
	if carrier.CarrierVersion != 1 || carrier.LaunchId == "" || len(carrier.LaunchCapability) < 32 || carrier.ClientInstanceId == "" || carrier.HostSessionId == "" || carrier.SessionId == "" || carrier.ParentPid == 0 || len(carrier.CarrierNonce) != 32 || (carrier.RequestedRole != protocolv3.AttachmentRole_ATTACHMENT_ROLE_OBSERVER && carrier.RequestedRole != protocolv3.AttachmentRole_ATTACHMENT_ROLE_CONTROLLER) {
		return kernelclient.Bootstrap{}, errors.New("Protocol v3 bootstrap fields are invalid")
	}
	if !filepath.IsAbs(carrier.UnixSocketPath) || len([]byte(carrier.UnixSocketPath)) > 103 {
		return kernelclient.Bootstrap{}, errors.New("Protocol v3 socket path is invalid")
	}
	return kernelclient.Bootstrap{
		LaunchID: carrier.LaunchId, LaunchCapability: append([]byte(nil), carrier.LaunchCapability...), ClientInstanceID: carrier.ClientInstanceId,
		HostSessionID: carrier.HostSessionId, SessionID: carrier.SessionId, SocketPath: carrier.UnixSocketPath, RequestedRole: carrier.RequestedRole, ParentPID: carrier.ParentPid,
	}, nil
}

func validateFingerprint(namespace string, message *protocolv3.TerminalKernelBootstrapCarrier, expected string) error {
	clone := proto.Clone(message).(*protocolv3.TerminalKernelBootstrapCarrier)
	clone.CarrierFingerprint = ""
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(clone)
	if err != nil {
		return err
	}
	digest := sha256.Sum256(append(append([]byte(namespace), 0), payload...))
	if expected != fmt.Sprintf("sha256:%x", digest) {
		return errors.New("Protocol v3 bootstrap fingerprint mismatch")
	}
	return nil
}

func supervise(program *tea.Program, parentPID uint64) func() {
	done := make(chan struct{})
	signals := make(chan os.Signal, 2)
	signal.Notify(signals, os.Interrupt, syscall.SIGTERM, syscall.SIGHUP)
	go func() {
		select {
		case <-signals:
			program.Quit()
		case <-done:
		}
	}()
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if syscall.Kill(int(parentPID), 0) != nil {
					program.Quit()
					return
				}
			case <-done:
				return
			}
		}
	}()
	return func() { signal.Stop(signals); close(done) }
}

func fail(format string, values ...any) int { fmt.Fprintf(os.Stderr, format+"\n", values...); return 2 }
