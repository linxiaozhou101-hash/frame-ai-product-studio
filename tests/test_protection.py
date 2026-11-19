"""Check the actual mask/composite invariants, without a model or paid API."""

from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

from images import read_product_image
from protection import compose_result, prepare_product, preview_cutout


def encode(image, fmt="PNG", **kwargs):
    output = BytesIO()
    image.save(output, format=fmt, **kwargs)
    return output.getvalue()


def decode(data):
    image = Image.open(BytesIO(data))
    image.load()
    return image


def product_and_mask(size=(512, 512)):
    image = Image.new("RGB", size, "#976d44")
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(image)
    md = ImageDraw.Draw(mask)
    w, h = size
    body = (w // 4, h // 4, w // 2, 3 * h // 4)
    handle = (w // 2 - 10, h // 3, 3 * w // 4, 2 * h // 3)
    hole = (w // 2 + 10, h // 3 + 12, 3 * w // 4 - 12, 2 * h // 3 - 12)
    for d, body_color, bg_color in ((draw, "#14876d", "#976d44"), (md, 255, 0)):
        d.ellipse(handle, fill=body_color)
        d.ellipse(hole, fill=bg_color)
        d.rectangle(body, fill=body_color)
    draw.text((w // 3, h // 2), "BRAND 123", fill="white", stroke_width=1)
    return read_product_image(encode(image)), encode(mask)


class ProductProtectionTests(unittest.TestCase):
    def test_binary_api_mask_has_correct_polarity_and_handle_hole(self):
        product, mask = product_and_mask()
        prepared = prepare_product(product, mask)
        api = decode(prepared.mask_png)
        self.assertEqual(api.mode, "RGB")
        self.assertEqual(api.size, (512, 512))
        self.assertEqual(set(api.getdata()), {(0, 0, 0), (255, 255, 255)})
        self.assertEqual(api.getpixel((180, 260)), (0, 0, 0))
        self.assertEqual(api.getpixel((310, 255)), (255, 255, 255))
        self.assertEqual(api.getpixel((20, 20)), (255, 255, 255))

    def test_core_and_text_pixels_are_exact_and_background_hole_is_replaced(self):
        product, mask = product_and_mask()
        prepared = prepare_product(product, mask)
        background = encode(Image.new("RGB", (512, 512), "#dcdbe1"))
        result = decode(compose_result(prepared, background))
        source = decode(prepared.source_png)
        core_diff = ImageChops.difference(result, source).crop((128, 128, 256, 384))
        self.assertIsNone(core_diff.getbbox(), "opaque body and all text must be bit-exact")
        self.assertEqual(result.getpixel((310, 255)), (220, 219, 225))
        self.assertEqual(result.getpixel((20, 20)), (220, 219, 225))

    def test_resize_background_never_resamples_the_product(self):
        product, mask = product_and_mask()
        prepared = prepare_product(product, mask)
        result = decode(compose_result(prepared, encode(Image.new("RGB", (1024, 1024), "white"))))
        self.assertEqual(result.size, (512, 512))
        self.assertIsNone(ImageChops.difference(result, decode(prepared.source_png)).crop((128, 128, 256, 384)).getbbox())

    def test_small_image_mask_uses_identical_resize(self):
        product, mask = product_and_mask((256, 384))
        prepared = prepare_product(product, mask)
        self.assertEqual((prepared.width, prepared.height), (512, 768))
        api = decode(prepared.mask_png)
        self.assertEqual(api.getpixel((150, 200)), (0, 0, 0))
        self.assertEqual(api.getpixel((20, 20)), (255, 255, 255))

    def test_extreme_aspect_padding_remains_editable(self):
        source = Image.new("RGB", (2000, 100), "#ed6531")
        mask = Image.new("L", source.size, 0)
        ImageDraw.Draw(mask).rectangle((300, 10, 800, 90), fill=255)
        prepared = prepare_product(read_product_image(encode(source)), encode(mask))
        api = decode(prepared.mask_png)
        self.assertEqual(api.size, (4096, 512))
        self.assertEqual(api.getpixel((700, 0)), (255, 255, 255))
        self.assertEqual(api.getpixel((700, 255)), (0, 0, 0))

    def test_exif_uses_visual_coordinates_for_manual_mask(self):
        source = Image.new("RGB", (640, 800), "red")
        exif = Image.Exif()
        exif[274] = 6
        product = read_product_image(encode(source, "JPEG", exif=exif))
        mask = Image.new("L", (800, 640), 0)
        ImageDraw.Draw(mask).rectangle((100, 100, 300, 400), fill=255)
        prepared = prepare_product(product, encode(mask))
        self.assertEqual(decode(prepared.mask_png).size, (800, 640))
        self.assertEqual(decode(prepared.mask_png).getpixel((150, 150)), (0, 0, 0))
        with self.assertRaisesRegex(ValueError, "尺寸"):
            prepare_product(product, encode(Image.new("L", (640, 800), 255)))

    def test_transparent_png_does_not_need_local_model(self):
        source = Image.new("RGBA", (512, 512), (19, 54, 80, 0))
        draw = ImageDraw.Draw(source)
        draw.rectangle((100, 100, 300, 400), fill=(19, 54, 80, 255))
        draw.rectangle((301, 100, 302, 400), fill=(19, 54, 80, 128))
        with patch("protection._automatic_alpha", side_effect=AssertionError("not needed")):
            prepared = prepare_product(read_product_image(encode(source)))
        self.assertEqual(prepared.method, "PNG 透明通道")
        alpha = decode(prepared.alpha_png)
        self.assertEqual(alpha.getpixel((0, 0)), 0)
        self.assertEqual(alpha.getpixel((200, 200)), 255)
        self.assertEqual(alpha.getpixel((301, 200)), 128)
        result = decode(compose_result(prepared, encode(Image.new("RGB", source.size, "white"))))
        self.assertEqual(result.getpixel((200, 200)), (19, 54, 80))
        self.assertNotEqual(result.getpixel((301, 200)), (19, 54, 80))

    def test_alpha_core_is_snapped_but_soft_edge_is_not_blurred(self):
        product, _ = product_and_mask()
        alpha = Image.new("L", (512, 512), 0)
        ImageDraw.Draw(alpha).rectangle((128, 128, 256, 384), fill=250)
        alpha.putpixel((127, 200), 40)
        prepared = prepare_product(product, encode(alpha))
        clean = decode(prepared.alpha_png)
        self.assertEqual(clean.getpixel((150, 200)), 255)
        self.assertEqual(clean.getpixel((127, 200)), 40)
        self.assertEqual(clean.getpixel((126, 200)), 0)

    def test_opaque_png_uses_local_model_and_retains_all_mask_components(self):
        product, mask = product_and_mask()
        with patch("protection._automatic_alpha", return_value=decode(mask)) as extractor:
            prepared = prepare_product(product)
        extractor.assert_called_once()
        self.assertEqual(prepared.method, "本地自动识别")
        self.assertGreater(prepared.foreground_fraction, 0)
        self.assertLess(prepared.foreground_fraction, 1)

    def test_automatic_failure_is_not_replaced_by_full_image_edit(self):
        product, _ = product_and_mask()
        with patch("protection._automatic_alpha", side_effect=ValueError("模型不可用")):
            with self.assertRaisesRegex(ValueError, "模型不可用"):
                prepare_product(product)

    def test_empty_full_mismatched_and_invalid_masks_are_rejected(self):
        product, _ = product_and_mask()
        cases = [encode(Image.new("L", (512, 512), 0)),
                 encode(Image.new("L", (512, 512), 255)),
                 encode(Image.new("L", (513, 512), 0)), b"invalid",
                 encode(Image.new("L", (512, 512), 0), "JPEG")]
        for bad_mask in cases:
            with self.subTest(length=len(bad_mask)), self.assertRaises(ValueError):
                prepare_product(product, bad_mask)

    def test_cutout_preview_contains_source_product_and_checker_background(self):
        product, mask = product_and_mask()
        prepared = prepare_product(product, mask)
        preview = decode(preview_cutout(prepared))
        self.assertEqual(preview.size, (512, 512))
        self.assertEqual(preview.getpixel((180, 250)), decode(prepared.source_png).getpixel((180, 250)))
        self.assertNotEqual(preview.getpixel((0, 0)), preview.getpixel((30, 0)))

    def test_bad_result_is_a_recoverable_value_error(self):
        product, mask = product_and_mask()
        prepared = prepare_product(product, mask)
        with self.assertRaisesRegex(ValueError, "生成结果"):
            compose_result(prepared, b"not a PNG")

    def test_generated_outer_ghost_removed_without_touching_core_or_far_background(self):
        product, mask = product_and_mask()
        prepared = prepare_product(product, mask)
        background = Image.new("RGB", (512, 512), (220, 219, 225))
        draw = ImageDraw.Draw(background)
        # Simulate a seven-pixel model-generated extrusion left of the cup.
        draw.rectangle((121, 160, 127, 320), fill="red")
        # A distant texture stripe and the shadow under the base must survive.
        draw.rectangle((20, 0, 30, 511), fill="#782411")
        draw.rectangle((140, 387, 250, 395), fill="#616161")
        result = decode(compose_result(prepared, encode(background)))
        for actual, expected in zip(result.getpixel((123, 250)), (220, 219, 225)):
            self.assertLessEqual(abs(actual - expected), 8)
        self.assertEqual(result.getpixel((25, 200)), background.getpixel((25, 200)))
        self.assertEqual(result.getpixel((200, 390)), background.getpixel((200, 390)))
        self.assertIsNone(ImageChops.difference(result, decode(prepared.source_png)).crop((128, 128, 256, 384)).getbbox())


if __name__ == "__main__":
    unittest.main()
