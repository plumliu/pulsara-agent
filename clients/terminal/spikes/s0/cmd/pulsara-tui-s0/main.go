package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"runtime/debug"
	"time"

	tea "charm.land/bubbletea/v2"

	"pulsara.local/terminal-s0/internal/spike"
)

var version = "dev"

type processResult struct {
	Version      string                    `json:"version"`
	Outcome      string                    `json:"outcome"`
	Dependencies map[string]string         `json:"dependencies"`
	Metrics      spike.Metrics             `json:"metrics"`
	RenderProbe  *spike.RenderProbeMetrics `json:"render_probe,omitempty"`
}

func main() {
	streamFD := flag.Int("stream-fd", -1, "read length-prefixed fake protobuf frames from this fd")
	autoQuit := flag.Duration("auto-quit", 0, "quit automatically after this duration")
	metricsFile := flag.String("metrics-file", "", "write terminal metrics as JSON")
	renderProbeEnabled := flag.Bool("render-probe", false, "record bounded physical output-write samples")
	showVersion := flag.Bool("version", false, "print version and exit")
	selfTest := flag.Bool("self-test", false, "run a non-TTY model smoke and exit")
	flag.Parse()

	if *showVersion {
		fmt.Printf("pulsara-tui-s0 %s\n", version)
		return
	}
	if *selfTest {
		result := runSelfTest()
		mustWriteJSON(os.Stdout, result)
		return
	}

	model := spike.NewModel()
	var renderWriter *spike.RenderProbeWriter
	programOptions := make([]tea.ProgramOption, 0, 1)
	if *renderProbeEnabled {
		renderWriter = spike.NewRenderProbeWriter(os.Stdout)
		programOptions = append(programOptions, tea.WithOutput(renderWriter))
	}
	program := tea.NewProgram(model, programOptions...)
	if *streamFD >= 0 {
		stream := os.NewFile(uintptr(*streamFD), "s0-protobuf-stream")
		if stream == nil {
			fatalf("invalid stream fd: %d", *streamFD)
		}
		defer stream.Close()
		go func() {
			err := spike.ReadProbeStream(stream, program.Send)
			if err != nil && !errors.Is(err, io.EOF) {
				program.Send(spike.StreamFailureMsg{Reason: err.Error()})
			}
		}()
	}
	if *autoQuit > 0 {
		go func() {
			time.Sleep(*autoQuit)
			program.Send(tea.Quit())
		}()
	}

	final, runErr := program.Run()
	outcome := "completed"
	if runErr != nil {
		outcome = runErr.Error()
	}
	metrics := model.Metrics()
	if typed, ok := final.(spike.Model); ok {
		metrics = typed.Metrics()
	}
	result := processResult{
		Version:      version,
		Outcome:      outcome,
		Dependencies: dependencyVersions(),
		Metrics:      metrics,
	}
	if renderWriter != nil {
		renderMetrics := renderWriter.Metrics()
		result.RenderProbe = &renderMetrics
	}
	if *metricsFile != "" {
		file, err := os.Create(*metricsFile)
		if err != nil {
			fatalf("create metrics file: %v", err)
		}
		mustWriteJSON(file, result)
		if err := file.Close(); err != nil {
			fatalf("close metrics file: %v", err)
		}
	}
	if runErr != nil {
		fmt.Fprintln(os.Stderr, runErr)
		os.Exit(1)
	}
}

func runSelfTest() processResult {
	model := spike.NewModel()
	next, _ := model.Update(tea.WindowSizeMsg{Width: 120, Height: 32})
	model = next.(spike.Model)
	next, _ = model.Update(tea.KeyPressMsg(tea.Key{Code: tea.KeyExtended, Text: "中文🙂ASCII"}))
	model = next.(spike.Model)
	next, _ = model.Update(spike.StreamDeltaMsg{Sequence: 1, Content: "fake delta", SentUnixNanos: time.Now().UnixNano()})
	model = next.(spike.Model)
	return processResult{
		Version:      version,
		Outcome:      "self_test_completed",
		Dependencies: dependencyVersions(),
		Metrics:      model.Metrics(),
	}
}

func dependencyVersions() map[string]string {
	versions := map[string]string{}
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return versions
	}
	for _, dependency := range info.Deps {
		switch dependency.Path {
		case "charm.land/bubbletea/v2", "charm.land/bubbles/v2", "charm.land/lipgloss/v2", "google.golang.org/protobuf":
			versions[dependency.Path] = dependency.Version
		}
	}
	return versions
}

func mustWriteJSON(writer io.Writer, value any) {
	encoder := json.NewEncoder(writer)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(value); err != nil {
		fatalf("encode JSON: %v", err)
	}
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(2)
}
