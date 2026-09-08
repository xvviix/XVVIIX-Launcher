"""Native icon frames must survive extraction; no invented high-resolution detail."""

from pathlib import Path
import os
import io
import struct
import shutil
import sys
import tempfile
import unittest

from PIL import Image
from xvviix.services import icons


class NativeIconQualityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="xvviix-icons-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.ico"
        Image.new("RGBA", (256, 256), (20, 100, 220, 180)).save(
            self.source, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (128, 128), (256, 256)]
        )

    def test_multi_resolution_master_is_preserved_byte_for_byte(self):
        filename = icons.extract_native_icon(self.source, self.root / "cache")
        output = self.root / "cache" / filename
        self.assertTrue(filename.startswith("icon_v2_"))
        self.assertEqual(output.read_bytes(), self.source.read_bytes())
        self.assertEqual(icons.native_icon_sizes(output), icons.native_icon_sizes(self.source))
        self.assertIn((256, 256), icons.native_icon_sizes(output))

    def test_exe_export_is_not_flattened_to_a_single_png_frame(self):
        source = self.source
        fake_exe = self.root / "fixture.exe"
        fake_exe.write_bytes(b"synthetic PE marker")

        class Extractor:
            def __init__(self, path):
                self.path = path

            def export_icon(self, destination, **_kwargs):
                shutil.copyfile(source, destination)

        filename = icons.extract_native_icon(
            fake_exe, self.root / "cache", extractor_factory=Extractor
        )
        self.assertEqual((self.root / "cache" / filename).read_bytes(), source.read_bytes())

    def test_display_uses_requested_size_but_does_not_rewrite_the_master(self):
        before = self.source.read_bytes()
        image = icons.load_display_icon(self.source, 48)
        self.assertEqual(image.size, (48, 48))
        self.assertEqual(image.mode, "RGBA")
        image.close()
        self.assertEqual(self.source.read_bytes(), before)

    def test_exact_native_frame_is_preferred_over_downscaling_another_frame(self):
        frames = []
        for size, color in ((16, "red"), (48, "green"), (256, "blue")):
            buffer = io.BytesIO()
            Image.new("RGBA", (size, size), color).save(buffer, format="PNG")
            frames.append((size, buffer.getvalue()))
        offset = 6 + 16 * len(frames)
        directory = []
        for size, data in frames:
            directory.append(
                struct.pack(
                    "<BBBBHHII",
                    size if size < 256 else 0,
                    size if size < 256 else 0,
                    0,
                    0,
                    1,
                    32,
                    len(data),
                    offset,
                )
            )
            offset += len(data)
        path = self.root / "distinct-frames.ico"
        path.write_bytes(
            struct.pack("<HHH", 0, 1, len(frames))
            + b"".join(directory)
            + b"".join(data for _size, data in frames)
        )
        image = icons.load_display_icon(path, 48)
        self.assertEqual(image.getpixel((20, 20)), (0, 128, 0, 255))
        image.close()

    def test_low_resolution_source_remains_low_resolution_in_storage(self):
        small = self.root / "small.ico"
        Image.new("RGBA", (16, 16), "#123456").save(small, format="ICO", sizes=[(16, 16)])
        filename = icons.extract_native_icon(small, self.root / "cache")
        self.assertEqual(icons.native_icon_sizes(self.root / "cache" / filename), [(16, 16)])

    def test_user_icons_are_not_auto_replaced_and_cache_is_reused(self):
        self.assertTrue(icons.is_user_icon("custom_test.png"))
        self.assertTrue(icons.is_user_icon("my-own-icon.ico"))
        self.assertFalse(icons.is_user_icon("icon_old.png"))
        first = icons.extract_native_icon(self.source, self.root / "cache")
        second = icons.extract_native_icon(self.source, self.root / "cache")
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.root / "cache").iterdir())), 1)

    @unittest.skipUnless(os.name == "nt", "Real Windows executable icon resources")
    def test_native_python_executable_icon_extraction(self):
        if icons.IconExtractor is None:
            self.skipTest("icoextract unavailable")
        filename = icons.extract_native_icon(sys.executable, self.root / "cache")
        self.assertTrue(filename)
        self.assertTrue(icons.native_icon_sizes(self.root / "cache" / filename))
