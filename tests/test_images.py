from io import BytesIO
import random
import unittest

from PIL import Image, ImageChops, ImageDraw

from images import ProductImage, read_product_image


MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def make_image(fmt="PNG", size=(512, 512)):
    buffer = BytesIO()
    Image.new("RGB", size, "#608372").save(buffer, format=fmt)
    return buffer.getvalue()


class ProductImageTests(unittest.TestCase):
    def assert_valid_api_image(self, product):
        self.assertLessEqual(len(product.data), MAX_UPLOAD_BYTES)
        self.assertTrue(512 <= product.width <= 4096)
        self.assertTrue(512 <= product.height <= 4096)
        with Image.open(BytesIO(product.data)) as decoded:
            decoded.load()
            self.assertEqual(decoded.size, (product.width, product.height))
            self.assertEqual(product.mime, {"PNG": "image/png", "JPEG": "image/jpeg"}[decoded.format])

    def assert_original_preserved(self, product, raw, size):
        self.assertEqual(product.original_data, raw)
        self.assertEqual(product.preview_data, raw)
        self.assertEqual((product.original_width, product.original_height), size)

    def test_jpeg_and_png_keep_uploaded_bytes_and_actual_mime(self):
        for fmt, mime in [("JPEG", "image/jpeg"), ("PNG", "image/png")]:
            with self.subTest(fmt=fmt):
                raw = make_image(fmt)
                product = read_product_image(raw)
                self.assertEqual(product.data, raw)
                self.assertEqual(product.mime, mime)
                self.assertEqual((product.width, product.height), (512, 512))
                self.assertEqual(product.adjustment, "")
                self.assert_original_preserved(product, raw, (512, 512))
                self.assert_valid_api_image(product)

    def test_existing_product_image_constructor_still_supports_preview(self):
        raw = make_image()
        product = ProductImage(raw, "image/png", 512, 512)
        self.assertEqual(product.preview_data, raw)

    def test_small_images_are_scaled_up_without_changing_aspect_ratio(self):
        for size, expected in [((200, 300), (512, 768)), ((300, 200), (768, 512)), ((1, 1), (512, 512))]:
            for fmt in ("PNG", "JPEG"):
                with self.subTest(size=size, fmt=fmt):
                    raw = make_image(fmt, size)
                    product = read_product_image(raw)
                    self.assertEqual((product.width, product.height), expected)
                    self.assertTrue(product.adjustment)
                    self.assertNotEqual(product.data, raw)
                    self.assert_original_preserved(product, raw, size)
                    self.assert_valid_api_image(product)

    def test_large_images_are_scaled_down_without_changing_aspect_ratio(self):
        for size, expected in [((6000, 3000), (4096, 2048)), ((3000, 6000), (2048, 4096)), ((8192, 1024), (4096, 512))]:
            with self.subTest(size=size):
                raw = make_image(size=size)
                product = read_product_image(raw)
                self.assertEqual((product.width, product.height), expected)
                self.assertTrue(product.adjustment)
                self.assert_original_preserved(product, raw, size)
                self.assert_valid_api_image(product)

    def test_previously_rejected_dimension_boundaries_are_accepted(self):
        for size in [(511, 512), (512, 511), (4097, 512), (512, 4097)]:
            with self.subTest(size=size):
                raw = make_image(size=size)
                product = read_product_image(raw)
                self.assert_valid_api_image(product)
                self.assert_original_preserved(product, raw, size)

    def test_exact_api_boundaries_keep_uploaded_bytes(self):
        for size in [(512, 4096), (4096, 512)]:
            with self.subTest(size=size):
                raw = make_image(size=size)
                product = read_product_image(raw)
                self.assertEqual(product.data, raw)
                self.assertEqual(product.adjustment, "")
                self.assert_valid_api_image(product)

    def test_extreme_aspect_ratios_are_centered_with_light_padding(self):
        cases = [
            ((2000, 100), (4096, 512), (4096, 205)),
            ((100, 2000), (512, 4096), (205, 4096)),
            ((4096, 100), (4096, 512), (4096, 100)),
            ((100, 4096), (512, 4096), (100, 4096)),
        ]
        for size, expected_canvas, expected_content in cases:
            with self.subTest(size=size):
                source = Image.new("RGB", size, "#608372")
                draw = ImageDraw.Draw(source)
                # Both ends must survive fitting, including for very thin inputs.
                draw.rectangle((0, 0, size[0] // 4, size[1] - 1), fill="#b03020")
                draw.rectangle((size[0] * 3 // 4, 0, size[0] - 1, size[1] - 1), fill="#2030b0")
                buffer = BytesIO()
                source.save(buffer, format="PNG")
                raw = buffer.getvalue()
                product = read_product_image(raw)
                self.assertEqual((product.width, product.height), expected_canvas)
                self.assert_valid_api_image(product)
                self.assert_original_preserved(product, raw, size)
                with Image.open(BytesIO(product.data)) as decoded:
                    decoded = decoded.convert("RGB")
                    padding = decoded.getpixel((0, 0))
                    self.assertGreaterEqual(min(padding), 220)
                    self.assertEqual(padding, decoded.getpixel((decoded.width - 1, decoded.height - 1)))
                    bounds = ImageChops.difference(decoded, Image.new("RGB", decoded.size, padding)).getbbox()
                    self.assertIsNotNone(bounds)
                    left, top, right, bottom = bounds
                    self.assertEqual((right - left, bottom - top), expected_content)
                    self.assertLessEqual(abs(left - (decoded.width - right)), 1)
                    self.assertLessEqual(abs(top - (decoded.height - bottom)), 1)
                    middle_y = (top + bottom) // 2
                    self.assertEqual(decoded.getpixel((left + 2, middle_y)), (176, 48, 32))
                    self.assertEqual(decoded.getpixel((right - 3, middle_y)), (32, 48, 176))

    def test_png_transparency_is_preserved_when_resizing(self):
        buffer = BytesIO()
        Image.new("RGBA", (256, 128), (30, 100, 180, 110)).save(buffer, format="PNG")
        raw = buffer.getvalue()
        product = read_product_image(raw)
        self.assert_valid_api_image(product)
        self.assert_original_preserved(product, raw, (256, 128))
        with Image.open(BytesIO(product.data)) as decoded:
            self.assertEqual(decoded.size, (1024, 512))
            self.assertEqual(decoded.mode, "RGBA")
            self.assertEqual(decoded.getpixel((512, 256))[3], 110)

    def test_exif_rotation_is_applied_and_original_preview_is_preserved(self):
        for orientation, expected_top_left in [(6, (0, 0, 255)), (8, (0, 255, 0))]:
            with self.subTest(orientation=orientation):
                source = Image.new("RGB", (640, 800))
                draw = ImageDraw.Draw(source)
                draw.rectangle((0, 0, 319, 399), fill=(255, 0, 0))
                draw.rectangle((320, 0, 639, 399), fill=(0, 255, 0))
                draw.rectangle((0, 400, 319, 799), fill=(0, 0, 255))
                draw.rectangle((320, 400, 639, 799), fill=(255, 255, 0))
                exif = Image.Exif()
                exif[274] = orientation
                buffer = BytesIO()
                source.save(buffer, format="JPEG", quality=95, exif=exif)
                raw = buffer.getvalue()
                product = read_product_image(raw)
                self.assertEqual((product.width, product.height), (800, 640))
                # Metadata reports the visual dimensions of the preview after EXIF rotation.
                self.assert_original_preserved(product, raw, (800, 640))
                self.assert_valid_api_image(product)
                self.assertTrue(product.adjustment)
                with Image.open(BytesIO(product.data)) as decoded:
                    self.assertIn(decoded.getexif().get(274), (None, 1))
                    for actual, expected in zip(decoded.getpixel((50, 50)), expected_top_left):
                        self.assertLessEqual(abs(actual - expected), 5)

    def test_converted_output_stays_below_api_byte_limit(self):
        # A compact indexed PNG can grow past 10 MB when orientation correction
        # expands its palette into RGB. Check the actual resulting payload.
        size = (4096, 2048)
        source = Image.frombytes("P", size, random.Random(31).randbytes(size[0] * size[1]))
        source.putpalette(random.Random(4).randbytes(768))
        exif = Image.Exif()
        exif[274] = 3
        buffer = BytesIO()
        source.save(buffer, format="PNG", exif=exif)
        raw = buffer.getvalue()
        self.assertLessEqual(len(raw), MAX_UPLOAD_BYTES)
        product = read_product_image(raw)
        self.assert_original_preserved(product, raw, size)
        self.assert_valid_api_image(product)
        self.assertEqual(product.mime, "image/jpeg")

    def test_reject_wrong_format_and_corrupt_image(self):
        for data in [b"not an image", make_image("GIF"), make_image()[:100]]:
            with self.subTest(data_length=len(data)):
                with self.assertRaises(ValueError):
                    read_product_image(data)

    def test_upload_limit_remains_10_mb(self):
        with self.assertRaisesRegex(ValueError, "10 MB"):
            read_product_image(b"x" * (MAX_UPLOAD_BYTES + 1))
        raw = make_image()
        raw += b"\0" * (MAX_UPLOAD_BYTES - len(raw))
        product = read_product_image(raw)
        self.assertEqual(product.data, raw)
        self.assert_valid_api_image(product)


if __name__ == "__main__":
    unittest.main()
