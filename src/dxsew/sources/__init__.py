"""External data sources. Every URL is read from configs/sources.yaml."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCES_YAML = REPO_ROOT / "configs" / "sources.yaml"
USER_AGENT = "dxsew-data-client"
TRIES = 3


def load_sources(path: Path | str = SOURCES_YAML) -> dict:
    """Return the source table of configs/sources.yaml."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def source_url(key: str, sources: dict | None = None) -> str:
    return (sources or load_sources())[key]["url"]


def _server_size(url: str) -> int | None:
    """Size in bytes the server reports for url, or None if it reports none."""
    for attempt in range(TRIES):
        try:
            r = requests.head(url, allow_redirects=True, timeout=60, headers={"User-Agent": USER_AGENT})
            if r.ok and "Content-Length" in r.headers:
                return int(r.headers["Content-Length"])
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def download(url: str, folder: Path | str, log=print) -> Path:
    """Download url into folder under its server file name and return the path.

    The file is written to a temporary name and renamed when complete. If the
    target already exists with the size the server reports, it is reused. If the
    server reports no size, an existing file is reused and a note is logged.
    Raises RuntimeError after three failed tries.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / unquote(Path(urlparse(url).path).name)
    size = _server_size(url)
    if target.exists():
        if size is not None and target.stat().st_size == size:
            log(f"reuse {target.name} ({size} bytes, matches server)")
            return target
        if size is None:
            log(f"reuse {target.name}: server reported no size, existing file kept")
            return target
    tmp = target.with_name(target.name + ".part")
    last_error: Exception | None = None
    for attempt in range(TRIES):
        try:
            with requests.get(url, stream=True, timeout=300, headers={"User-Agent": USER_AGENT}) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
            got = tmp.stat().st_size
            if size is not None and got != size:
                raise RuntimeError(f"{url}: got {got} bytes, server reported {size}")
            tmp.replace(target)
            log(f"downloaded {target.name} ({got} bytes)")
            return target
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            log(f"try {attempt + 1} of {TRIES} failed for {url}: {e}")
            time.sleep(5 * (attempt + 1))
    if tmp.exists():
        tmp.unlink()
    raise RuntimeError(f"download failed after {TRIES} tries: {url}: {last_error}")
