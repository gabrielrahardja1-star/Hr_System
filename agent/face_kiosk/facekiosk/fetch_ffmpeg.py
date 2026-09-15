"""Download a static ffmpeg into vendor/ so the built exe needs no separate
ffmpeg install on the target machine.

    python -m facekiosk.fetch_ffmpeg

macOS: a static build from evermeet.cx (x86_64 — runs fine under Rosetta 2 on
Apple Silicon, which is near-universally already present). Windows: gyan.dev's
"essentials" build. Both are GPL-licensed full builds (way more codecs than
this app uses, since neither source offers a build with just avfoundation/
dshow input + rawvideo output) — fine for an internal tool, worth a second
look before external distribution.
"""

from __future__ import annotations

import io
import sys
import urllib.request
import zipfile

from .config import FFMPEG_VENDORED, IS_WINDOWS, VENDOR_DIR

MAC_URL = "https://evermeet.cx/ffmpeg/ffmpeg-9.0.1.zip"
WINDOWS_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def _download(url: str) -> bytes:
    print(f"  fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "facekiosk/fetch"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 (trusted host)
        return resp.read()


def _extract_binary(zip_bytes: bytes, member_suffix: str, dest) -> None:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        candidates = [n for n in zf.namelist() if n.lower().endswith(member_suffix)]
        if not candidates:
            raise RuntimeError(f"no {member_suffix!r} found in the downloaded archive")
        # gyan.dev nests it under .../bin/ffmpeg.exe; evermeet's zip has it at the root.
        member = min(candidates, key=len)
        with zf.open(member) as fh:
            dest.write_bytes(fh.read())


def main() -> int:
    if FFMPEG_VENDORED.exists():
        print(f"  {FFMPEG_VENDORED.name}  already present, skipping")
        return 0

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    print(f"fetching ffmpeg into {VENDOR_DIR}")
    if IS_WINDOWS:
        data = _download(WINDOWS_URL)
        _extract_binary(data, "bin/ffmpeg.exe", FFMPEG_VENDORED)
    else:
        data = _download(MAC_URL)
        _extract_binary(data, "ffmpeg", FFMPEG_VENDORED)
        FFMPEG_VENDORED.chmod(0o755)
    print(f"  ok  {FFMPEG_VENDORED.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
