"""
Samplette API — FastAPI wrapper around the Samplette discovery API.

Endpoints:
    GET /challenge                – random sample with full metadata
    GET /sample/{video_id}        – single sample by video ID
    GET /download/{video_id}      – YouTube URL for client-side playback
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Optional

import cloudscraper
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Samplette API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Samplette config ──────────────────────────────────────────────────────────

SAMPLETTE_BASE = "https://samplette.io"
GET_SAMPLE_URL = f"{SAMPLETTE_BASE}/get_sample"

# ── session state ─────────────────────────────────────────────────────────────

_SEEN_IDS: list[int] = []
_LAST_SEED_ID: Optional[int] = None

# Reused cloudscraper session (bypasses Cloudflare JS challenge)
_SCRAPER: Optional[cloudscraper.CloudScraper] = None
_CSRF_TOKEN: Optional[str] = None


async def _ensure_session() -> cloudscraper.CloudScraper:
    global _SCRAPER, _CSRF_TOKEN
    if _SCRAPER is not None and _CSRF_TOKEN is not None:
        return _SCRAPER

    scraper = cloudscraper.create_scraper()
    scraper.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })

    resp = await asyncio.to_thread(scraper.get, f"{SAMPLETTE_BASE}/")

    m = re.search(r'<meta\s+content="([^"]+)"\s+name="csrf-token"\s*>', resp.text)
    if not m:
        m = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"\s*>', resp.text)
    if not m:
        raise HTTPException(status_code=502, detail="Could not extract CSRF token from Samplette")

    _CSRF_TOKEN = m.group(1)
    _SCRAPER = scraper
    logger.info("Samplette session established: csrf_token=%.20s", _CSRF_TOKEN)
    return _SCRAPER


# ── TTL cache ─────────────────────────────────────────────────────────────────

_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300


async def _fetch_samplette(
    count: int = 10,
    video_id: Optional[int] = None,
    kind: Optional[str] = None,
    exclude: Optional[list[int]] = None,
    previous_ids: Optional[list[int]] = None,
    include_previously_seen: bool = False,
    repeat_between_sessions: bool = False,
) -> list[dict]:
    scraper = await _ensure_session()

    body: dict = {
        "count": count,
        "include-previously-seen": include_previously_seen,
        "repeat-between-sessions": repeat_between_sessions,
        "exclude": exclude or [],
        "previous-ids": previous_ids or [],
    }

    if video_id is not None:
        body["id"] = video_id
        body["kind"] = kind or "direct"
    else:
        body["id"] = None
        body["kind"] = "random"

    cache_key = f"samplette:{json.dumps(body, sort_keys=True)}"
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return json.loads(cached[1])

    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "*/*",
        "Referer": f"{SAMPLETTE_BASE}/",
        "Origin": SAMPLETTE_BASE,
        "x-requested-with": "XMLHttpRequest",
        "x-csrftoken": _CSRF_TOKEN,
    }

    logger.info("Samplette POST %s ids=%s", GET_SAMPLE_URL, body.get("previous-ids", [])[:3])

    resp = await asyncio.to_thread(scraper.post, GET_SAMPLE_URL, json=body, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Samplette returned {resp.status_code}: {resp.text[:200]}",
        )

    data = resp.json()
    if not isinstance(data, list):
        logger.warning("Samplette returned non-list: %s", str(data)[:200])
        return []

    _CACHE[cache_key] = (now, json.dumps(data))
    return data


# ── YouTube helpers ───────────────────────────────────────────────────────────

_YOUTUBE_RE = re.compile(r"[?&]v=([a-zA-Z0-9_-]{11})")


def _extract_video_id(url: str) -> Optional[str]:
    m = _YOUTUBE_RE.search(url)
    return m.group(1) if m else None


# ── response normalizer ──────────────────────────────────────────────────────

def _normalize(raw: dict) -> dict:
    ab = raw.get("acousticbrainz") or {}
    discogs = raw.get("discogs") or {}
    artist_array = discogs.get("artist_array") or []
    label_array = discogs.get("label_array") or []
    genre_array = discogs.get("genre_array") or []
    style_array = discogs.get("style_array") or []

    youtube_url = raw.get("url", "")
    video_id = _extract_video_id(youtube_url)

    published = raw.get("published", "") or ""
    if published and " " in published:
        published = published.split(" ")[0]

    return {
        "id": raw.get("id"),
        "title": raw.get("best_title") or raw.get("title", ""),
        "full_title": raw.get("title", ""),
        "youtube_url": youtube_url,
        "youtube_video_id": video_id,
        "channel": raw.get("channel", ""),
        "channel_id": raw.get("channel_id", ""),
        "duration": raw.get("duration"),
        "views": raw.get("views"),
        "published": published,
        "bpm": ab.get("tempo"),
        "key": ab.get("key"),
        "scale": ab.get("scale"),
        "tonality": ab.get("tonality"),
        "original_artist": artist_array[0] if artist_array else None,
        "original_title": discogs.get("title"),
        "genre_tags": genre_array,
        "style_tags": style_array,
        "label": label_array[0] if label_array else None,
        "country": discogs.get("country"),
        "year": discogs.get("year"),
        "cover_image": discogs.get("cover_image"),
        "thumb": discogs.get("thumb"),
        "discogs_url": discogs.get("uri"),
    }


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/challenge")
async def get_challenge(
    genre: Optional[str] = Query(None, description="Ignored — kept for API compatibility"),
    enrich: bool = Query(False, description="No-op — Samplette already returns full metadata"),
):
    global _LAST_SEED_ID, _SEEN_IDS

    previous_ids = _SEEN_IDS[-20:] if _SEEN_IDS else None

    kw: dict = {
        "count": 10,
        "include_previously_seen": False,
        "repeat_between_sessions": False,
    }
    if _LAST_SEED_ID is not None:
        kw["video_id"] = _LAST_SEED_ID
        kw["kind"] = "direct"
    if previous_ids:
        kw["previous_ids"] = previous_ids

    results = await _fetch_samplette(**kw)

    if not results:
        kw.pop("video_id", None)
        kw.pop("kind", None)
        kw.pop("previous_ids", None)
        results = await _fetch_samplette(**kw)
        if not results:
            raise HTTPException(status_code=404, detail="No samples available from Samplette.")

    raw = random.choice(results)

    _LAST_SEED_ID = raw.get("id")
    _SEEN_IDS.append(raw.get("id"))
    if len(_SEEN_IDS) > 100:
        _SEEN_IDS[:] = _SEEN_IDS[-100:]

    return _normalize(raw)


@app.get("/sample/{video_id}")
async def get_sample(video_id: int):
    results = await _fetch_samplette(count=1, video_id=video_id, kind="direct")
    if not results:
        raise HTTPException(status_code=404, detail=f"Sample {video_id} not found.")
    return _normalize(results[0])


@app.get("/download/{video_id}")
async def download_sample(video_id: int):
    results = await _fetch_samplette(count=1, video_id=video_id, kind="direct")
    if not results:
        raise HTTPException(status_code=404, detail=f"Sample {video_id} not found.")

    raw = results[0]
    youtube_url = raw.get("url", "")
    vid = _extract_video_id(youtube_url)

    return JSONResponse(content={
        "youtube_url": youtube_url,
        "video_id": vid,
    })
