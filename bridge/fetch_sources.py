"""Fetch immutable upstream inputs into ignored build caches, with SHA256 checks."""
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def fetch_file(url, target, expected, limit=4*1024*1024):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ('raw.githubusercontent.com', 'codeload.github.com'):
        raise ValueError('Unsupported upstream source')
    target = Path(target)
    if target.is_file():
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f'Cached source checksum mismatch: {target.name}')
        return data
    with urlopen(Request(url, headers={'User-Agent': 'iOS-History-App-build/1.0'}), timeout=60) as response:
        data = response.read(limit+1)
    if len(data) > limit or hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError(f'Upstream source checksum mismatch: {target.name}')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return data
