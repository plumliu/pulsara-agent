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
from unittest.mock import patch
import av
from PIL import Image, ImageDraw
from pulsara_agent.media import frames
from pulsara_agent.media.frames import FRAME_ANALYSIS_FRAMES, FrameBudget, prepare_frame_plan, read_video_frames
from pulsara_agent.media.frames_decode import VideoDecoder
from pulsara_agent.media.frames_layout import row_candidates, pack_rows
from pulsara_agent.media.frames_selection import Sample, detect_changes
from pulsara_agent.media.source import MediaSource, probe_media


def fixture(path, rate, duration, *, local=False, onset=0.04):
    with av.open(str(path), "w") as output:
        stream = output.add_stream("ffv1", rate=rate)
        stream.width, stream.height = 320, 180
        stream.pix_fmt = "bgr0"
        stream.time_base = Fraction(1, rate)
        for i in range(math.ceil(0.2 * rate)):
            active = Fraction(str(onset)) <= Fraction(i, rate) < Fraction(str(onset)) + Fraction(str(duration))
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


def in_excursion(t, onset, duration):
    return Fraction(str(onset)) <= t < Fraction(str(onset)) + Fraction(str(duration))


def delivered_plan(path, mode):
    """Exercise the production prepare/render/continuation pipeline, page by page."""
    decoders = []

    class CountedDecoder(VideoDecoder):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            decoders.append(self)

    page_budget = FrameBudget(4_000_000, 1280 * 720, 1, FRAME_ANALYSIS_FRAMES)
    began = time.perf_counter()
    with MediaSource.open(path) as source, patch.object(frames, "VideoDecoder", CountedDecoder):
        info = probe_media(source)
        plan = prepare_frame_plan(source, info, start=0, end=info.duration, mode=mode)
        prepared_seconds = time.perf_counter() - began
        planned_times = [observation.t for observation in plan.priority]
        continuation = None
        seen = set()
        delivered, page_counts = [], []
        encoded_bytes = index_bytes = 0
        while True:
            result = read_video_frames(
                source, info, start=0, end=info.duration, mode=mode,
                continuation=continuation, budget=page_budget,
                accepts=lambda page: len(json.loads(page.text)["frames"]) <= 3,
            )
            body = json.loads(result.text)
            times = [Fraction(str(frame["t"])) for frame in body["frames"]]
            assert times and not set(times).intersection(delivered)
            delivered.extend(times)
            page_counts.append(len(times))
            encoded_bytes += sum(len(image.data) for image in result.images)
            index_bytes += len(result.text.encode("utf-8"))
            if body["next"] is None:
                assert body["complete"]
                break
            arguments = body["next"]["arguments"]
            assert arguments["from"] == 0 and arguments["to"] == info.duration
            continuation = arguments["continuation"]
            cursor = json.dumps(continuation, sort_keys=True)
            assert cursor not in seen
            seen.add(cursor)
        # These fixtures have exact millisecond PTS. Source-time output floats
        # therefore round-trip exactly; the production cursor still uses PTS.
        assert sorted(delivered) == sorted(planned_times)
    return planned_times, delivered, {
        "selection_observations": len(planned_times),
        "page_count": len(page_counts),
        "frames_per_page": page_counts,
        "encoded_bytes": encoded_bytes,
        "index_utf8_bytes": index_bytes,
        "probe_and_prepare_seconds": round(prepared_seconds, 6),
        "read_all_pages_seconds": round(time.perf_counter() - began - prepared_seconds, 6),
        "decode_frames": sum(decoder.decoded_frames for decoder in decoders),
        "decoder_opens": sum(decoder.opens for decoder in decoders),
        "seeks": sum(decoder.seeks for decoder in decoders),
        "rewinds": sum(decoder.rewinds for decoder in decoders),
        "page_budget": {
            "maximum_bytes": page_budget.maximum_bytes,
            "image_pixels": page_budget.image_pixels,
            "maximum_images": page_budget.maximum_images,
            "analysis_frames": page_budget.analysis_frames,
            "maximum_delivered_observations_for_benchmark": 3,
        },
    }


def run():
    totals = Counter()
    cases, baseline_cases = [], []
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="pulsara-frame-benchmark-") as directory:
        for rate in (25, 50, 100):
            for duration, onset in (
                (0.01, 0.04), (0.04, 0.04), (0.08, 0.04), (0.005, 0.045),
            ):
                for local in (False, True):
                    path = Path(directory).resolve() / f"{rate}-{duration}-{local}.mkv"
                    fixture(path, rate, duration, local=local, onset=onset)
                    with MediaSource.open(path) as source:
                        info = probe_media(source)
                        with VideoDecoder(source, info) as decoder:
                            observed = list(decoder.scan(0, info.duration, FRAME_ANALYSIS_FRAMES))
                    recorded = any(in_excursion(sample.t, onset, duration) for sample in observed)
                    for mode in ("overview", "detail", "motion"):
                        planned, delivered, resources = delivered_plan(path, mode)
                        selected_b = any(in_excursion(t, onset, duration) for t in planned)
                        delivered_b = any(in_excursion(t, onset, duration) for t in delivered)
                        assert selected_b == delivered_b
                        category = (
                            "source_not_recorded" if not recorded
                            else "recorded_not_selected" if not selected_b
                            else "selected_and_delivered"
                        )
                        totals[f"{mode}:{category}"] += 1
                        cases.append({
                            "rate": rate, "b_seconds": duration, "b_onset": onset,
                            "local": local, "mode": mode, "recorded": recorded,
                            "selected_b": selected_b, "delivered_b": delivered_b,
                            "selected_times": list(map(float, planned)),
                            "delivered_times": list(map(float, delivered)),
                            **resources,
                        })
                    # Explicit cadence/phase baselines measure probe misses;
                    # they are not a second production selector or delivery path.
                    for interval in (0.01, 0.05, 0.1):
                        for phase in (0, 0.025):
                            chosen = {0, len(observed) - 1}
                            target = Fraction(str(phase))
                            while target < info.duration:
                                visible = [i for i, sample in enumerate(observed) if sample.t <= target]
                                if visible:
                                    chosen.add(visible[-1])
                                target += Fraction(str(interval))
                            samples = [Sample(observed[i].t, observed[i].image, 0, observed[i].end)
                                       for i in sorted(chosen)]
                            captured = any(in_excursion(sample.t, onset, duration) for sample in samples)
                            changes = detect_changes(samples)
                            baseline_cases.append({
                                "rate": rate, "b_seconds": duration, "b_onset": onset,
                                "local": local, "interval": interval, "phase": phase,
                                "recorded": recorded, "sampled_b": captured,
                                "candidate_aba": bool(changes.round_trips),
                                "sample_count": len(samples),
                                "actual_dt": [round(float(b.t - a.t), 6)
                                              for a, b in zip(samples, samples[1:])],
                            })
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
        "scope": "synthetic PTS/feature/fixed-plan pagination/layout only; no natural-video or model accuracy claim",
        "decode_scope": "resource counts include the explicit plan oracle and every production page prepare/render/retry",
        "totals": dict(totals),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "cases": cases,
        "baseline_cases": baseline_cases,
        "layouts": layouts,
    }


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
