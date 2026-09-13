"""Local D2 resource experiments; no provider, settings or database access.

Pillow owns decoding/encoding. Python subprocesses isolate measurements; their
30-second experimental deadline kills and reaps the worker. This is not a
production decoder, image admission implementation, or product resource limit.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import gc
from io import BytesIO
import json
from pathlib import Path
import platform
import resource
import subprocess
import sys
import threading
from time import perf_counter
import warnings

from PIL import Image, ImageFile, PngImagePlugin, __version__ as pillow_version


MIB = 1 << 20
SUPPORTED = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class ImagePolicyRejection(ValueError):
    pass


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def decode_image(
    spec: dict, *, max_pixels: int | None = None
) -> tuple[Image.Image, bytes, dict]:
    """Experimental verify/reopen/load path; dimensions come from actual bytes."""
    t0 = perf_counter()
    body = Path(spec["path"]).read_bytes()
    t1 = perf_counter()
    image = Image.open(BytesIO(body))
    try:
        header = {
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "mode": image.mode,
            "frames": getattr(image, "n_frames", 1),
            "info_text_bytes": sum(
                len(v.encode("utf-8"))
                for v in image.info.values()
                if isinstance(v, str)
            ),
            "info_binary_bytes": sum(
                len(v) for v in image.info.values() if isinstance(v, bytes)
            ),
        }
        if image.format not in SUPPORTED:
            raise ImagePolicyRejection(f"unsupported_format:{image.format}")
        if spec.get("mime") and spec["mime"] != SUPPORTED[image.format]:
            raise ImagePolicyRejection(
                f"mime_mismatch:{spec['mime']}:{SUPPORTED[image.format]}"
            )
        if header["frames"] != 1 or getattr(image, "is_animated", False):
            raise ImagePolicyRejection(f"animated_or_multiframe:{header['frames']}")
        if max_pixels is not None and image.width * image.height > max_pixels:
            raise ImagePolicyRejection(
                f"experimental_pixel_candidate:{image.width * image.height}>{max_pixels}"
            )
        t2 = perf_counter()
        image.verify()
        t3 = perf_counter()
    finally:
        image.close()
        del image  # Release header metadata before reopening the same bytes.
    image = Image.open(BytesIO(body))
    try:
        image.load()
        t4 = perf_counter()
        return (
            image,
            body,
            {
                **header,
                "encoded_bytes": len(body),
                "read_ms": 1000 * (t1 - t0),
                "header_ms": 1000 * (t2 - t1),
                "verify_ms": 1000 * (t3 - t2),
                "reopen_load_ms": 1000 * (t4 - t3),
            },
        )
    except BaseException:
        image.close()
        raise


def worker(job: dict) -> dict:
    # Import/initialize codecs before recording the baseline. Warmup also makes
    # one-time encoder/decoder initialization comparable across fresh workers.
    Image.init()
    for codec in SUPPORTED:
        sink = BytesIO()
        with Image.new("RGB", (1, 1)) as im:
            im.save(sink, format=codec)
        with Image.open(BytesIO(sink.getvalue())) as im:
            im.load()
    gc.collect()
    if job.get("png_text_memory_candidate") is not None:
        PngImagePlugin.MAX_TEXT_MEMORY = job["png_text_memory_candidate"]
    initial_peak = peak_rss_bytes()
    start = perf_counter()
    result = {"status": "ok", "baseline_peak_rss_bytes": initial_peak}
    mode = job["operation"]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            if mode == "decode":
                image, body, facts = decode_image(
                    job["fixture"], max_pixels=job.get("max_pixels")
                )
                result.update(facts)
                result["loaded_peak_rss_bytes"] = peak_rss_bytes()
                image.close()
                del image, body
            elif mode == "decode_batch":
                count, concurrency = job["copies"], job["concurrency"]
                barrier = threading.Barrier(concurrency)
                retained = []

                def one(_):
                    try:
                        image, body, facts = decode_image(job["fixture"])
                        if job["retention"] == "retain_all":
                            retained.append((image, body))
                        else:
                            # All concurrent pixel buffers overlap before release.
                            barrier.wait(timeout=20)
                            image.close()
                        return facts
                    except BaseException:
                        barrier.abort()
                        raise

                with ThreadPoolExecutor(max_workers=concurrency) as executor:
                    facts = list(executor.map(one, range(count)))
                result["completed_images"] = len(facts)
                result["sum_decode_ms"] = sum(f["reopen_load_ms"] for f in facts)
                result["loaded_peak_rss_bytes"] = peak_rss_bytes()
                for image, body in retained:
                    image.close()
                retained.clear()
            elif mode == "wire":
                fixture, copies = job["fixture"], job["copies"]
                if job["sharing"] == "shared":
                    body = Path(fixture["path"]).read_bytes()
                    bodies = [body] * copies
                    url = f"data:{fixture['mime']};base64," + base64.b64encode(
                        body
                    ).decode("ascii")
                    urls = [url] * copies
                else:
                    bodies = [Path(fixture["path"]).read_bytes() for _ in range(copies)]
                    urls = [
                        f"data:{fixture['mime']};base64,"
                        + base64.b64encode(b).decode("ascii")
                        for b in bodies
                    ]
                text = "Compare these images."
                if job["wire_api"] == "chat":
                    parts = [{"type": "text", "text": text}] + [
                        {"type": "image_url", "image_url": {"url": u, "detail": "auto"}}
                        for u in urls
                    ]
                    projection = {"messages": [{"role": "user", "content": parts}]}
                else:
                    parts = [{"type": "input_text", "text": text}] + [
                        {"type": "input_image", "image_url": u, "detail": "auto"}
                        for u in urls
                    ]
                    projection = {"input": [{"role": "user", "content": parts}]}
                wire = json.dumps(
                    projection,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                key = "messages" if job["wire_api"] == "chat" else "input"
                prefix = f"data:{fixture['mime']};base64,"
                blank_images = (
                    [
                        {
                            "type": "image_url",
                            "image_url": {"url": prefix, "detail": "auto"},
                        }
                        for _ in urls
                    ]
                    if job["wire_api"] == "chat"
                    else [
                        {"type": "input_image", "image_url": prefix, "detail": "auto"}
                        for _ in urls
                    ]
                )
                without_payload = {
                    key: [{"role": "user", "content": [parts[0], *blank_images]}]
                }
                wrapper_bytes = len(
                    json.dumps(
                        without_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                payload_bytes = sum(4 * ((len(b) + 2) // 3) for b in bodies)
                assert len(wire) == wrapper_bytes + payload_bytes
                result.update(
                    {
                        "encoded_occurrence_bytes": sum(map(len, bodies)),
                        "unique_file_bytes": len(bodies[0]),
                        "physical_encoded_objects": len({id(b) for b in bodies}),
                        "base64_payload_bytes": payload_bytes,
                        "text_and_wrapper_bytes": wrapper_bytes,
                        "materialized_context_json_bytes": len(wire),
                        "wire_over_64mib": len(wire) > 64 * MIB,
                        "loaded_peak_rss_bytes": peak_rss_bytes(),
                    }
                )
            else:
                raise ValueError(f"Unknown operation: {mode}")
        except Exception as exc:
            result.update(
                status="rejected"
                if isinstance(exc, ImagePolicyRejection)
                else "decoder_error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        result["warnings"] = [
            {"category": w.category.__name__, "message": str(w.message)} for w in caught
        ]
    result.update(
        elapsed_ms=1000 * (perf_counter() - start), peak_rss_bytes=peak_rss_bytes()
    )
    result["peak_increase_bytes"] = max(0, result["peak_rss_bytes"] - initial_peak)
    return result


def extra_fixtures(directory: Path) -> list[dict]:
    directory.mkdir()
    rows = []

    def save(name, image, *, codec="PNG", expected="ok", mime=None, **options):
        suffix = {
            "PNG": "png",
            "JPEG": "jpg",
            "WEBP": "webp",
            "GIF": "gif",
            "TIFF": "tiff",
        }[codec]
        path = directory / f"{name}.{suffix}"
        image.save(path, format=codec, **options)
        row = {
            "id": name,
            "path": str(path),
            "mime": mime or Image.MIME[codec],
            "expected_status": expected,
            "width": image.width,
            "height": image.height,
            "encoded_bytes": path.stat().st_size,
        }
        rows.append(row)
        return row

    for w, h in ((4096, 4096), (8192, 4096), (8192, 8192)):
        with Image.new("RGB", (w, h), "#6388ad") as im:
            save(f"solid_rgb_{w}x{h}", im, compress_level=9)
    for mode in ("L", "RGBA", "I;16"):
        with Image.new(mode, (4096, 4096), 127) as im:
            save(f"mode_{mode.replace(';', '')}_4096", im)
    with Image.new("P", (1024, 1024), 7) as im:
        im.putpalette([n for i in range(256) for n in (i, 255 - i, i)])
        save("palette_transparency", im, transparency=7)
    with Image.new("RGB", (2048, 1024), "#6388ad") as im:
        exif = Image.Exif()
        exif[274] = 6
        save(
            "jpeg_progressive_exif_orientation6",
            im,
            codec="JPEG",
            progressive=True,
            exif=exif,
        )
        with im.convert("CMYK") as cmyk:
            save("jpeg_cmyk", cmyk, codec="JPEG")
        save("webp_lossless", im, codec="WEBP", lossless=True)
    with Image.new("RGBA", (1024, 2048), (100, 130, 170, 100)) as im:
        save("webp_alpha", im, codec="WEBP", lossless=True)
    for n in (2048, 4096):
        with Image.new("RGB", (n, n), "#6388ad") as im:
            save(f"raw_rgb_{n}", im, compress_level=0)
    with (
        Image.new("RGB", (64, 64), "red") as a,
        Image.new("RGB", (64, 64), "blue") as b,
    ):
        for codec in ("PNG", "WEBP", "GIF", "TIFF"):
            save(
                f"two_frames_{codec.lower()}",
                a,
                codec=codec,
                expected="rejected",
                save_all=True,
                append_images=[b],
            )
        save("static_gif", a, codec="GIF", expected="rejected")
        pristine = save("valid_small_png", a)
        jpeg = save("valid_small_jpeg", a, codec="JPEG")
        for name, count, chunk_size, expected in (
            ("text_chunk_1m", 1, MIB, "ok"),
            ("text_chunk_1m_plus1", 1, MIB + 1, "decoder_error"),
            ("text_total_32m", 32, MIB, "ok"),
            ("text_total_64m", 64, MIB, "ok"),
            ("text_total_65m", 65, MIB, "decoder_error"),
        ):
            info = PngImagePlugin.PngInfo()
            for i in range(count):
                info.add_text(f"note{i}", "X" * chunk_size, zip=True)
            save(name, a, pnginfo=info, expected=expected)
    for source, name, body in (
        (pristine, "truncated_png", Path(pristine["path"]).read_bytes()[:75]),
        (jpeg, "truncated_jpeg", Path(jpeg["path"]).read_bytes()[:-30]),
        (pristine, "invalid_signature", b"this is not an image\x00"),
    ):
        path = directory / f"{name}.bin"
        path.write_bytes(body)
        rows.append(
            {
                **source,
                "id": name,
                "path": str(path),
                "encoded_bytes": len(body),
                "expected_status": "decoder_error",
            }
        )
    rows.append(
        {
            **jpeg,
            "id": "mime_mismatch_jpeg_as_png",
            "mime": "image/png",
            "expected_status": "rejected",
        }
    )
    return rows


def run_job(job: dict, timeout: int) -> dict:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "tools.probe_image_resources", "--worker"],
            input=json.dumps(job),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        if completed.returncode:
            return {
                "status": "worker_error",
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        return json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        return {
            "status": "experiment_timeout",
            "deadline_seconds": timeout,
            "worker_killed_and_reaped": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path(
            "scratch/image-resource-fixtures/20260913T051948892348Z/manifest.json"
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(json.load(sys.stdin)), ensure_ascii=False))
        return
    if args.repeat < 1 or args.timeout < 1:
        parser.error("repeat and timeout must be positive experimental controls")
    output = (
        args.output
        or Path("scratch/image-resource-probe")
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    ).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = json.loads(args.fixtures.read_text())
    extras = extra_fixtures(output / "extra-images")
    fixtures = [{**f, "expected_status": "ok"} for f in source["fixtures"]] + extras
    by_id = {f["id"]: f for f in fixtures}
    jobs = []

    def add(operation, fixture, **parameters):
        for repeat in range(1, args.repeat + 1):
            jobs.append(
                {
                    "job_id": f"job-{len(jobs) + 1:04d}",
                    "operation": operation,
                    "fixture": fixture,
                    "repeat": repeat,
                    **parameters,
                }
            )

    for fixture in fixtures:
        add("decode", fixture)
    for name in ("solid_rgb_4096x4096", "solid_rgb_8192x4096", "solid_rgb_8192x8192"):
        add(
            "decode",
            by_id[name],
            max_pixels=4096**2,
            experiment="pixel_candidate_16mp",
        )
    for name in ("text_chunk_1m", "text_total_32m", "text_total_64m", "text_total_65m"):
        add(
            "decode",
            by_id[name],
            png_text_memory_candidate=MIB,
            experiment="png_text_candidate_1mib",
        )
    for name in ("solid_rgb_4096x4096", "raw_rgb_2048", "text_total_32m"):
        for concurrency in (1, 2, 4, 8):
            add(
                "decode_batch",
                by_id[name],
                copies=8,
                concurrency=concurrency,
                retention="release_each",
            )
        add(
            "decode_batch", by_id[name], copies=8, concurrency=1, retention="retain_all"
        )
    raw = next(
        f
        for f in fixtures
        if f["id"].startswith("scene_") and f["id"].endswith("_png0")
    )
    for name in (raw["id"], "raw_rgb_2048", "raw_rgb_4096"):
        for copies in (
            (1, 2, 4, 8, 16)
            if name == raw["id"]
            else (1, 2, 4)
            if name == "raw_rgb_2048"
            else (1,)
        ):
            for api in ("chat", "responses"):
                for sharing in ("shared", "independent"):
                    add(
                        "wire",
                        by_id[name],
                        copies=copies,
                        sharing=sharing,
                        wire_api=api,
                    )
    (output / "jobs.json").write_text(
        json.dumps(jobs, ensure_ascii=False, indent=2) + "\n"
    )
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_manifest": str(args.fixtures.resolve()),
        "python_version": platform.python_version(),
        "pillow_version": pillow_version,
        "platform": platform.platform(),
        "repeat": args.repeat,
        "worker_deadline_seconds": args.timeout,
        "planned_jobs": len(jobs),
        "source_fixtures": len(source["fixtures"]),
        "extra_cases": extras,
        "method": "Fresh subprocess per job, preinitialized codecs; RUSAGE_SELF ru_maxrss high-water mark, bytes on macOS, KiB converted on Linux; increase is post-peak minus baseline-peak, not exact allocations. Batch holds concurrent decoded images at a barrier; wire measures a synthetic context projection, no actual adapter or HTTP request.",
        "pillow_default_pixel_warning": Image.MAX_IMAGE_PIXELS,
        "pillow_default_text_chunk_bytes": PngImagePlugin.MAX_TEXT_CHUNK,
        "pillow_default_text_total_bytes": PngImagePlugin.MAX_TEXT_MEMORY,
        "pillow_load_truncated_images": ImageFile.LOAD_TRUNCATED_IMAGES,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"Output: {output}\nPlanned isolated jobs: {len(jobs)}", flush=True)
    rows = []
    with (output / "results.jsonl").open("w") as sink:
        for i, job in enumerate(jobs, 1):
            result = {**job, **run_job(job, args.timeout)}
            rows.append(result)
            sink.write(json.dumps(result, ensure_ascii=False) + "\n")
            sink.flush()
            if (
                i % 20 == 0
                or i == len(jobs)
                or result["status"] not in ("ok", "rejected", "decoder_error")
            ):
                print(
                    f"{i}/{len(jobs)} {job['operation']} {job['fixture']['id']}: {result['status']}",
                    flush=True,
                )
    flat = [
        {
            **{k: v for k, v in r.items() if k not in ("fixture", "warnings")},
            "fixture_id": r["fixture"]["id"],
            "fixture_path": r["fixture"]["path"],
            "warnings": json.dumps(r.get("warnings", []), ensure_ascii=False),
        }
        for r in rows
    ]
    with (output / "results.csv").open("w", newline="") as sink:
        writer = csv.DictWriter(sink, fieldnames=sorted({k for r in flat for k in r}))
        writer.writeheader()
        writer.writerows(flat)
    print(f"Completed: {output / 'results.csv'}", flush=True)


if __name__ == "__main__":
    main()
