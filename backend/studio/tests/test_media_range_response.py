"""Isolated streaming transport checks; no model or business data."""
from io import BytesIO
from unittest.mock import Mock

from django.test import SimpleTestCase, RequestFactory

from studio.views import _range_file_response


class MediaRangeResponseTests(SimpleTestCase):
    def response(self, header, payload=b"0123456789"):
        handle = BytesIO(payload)
        path = Mock(name="media_path")
        path.name = "isolated.mp4"
        path.open.return_value = handle
        response = _range_file_response(path, RequestFactory().get("/", HTTP_RANGE=header),
            content_type="video/mp4", as_attachment=True)
        self.addCleanup(response.close)
        return response, handle, path

    def test_range_binds_handle_before_streaming_and_never_reopens_replaced_path(self):
        response, handle, path = self.response("bytes=2-5")
        path.open.assert_called_once_with("rb")
        replacement = BytesIO(b"changed!!!")
        self.addCleanup(replacement.close)
        path.open.return_value = replacement
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 2-5/10")
        self.assertEqual(b"".join(response.streaming_content), b"2345")
        self.assertTrue(handle.closed)
        path.open.assert_called_once()

    def test_unconsumed_range_response_closes_open_handle(self):
        response, handle, _ = self.response("bytes=2-5")
        response.close()
        self.assertTrue(handle.closed)

    def test_truncated_stream_is_an_error_not_a_short_success(self):
        response, handle, _ = self.response("bytes=2-8")
        handle.truncate(3)
        with self.assertRaisesRegex(OSError, "truncated"):
            b"".join(response.streaming_content)
        self.assertTrue(handle.closed)

    def test_invalid_ranges_close_the_handle_and_keep_416(self):
        for header in ("bytes=20-30", "bytes=-0", "bytes=-", "bytes=5-2", "bytes=1-2,4-5", "bad"):
            with self.subTest(header=header):
                response, handle, _ = self.response(header)
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response["Content-Range"], "bytes */10")
                self.assertTrue(handle.closed)

    def test_full_and_suffix_downloads_keep_exact_bytes(self):
        for header, status, expected in (("", 200, b"0123456789"), ("bytes=-3", 206, b"789"), ("bytes=8-", 206, b"89")):
            with self.subTest(header=header):
                response, handle, path = self.response(header)
                self.assertEqual(response.status_code, status)
                self.assertTrue(response["Content-Disposition"].startswith("attachment;"))
                self.assertEqual(b"".join(response.streaming_content), expected)
                response.close()
                self.assertTrue(handle.closed)
                path.open.assert_called_once()
