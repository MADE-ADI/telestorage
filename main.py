import os
import json
import uuid
import asyncio
import mimetypes
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

import aiofiles
import aiosqlite
from pyrogram import Client
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_ID = os.getenv("TELEGRAM_API_ID", "")
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
DB_PATH = "telestorage.db"
JSON_DB_PATH = Path("files_db.json")
CHUNK_SIZE = 1900 * 1024 * 1024  # 1.9 GB per chunk (safely under 2GB bot limit)
IO_BUFFER = 8 * 1024 * 1024  # 8 MB I/O buffer
MAX_PARALLEL = 5  # max concurrent chunk uploads/downloads per request
TEMP_DIR = Path(tempfile.gettempdir()) / "telestorage"
TEMP_DIR.mkdir(exist_ok=True)

if not all([BOT_TOKEN, CHAT_ID, API_ID, API_HASH]):
    raise RuntimeError(
        "Missing env vars. Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, "
        "TELEGRAM_API_ID, and TELEGRAM_API_HASH in .env"
    )

# ── Pyrogram client ─────────────────────────────────────────────────────────
tg = Client(
    "telestorage_bot",
    api_id=int(API_ID),
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8,
    max_concurrent_transmissions=MAX_PARALLEL,
)


# ── SQLite database ─────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                mime TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS file_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                part INTEGER NOT NULL,
                size INTEGER NOT NULL,
                tg_file_id TEXT NOT NULL,
                tg_message_id INTEGER NOT NULL,
                UNIQUE(file_id, part)
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_parts_file ON file_parts(file_id)"
        )
        await db.commit()


async def migrate_from_json():
    """Migrate existing files_db.json data to SQLite (one-time)."""
    if not JSON_DB_PATH.exists():
        return
    try:
        records = json.loads(JSON_DB_PATH.read_text())
    except (json.JSONDecodeError, Exception):
        return
    if not records:
        JSON_DB_PATH.rename(JSON_DB_PATH.with_suffix(".json.bak"))
        return

    async with aiosqlite.connect(DB_PATH) as db:
        for r in records:
            existing = await db.execute_fetchall(
                "SELECT 1 FROM files WHERE id = ?", (r["id"],)
            )
            if existing:
                continue

            await db.execute(
                "INSERT INTO files (id, filename, size, mime, uploaded_at) VALUES (?,?,?,?,?)",
                (r["id"], r["filename"], r["size"], r["mime"], r["uploaded_at"]),
            )

            parts = r.get("parts", [])
            # Backward compat: old records without parts list
            if not parts and r.get("tg_message_id"):
                parts = [{
                    "part": 1,
                    "size": r["size"],
                    "tg_file_id": r.get("tg_file_id", ""),
                    "tg_message_id": r["tg_message_id"],
                }]

            for p in parts:
                await db.execute(
                    "INSERT INTO file_parts (file_id, part, size, tg_file_id, tg_message_id) "
                    "VALUES (?,?,?,?,?)",
                    (r["id"], p["part"], p["size"], p["tg_file_id"], p["tg_message_id"]),
                )
        await db.commit()

    JSON_DB_PATH.rename(JSON_DB_PATH.with_suffix(".json.bak"))


async def db_get_files() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM files ORDER BY uploaded_at DESC"
        )
        files = []
        for row in rows:
            parts = await db.execute_fetchall(
                "SELECT part, size, tg_file_id, tg_message_id FROM file_parts "
                "WHERE file_id = ? ORDER BY part", (row["id"],)
            )
            files.append({
                **dict(row),
                "parts": [dict(p) for p in parts],
            })
        return files


async def db_get_file(file_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM files WHERE id = ?", (file_id,)
        )
        if not rows:
            return None
        row = rows[0]
        parts = await db.execute_fetchall(
            "SELECT part, size, tg_file_id, tg_message_id FROM file_parts "
            "WHERE file_id = ? ORDER BY part", (file_id,)
        )
        return {**dict(row), "parts": [dict(p) for p in parts]}


async def db_insert_file(record: dict, parts: list[dict]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO files (id, filename, size, mime, uploaded_at) VALUES (?,?,?,?,?)",
            (record["id"], record["filename"], record["size"],
             record["mime"], record["uploaded_at"]),
        )
        for p in parts:
            await db.execute(
                "INSERT INTO file_parts (file_id, part, size, tg_file_id, tg_message_id) "
                "VALUES (?,?,?,?,?)",
                (record["id"], p["part"], p["size"], p["tg_file_id"], p["tg_message_id"]),
            )
        await db.commit()


async def db_delete_file(file_id: str) -> list[dict]:
    """Delete file and return its parts (for Telegram cleanup)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        parts = await db.execute_fetchall(
            "SELECT tg_message_id FROM file_parts WHERE file_id = ?", (file_id,)
        )
        if not parts:
            return []
        await db.execute("DELETE FROM file_parts WHERE file_id = ?", (file_id,))
        await db.execute("DELETE FROM files WHERE id = ?", (file_id,))
        await db.commit()
        return [dict(p) for p in parts]


# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app):
    await init_db()
    await migrate_from_json()
    await tg.start()
    yield
    await tg.stop()


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="TeleStorage", version="2.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Chunked upload helper ───────────────────────────────────────────────────
async def _upload_one_chunk(chunk_path: Path, caption: str) -> dict:
    """Upload a single chunk file to Telegram."""
    message = await tg.send_document(
        chat_id=int(CHAT_ID),
        document=str(chunk_path),
        caption=caption,
        force_document=True,
    )
    return {
        "tg_file_id": message.document.file_id,
        "tg_message_id": message.id,
    }


async def split_and_upload(temp_path: Path, filename: str) -> list[dict]:
    """Split file into chunks and upload to Telegram in parallel."""
    file_size = temp_path.stat().st_size
    total_parts = -(-file_size // CHUNK_SIZE)

    chunk_infos = []
    async with aiofiles.open(temp_path, "rb") as f:
        for part_num in range(1, total_parts + 1):
            chunk_data = await f.read(CHUNK_SIZE)
            if not chunk_data:
                break
            part_name = f"{filename}.part{part_num:03d}" if total_parts > 1 else filename
            chunk_path = TEMP_DIR / f"{uuid.uuid4()}_{part_name}"
            async with aiofiles.open(chunk_path, "wb") as cf:
                await cf.write(chunk_data)

            caption = (
                f"📁 {filename}\n📦 Part {part_num}/{total_parts} "
                f"({len(chunk_data) / (1024**3):.2f} GB)"
                if total_parts > 1
                else f"📁 {filename}"
            )
            chunk_infos.append({
                "part": part_num,
                "size": len(chunk_data),
                "chunk_path": chunk_path,
                "caption": caption,
            })

    sem = asyncio.Semaphore(MAX_PARALLEL)
    parts = [None] * len(chunk_infos)

    async def upload_with_sem(idx, info):
        async with sem:
            try:
                result = await _upload_one_chunk(info["chunk_path"], info["caption"])
                parts[idx] = {
                    "part": info["part"],
                    "size": info["size"],
                    **result,
                }
            finally:
                info["chunk_path"].unlink(missing_ok=True)

    await asyncio.gather(*(upload_with_sem(i, ci) for i, ci in enumerate(chunk_infos)))
    return parts


# ── Parallel download helper ────────────────────────────────────────────────
async def _download_part_to_temp(part: dict) -> Path:
    """Download one part from Telegram to a temp file."""
    message = await tg.get_messages(int(CHAT_ID), part["tg_message_id"])
    if not message or not message.document:
        raise HTTPException(404, f"Part {part['part']} not found on Telegram")

    temp_path = TEMP_DIR / f"dl_{uuid.uuid4()}"
    await tg.download_media(message, file_name=str(temp_path))
    return temp_path


# ── Routes ───────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload file → split if needed → send to Telegram via MTProto."""
    temp_path = TEMP_DIR / f"{uuid.uuid4()}_{file.filename}"
    size = 0

    try:
        async with aiofiles.open(temp_path, "wb") as f:
            while chunk := await file.read(IO_BUFFER):
                size += len(chunk)
                await f.write(chunk)

        parts = await split_and_upload(temp_path, file.filename)
    finally:
        temp_path.unlink(missing_ok=True)

    mime = (
        file.content_type
        or mimetypes.guess_type(file.filename)[0]
        or "application/octet-stream"
    )

    record = {
        "id": str(uuid.uuid4()),
        "filename": file.filename,
        "size": size,
        "mime": mime,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }

    await db_insert_file(record, parts)
    record["parts"] = parts

    return JSONResponse({"success": True, "file": record})


@app.get("/files")
async def list_files():
    """Return all uploaded files metadata."""
    return JSONResponse({"files": await db_get_files()})


@app.get("/files/{file_id}/download")
async def download_file(file_id: str):
    """Download parts in parallel to temp, then stream to client."""
    record = await db_get_file(file_id)
    if not record:
        raise HTTPException(404, "File not found")

    parts = record.get("parts", [])
    if not parts:
        raise HTTPException(404, "File parts not found")

    sorted_parts = sorted(parts, key=lambda p: p["part"])

    # Single part: stream directly
    if len(sorted_parts) == 1:
        message = await tg.get_messages(int(CHAT_ID), sorted_parts[0]["tg_message_id"])
        if not message or not message.document:
            raise HTTPException(404, "File not found on Telegram")

        async def stream_single():
            async for chunk in tg.stream_media(message):
                yield chunk

        headers = {
            "Content-Disposition": f'attachment; filename="{record["filename"]}"',
            "Content-Type": record["mime"],
            "Content-Length": str(record["size"]),
        }
        return StreamingResponse(stream_single(), headers=headers)

    # Multi-part: download all parts in parallel, then stream
    sem = asyncio.Semaphore(MAX_PARALLEL)
    temp_files = [None] * len(sorted_parts)

    async def dl_with_sem(idx, part):
        async with sem:
            temp_files[idx] = await _download_part_to_temp(part)

    await asyncio.gather(*(dl_with_sem(i, p) for i, p in enumerate(sorted_parts)))

    async def stream_parts():
        try:
            for tf in temp_files:
                async with aiofiles.open(tf, "rb") as f:
                    while chunk := await f.read(IO_BUFFER):
                        yield chunk
        finally:
            for tf in temp_files:
                if tf:
                    tf.unlink(missing_ok=True)

    headers = {
        "Content-Disposition": f'attachment; filename="{record["filename"]}"',
        "Content-Type": record["mime"],
        "Content-Length": str(record["size"]),
    }
    return StreamingResponse(stream_parts(), headers=headers)


@app.delete("/files/{file_id}")
async def delete_file(file_id: str):
    """Remove file record and delete all part messages from Telegram."""
    parts = await db_delete_file(file_id)
    if not parts:
        raise HTTPException(404, "File not found")

    for part in parts:
        try:
            await tg.delete_messages(int(CHAT_ID), part["tg_message_id"])
        except Exception:
            pass

    return {"success": True, "message": "File deleted"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
