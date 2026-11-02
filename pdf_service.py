"""Source-independent PDF generation. Heavy dependencies are loaded on demand."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


class PdfGenerationError(RuntimeError):
    pass


class PdfService:
    async def build(self, images: tuple[Path, ...], target: Path, password: str = "") -> None:
        # A cancelled coroutine cannot stop a worker thread. Await its completion
        # before the caller can remove the working directory or release its slot.
        task = asyncio.create_task(asyncio.to_thread(self._build, images, target, password))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.gather(task, return_exceptions=True)
            raise

    @staticmethod
    def _build(images: tuple[Path, ...], target: Path, password: str) -> None:
        try:
            import img2pdf
            import pikepdf
            from PIL import Image, ImageOps

            if not images:
                raise ValueError("empty image manifest")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="pdf-", dir=target.parent) as work:
                paths = []
                for index, source in enumerate(images):
                    # Verify every page; a broken page must not silently disappear.
                    with Image.open(source) as image:
                        image.load()
                        if image.format == "JPEG" and image.mode == "RGB" and not image.getexif().get(274):
                            paths.append(str(source))
                            continue
                        page = ImageOps.exif_transpose(image).convert("RGBA")
                        rgb = Image.new("RGB", page.size, "white")
                        rgb.paste(page, mask=page.getchannel("A"))
                        converted = Path(work) / f"{index:06d}.jpg"
                        rgb.save(converted, quality=95)
                        paths.append(str(converted))
                plain = Path(work) / "plain.pdf"
                with plain.open("wb") as stream:
                    img2pdf.convert(paths, outputstream=stream)
                ready = Path(work) / "ready.pdf"
                with pikepdf.open(plain) as pdf:
                    if len(pdf.pages) != len(images):
                        raise ValueError("page count mismatch")
                    if password:
                        pdf.save(ready, encryption=pikepdf.Encryption(owner=password, user=password, R=6))
                    else:
                        pdf.save(ready)
                with pikepdf.open(ready, password=password) as pdf:
                    if len(pdf.pages) != len(images) or pdf.is_encrypted != bool(password):
                        raise ValueError("PDF validation failed")
                ready.replace(target)
        except Exception as exc:
            raise PdfGenerationError("PDF 生成或验证失败") from exc
