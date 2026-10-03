from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import cv2
import httpx
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pyzbar.pyzbar import decode as decode_barcodes
from PIL import Image

load_dotenv()

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DATABASE_PATH", "./data/atreads.db"))
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./data/media"))
TTL_MINUTES = max(1, min(int(os.getenv("TTL_MINUTES", "15")), 15))
CLEANUP_INTERVAL = max(5, int(os.getenv("CLEANUP_INTERVAL_SECONDS", "15")))
MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_BYTES", str(10 * 1024 * 1024)))
TIMEOUT = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "15"))
ALLOWED_HOSTS = {h.strip().lower() for h in os.getenv("ALLOWED_MEDIA_HOSTS", "").split(",") if h.strip()}

DB_PATH.parent.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Universal Media ShopeePay Barcode Detector", version="2.0.0")
_stop = threading.Event()

SOURCE_HOSTS = {
    "instagram": ("instagram.com", "www.instagram.com"),
    "tiktok": ("tiktok.com", "www.tiktok.com", "vm.tiktok.com"),
    "youtube": ("youtube.com", "www.youtube.com", "youtu.be"),
    "facebook": ("facebook.com", "www.facebook.com", "fb.watch"),
    "x": ("x.com", "www.x.com", "twitter.com", "www.twitter.com"),
    "telegram": ("t.me", "telegram.me", "www.t.me"),
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(db()) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS media_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            post_id TEXT,
            media_url TEXT NOT NULL,
            media_hash TEXT,
            published_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            detected_json TEXT,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_media_expiry ON media_items(expires_at)")
        conn.commit()


def host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        return False
    if not ALLOWED_HOSTS:
        return True
    return host in ALLOWED_HOSTS or any(host.endswith("." + allowed) for allowed in ALLOWED_HOSTS)


def source_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for source, hosts in SOURCE_HOSTS.items():
        if host in hosts or any(host.endswith("." + h) for h in hosts):
            return source
    return "web"


def is_image(raw: bytes) -> bool:
    try:
        Image.open(io.BytesIO(raw)).verify()
        return True
    except Exception:
        return False


def scan_image(raw: bytes) -> list[dict]:
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    arr = np.array(image)
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    results: list[dict] = []

    qr = cv2.QRCodeDetector()
    try:
        ok, decoded_info, _, _ = qr.detectAndDecodeMulti(bgr)
        if ok:
            for value in decoded_info:
                if value:
                    results.append({"type": "qr", "format": "QR_CODE", "data": value})
        else:
            value, _, _ = qr.detectAndDecode(bgr)
            if value:
                results.append({"type": "qr", "format": "QR_CODE", "data": value})
    except cv2.error:
        pass

    try:
        for item in decode_barcodes(arr):
            data = item.data.decode("utf-8", errors="replace")
            if data:
                results.append({
                    "type": "barcode",
                    "format": item.type,
                    "data": data,
                    "rect": {
                        "left": item.rect.left,
                        "top": item.rect.top,
                        "width": item.rect.width,
                        "height": item.rect.height,
                    },
                })
    except Exception:
        pass

    unique = {}
    for item in results:
        unique[(item["type"], item["format"], item["data"])] = item
    return list(unique.values())


def scan_video(raw: bytes) -> list[dict]:
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(raw)
        path = f.name
    cap = cv2.VideoCapture(path)
    results: list[dict] = []
    try:
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        duration = frame_count / fps if fps > 0 else 0
        sample_times = sorted({0.0, max(0.0, duration * 0.25), max(0.0, duration * 0.5), max(0.0, duration * 0.75)})
        for seconds in sample_times:
            cap.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            buf = io.BytesIO()
            Image.fromarray(rgb).save(buf, format="JPEG", quality=90)
            results.extend(scan_image(buf.getvalue()))
    finally:
        cap.release()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    unique = {}
    for item in results:
        unique[(item["type"], item["format"], item["data"])] = item
    return list(unique.values())


async def download(url: str) -> tuple[bytes, str]:
    if not host_allowed(url):
        raise HTTPException(status_code=400, detail="host media tidak diizinkan")

    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; MediaShopeePayDetector/2.0)"},
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > MAX_MEDIA_BYTES:
                raise HTTPException(status_code=413, detail="media terlalu besar")
            chunks, total = [], 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise HTTPException(status_code=413, detail="media terlalu besar")
                chunks.append(chunk)
            return b"".join(chunks), response.headers.get("content-type", "").lower()


def metadata(html: str, page_url: str) -> dict:
    def meta_value(pattern: str) -> str | None:
        match = re.search(pattern, html, re.I | re.S)
        return match.group(1).strip() if match else None

    image = meta_value(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)')
    if not image:
        image = meta_value(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\']')
    video = meta_value(r'<meta[^>]+(?:property|name)=["\']og:video(?::secure_url)?["\'][^>]+content=["\']([^"\']+)')
    published = meta_value(r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|datePublished|uploadDate)["\'][^>]+content=["\']([^"\']+)')
    if not published:
        published = meta_value(r'<time[^>]+datetime=["\']([^"\']+)')
    canonical = meta_value(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)')
    return {
        "image_url": urljoin(page_url, image) if image else None,
        "video_url": urljoin(page_url, video) if video else None,
        "published_at": published,
        "canonical_url": urljoin(page_url, canonical) if canonical else page_url,
    }


class IngestRequest(BaseModel):
    source: str = Field(min_length=1, max_length=50)
    media_url: str = Field(min_length=8, max_length=4096)
    published_at: str | None = None
    post_id: str | None = Field(default=None, max_length=255)


class URLRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    source: str | None = Field(default=None, max_length=50)
    published_at: str | None = None
    post_id: str | None = Field(default=None, max_length=255)


@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=cleanup_loop, daemon=True).start()


@app.on_event("shutdown")
def shutdown():
    _stop.set()


@app.get("/health")
def health():
    with closing(db()) as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM media_items WHERE status='active' AND expires_at > ?",
            (now_utc().isoformat(),),
        ).fetchone()["n"]
    return {"ok": True, "active_items": active, "ttl_minutes": TTL_MINUTES, "sources": list(SOURCE_HOSTS)}


@app.post("/ingest")
async def ingest(payload: IngestRequest):
    current = now_utc()
    published = parse_time(payload.published_at) if payload.published_at else current
    expires = published + timedelta(minutes=TTL_MINUTES)
    if expires <= current:
        return {"accepted": False, "reason": "media sudah kadaluarsa", "expires_at": expires.isoformat()}

    raw, content_type = await download(payload.media_url)
    detections = scan_image(raw) if is_image(raw) or content_type.startswith("image/") else scan_video(raw)
    digest = hashlib.sha256(raw).hexdigest()

    with closing(db()) as conn:
        existing = conn.execute(
            "SELECT id FROM media_items WHERE media_hash=? AND status='active'",
            (digest,),
        ).fetchone()
        if existing:
            return {"accepted": True, "duplicate": True, "id": existing["id"], "detections": detections}

        cur = conn.execute(
            """INSERT INTO media_items
            (source, post_id, media_url, media_hash, published_at, expires_at, detected_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload.source,
                payload.post_id,
                payload.media_url,
                digest,
                published.isoformat(),
                expires.isoformat(),
                json.dumps(detections, ensure_ascii=False),
                current.isoformat(),
            ),
        )
        item_id = cur.lastrowid
        conn.commit()

    (MEDIA_DIR / f"{item_id}-{digest[:16]}.bin").write_bytes(raw)
    return {
        "accepted": True,
        "id": item_id,
        "source": payload.source,
        "published_at": published.isoformat(),
        "expires_at": expires.isoformat(),
        "detections": detections,
        "shopeepay_candidates": [
            x for x in detections
            if "shopee" in x.get("data", "").lower() or "shopeepay" in x.get("data", "").lower()
        ],
    }


@app.post("/ingest-url")
async def ingest_url(payload: URLRequest):
    if not host_allowed(payload.url):
        raise HTTPException(status_code=400, detail="host URL tidak diizinkan")
    raw, content_type = await download(payload.url)

    if content_type.startswith("image/") or is_image(raw):
        media_url = payload.url
        published = payload.published_at
        source = payload.source or source_from_url(payload.url)
    elif content_type.startswith("video/"):
        published = payload.published_at
        source = payload.source or source_from_url(payload.url)
        return await ingest(IngestRequest(source=source, media_url=payload.url, published_at=published, post_id=payload.post_id))
    else:
        page = metadata(raw.decode("utf-8", errors="ignore"), payload.url)
        media_url = page["image_url"] or page["video_url"]
        if not media_url:
            raise HTTPException(status_code=422, detail="halaman tidak menyediakan media publik yang bisa diproses")
        published = payload.published_at or page["published_at"]
        source = payload.source or source_from_url(payload.url)
        if not published:
            published = now_utc().isoformat()
        return await ingest(IngestRequest(source=source, media_url=media_url, published_at=published, post_id=payload.post_id))

    if not published:
        published = now_utc().isoformat()
    return await ingest(IngestRequest(source=source, media_url=media_url, published_at=published, post_id=payload.post_id))


@app.get("/items")
def items():
    current = now_utc().isoformat()
    with closing(db()) as conn:
        rows = conn.execute(
            """SELECT id, source, post_id, media_url, published_at, expires_at, detected_json, created_at
               FROM media_items WHERE status='active' AND expires_at > ? ORDER BY published_at DESC""",
            (current,),
        ).fetchall()
    return {
        "count": len(rows),
        "items": [
            {**dict(row), "detections": json.loads(row["detected_json"] or "[]")}
            for row in rows
        ],
    }


def cleanup_expired():
    current = now_utc().isoformat()
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT id, media_hash FROM media_items WHERE status='active' AND expires_at <= ?",
            (current,),
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM media_items WHERE id=?", (row["id"],))
        conn.commit()

    for row in rows:
        prefix = f"{row['id']}-{(row['media_hash'] or '')[:16]}"
        for path in MEDIA_DIR.glob(prefix + ".bin"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def cleanup_loop():
    while not _stop.wait(CLEANUP_INTERVAL):
        try:
            cleanup_expired()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
