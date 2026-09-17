from __future__ import annotations

import io
import unittest

from PIL import Image

from image_utils import ImagePreparationError, prepare_image


def make_image(width: int, height: int, fmt: str = "PNG") -> bytes:
    image = Image.new("RGB", (width, height), (120, 40, 200))
    output = io.BytesIO()
    image.save(output, format=fmt)
    return output.getvalue()


class ImagePreparationTests(unittest.TestCase):
    def test_small_png_is_not_changed(self) -> None:
        raw = make_image(128, 64)
        prepared = prepare_image(raw, "sample.png")
        self.assertFalse(prepared.changed)
        self.assertEqual(prepared.content_type, "image/png")
        self.assertEqual(prepared.data, raw)

    def test_wide_image_is_scaled_and_encoded_as_jpeg(self) -> None:
        raw = make_image(2500, 1000)
        prepared = prepare_image(raw, "wide.png")
        self.assertTrue(prepared.changed)
        self.assertEqual(prepared.content_type, "image/jpeg")
        with Image.open(io.BytesIO(prepared.data)) as result:
            self.assertLessEqual(result.width, 2000)
            self.assertLessEqual(len(prepared.data), 5 * 1024 * 1024)

    def test_invalid_image_is_rejected(self) -> None:
        with self.assertRaises(ImagePreparationError):
            prepare_image(b"not an image", "bad.bin")


if __name__ == "__main__":
    unittest.main()
