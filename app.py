from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

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

app = FastAPI(title="Atreads Media Barcode Bot", version="1.0.0")
_stop = threading.Event()


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
    host = (urlparse(url).hostname or "").lower()
    if urlparse(url).scheme not in {"http", "https"}:
        return False
    return not ALLOWED_HOSTS or host in ALLOWED_HOSTS


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
        # pyzbar may fail when native ZBar is unavailable.
        pass

    unique = {}
    for item in results:
        unique[(item["type"], item["format"], item["data"])] = item
    return list(unique.values())


async def download_media(url: str) -> bytes:
    if not host_allowed(url):
        raise HTTPException(status_code=400, detail="media host tidak diizinkan")

    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": "media-atreads-bot/1.0"},
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            length = response.headers.get("content-length")
            if length and int(length) > MAX_MEDIA_BYTES:
                raise HTTPException(status_code=413, detail="media terlalu besar")
            chunks = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise HTTPException(status_code=413, detail="media terlalu besar")
                chunks.append(chunk)
            return b"".join(chunks)


class IngestRequest(BaseModel):
    source: str = Field(min_length=1, max_length=50)
    media_url: str = Field(min_length=8, max_length=4096)
    published_at: str
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
    return {"ok": True, "active_items": active, "ttl_minutes": TTL_MINUTES}


@app.post("/ingest")
async def ingest(payload: IngestRequest):
    published = parse_time(payload.published_at)
    current = now_utc()

    # Media yang sudah melewati TTL tidak boleh masuk kembali.
    expires = published + timedelta(minutes=TTL_MINUTES)
    if expires <= current:
        return {"accepted": False, "reason": "media sudah kadaluarsa", "expires_at": expires.isoformat()}

    raw = await download_media(payload.media_url)
    digest = hashlib.sha256(raw).hexdigest()
    detections = scan_image(raw)

    with closing(db()) as conn:
        existing = conn.execute(
            "SELECT id FROM media_items WHERE media_hash=? AND status='active'",
            (digest,),
        ).fetchone()
        if existing:
            return {"accepted": True, "duplicate": True, "id": existing["id"], "detections": detections}

        cur = conn.execute(
            """
            INSERT INTO media_items
            (source, post_id, media_url, media_hash, published_at, expires_at, detected_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.source,
                payload.post_id,
                payload.media_url,
                digest,
                published.isoformat(),
                expires.isoformat(),
                __import__("json").dumps(detections, ensure_ascii=False),
                current.isoformat(),
            ),
        )
        item_id = cur.lastrowid
        conn.commit()

    # File lokal hanya untuk proses/arsip sementara selama TTL.
    (MEDIA_DIR / f"{item_id}-{digest[:16]}.bin").write_bytes(raw)

    return {
        "accepted": True,
        "id": item_id,
        "published_at": published.isoformat(),
        "expires_at": expires.isoformat(),
        "detections": detections,
    }


@app.get("/items")
def items():
    current = now_utc().isoformat()
    with closing(db()) as conn:
        rows = conn.execute(
            """
            SELECT id, source, post_id, media_url, published_at, expires_at,
                   detected_json, created_at
            FROM media_items
            WHERE status='active' AND expires_at > ?
            ORDER BY published_at DESC
            """,
            (current,),
        ).fetchall()

    import json
    return {
        "count": len(rows),
        "items": [
            {
                **dict(row),
                "detections": json.loads(row["detected_json"] or "[]"),
            }
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
            conn.execute(
                "DELETE FROM media_items WHERE id=?",
                (row["id"],),
            )

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
            # Worker harus tetap hidup walau satu siklus cleanup gagal.
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
