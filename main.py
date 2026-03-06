import os
import json
import uuid
import hmac
import hashlib
import asyncio
import mimetypes
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager

import aiofiles
import aiosqlite
from pyrogram import Client
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form, Cookie, Query
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_ID = os.getenv("TELEGRAM_API_ID", "")
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_SECRET = hashlib.sha256(f"telestorage-{ADMIN_PASSWORD}".encode()).hexdigest()
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
            CREATE TABLE IF NOT EXISTS folders (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                parent_id TEXT REFERENCES folders(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id)"
        )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                size INTEGER NOT NULL,
                mime TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                folder_id TEXT REFERENCES folders(id) ON DELETE CASCADE
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
        # Add folder_id column if migrating from old schema
        try:
            await db.execute("ALTER TABLE files ADD COLUMN folder_id TEXT REFERENCES folders(id) ON DELETE CASCADE")
        except Exception:
            pass  # column already exists
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id)"
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


async def db_get_files(folder_id: str | None = None, search: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if search:
            rows = await db.execute_fetchall(
                "SELECT * FROM files WHERE filename LIKE ? ORDER BY uploaded_at DESC",
                (f"%{search}%",)
            )
        elif folder_id is not None:
            rows = await db.execute_fetchall(
                "SELECT * FROM files WHERE folder_id = ? ORDER BY uploaded_at DESC",
                (folder_id,)
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM files WHERE folder_id IS NULL ORDER BY uploaded_at DESC"
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


async def db_get_all_files() -> list[dict]:
    """Get all files regardless of folder (for admin stats)."""
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
            "INSERT INTO files (id, filename, size, mime, uploaded_at, folder_id) VALUES (?,?,?,?,?,?)",
            (record["id"], record["filename"], record["size"],
             record["mime"], record["uploaded_at"], record.get("folder_id")),
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


# ── Folder DB helpers ────────────────────────────────────────────────────────

async def db_create_folder(name: str, parent_id: str | None = None) -> dict:
    folder = {
        "id": str(uuid.uuid4()),
        "name": name,
        "parent_id": parent_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    async with aiosqlite.connect(DB_PATH) as db:
        if parent_id:
            rows = await db.execute_fetchall(
                "SELECT 1 FROM folders WHERE id = ?", (parent_id,)
            )
            if not rows:
                raise HTTPException(404, "Parent folder not found")
        await db.execute(
            "INSERT INTO folders (id, name, parent_id, created_at) VALUES (?,?,?,?)",
            (folder["id"], folder["name"], folder["parent_id"], folder["created_at"]),
        )
        await db.commit()
    return folder


async def db_get_folders(parent_id: str | None = None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if parent_id:
            rows = await db.execute_fetchall(
                "SELECT * FROM folders WHERE parent_id = ? ORDER BY name", (parent_id,)
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM folders WHERE parent_id IS NULL ORDER BY name"
            )
        return [dict(r) for r in rows]


async def db_get_folder(folder_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall(
            "SELECT * FROM folders WHERE id = ?", (folder_id,)
        )
        return dict(rows[0]) if rows else None


async def db_get_folder_breadcrumbs(folder_id: str) -> list[dict]:
    """Get breadcrumb trail from root to the given folder."""
    crumbs = []
    current_id = folder_id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        while current_id:
            rows = await db.execute_fetchall(
                "SELECT * FROM folders WHERE id = ?", (current_id,)
            )
            if not rows:
                break
            folder = dict(rows[0])
            crumbs.insert(0, folder)
            current_id = folder.get("parent_id")
    return crumbs


async def db_delete_folder(folder_id: str) -> list[dict]:
    """Recursively delete folder, subfolders, and all files. Returns tg_message_ids for cleanup."""
    all_message_ids = []

    async def _collect_and_delete(fid: str, db):
        # Collect file parts for Telegram cleanup
        parts = await db.execute_fetchall(
            "SELECT tg_message_id FROM file_parts fp "
            "JOIN files f ON fp.file_id = f.id WHERE f.folder_id = ?", (fid,)
        )
        all_message_ids.extend([dict(p)["tg_message_id"] for p in parts])

        # Delete file parts and files in this folder
        await db.execute(
            "DELETE FROM file_parts WHERE file_id IN (SELECT id FROM files WHERE folder_id = ?)", (fid,)
        )
        await db.execute("DELETE FROM files WHERE folder_id = ?", (fid,))

        # Recurse into subfolders
        subfolders = await db.execute_fetchall(
            "SELECT id FROM folders WHERE parent_id = ?", (fid,)
        )
        for sf in subfolders:
            await _collect_and_delete(dict(sf)["id"], db)

        # Delete this folder
        await db.execute("DELETE FROM folders WHERE id = ?", (fid,))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        exists = await db.execute_fetchall("SELECT 1 FROM folders WHERE id = ?", (folder_id,))
        if not exists:
            return []
        await _collect_and_delete(folder_id, db)
        await db.commit()

    return [{"tg_message_id": mid} for mid in all_message_ids]


async def db_rename_folder(folder_id: str, new_name: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT * FROM folders WHERE id = ?", (folder_id,))
        if not rows:
            return None
        await db.execute("UPDATE folders SET name = ? WHERE id = ?", (new_name, folder_id))
        await db.commit()
        return {**dict(rows[0]), "name": new_name}


# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_app):
    await init_db()
    await migrate_from_json()
    await tg.start()
    yield
    await tg.stop()


# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="TeleStorage", version="2.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
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
async def upload_file(request: Request, file: UploadFile = File(...), folder_id: str | None = Form(default=None)):
    """Upload file → split if needed → send to Telegram via MTProto."""
    if folder_id:
        folder = await db_get_folder(folder_id)
        if not folder:
            raise HTTPException(404, "Folder not found")

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
        "folder_id": folder_id,
    }

    await db_insert_file(record, parts)
    record["parts"] = parts

    base_url = str(request.base_url).rstrip("/")
    download_url = f"{base_url}/files/{record['id']}/download"

    return JSONResponse({
        "success": True,
        "file": record,
        "download_url": download_url,
    })


@app.get("/files")
async def list_files(
    folder_id: str | None = Query(default=None),
    search: str | None = Query(default=None),
    all: bool = Query(default=False),
):
    """Return files metadata. Supports folder filtering, search, and listing all."""
    if all:
        return JSONResponse({"files": await db_get_all_files()})
    if search:
        return JSONResponse({"files": await db_get_files(search=search)})
    return JSONResponse({
        "files": await db_get_files(folder_id=folder_id),
        "folders": await db_get_folders(folder_id),
        "breadcrumbs": (await db_get_folder_breadcrumbs(folder_id)) if folder_id else [],
    })


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


def _is_admin(admin_token: str | None) -> bool:
    """Check if the admin_token cookie is valid."""
    if not admin_token:
        return False
    return hmac.compare_digest(admin_token, ADMIN_SECRET)


@app.delete("/files/{file_id}")
async def delete_file(file_id: str, admin_token: str | None = Cookie(default=None)):
    """Remove file record and delete all part messages from Telegram. Admin only."""
    if not _is_admin(admin_token):
        raise HTTPException(403, "Admin access required")

    parts = await db_delete_file(file_id)
    if not parts:
        raise HTTPException(404, "File not found")

    for part in parts:
        try:
            await tg.delete_messages(int(CHAT_ID), part["tg_message_id"])
        except Exception:
            pass

    return {"success": True, "message": "File deleted"}


# ── Folder Routes ────────────────────────────────────────────────────────────

@app.post("/folders")
async def create_folder(
    name: str = Form(...),
    parent_id: str | None = Form(default=None),
):
    """Create a new folder."""
    if not name.strip():
        raise HTTPException(400, "Folder name cannot be empty")
    folder = await db_create_folder(name.strip(), parent_id or None)
    return JSONResponse({"success": True, "folder": folder})


@app.delete("/folders/{folder_id}")
async def delete_folder(folder_id: str, admin_token: str | None = Cookie(default=None)):
    """Delete folder and all contents recursively. Admin only."""
    if not _is_admin(admin_token):
        raise HTTPException(403, "Admin access required")

    parts = await db_delete_folder(folder_id)

    for part in parts:
        try:
            await tg.delete_messages(int(CHAT_ID), part["tg_message_id"])
        except Exception:
            pass

    return {"success": True, "message": "Folder deleted"}


@app.patch("/folders/{folder_id}")
async def rename_folder(folder_id: str, name: str = Form(...)):
    """Rename a folder."""
    if not name.strip():
        raise HTTPException(400, "Folder name cannot be empty")
    folder = await db_rename_folder(folder_id, name.strip())
    if not folder:
        raise HTTPException(404, "Folder not found")
    return JSONResponse({"success": True, "folder": folder})


# ── Admin Routes ─────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, admin_token: str | None = Cookie(default=None)):
    if not _is_admin(admin_token):
        return templates.TemplateResponse("admin_login.html", {"request": request})
    return templates.TemplateResponse("admin.html", {"request": request})


@app.post("/admin/login")
async def admin_login(password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        return HTMLResponse(
            '<script>alert("Password salah!");window.location="/admin";</script>'
        )
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie("admin_token", ADMIN_SECRET, httponly=True, samesite="strict")
    return response


@app.post("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie("admin_token")
    return response


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
