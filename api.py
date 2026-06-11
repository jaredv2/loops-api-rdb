"""
looperman_api.py
----------------
FastAPI wrapper around the Loopazon scraper/parser.

Endpoints:
    GET /challenge              – random loop with mp3, waveform, details
    GET /download/{loop_id}     – proxy-download the MP3
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from parser import (
    parse_listing_page,
    parse_detail_page,
    get_genres,
    BASE_URL,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Loopazon API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_ALL_GENRES = sorted(get_genres().keys())

_SEARCH_URL = (
    "https://www.loopazon.com/"
    "?subcats=Y&pcode_from_q=Y&pshort=Y&pfull=Y&pname=Y"
    "&pkeywords=Y&search_performed=Y&q={}&dispatch=products.search"
)


# ── TTL cache ──────────────────────────────────────────────────────────────────

_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300


async def _fetch(url: str) -> str:
    now = time.time()
    cached = _CACHE.get(url)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return cached[1]
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "looperman-api/1.0.0"})
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail="Not found on Loopazon")
        resp.raise_for_status()
        text = resp.text
    _CACHE[url] = (now, text)
    return text


async def _fetch_bytes(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "looperman-api/1.0.0"})
        resp.raise_for_status()
        return resp.content


# ── helpers ────────────────────────────────────────────────────────────────────

async def _find_loop(loop_id: int) -> Optional[dict]:
    html = await _fetch(_SEARCH_URL.format(loop_id))
    listing = parse_listing_page(html, genre="", page=1)
    return next((l for l in listing.loops if l.get("id") == loop_id), None)


# ── endpoints ──────────────────────────────────────────────────────────────────

@app.get("/challenge")
async def get_challenge(
    genre: Optional[str] = Query(None, description="Genre slug; random if omitted"),
    enrich: bool = Query(False, description="Fetch detail page for live mp3/waveform"),
):
    chosen_genre = genre if genre else random.choice(_ALL_GENRES)
    page = random.randint(1, 10)
    url = f"{BASE_URL}/free-loops/genres-{chosen_genre}"
    if page > 1:
        url += f"/page-{page}"
    try:
        html = await _fetch(url)
    except HTTPException as exc:
        if exc.status_code == 404:
            url = f"{BASE_URL}/free-loops/genres-{chosen_genre}"
            html = await _fetch(url)
        else:
            raise
    listing = parse_listing_page(html, genre=chosen_genre, page=page)
    if not listing.loops:
        raise HTTPException(status_code=404, detail=f"No loops for genre '{chosen_genre}'.")
    raw = random.choice(listing.loops)

    loop = {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "bpm": raw.get("bpm"),
        "key": raw.get("key"),
        "genre": raw.get("genre"),
        "mp3_url": raw.get("mp3_url"),
        "waveform_url": raw.get("waveform_url"),
        "waveform_img_url": raw.get("waveform_img_url"),
        "detail_url": raw.get("detail_url"),
        "uploader": raw.get("uploader"),
        "tags": raw.get("tags", []),
    }

    if enrich and raw.get("detail_url"):
        try:
            html = await _fetch(raw["detail_url"])
            data = parse_detail_page(html)
            if data:
                loop["mp3_url"] = data.get("mp3_url", "")
                loop["waveform_url"] = data.get("waveform_url", "")
                loop["waveform_img_url"] = data.get("waveform_img_url", "")
                loop["tags"] = data.get("tags", [])
                loop["description"] = data.get("description", "")
        except Exception as exc:
            logger.warning("Enrich failed: %s", exc)

    return loop


@app.get("/download/{loop_id}")
async def download_loop(loop_id: int):
    match = await _find_loop(loop_id)
    if not match or not match.get("mp3_url"):
        raise HTTPException(status_code=404, detail=f"Loop {loop_id} not found.")

    content = await _fetch_bytes(match["mp3_url"])
    filename = f"rundatbeat-sample.mp3"

    return Response(
        content=content,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
            "X-Loop-Title": match.get("title", ""),
            "X-Loop-BPM": str(match.get("bpm") or ""),
            "X-Loop-Key": match.get("key") or "",
        },
    )
