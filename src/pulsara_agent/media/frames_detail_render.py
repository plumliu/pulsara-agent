"""Page-local exact sharing of detail images without dropping observations.

The selector owns source observations. This renderer only shares their final
unlabelled RGB rasters when every pixel is equal; it establishes no unchanged
interval and keeps no authority or cache beyond the current candidate page.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import io
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageDraw

from .frames_decode import VideoDecoder
from .source import MediaError, MediaInfo, MediaSource, exact_seconds

if TYPE_CHECKING:
    from .frames import FrameBudget, FrameImage, FrameObservation, FramePlan


def render_detail_images(
    source: MediaSource,
    info: MediaInfo,
    plan: FramePlan,
    selected: Sequence[FrameObservation],
    *,
    budget: FrameBudget,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[tuple[FrameImage, ...], list[dict]]:
    """Render an admitted prefix, counting actual unique images.

    ``n`` numbers observations, while ``image`` and the burned number identify
    this page's unique picture. Repeated pictures retain independent source
    times and visible intervals. Physical limits apply to unique pictures;
    their complete time index is admitted by the caller's joint page quote.
    Once an additional unique picture exceeds a physical limit, the existing
    nonempty prefix is returned. An inadmissible first picture is a boundary.
    """
    # frames owns crop/canvas geometry and the delivered image type. The local
    # import keeps that existing owner without a module-initialization cycle.
    from .frames import FrameImage, _canvas, _check, _crop, scene_dimensions

    _check(cancelled)
    source.assert_unchanged()
    width, height = _canvas(plan.display_aspect)
    minimum = scene_dimensions(plan.display_aspect)
    if width < minimum[0] or height < minimum[1]:
        raise MediaError(
            "RESOURCE_BOUNDARY", "Clear detail images do not fit the physical page allocation"
        )
    pixels = width * height
    if pixels > budget.image_pixels:
        raise MediaError(
            "RESOURCE_BOUNDARY", "A clear detail image exceeds the physical pixel allocation"
        )
    unique: list[Image.Image] = []
    index: list[dict] = []
    requested_start, requested_end = map(exact_seconds, plan.requested_range)
    try:
        with VideoDecoder(source, info, cancelled=cancelled) as decoder:
            visible_frames = decoder.visible_many(
                observation.position for observation in selected
            )
            for observation in selected:
                visible = next(visible_frames)
                _check(cancelled)
                if visible.position != observation.position or visible.t != observation.t:
                    raise MediaError(
                        "MEDIA_SOURCE_CHANGED", "Selected presentation frame changed during rendering"
                    )
                if observation.end is None:
                    raise MediaError(
                        "MEDIA_FRAME_UNAVAILABLE", "A detail observation has no verifiable display end"
                    )
                original = _crop(visible.image, plan.crop)
                rendered = original.resize((width, height), Image.Resampling.LANCZOS)
                del original, visible
                image_number = None
                try:
                    for number, previous in enumerate(unique, 1):
                        _check(cancelled)
                        # Pixel equality deliberately ignores Pillow metadata.
                        # RGB difference needs only one temporary target raster.
                        if previous.size == rendered.size and ImageChops.difference(
                            previous, rendered
                        ).getbbox() is None:
                            image_number = number
                            break
                    if image_number is None:
                        count = len(unique) + 1
                        if count > budget.maximum_images or count * pixels > budget.image_pixels:
                            if index:
                                break
                            raise MediaError(
                                "RESOURCE_BOUNDARY", "A clear detail image exceeds the physical page allocation"
                            )
                        unique.append(rendered)
                        rendered = None
                        image_number = count
                finally:
                    if rendered is not None:
                        rendered.close()
                item = {
                    "n": len(index) + 1,
                    "image": image_number,
                    "t": float(observation.t),
                    "cell_rect": [0.0, 0.0, 1.0, 1.0],
                    "visible_range": [
                        float(max(observation.t, requested_start)),
                        float(min(observation.end, requested_end)),
                    ],
                }
                if index:
                    item["dt"] = float(observation.t - selected[len(index) - 1].t)
                index.append(item)
            if len(index) < len(selected):
                visible_frames.close()
            elif next(visible_frames, None) is not None:
                raise RuntimeError("Decoder returned more render observations than requested")

        images = []
        encoded_bytes = 0
        # Labels are added only after every equality comparison, so a picture's
        # observation number cannot prevent sharing or change its identity.
        for number, page in enumerate(unique, 1):
            _check(cancelled)
            draw = ImageDraw.Draw(page)
            box = draw.textbbox((4, 3), str(number))
            draw.rectangle((0, 0, box[2] + 5, box[3] + 4), fill="black")
            draw.text((4, 3), str(number), fill="white")
            stream = io.BytesIO()
            page.save(stream, format="JPEG", quality=90)
            data = stream.getvalue()
            if encoded_bytes + len(data) > budget.maximum_bytes:
                if not images:
                    raise MediaError(
                        "RESOURCE_BOUNDARY", "One encoded detail image exceeds the physical byte allocation"
                    )
                # Unique image numbers follow their first source occurrence.
                # Cutting before the first reference to this image therefore
                # retains a complete observation and encoded-image prefix.
                stop = next(i for i, item in enumerate(index) if item["image"] == number)
                del index[stop:]
                break
            encoded_bytes += len(data)
            images.append(FrameImage(data, "image/jpeg", width, height))
            page.close()
        source.assert_unchanged()
        return tuple(images), index
    finally:
        for page in unique:
            page.close()
