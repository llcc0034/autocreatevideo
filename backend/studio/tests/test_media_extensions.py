from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.media_extensions import verified_media_extension


class VerifiedMediaExtensionTests(SimpleTestCase):
    def test_supported_probe_formats_have_canonical_extensions(self):
        expected = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "image/bmp": ".bmp", "image/gif": ".gif", "image/tiff": ".tiff",
            "video/mp4": ".mp4", "video/webm": ".webm", "video/x-matroska": ".mkv",
            "audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/flac": ".flac",
            "audio/ogg": ".ogg", "audio/mp4": ".m4a", "text/markdown": ".md"}
        for mime, suffix in expected.items():
            with self.subTest(mime=mime):
                self.assertEqual(verified_media_extension(mime, mime.split("/", 1)[0]), suffix)

    def test_unknown_or_wrong_kind_does_not_invent_a_file_format(self):
        for mime, kind in (("image/unknown", "image"), ("application/octet-stream", "video"), ("image/png", "audio")):
            with self.subTest(mime=mime), self.assertRaises(ValidationError):
                verified_media_extension(mime, kind)
