"""
BeatSaver API Search & Map Downloader.

Supports searching community maps by keyword, fetching map details by ID or URL,
and caching downloaded .zip packages locally.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

USER_AGENT = "PS5BeatSaberCustomSongConverter/1.0"
API_BASE = "https://api.beatsaver.com"


@dataclass
class BeatSaverSearchResult:
    map_id: str
    song_name: str
    song_sub_name: str
    song_author_name: str
    level_author_name: str
    bpm: float
    duration_seconds: int
    score_pct: float
    upvotes: int
    downvotes: int
    difficulties_summary: str
    download_url: str


def extract_beatsaver_id(id_or_url: str) -> str:
    """Extract hex BeatSaver map ID from a raw key or beatsaver.com / bsaber.com URL."""
    s = id_or_url.strip()
    m = re.search(r"(?:beatsaver\.com/maps/|bsaber\.com/songs/)([0-9a-fA-F]+)", s, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    if re.fullmatch(r"[0-9a-fA-F]{1,10}", s):
        return s.lower()
    raise ValueError(
        f"'{id_or_url}' is not a valid BeatSaver map ID (e.g. '30560') or beatsaver.com/maps/<id> URL."
    )


def _http_get_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_doc(doc: dict) -> Optional[BeatSaverSearchResult]:
    versions = doc.get("versions") or []
    if not versions:
        return None
    v0 = versions[0]
    dl_url = v0.get("downloadURL") or ""
    if not dl_url:
        return None

    meta = doc.get("metadata") or {}
    stats = doc.get("stats") or {}
    diffs = []
    for d in v0.get("diffs") or []:
        ch = d.get("characteristic", "Standard")
        df = d.get("difficulty", "")
        label = df if ch == "Standard" else f"{ch}:{df}"
        if label not in diffs:
            diffs.append(label)

    return BeatSaverSearchResult(
        map_id=str(doc.get("id", "")).lower(),
        song_name=str(meta.get("songName") or doc.get("name") or "Unknown"),
        song_sub_name=str(meta.get("songSubName") or ""),
        song_author_name=str(meta.get("songAuthorName") or ""),
        level_author_name=str(meta.get("levelAuthorName") or ""),
        bpm=float(meta.get("bpm") or 120.0),
        duration_seconds=int(meta.get("duration") or 0),
        score_pct=round(float(stats.get("score") or 0.0) * 100.0, 1),
        upvotes=int(stats.get("upvotes") or 0),
        downvotes=int(stats.get("downvotes") or 0),
        difficulties_summary=", ".join(diffs),
        download_url=dl_url,
    )


def search_beatsaver(query: str, page: int = 0, sort_order: str = "Relevance") -> List[BeatSaverSearchResult]:
    q_enc = urllib.parse.quote(query.strip())
    url = f"{API_BASE}/search/text/{page}?sortOrder={urllib.parse.quote(sort_order)}&q={q_enc}"
    payload = _http_get_json(url)
    results: List[BeatSaverSearchResult] = []
    for doc in payload.get("docs") or []:
        parsed = _parse_doc(doc)
        if parsed:
            results.append(parsed)
    return results


def fetch_beatsaver_map_info(id_or_url: str) -> BeatSaverSearchResult:
    map_id = extract_beatsaver_id(id_or_url)
    doc = _http_get_json(f"{API_BASE}/maps/id/{map_id}")
    parsed = _parse_doc(doc)
    if not parsed:
        raise ValueError(f"BeatSaver map '{map_id}' has no downloadable version.")
    return parsed


def download_beatsaver_zip(
    id_or_url_or_result: str | BeatSaverSearchResult,
    cache_dir: Optional[str] = None,
) -> str:
    """Download a BeatSaver map .zip to cache_dir and return the local .zip path."""
    if isinstance(id_or_url_or_result, BeatSaverSearchResult):
        info = id_or_url_or_result
    else:
        info = fetch_beatsaver_map_info(id_or_url_or_result)

    if cache_dir is None:
        cache_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_beatsaver_cache")
    os.makedirs(cache_dir, exist_ok=True)

    safe_title = re.sub(r"[^A-Za-z0-9 _.-]+", "", f"{info.map_id}_{info.song_name}")[:60].strip()
    dest_path = os.path.join(cache_dir, f"{safe_title or info.map_id}.zip")
    if os.path.isfile(dest_path) and os.path.getsize(dest_path) > 256:
        return dest_path

    req = urllib.request.Request(info.download_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60.0) as resp:
        raw = resp.read()

    tmp_path = dest_path + ".part"
    with open(tmp_path, "wb") as f:
        f.write(raw)
    os.replace(tmp_path, dest_path)
    return dest_path
