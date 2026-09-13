"""Generate offline D2 geometry fixtures, reusing the D1 renderer and Pillow.

The finite dimensions and sample counts are experiment choices, not product
admission limits. No settings, database, provider request or network is used.
Run from the repository root with python -m tools.generate_image_resource_fixtures.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from fractions import Fraction
import json
from math import isqrt, sqrt
from pathlib import Path
import platform

from PIL import Image, ImageDraw, ImageFont, __version__ as pillow_version

from tools.probe_vision_tokens import Case, render_image


# Exact requested proportions, with one practical size for each. 1.91 is 191/100.
COMMON = (
    ("16:10", 1920, 1200),
    ("16:9", 1920, 1080),
    ("3:2", 1800, 1200),
    ("4:3", 1600, 1200),
    ("5:4", 1500, 1200),
    ("21:9", 2520, 1080),
    ("1.91:1", 1910, 1000),
)

# Physical sizes are experiment inputs. A4's integer millimetres and raster
# pixels approximate the ideal sqrt(2) ratio; they cannot represent it exactly.
PAPER_PHOTO = (
    ("A4", Fraction(297), Fraction(210), "mm", "sqrt(2)"),
    ("6-inch photo (6 x 4 inch)", Fraction(6), Fraction(4), "inch", "3:2"),
    ("6-inch photo (user 15 x 10 cm)", Fraction(15), Fraction(10), "cm", "3:2"),
    ("5-inch photo (5 x 3.5 inch)", Fraction(5), Fraction(7, 2), "inch", "10:7"),
    ("7-inch photo (7 x 5 inch)", Fraction(7), Fraction(5), "inch", "7:5"),
)


def near_area(area: int, ratio: Fraction) -> tuple[int, int]:
    """Nearest integer edges; actual ratio/area errors remain in the manifest."""
    floor_width = isqrt(area * ratio.numerator // ratio.denominator)
    width = min(
        (max(1, floor_width), max(1, floor_width + 1)),
        key=lambda w: abs(w * w * ratio.denominator - area * ratio.numerator),
    )
    height = max(1, (area + width // 2) // width)
    return width, height


def geometry_matrix() -> dict[tuple[int, int], list[dict[str, object]]]:
    geometries: dict[tuple[int, int], list[dict[str, object]]] = {}

    def add_pair(width: int, height: int, group: str, **metadata: object) -> None:
        for w, h in dict.fromkeys(((width, height), (height, width))):
            geometries.setdefault((w, h), []).append(
                {"group": group, "transposed": (w, h) != (width, height), **metadata}
            )

    for label, width, height in COMMON:
        add_pair(
            width, height, "exact_requested_ratio", requested_landscape_ratio=label
        )
    area_ratios = [(label, Fraction(w, h)) for label, w, h in COMMON] + [
        ("1:1", Fraction(1)),
        ("A4 sqrt(2)", Fraction(sqrt(2))),
        ("5-inch photo 10:7", Fraction(10, 7)),
        ("7-inch photo 7:5", Fraction(7, 5)),
    ]
    for label, ratio in area_ratios:
        for area in (256**2, 1024**2, 2048**2):
            w, h = near_area(area, ratio)
            add_pair(
                w,
                h,
                "area_matched",
                requested_landscape_ratio=label,
                target_pixels=area,
                area_error_pct=100 * (w * h / area - 1),
                landscape_ratio_error_pct=float(100 * (Fraction(w, h) / ratio - 1)),
            )
    for label, width, height, unit, ideal_ratio in PAPER_PHOTO:
        units_per_inch = {
            "inch": Fraction(1),
            "cm": Fraction(254, 100),
            "mm": Fraction(254, 10),
        }[unit]
        for dpi in (150, 300):
            w, h = (
                round(width * dpi / units_per_inch),
                round(height * dpi / units_per_inch),
            )
            add_pair(
                w,
                h,
                "paper_photo",
                label=label,
                physical_landscape_width=float(width),
                physical_landscape_height=float(height),
                physical_unit=unit,
                requested_dpi=dpi,
                ideal_landscape_ratio=ideal_ratio,
                pixel_rounding="nearest integer",
            )
    for numerator, denominator in (
        (2, 1),
        (3, 1),
        (5, 1),
        (10, 1),
        (32, 9),
        (9, 8),
        (7, 5),
        (13, 7),
        (17, 13),
        (37, 11),
        (100, 1),
    ):
        ratio = Fraction(numerator, denominator)
        w, h = near_area(1024**2, ratio)
        add_pair(
            w,
            h,
            "uncommon_ratio",
            requested_landscape_ratio=f"{numerator}:{denominator}",
            target_pixels=1024**2,
            area_error_pct=100 * (w * h / 1024**2 - 1),
            landscape_ratio_error_pct=float(100 * (Fraction(w, h) / ratio - 1)),
        )
    for w, h in (
        (4096, 1),
        (8191, 2),
        (4093, 7),
        (65521, 17),
        (32749, 31),
        (4096, 57),
        (4093, 257),
        (65535, 1),
        (10001, 97),
        (16384, 64),
    ):
        add_pair(w, h, "extreme_skinny")
    for w, h in (
        (997, 1009),
        (1023, 1025),
        (1365, 767),
        (1919, 1079),
        (3440, 1440),
        (3840, 1600),
        (4096, 4096),
        (4095, 4097),
    ):
        add_pair(w, h, "irregular_or_large")
    # Geometry controls around 28- and 32-pixel grid edges; not D2 limit values.
    for widths, heights in (
        ((1007, 1008, 1009), (559, 560, 561)),
        ((2047, 2048, 2049), (1023, 1024, 1025)),
    ):
        for w in widths:
            for h in heights:
                add_pair(w, h, "grid_edge_neighborhood")
    return geometries


def save_fixture(
    case: Case, groups: list[dict[str, object]], directory: Path
) -> dict[str, object]:
    suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[case.codec]
    path = directory / f"{case.name}{suffix}"
    options = (
        {"compress_level": case.compression}
        if case.codec == "PNG"
        else {"quality": case.quality}
    )
    dpi_values = {g["requested_dpi"] for g in groups if g["group"] == "paper_photo"}
    assert len(dpi_values) <= 1
    requested_dpi = next(iter(dpi_values), None)
    if requested_dpi is not None:
        options["dpi"] = (requested_dpi, requested_dpi)
    with render_image(case) as image:
        image.save(path, format=case.codec, **options)
    # Inspect and decode the actual file, not just its requested metadata.
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        assert image.format == case.codec
        assert image.size == (case.width, case.height)
        # Pillow's JPEG plugin has no n_frames attribute; this generated JPEG
        # format is single-frame. PNG and WebP expose an explicit frame count.
        frames = 1 if image.format == "JPEG" else image.n_frames
        assert frames == 1
        image.load()
        assert image.mode == "RGB"
        measured = {
            "width": image.width,
            "height": image.height,
            "format": image.format,
            "mime": Image.MIME[image.format],
            "mode": image.mode,
            "frames": frames,
            "dpi_read": image.info.get("dpi"),
        }
        if requested_dpi is not None:
            assert measured["dpi_read"] is not None
            assert all(
                abs(actual - requested_dpi) < 0.02 for actual in measured["dpi_read"]
            )
    encoded = path.stat().st_size
    return {
        "id": case.name,
        "path": str(path),
        **measured,
        "actual_ratio": str(Fraction(case.width, case.height)),
        "actual_ratio_decimal": case.width / case.height,
        "pixels": case.width * case.height,
        "encoded_bytes": encoded,
        "base64_payload_bytes": 4 * ((encoded + 2) // 3),
        "rgb_pixel_plane_bytes": 3 * case.width * case.height,
        "pixel_plane_note": "RGB pixel plane only; not measured decoder/process peak memory",
        "style": case.style,
        "encoder_options": options,
        "groups": groups,
    }


def contact_sheet(
    rows: list[dict[str, object]],
    path: Path,
    *,
    columns: int,
    photo_labels: bool = False,
) -> None:
    cell_w, cell_h = 240, 230
    sheet = Image.new(
        "RGB",
        (columns * cell_w, ((len(rows) + columns - 1) // columns) * cell_h),
        "#e8edf4",
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=17)
    for i, row in enumerate(rows):
        x, y = (i % columns) * cell_w, (i // columns) * cell_h
        draw.rounded_rectangle(
            (x + 5, y + 5, x + cell_w - 5, y + cell_h - 5), radius=8, fill="white"
        )
        with Image.open(str(row["path"])) as original:
            original.thumbnail((cell_w - 24, cell_h - 70))
            sheet.paste(
                original,
                (
                    x + (cell_w - original.width) // 2,
                    y + 14 + (cell_h - 70 - original.height) // 2,
                ),
            )
        draw.text(
            (x + 12, y + cell_h - 53),
            f"{row['width']} x {row['height']}",
            fill="#10243a",
            font=font,
        )
        photo = next((g for g in row["groups"] if g["group"] == "paper_photo"), None)
        if photo and photo_labels:
            label = photo["label"].split(" photo")[0].replace("-inch", "in")
            if label == "6in":
                label += " (cm)" if photo["physical_unit"] == "cm" else " (inch)"
            caption = f"{label}, {photo['requested_dpi']} DPI"
        else:
            caption = f"ratio {row['actual_ratio']}"
        draw.text((x + 12, y + cell_h - 29), caption, fill="#526070", font=font)
    sheet.save(path)
    sheet.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="New output directory; existing directories are never overwritten.",
    )
    args = parser.parse_args()
    output = (
        args.output
        or Path("scratch/image-resource-fixtures")
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    ).resolve()
    output.mkdir(parents=True, exist_ok=False)
    images = output / "images"
    images.mkdir()
    geometries = geometry_matrix()
    json.dumps(list(geometries.values()), allow_nan=False)
    print(
        f"Output: {output}\nBase geometries: {len(geometries)}; codec/content controls: 28",
        flush=True,
    )
    rows = []
    for index, ((w, h), groups) in enumerate(geometries.items(), 1):
        rows.append(save_fixture(Case(f"scene_{w}x{h}_png9", w, h), groups, images))
        if index % 20 == 0:
            print(f"Verified {index}/{len(geometries)} geometries", flush=True)
    by_size = {(r["width"], r["height"]): r for r in rows}
    for label, w, h in COMMON:
        w, h = near_area(1024**2, Fraction(w, h))
        baseline = by_size[w, h]
        groups = [
            {
                "group": "codec_content_control",
                "requested_landscape_ratio": label,
                "reference_id": baseline["id"],
            }
        ]
        for case in (
            Case(f"scene_{w}x{h}_png0", w, h, compression=0),
            Case(f"scene_{w}x{h}_jpeg85", w, h, codec="JPEG", quality=85),
            Case(f"scene_{w}x{h}_webp80", w, h, codec="WEBP", quality=80),
            Case(f"noise_{w}x{h}_png9", w, h, style="noise"),
        ):
            row = save_fixture(case, groups, images)
            if case.style == "scene" and case.codec == "PNG":
                with (
                    Image.open(str(baseline["path"])) as compressed,
                    Image.open(str(row["path"])) as raw,
                ):
                    assert compressed.tobytes() == raw.tobytes()
                row["lossless_pixels_equal_to_reference"] = True
            else:
                row["lossless_pixels_equal_to_reference"] = None
            rows.append(row)
    assert len(rows) == len(geometries) + 28
    assert len({r["id"] for r in rows}) == len(rows)
    group_counts = Counter(g["group"] for r in rows for g in r["groups"])
    validation = {
        "all_files_reopened_verified_and_fully_decoded": len(rows),
        "all_dimensions_formats_modes_frames_match": True,
        "lossless_compression_pairs_equal": sum(
            r.get("lossless_pixels_equal_to_reference") is True for r in rows
        ),
        "every_base_geometry_has_transpose": all(
            (h, w) in geometries for w, h in geometries
        ),
        "requested_exact_ratio_pairs": len(COMMON),
        "paper_photo_orientation_dpi_memberships": group_counts["paper_photo"],
        "max_area_matched_error_pct": max(
            abs(g["area_error_pct"])
            for r in rows
            for g in r["groups"]
            if g["group"] == "area_matched"
        ),
        "max_area_matched_ratio_error_pct": max(
            abs(g["landscape_ratio_error_pct"])
            for r in rows
            for g in r["groups"]
            if g["group"] == "area_matched"
        ),
    }
    assert validation["every_base_geometry_has_transpose"]
    for label, w, h in COMMON:
        assert (w, h) in geometries and (h, w) in geometries
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "D2 local aspect-ratio and resource experiment fixtures; not admission limits or a completed resource experiment",
        "python_version": platform.python_version(),
        "pillow_version": pillow_version,
        "source_renderer": "tools.probe_vision_tokens.render_image; stretched synthetic dashboard, deterministic noise seed 1701; not an OCR benchmark",
        "requested_ratios": [r[0] for r in COMMON],
        "paper_photo_specs": [
            {
                "label": label,
                "width": float(w),
                "height": float(h),
                "unit": unit,
                "ideal_landscape_ratio": ratio,
                "dpi_levels": [150, 300],
            }
            for label, w, h, unit, ratio in PAPER_PHOTO
        ],
        "base_geometries": len(geometries),
        "files": len(rows),
        "group_membership_counts": dict(group_counts),
        "total_encoded_bytes": sum(r["encoded_bytes"] for r in rows),
        "validation": validation,
        "fixtures": rows,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    flat = [
        {
            **{
                k: v
                for k, v in r.items()
                if k
                not in (
                    "groups",
                    "encoder_options",
                    "lossless_pixels_equal_to_reference",
                )
            },
            "encoder_options": json.dumps(r["encoder_options"]),
            "groups": json.dumps(r["groups"], ensure_ascii=False),
            "lossless_pixels_equal_to_reference": r.get(
                "lossless_pixels_equal_to_reference"
            ),
        }
        for r in rows
    ]
    with (output / "fixtures.csv").open("w", newline="") as sink:
        writer = csv.DictWriter(sink, fieldnames=list(flat[0]))
        writer.writeheader()
        writer.writerows(flat)
    previews = {
        "common-ratios.png": [
            r
            for r in rows
            if any(g["group"] == "exact_requested_ratio" for g in r["groups"])
        ],
        "paper-and-photos.png": [
            r for r in rows if any(g["group"] == "paper_photo" for g in r["groups"])
        ],
        "uncommon-ratios.png": [
            r for r in rows if any(g["group"] == "uncommon_ratio" for g in r["groups"])
        ],
        "extreme-and-irregular.png": [
            r
            for r in rows
            if any(
                g["group"] in ("extreme_skinny", "irregular_or_large")
                for g in r["groups"]
            )
        ],
    }
    for name, selected in previews.items():
        contact_sheet(
            selected,
            output / name,
            columns=4 if name == "common-ratios.png" else 5,
            photo_labels=name == "paper-and-photos.png",
        )
    lines = [
        "# D2 多比例图片素材",
        "",
        f"已生成并逐文件验证 {len(rows)} 张图片，覆盖 {len(geometries)} 个不同宽高组合。原始图片合计 {manifest['total_encoded_bytes'] / 1024**2:.2f} MiB。",
        "",
        "本轮只准备几何、编码与内容控制素材，没有调用 provider，也没有测量并发/RSS 峰值或证明 kernel 接纳与压缩。全部为单帧 RGB；透明、EXIF、动画和损坏文件仍是后续独立素材类别。这里的最大尺寸和文件数量不是产品限制。",
        "",
        "## 覆盖范围",
        "",
        "| 精确比例 | 横版 | 竖版 |",
        "| --- | --- | --- |",
        *[f"| {label} | {w}×{h} | {h}×{w} |" for label, w, h in COMMON],
        "",
        "- 面积对照：上述 7 种比例加 1:1、A4 的 √2、5 寸照片 10:7、7 寸照片 7:5，分别接近 65,536、1,048,576、4,194,304 像素；横竖配对。整数宽高会引入小量比例/面积误差，清单分别保留目标和实测值。",
        f"- 面积对照最大面积误差 {validation['max_area_matched_error_pct']:.3f}%，最大横版比例误差 {validation['max_area_matched_ratio_error_pct']:.3f}%。",
        "- 非常见比例：2:1、3:1、5:1、10:1、32:9、9:8、7:5、13:7、17:13、37:11、100:1，全部包括转置；这一组保持接近 1,048,576 像素，比例同样有整数取整误差。",
        "- 极端窄边：4096×1、8191×2、4093×7、65521×17、32749×31、4096×57 等及其转置，用于暴露行数/列数不能只靠总像素解释的处理成本。",
        "- 不整齐与较大尺寸：997×1009、1023×1025、1365×767、1919×1079、3440×1440、3840×1600、4096×4096、4095×4097 及其转置。",
        "- 网格邻域：1008×560 和 2048×1024 的两个边分别取 −1/0/+1 的组合，全部包括转置。用于几何取整检查，不能称为尚未冻结的 D2 soft/hard 边界测试。",
        "- 编码/内容控制：7 种常见比例各选约 1 MP 横图，额外生成无压缩 PNG、JPEG 85、WebP 80 和噪声 PNG。7 对 PNG 压缩控制的解码像素完全相同；JPEG/WebP 是有损编码，噪声改变了内容。",
        "",
        "## A4 与照片尺寸",
        "",
        "全部生成 150、300 DPI，两个方向。A4 的宽:高在竖版为约 1:√2，横版为约 √2:1；标准 210×297 mm 与整数像素都只是理想 √2 的近似。",
        "",
        "| 规格（横版物理尺寸） | 150 DPI 像素 | 300 DPI 像素 |",
        "| --- | --- | --- |",
        *[
            f"| {label}，{float(w):g}×{float(h):g} {unit} | "
            + " | ".join(
                f"{round(w * dpi / {'inch': Fraction(1), 'cm': Fraction(254, 100), 'mm': Fraction(254, 10)}[unit])}×{round(h * dpi / {'inch': Fraction(1), 'cm': Fraction(254, 100), 'mm': Fraction(254, 10)}[unit])}"
                for dpi in (150, 300)
            )
            + " |"
            for label, w, h, unit, _ in PAPER_PHOTO
        ],
        "",
        "6 寸照片分别覆盖常见的 6×4 英寸和用户指定的 15×10 cm，不把两者的实际物理尺寸当成完全相同。5 寸照片按 5×3.5 英寸（10:7），7 寸按 7×5 英寸（7:5）生成。PNG 写入对应 DPI 并重新读取检查；PNG 的单位换算会使读回值与整数 DPI 有微小差别，实际值保存在清单。",
        "",
        "DPI 用于将纸张/照片的物理尺寸换算成像素，不会直接进入 D1 公式或取代像素、字节、解码内存的 D2 计量。",
        "",
        "## 如何使用",
        "",
        "原始尺寸从每个文件重新读取并完整解码校验。manifest 和 CSV 保留实际格式、帧数、RGB 模式、尺寸、精确分数比例、像素数、编码字节、base64 payload 字节、实验分组与目标误差。RGB 像素面字节只是 3×W×H，不是解码器或进程的实际内存峰值；base64 payload 字节也不包含完整 wire 包装。",
        "",
        "仪表盘沿用 D1 合成画面并按尺寸拉伸，便于复现；本素材集用于资源检查，不以长条图里文字能否读出来评判模型视觉能力。总像素相近并不能消除所有内容差异，需要结合编码/内容控制解释结果。",
        "",
        f"- [完整清单]({output / 'manifest.json'})",
        f"- [CSV]({output / 'fixtures.csv'})",
        *[f"- [{name}]({output / name})" for name in previews],
        "",
        "## 预览",
        "",
        f"![常见比例及其竖版]({output / 'common-ratios.png'})",
        "",
        f"![A4 与照片]({output / 'paper-and-photos.png'})",
        "",
        f"![非常见比例]({output / 'uncommon-ratios.png'})",
        "",
        f"![极端与不规则尺寸]({output / 'extreme-and-irregular.png'})",
        "",
    ]
    (output / "README.zh.md").write_text("\n".join(lines))
    print(
        json.dumps(
            {
                "files": len(rows),
                "base_geometries": len(geometries),
                "encoded_mib": manifest["total_encoded_bytes"] / 1024**2,
                "validation": validation,
                "report": str(output / "README.zh.md"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
