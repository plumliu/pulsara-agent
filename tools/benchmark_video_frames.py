"""Reproducible local sampling and ordered-layout benchmark (no model calls).

Run: PYTHONPATH=src .venv/bin/python tools/benchmark_video_frames.py
Synthetic truth proves only the recorded/sampled cases, not general accuracy.
"""

from __future__ import annotations
from collections import Counter
from fractions import Fraction
import json
import math
from pathlib import Path
import tempfile
import time
import av
from PIL import Image, ImageDraw
from pulsara_agent.media.frames import _scan
from pulsara_agent.media.frames_layout import row_candidates, pack_rows
from pulsara_agent.media.frames_selection import Sample, detect_changes, select_samples
from pulsara_agent.media.source import MediaSource, probe_media


def fixture(path, rate, duration, *, local=False, onset=0.04):
    with av.open(str(path), "w") as output:
        stream = output.add_stream("ffv1", rate=rate)
        stream.width, stream.height = 320, 180
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, rate)
        for i in range(math.ceil(0.2 * rate)):
            active = onset <= i / rate < onset + duration
            image = Image.new("RGB", (320, 180), "black")
            if active:
                if local:
                    ImageDraw.Draw(image).rectangle((20, 20, 40, 40), fill="white")
                else:
                    image.paste("blue", (0, 0, 320, 180))
            frame = av.VideoFrame.from_image(image)
            frame.pts = i
            frame.time_base = Fraction(1, rate)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)


def run():
    totals = Counter()
    cases = []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="pulsara-frame-benchmark-") as directory:
        for rate in (25, 50, 100):
            for duration, onset in (
                (0.01, 0.04),
                (0.04, 0.04),
                (0.08, 0.04),
                (0.005, 0.045),
            ):
                for local in (False, True):
                    path = Path(directory).resolve() / f"{rate}-{duration}-{local}.mkv"
                    fixture(path, rate, duration, local=local, onset=onset)
                    with MediaSource.open(path) as source:
                        info = probe_media(source)
                        observed, _, _ = _scan(
                            source, info, 0, info.duration, 100, None, None
                        )
                    recorded = any(
                        onset <= sample.t < onset + duration for sample in observed
                    )
                    for interval in (0.01, 0.05, 0.1):
                        for phase in (0, 0.025):
                            chosen = {0, len(observed) - 1}
                            target = phase
                            while target < info.duration:
                                visible = [
                                    i for i, s in enumerate(observed) if s.t <= target
                                ]
                                if visible:
                                    chosen.add(visible[-1])
                                target += interval
                            samples = [
                                Sample(
                                    observed[i].t, observed[i].image, 0, observed[i].end
                                )
                                for i in sorted(chosen)
                            ]
                            captured = any(
                                onset <= s.t < onset + duration for s in samples
                            )
                            changes = detect_changes(samples)
                            selected = select_samples(
                                samples, changes, min(8, len(samples)), "motion"
                            )
                            delivered = any(
                                onset <= samples[i].t < onset + duration
                                for i in selected
                            )
                            category = (
                                "source_not_recorded"
                                if not recorded
                                else "sampling_miss"
                                if not captured
                                else "sampled_and_delivered"
                                if delivered
                                else "sampled_then_removed"
                            )
                            totals[category] += 1
                            if changes.round_trips:
                                totals["round_trip_candidates"] += 1
                            cases.append(
                                {
                                    "rate": rate,
                                    "b_seconds": duration,
                                    "b_onset": onset,
                                    "local": local,
                                    "interval": interval,
                                    "phase": phase,
                                    "recorded": recorded,
                                    "sampled_b": captured,
                                    "candidate_aba": bool(changes.round_trips),
                                    "delivered_b": delivered,
                                    "sample_count": len(samples),
                                    "actual_dt": [
                                        round(samples[i].t - samples[i - 1].t, 6)
                                        for i in range(1, len(samples))
                                    ],
                                }
                            )
    layouts = []
    for tiers in (
        (3, 3, 3, 3, 3, 3, 3, 3),
        (1, 6, 6, 3, 2, 6, 6, 3),
        (2, 3, 4, 5, 6, 7, 8, 3),
    ):
        rows = row_candidates(tiers, 1280, 720, 16 / 9, (160, 90))
        count = len(tiers)
        columns = math.ceil(math.sqrt(count))
        row_count = math.ceil(count / columns)
        cell_width = min(1280 // columns, int(720 / row_count * 16 / 9))
        cell_height = math.ceil(cell_width * 9 / 16)
        area = count * cell_width * cell_height
        for penalty in (1.02, 1.5, 2.5):
            begin = time.perf_counter()
            layout = pack_rows(rows, 1280, 720, penalty=penalty)
            layouts.append(
                {
                    "tiers": tiers,
                    "lambda": penalty,
                    "dp_pages": len(layout.page_sizes),
                    "dp_area": layout.area,
                    "dp_score": round(layout.score, 6),
                    "grid_pages": 1,
                    "grid_area": area,
                    "grid_score": round(area / (1280 * 720) - penalty, 6),
                    "solve_ms": round((time.perf_counter() - begin) * 1000, 3),
                }
            )
    return {
        "scope": "synthetic PTS sampling/feature/layout only; no natural-video or model accuracy claim",
        "totals": dict(totals),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "cases": cases,
        "layouts": layouts,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
