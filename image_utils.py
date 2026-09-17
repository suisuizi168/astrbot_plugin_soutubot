from __future__ import annotations

import asyncio
import io
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
SUPPORTED_PASSTHROUGH_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "WEBP": (".webp", "image/webp"),
}


class ImagePreparationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedImage:
    data: bytes
    filename: str
    content_type: str
    original_size: int
    changed: bool


def _safe_stem(filename: str) -> str:
    stem = Path(filename or "image").stem.strip() or "image"
    safe = "".join(char for char in stem if char.isalnum() or char in "-_ ").strip()
    return (safe or "image")[:80]


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if "A" in image.getbands():
        background = Image.new("RGB", image.size, "white")
        alpha = image.getchannel("A")
        background.paste(image.convert("RGB"), mask=alpha)
        return background
    return image.convert("RGB")


def prepare_image(
    image_bytes: bytes,
    filename: str = "image",
    *,
    max_upload_bytes: int = 5 * 1024 * 1024,
    max_image_width: int = 2000,
) -> PreparedImage:
    """Match the website's client-side limits without creating temporary files."""

    if not image_bytes:
        raise ImagePreparationError("图片内容为空")
    if len(image_bytes) > MAX_INPUT_BYTES:
        raise ImagePreparationError("原始图片超过 25 MiB，拒绝处理")
    if max_upload_bytes <= 0 or max_image_width <= 0:
        raise ImagePreparationError("图片限制配置无效")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as opened:
                width, height = opened.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ImagePreparationError("图片尺寸无效或像素数量过大")
                opened.load()
                image = ImageOps.exif_transpose(opened)
                source_format = (opened.format or "").upper()

                passthrough = SUPPORTED_PASSTHROUGH_FORMATS.get(source_format)
                if (
                    passthrough
                    and width <= max_image_width
                    and len(image_bytes) <= max_upload_bytes
                ):
                    extension, content_type = passthrough
                    return PreparedImage(
                        data=image_bytes,
                        filename=f"{_safe_stem(filename)}{extension}",
                        content_type=content_type,
                        original_size=len(image_bytes),
                        changed=False,
                    )

                rgb = _flatten_to_rgb(image)
                scale = min(1.0, max_image_width / width)
                quality = 90

                for _ in range(14):
                    target_width = max(1, round(width * scale))
                    target_height = max(1, round(height * scale))
                    candidate = rgb
                    if candidate.size != (target_width, target_height):
                        candidate = rgb.resize(
                            (target_width, target_height), Image.Resampling.LANCZOS
                        )
                    output = io.BytesIO()
                    candidate.save(
                        output,
                        format="JPEG",
                        quality=quality,
                        optimize=True,
                        progressive=True,
                    )
                    encoded = output.getvalue()
                    if len(encoded) <= max_upload_bytes:
                        return PreparedImage(
                            data=encoded,
                            filename=f"{_safe_stem(filename)}.jpg",
                            content_type="image/jpeg",
                            original_size=len(image_bytes),
                            changed=True,
                        )
                    if quality > 66:
                        quality -= 8
                    else:
                        scale *= 0.82
    except ImagePreparationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise ImagePreparationError("无法解码该图片") from exc

    raise ImagePreparationError("图片压缩后仍超过 5 MiB")


async def prepare_image_async(
    image_bytes: bytes,
    filename: str = "image",
    *,
    max_upload_bytes: int = 5 * 1024 * 1024,
    max_image_width: int = 2000,
) -> PreparedImage:
    return await asyncio.to_thread(
        prepare_image,
        image_bytes,
        filename,
        max_upload_bytes=max_upload_bytes,
        max_image_width=max_image_width,
    )
