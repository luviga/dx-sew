"""Listing and download of public objects of the OEDI data lake.

The bucket is listed through the S3 ListObjectsV2 endpoint with plain
``requests``, without sign-in, the same method as
``0_download_smartds/download_smartds.py`` of DeltaE/dss-eulp-coupling:
https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html
"""

from __future__ import annotations

import os
import threading
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.parse import quote

import pandas as pd
import requests

PAGE_SIZE = "1000"  # the largest page ListObjectsV2 returns
TIMEOUT_S = 120.0
RETRIES = 6
CHUNK_BYTES = 1024 * 1024


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str, default: str = "") -> str:
    for child in element:
        if _tag(child) == name:
            return child.text or default
    return default


def get_with_retries(
    session: requests.Session, url: str, params: dict | None = None, stream: bool = False
) -> requests.Response:
    """GET with a growing pause between attempts; raises after the last one."""
    last_error: BaseException | None = None
    for attempt in range(RETRIES + 1):
        try:
            response = session.get(url, params=params, timeout=TIMEOUT_S, stream=stream)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
            if attempt < RETRIES:
                time.sleep(min(2**attempt, 30))
    raise RuntimeError(f"Request failed after {RETRIES + 1} attempts: {url}: {last_error}")


def parse_list_page(content: bytes) -> tuple[list[tuple[str, int]], str | None]:
    """Read one ListObjectsV2 answer: ``(key, size)`` pairs and the next token."""
    root = ET.fromstring(content)
    objects: list[tuple[str, int]] = []
    token: str | None = None
    for child in root:
        tag = _tag(child)
        if tag == "Contents":
            key = _child_text(child, "Key")
            if key:
                objects.append((key, int(_child_text(child, "Size", "0"))))
        elif tag == "NextContinuationToken":
            token = child.text
    return objects, token


def iter_objects(
    session: requests.Session, bucket_url: str, prefix: str
) -> Iterator[tuple[str, int]]:
    """Yield ``(key, size)`` of every object under ``prefix``, page by page."""
    token: str | None = None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": PAGE_SIZE}
        if token:
            params["continuation-token"] = token
        objects, token = parse_list_page(get_with_retries(session, bucket_url, params).content)
        yield from objects
        if not token:
            return


def list_prefix(bucket_url: str, prefix: str, progress: Callable[[int], None] | None = None) -> pd.DataFrame:
    """Full listing of ``prefix`` as a frame with the columns ``key`` and ``size``."""
    keys: list[str] = []
    sizes: list[int] = []
    with requests.Session() as session:
        for key, size in iter_objects(session, bucket_url, prefix):
            keys.append(key)
            sizes.append(size)
            if progress and len(keys) % 50000 == 0:
                progress(len(keys))
    return pd.DataFrame({"key": keys, "size": pd.array(sizes, dtype="int64")})


def key_url(bucket_url: str, key: str) -> str:
    return f"{bucket_url}/{quote(key, safe='/')}"


def fetch(session: requests.Session, url: str, target: Path, size: int | None = None) -> str:
    """Download ``url`` to ``target`` through a temporary name.

    A target that exists with the expected size is reused. When no size is
    given, the size the server reports in a HEAD answer is used. Returns
    ``"reused"`` or ``"downloaded"``.
    """
    target = Path(target)
    if target.exists():
        if size is None:
            head = session.head(url, timeout=TIMEOUT_S, allow_redirects=True)
            head.raise_for_status()
            size = int(head.headers.get("Content-Length", "-1"))
        if target.stat().st_size == size:
            return "reused"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.part")
    response = get_with_retries(session, url, stream=True)
    with temporary.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
            if chunk:
                handle.write(chunk)
    if size is not None and size >= 0 and temporary.stat().st_size != size:
        temporary.unlink()
        raise RuntimeError(f"{url}: received a size other than the {size} bytes expected.")
    temporary.replace(target)
    return "downloaded"


def fetch_many(
    bucket_url: str,
    items: Iterable[tuple[str, int, Path]],
    workers: int,
    before_each: Callable[[int], bool] | None = None,
    label: str = "download",
) -> dict:
    """Download ``(key, size, target)`` items with a pool of threads.

    ``before_each(size)`` is called before every download that is not a reuse;
    when it returns False that item and all later ones are left out (this is
    the disk guard of the staging). Returns counts, bytes and the keys left.
    """
    items = list(items)
    result = {"downloaded": 0, "reused": 0, "bytes_downloaded": 0, "stopped": False, "left": []}
    local = threading.local()
    lock = threading.Lock()

    def one(item: tuple[str, int, Path]) -> None:
        key, size, target = item
        if target.exists() and target.stat().st_size == size:
            with lock:
                result["reused"] += 1
            return
        with lock:
            if result["stopped"] or (before_each and not before_each(size)):
                result["stopped"] = True
                result["left"].append(key)
                return
        if not hasattr(local, "session"):
            local.session = requests.Session()
        fetch(local.session, key_url(bucket_url, key), target, size)
        with lock:
            result["downloaded"] += 1
            result["bytes_downloaded"] += size
            done = result["downloaded"]
        if done % 2000 == 0:
            print(f"  {label}: {done} files downloaded", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(one, items):
            pass
    return result
