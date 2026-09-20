import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.fetch_sources import fetch_file


class SourceTests(unittest.TestCase):
    def test_verified_download_reuses_cache_without_network(self):
        data = b'fixed upstream content'
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'upstream/source'
            with patch('bridge.fetch_sources.urlopen', return_value=io.BytesIO(data)) as request:
                self.assertEqual(fetch_file('https://raw.githubusercontent.com/org/repo/commit/file', target, digest), data)
                self.assertEqual(fetch_file('https://raw.githubusercontent.com/org/repo/commit/file', target, digest), data)
                self.assertEqual(request.call_count, 1)

    def test_corrupt_download_is_not_saved_and_corrupt_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'source'
            with patch('bridge.fetch_sources.urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaises(RuntimeError):
                    fetch_file('https://codeload.github.com/org/repo/tar.gz/commit', target, '0'*64)
            self.assertFalse(target.exists())
            target.write_bytes(b'bad cache')
            with patch('bridge.fetch_sources.urlopen') as request:
                with self.assertRaises(RuntimeError):
                    fetch_file('https://codeload.github.com/org/repo/tar.gz/commit', target, '0'*64)
                request.assert_not_called()

    def test_unexpected_origin_rejected(self):
        with patch('bridge.fetch_sources.urlopen') as request:
            with self.assertRaises(ValueError):
                fetch_file('https://example.com/source', Path('unused'), '0'*64)
            request.assert_not_called()
