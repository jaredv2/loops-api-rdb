"""
loopazon_parser.py
------------------
Scrapes and parses Loopazon.com loop listing pages into clean dicts.

Supported:
  - Genre listing: https://www.loopazon.com/free-loops/genres-<genre>
  - Genre listing paginated: https://www.loopazon.com/free-loops/genres-<genre>/page-<n>

Key differences from Looperman:
  - No auth required — download URLs are fully public
  - Preview and download are the same mp3 file
  - No CSRF, no session cookies, no getfiles complexity
  - Clean CS-Cart HTML structure

Loop card fields extracted:
  id, title, slug, detail_url, mp3_url (= download_url), waveform_url,
  waveform_img_url, uploader, uploader_url, date, bpm, key, genre,
  category, daw, downloads, comments, tags
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

BASE_URL = "https://www.loopazon.com"


# ── data model ────────────────────────────────────────────────────────────────

@dataclass
class LoopazonLoop:
    id:               Optional[int]
    title:            str
    slug:             str
    detail_url:       str
    mp3_url:          str          # public direct download — no auth needed
    waveform_url:     str          # JSON waveform data endpoint
    waveform_img_url: str          # waveform image endpoint
    uploader:         str
    uploader_url:     str
    date:             str
    bpm:              Optional[int]
    key:              Optional[str]
    genre:            Optional[str]
    category:         Optional[str]
    daw:              Optional[str]
    downloads:        int
    comments:         int
    tags:             list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LoopazonListing:
    genre:         str
    page:          int
    total_pages:   int
    loops:         list[dict] = field(default_factory=list)
    next_page_url: Optional[str] = None
    prev_page_url: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "genre":         self.genre,
            "page":          self.page,
            "total_pages":   self.total_pages,
            "next_page_url": self.next_page_url,
            "prev_page_url": self.prev_page_url,
            "loops":         self.loops,
        }


# ── helpers ───────────────────────────────────────────────────────────────────

def _int_or_none(text: str | None) -> Optional[int]:
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", str(text).strip())
    return int(cleaned) if cleaned else None


def _abs(href: str) -> str:
    if not href:
        return ""
    return href if href.startswith("http") else BASE_URL + href


def _extract_id_from_url(url: str) -> Optional[int]:
    """
    Extract the numeric product ID from a Loopazon URL.
    Handles: .../slug-368569, .../slug-368569/, .../368569
    Strips query strings before matching.
    """
    path = url.split("?")[0].rstrip("/")
    matches = re.findall(r"\d+", path)
    return int(matches[-1]) if matches else None


def _parse_bpm_key_list(item_div: Tag) -> dict:
    """Parse the .ty-bpm-key <ul> into a flat dict."""
    result: dict[str, str] = {}
    for li in item_div.find_all("li", class_="ty-bpm-key__item"):
        label_el = li.find("span", class_="ty-bpm-key__label")
        value_el = li.find("a", class_="ty-bpm-key__value")
        if not label_el:
            continue
        label = label_el.get_text(strip=True).rstrip(":").strip()
        value = value_el.get_text(strip=True) if value_el else li.get_text(strip=True).replace(label_el.get_text(), "").strip()
        if label and value:
            result[label] = value
    return result


# ── core item parser ──────────────────────────────────────────────────────────

def _parse_item(item: Tag) -> Optional[dict]:
    """Parse a single .cp-product-list__item div."""

    # ── title + detail URL ──
    title_a = item.find("a", class_="product-title")
    if not title_a:
        return None
    title     = title_a.get_text(strip=True)
    detail_url = _abs(title_a.get("href", ""))
    slug       = detail_url.rstrip("/").split("/")[-1]
    loop_id    = _extract_id_from_url(detail_url)

    logger.debug("[PARSE] Loop: %s (id=%s)", title, loop_id)

    # ── waveform wrapper — holds preview/download URL ──
    waveform_div = item.find("div", class_="ty-waveform-wrapper")
    mp3_url          = ""
    waveform_url     = ""
    waveform_img_url = ""

    if waveform_div:
        # data-ca-file-url  → direct mp3 download (public, no auth)
        raw_file = waveform_div.get("data-ca-file-url", "")
        mp3_url  = _abs(raw_file)
        # data-ca-waveform-url → waveform JSON
        waveform_url = _abs(waveform_div.get("data-ca-waveform-url", ""))

    # waveform image (separate img tag above the div)
    waveform_img_el = item.find("img", src=re.compile(r"get_waveform"))
    if waveform_img_el:
        waveform_img_url = _abs(waveform_img_el.get("src", ""))

    # fallback: parse from the download button href
    if not mp3_url:
        dl_a = item.find("a", class_="cp_download_btn")
        if dl_a:
            mp3_url = _abs(dl_a.get("href", ""))

    # ── uploader ──
    uploader_a = item.find("a", class_="cp-product__company-name")
    uploader     = uploader_a.get_text(strip=True) if uploader_a else ""
    uploader_url = _abs(uploader_a.get("href", "")) if uploader_a else ""

    # ── date ──
    date_span = item.find("span", class_="cp-product__date")
    date = date_span.get_text(strip=True) if date_span else ""
    # strip the clock icon text
    date = re.sub(r"^\s*\S+\s*", "", date).strip() if date else ""

    # ── bpm / key / genre / category / daw ──
    bpm_key_div = item.find("ul", class_="ty-bpm-key")
    meta: dict[str, str] = {}
    if bpm_key_div:
        meta = _parse_bpm_key_list(bpm_key_div)

    bpm      = _int_or_none(meta.get("BPM"))
    key      = meta.get("Key")
    genre    = meta.get("Genre")
    category = meta.get("Category")
    daw      = meta.get("DAW")

    # ── downloads + comments ──
    dl_span = item.find("span", class_="ty-product-downloads")
    downloads = _int_or_none(dl_span.get_text()) if dl_span else 0

    comments_a = item.find("a", class_="cp-product__link-reviews")
    comments = 0
    if comments_a:
        comments = _int_or_none(comments_a.get_text()) or 0

    # ── tags ──
    tags_div = item.find("div", class_="ty-tags")
    tags: list[str] = []
    if tags_div:
        tags = [a.get_text(strip=True) for a in tags_div.find_all("a") if a.get_text(strip=True)]

    return {
        "id":               loop_id,
        "title":            title,
        "slug":             slug,
        "detail_url":       detail_url,
        "mp3_url":          mp3_url,
        "waveform_url":     waveform_url,
        "waveform_img_url": waveform_img_url,
        "uploader":         uploader,
        "uploader_url":     uploader_url,
        "date":             date,
        "bpm":              bpm,
        "key":              key,
        "genre":            genre,
        "category":         category,
        "daw":              daw,
        "downloads":        downloads or 0,
        "comments":         comments,
        "tags":             tags,
    }


# ── known genres ──────────────────────────────────────────────────────────────

GENRES: dict[str, str] = {
    "8bit-chiptune": "8Bit Chiptune",
    "acid": "Acid",
    "acoustic": "Acoustic",
    "afrobeat": "Afrobeat",
    "ambient": "Ambient",
    "big-room": "Big Room",
    "blues": "Blues",
    "boom-bap": "Boom Bap",
    "breakbeat": "Breakbeat",
    "chill-out": "Chill Out",
    "cinematic": "Cinematic",
    "classical": "Classical",
    "comedy": "Comedy",
    "country": "Country",
    "crunk": "Crunk",
    "dance": "Dance",
    "dancehall": "Dancehall",
    "deep-house": "Deep House",
    "dirty": "Dirty",
    "disco": "Disco",
    "drum-and-bass": "Drum And Bass",
    "dub": "Dub",
    "dubstep": "Dubstep",
    "edm": "EDM",
    "electro": "Electro",
    "electronic": "Electronic",
    "ethnic": "Ethnic",
    "folk": "Folk",
    "funk": "Funk",
    "fusion": "Fusion",
    "garage": "Garage",
    "glitch": "Glitch",
    "grime": "Grime",
    "grunge": "Grunge",
    "hardcore": "Hardcore",
    "hardstyle": "Hardstyle",
    "heavy-metal": "Heavy Metal",
    "hip-hop": "Hip Hop",
    "house": "House",
    "indie": "Indie",
    "industrial": "Industrial",
    "jazz": "Jazz",
    "jungle": "Jungle",
    "latin": "Latin",
    "lo-fi": "Lo-Fi",
    "moombahton": "Moombahton",
    "orchestral": "Orchestral",
    "pop": "Pop",
    "psychedelic": "Psychedelic",
    "punk": "Punk",
    "rap": "Rap",
    "rave": "Rave",
    "reggae": "Reggae",
    "reggaeton": "Reggaeton",
    "religious": "Religious",
    "rnb": "RnB",
    "rock": "Rock",
    "samba": "Samba",
    "ska": "Ska",
    "soul": "Soul",
    "spoken-word": "Spoken Word",
    "techno": "Techno",
    "trance": "Trance",
    "trap": "Trap",
    "trip-hop": "Trip Hop",
    "uk-drill": "UK Drill",
    "weird": "Weird",
}


def get_genres() -> dict[str, str]:
    """Return the slug→name mapping of all known Loopazon genres."""
    return dict(GENRES)


# ── detail page parser ────────────────────────────────────────────────────────

def parse_detail_page(html: str) -> Optional[dict]:
    """
    Parse a single Loopazon loop detail page.
    Returns a dict with all loop fields plus similar_loops.
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── title ──
    title_el = soup.find("h1")
    if not title_el:
        return None
    title = title_el.get_text(strip=True)

    # ── waveform / mp3 ──
    waveform_div = soup.find("div", class_="ty-waveform-wrapper")
    mp3_url = ""
    waveform_url = ""
    if waveform_div:
        mp3_url = _abs(waveform_div.get("data-ca-file-url", ""))
        waveform_url = _abs(waveform_div.get("data-ca-waveform-url", ""))

    # fallback: download button
    if not mp3_url:
        dl_a = soup.find("a", class_="cp_download_btn")
        if dl_a:
            mp3_url = _abs(dl_a.get("href", ""))

    # waveform image
    waveform_img = soup.find("img", src=re.compile(r"get_waveform"))
    waveform_img_url = _abs(waveform_img.get("src", "")) if waveform_img else ""

    # ── uploader (text hidden inside icon span, grab from href) ──
    up_a = soup.find("a", class_="cp-product__company-name")
    uploader = ""
    uploader_url = ""
    if up_a:
        uploader_url = _abs(up_a.get("href", ""))
        uploader = uploader_url.rstrip("/").split("/")[-1] if uploader_url else ""

    # ── date ──
    date_span = soup.find("span", class_="cp-product__date")
    date = date_span.get_text(strip=True) if date_span else ""
    date = re.sub(r"^\s*\S+\s*", "", date).strip() if date else ""

    # ── metadata list (bpm/key/genre/category/daw) ──
    bpm_ul = soup.find("ul", class_="ty-bpm-key")
    meta: dict[str, str] = {}
    if bpm_ul:
        meta = _parse_bpm_key_list(bpm_ul)

    bpm = _int_or_none(meta.get("BPM"))
    key = meta.get("Key")
    genre = meta.get("Genre")
    category = meta.get("Category")
    daw = meta.get("DAW")

    # ── downloads ──
    dl_span = soup.find("span", class_="ty-product-downloads")
    downloads = _int_or_none(dl_span.get_text()) if dl_span else 0

    # ── comments ──
    comments_a = soup.find("a", class_="cp-product__link-reviews")
    comments = _int_or_none(comments_a.get_text()) if comments_a else 0

    # ── tags ──
    tags_div = soup.find("div", class_="ty-tags")
    tags: list[str] = []
    if tags_div:
        tags = [a.get_text(strip=True) for a in tags_div.find_all("a") if a.get_text(strip=True)]

    # ── description ──
    desc_div = soup.find("div", class_="ty-wysiwyg-content")
    description = desc_div.get_text(strip=True) if desc_div else ""

    # ── detail URL + slug / id ──
    canonical = soup.find("link", rel="canonical")
    detail_url = _abs(canonical.get("href", "")) if canonical else ""
    slug = detail_url.rstrip("/").split("/")[-1] if detail_url else ""
    loop_id = _extract_id_from_url(detail_url) if detail_url else None

    # ── similar loops ──
    similar_loops: list[dict] = []
    for item in soup.find_all("div", class_="cp-product-list__item"):
        parsed = _parse_item(item)
        if parsed and parsed.get("mp3_url"):
            similar_loops.append(parsed)

    return {
        "id":               loop_id,
        "title":            title,
        "slug":             slug,
        "detail_url":       detail_url,
        "mp3_url":          mp3_url,
        "waveform_url":     waveform_url,
        "waveform_img_url": waveform_img_url,
        "uploader":         uploader,
        "uploader_url":     uploader_url,
        "date":             date,
        "bpm":              bpm,
        "key":              key,
        "genre":            genre,
        "category":         category,
        "daw":              daw,
        "downloads":        downloads or 0,
        "comments":         comments,
        "tags":             tags,
        "description":      description,
        "similar_loops":    similar_loops,
    }


# ── public parser ─────────────────────────────────────────────────────────────

def parse_listing_page(html: str, genre: str = "", page: int = 1) -> LoopazonListing:
    """
    Parse a Loopazon genre listing page.
    Returns a LoopazonListing with .loops as a list of plain dicts.
    """
    logger.debug("[PARSE] parse_listing_page genre=%s page=%d", genre, page)
    soup = BeautifulSoup(html, "html.parser")

    # ── pagination ──
    pagination = soup.find("div", class_="ty-pagination")
    total_pages  = 1
    next_page_url = None
    prev_page_url = None

    if pagination:
        # All page number links
        page_links = pagination.find_all("a", class_="ty-pagination__item")
        nums = []
        for a in page_links:
            n = _int_or_none(a.get_text())
            if n:
                nums.append(n)
        if nums:
            total_pages = max(nums)

        # Next / prev
        next_a = pagination.find("a", class_="ty-pagination__next")
        prev_a = pagination.find("a", class_="ty-pagination__btn")
        if next_a and next_a.get("href"):
            next_page_url = _abs(next_a["href"])
        if prev_a and prev_a.get("href") and "prev" in str(prev_a.get("class", [])):
            prev_page_url = _abs(prev_a["href"])

    # ── loop items ──
    items = soup.find_all("div", class_="cp-product-list__item")
    logger.debug("[PARSE] Found %d .cp-product-list__item elements", len(items))

    loops: list[dict] = []
    for item in items:
        parsed = _parse_item(item)
        if parsed and parsed.get("mp3_url"):
            loops.append(parsed)

    logger.debug("[PARSE] Parsed %d loops", len(loops))

    return LoopazonListing(
        genre=genre,
        page=page,
        total_pages=total_pages,
        loops=loops,
        next_page_url=next_page_url,
        prev_page_url=prev_page_url,
    )
