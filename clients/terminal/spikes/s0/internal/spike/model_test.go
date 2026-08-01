package spike

import (
	"bytes"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	tea "charm.land/bubbletea/v2"
)

func TestMetricsExposeNearestRankDeliveryDistribution(t *testing.T) {
	model := NewModel()
	model.deliveryLatencyMicros = []int64{100, 10, 50, 20, 30}
	metrics := model.Metrics()
	if got, want := metrics.DeliveryP50Micros, int64(30); got != want {
		t.Fatalf("p50 = %d, want %d", got, want)
	}
	if got, want := metrics.DeliveryP95Micros, int64(100); got != want {
		t.Fatalf("p95 = %d, want %d", got, want)
	}
	if got, want := metrics.DeliveryP99Micros, int64(100); got != want {
		t.Fatalf("p99 = %d, want %d", got, want)
	}
	if got, want := metrics.DeliveryMaxMicros, int64(100); got != want {
		t.Fatalf("max = %d, want %d", got, want)
	}
}

func TestRenderProbeWriterPreservesBytesAndRecordsPhysicalWrites(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "render-probe-*.out")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()

	writer := NewRenderProbeWriter(file)
	if _, err := writer.Write([]byte("first")); err != nil {
		t.Fatal(err)
	}
	if _, err := writer.Write([]byte("second")); err != nil {
		t.Fatal(err)
	}
	if _, err := file.Seek(0, 0); err != nil {
		t.Fatal(err)
	}
	got, err := io.ReadAll(file)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, []byte("firstsecond")) {
		t.Fatalf("written payload = %q", got)
	}
	metrics := writer.Metrics()
	if metrics.WriteCount != 2 || metrics.WrittenBytes != 11 || len(metrics.Samples) != 2 {
		t.Fatalf("render metrics = %+v", metrics)
	}
	if writer.Fd() != file.Fd() {
		t.Fatal("render writer did not preserve terminal file descriptor")
	}
}

func TestCJKEmojiCursorOperationsStayOnRuneBoundaries(t *testing.T) {
	model := NewModel()
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyExtended, Text: "中🙂A"}))
	if got, want := model.ComposerValue(), "中🙂A"; got != want {
		t.Fatalf("initial value = %q, want %q", got, want)
	}
	if got, want := DisplayWidth(model.ComposerValue()), 5; got != want {
		t.Fatalf("display width = %d, want %d", got, want)
	}

	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyHome}))
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyRight}))
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyExtended, Text: "界"}))
	if got, want := model.ComposerValue(), "中界🙂A"; got != want {
		t.Fatalf("insert at wide-rune boundary = %q, want %q", got, want)
	}

	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyDelete}))
	if got, want := model.ComposerValue(), "中界A"; got != want {
		t.Fatalf("forward delete = %q, want %q", got, want)
	}
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyBackspace}))
	if got, want := model.ComposerValue(), "中A"; got != want {
		t.Fatalf("backspace = %q, want %q", got, want)
	}
}

func TestTextareaMultilineShiftEnterFallbackAndUndo(t *testing.T) {
	model := NewModel()
	model = updateModel(t, model, tea.PasteMsg{Content: "第一行\nsecond🙂\n第三行"})
	if got, want := model.ComposerValue(), "第一行\nsecond🙂\n第三行"; got != want {
		t.Fatalf("multiline paste = %q, want %q", got, want)
	}
	if model.ComposerHeight() < 3 {
		t.Fatalf("dynamic textarea height = %d, want at least 3", model.ComposerHeight())
	}

	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyEnter, Mod: tea.ModShift}))
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: 'j', Mod: tea.ModCtrl}))
	if !strings.HasSuffix(model.ComposerValue(), "\n\n") {
		t.Fatalf("shift-enter and ctrl+j fallback did not append two newlines: %q", model.ComposerValue())
	}

	beforeUndo := model.ComposerValue()
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyExtended, Text: "undo-me"}))
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: 'z', Mod: tea.ModCtrl}))
	if got := model.ComposerValue(); got != beforeUndo {
		t.Fatalf("undo = %q, want %q", got, beforeUndo)
	}
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: 'z', Mod: tea.ModCtrl | tea.ModShift}))
	if got := model.ComposerValue(); got != beforeUndo+"undo-me" {
		t.Fatalf("redo = %q, want %q", got, beforeUndo+"undo-me")
	}
}

func TestOneMiBPasteIsExternalizedWithoutFreezingOrDraftRetention(t *testing.T) {
	model := NewModel()
	payload := strings.Repeat("界", 1024*1024/len("界")+1)
	payload = payload[:1024*1024]
	started := time.Now()
	model = updateModel(t, model, tea.PasteMsg{Content: payload})
	elapsed := time.Since(started)

	metrics := model.Metrics()
	if metrics.LargePasteBytes != len(payload) {
		t.Fatalf("large paste bytes = %d, want %d", metrics.LargePasteBytes, len(payload))
	}
	if metrics.Draft != "" {
		t.Fatalf("large paste leaked into textarea: %d bytes", len(metrics.Draft))
	}
	if metrics.LargePasteSHA256 == "" {
		t.Fatal("large paste fingerprint missing")
	}
	if elapsed > 250*time.Millisecond {
		t.Fatalf("large paste update took %s, limit 250ms", elapsed)
	}
}

func TestPasteCancellationBoundaryDoesNotMutateDraft(t *testing.T) {
	model := NewModel()
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyExtended, Text: "keep"}))
	model = updateModel(t, model, tea.PasteStartMsg{})
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyEscape}))
	if got := model.ComposerValue(); got != "keep" {
		t.Fatalf("cancelled paste changed draft: %q", got)
	}
	if got := model.Metrics().LastAction; got != "escape" {
		t.Fatalf("last action = %q, want escape", got)
	}
}

func TestResizeMatrixAndExtremeViewportDoNotPanic(t *testing.T) {
	model := NewModel()
	model = updateModel(t, model, tea.KeyPressMsg(tea.Key{Code: tea.KeyExtended, Text: "中文🙂 mixed ASCII"}))
	for _, width := range []int{1, 8, 80, 120, 160} {
		for _, height := range []int{1, 2, 8, 24, 60} {
			model = updateModel(t, model, tea.WindowSizeMsg{Width: width, Height: height})
			view := model.View()
			if view.Content == "" {
				t.Fatalf("empty view at %dx%d", width, height)
			}
			if view.Cursor != nil && (view.Cursor.Position.X < 0 || view.Cursor.Position.Y < 0) {
				t.Fatalf("negative cursor at %dx%d: %+v", width, height, view.Cursor.Position)
			}
		}
	}
}

func TestProgramSerializesHundredHzDeltasWithoutDraftLoss(t *testing.T) {
	program := tea.NewProgram(
		NewModel(),
		tea.WithInput(nil),
		tea.WithOutput(io.Discard),
		tea.WithoutRenderer(),
	)
	type runResult struct {
		model tea.Model
		err   error
	}
	done := make(chan runResult, 1)
	go func() {
		model, err := program.Run()
		done <- runResult{model: model, err: err}
	}()

	program.Send(tea.WindowSizeMsg{Width: 120, Height: 40})
	wantDraft := "持续输入中文🙂ASCII"
	for _, value := range []rune(wantDraft) {
		program.Send(tea.KeyPressMsg(tea.Key{Code: value, Text: string(value)}))
	}
	for sequence := uint64(1); sequence <= 200; sequence++ {
		program.Send(StreamDeltaMsg{
			Sequence:      sequence,
			Content:       "fake delta",
			SentUnixNanos: time.Now().UnixNano(),
		})
		time.Sleep(10 * time.Millisecond)
	}
	program.Send(tea.Quit())

	select {
	case result := <-done:
		if result.err != nil {
			t.Fatalf("program run: %v", result.err)
		}
		model := result.model.(Model)
		if got := model.ComposerValue(); got != wantDraft {
			t.Fatalf("draft = %q, want %q", got, wantDraft)
		}
		metrics := model.Metrics()
		if metrics.DeltaCount != 200 || metrics.LastDeltaSequence != 200 {
			t.Fatalf("delta metrics = %+v", metrics)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("program did not terminate")
	}
}

func updateModel(t *testing.T, model Model, msg tea.Msg) Model {
	t.Helper()
	next, _ := model.Update(msg)
	return next.(Model)
}
