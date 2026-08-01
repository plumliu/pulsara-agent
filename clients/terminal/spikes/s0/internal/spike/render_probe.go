package spike

import (
	"os"
	"sync"
	"time"
)

const maxRenderWriteSamples = 8192

// RenderWriteSample records the physical write seam used by Bubble Tea's
// renderer. It deliberately records neither frame contents nor terminal
// secrets. The S0 parent uses the timestamp and byte count to measure output
// cadence over an explicitly bounded observation window.
type RenderWriteSample struct {
	UnixNanos int64 `json:"unix_nanos"`
	Bytes     int   `json:"bytes"`
}

type RenderProbeMetrics struct {
	WriteCount     uint64              `json:"write_count"`
	WrittenBytes   uint64              `json:"written_bytes"`
	DroppedSamples uint64              `json:"dropped_samples"`
	Samples        []RenderWriteSample `json:"samples"`
}

// RenderProbeWriter preserves the concrete terminal file surface (including
// Fd, Read, Close, and Name) while instrumenting non-empty Write calls. This
// lets Bubble Tea retain normal PTY detection and terminal behavior.
type RenderProbeWriter struct {
	*os.File

	mu             sync.Mutex
	writeCount     uint64
	writtenBytes   uint64
	droppedSamples uint64
	samples        []RenderWriteSample
}

func NewRenderProbeWriter(file *os.File) *RenderProbeWriter {
	return &RenderProbeWriter{File: file}
}

func (w *RenderProbeWriter) Write(payload []byte) (int, error) {
	written, err := w.File.Write(payload)
	if written > 0 {
		w.mu.Lock()
		w.writeCount++
		w.writtenBytes += uint64(written)
		if len(w.samples) < maxRenderWriteSamples {
			w.samples = append(w.samples, RenderWriteSample{
				UnixNanos: time.Now().UnixNano(),
				Bytes:     written,
			})
		} else {
			w.droppedSamples++
		}
		w.mu.Unlock()
	}
	return written, err
}

func (w *RenderProbeWriter) Metrics() RenderProbeMetrics {
	w.mu.Lock()
	defer w.mu.Unlock()
	return RenderProbeMetrics{
		WriteCount:     w.writeCount,
		WrittenBytes:   w.writtenBytes,
		DroppedSamples: w.droppedSamples,
		Samples:        append([]RenderWriteSample(nil), w.samples...),
	}
}
