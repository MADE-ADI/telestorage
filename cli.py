#!/usr/bin/env python3
"""TeleStorage CLI — upload, list, download, and delete files from the terminal."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urljoin

try:
    import httpx
except ImportError:
    print("Install httpx dulu: pip install httpx")
    sys.exit(1)

DEFAULT_BASE = "http://localhost:8000"


def get_base_url():
    return os.getenv("TELESTORAGE_URL", DEFAULT_BASE).rstrip("/")


def fmt_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 ** 2:
        return f"{b / 1024:.1f} KB"
    if b < 1024 ** 3:
        return f"{b / 1024 ** 2:.1f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def cmd_upload(args):
    base = get_base_url()
    for filepath in args.files:
        path = Path(filepath)
        if not path.exists():
            print(f"❌ File tidak ditemukan: {filepath}")
            continue

        size = path.stat().st_size
        print(f"📤 Mengupload: {path.name} ({fmt_size(size)})")

        with open(path, "rb") as f:
            with httpx.Client(timeout=None) as client:
                resp = client.post(
                    f"{base}/upload",
                    files={"file": (path.name, f)},
                )

        if resp.status_code == 200:
            data = resp.json()
            rec = data["file"]
            print(f"   ✅ Berhasil! ID: {rec['id']}")
        else:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            print(f"   ❌ Gagal: {detail}")


def cmd_list(args):
    base = get_base_url()
    with httpx.Client(timeout=30) as client:
        resp = client.get(f"{base}/files")

    files = resp.json()["files"]
    if not files:
        print("📭 Belum ada file yang diupload.")
        return

    print(f"📦 {len(files)} file tersimpan:\n")
    print(f"  {'ID':<38} {'NAMA':<30} {'UKURAN':>10}  TANGGAL")
    print(f"  {'─' * 38} {'─' * 30} {'─' * 10}  {'─' * 20}")

    for f in files:
        fid = f["id"]
        name = f["filename"][:30]
        size = fmt_size(f["size"])
        date = f["uploaded_at"][:19].replace("T", " ")
        print(f"  {fid}  {name:<30} {size:>10}  {date}")


def cmd_download(args):
    base = get_base_url()
    for file_id in args.ids:
        print(f"⏳ Mendownload {file_id}...")

        with httpx.Client(timeout=None, follow_redirects=True) as client:
            with client.stream("GET", f"{base}/files/{file_id}/download") as resp:
                if resp.status_code != 200:
                    print(f"   ❌ Gagal: {resp.status_code}")
                    continue

                cd = resp.headers.get("content-disposition", "")
                if 'filename="' in cd:
                    filename = cd.split('filename="')[1].rstrip('"')
                else:
                    filename = f"{file_id}"

                out_dir = Path(args.output) if args.output else Path(".")
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / filename

                total = int(resp.headers.get("content-length", 0))
                downloaded = 0

                with open(out_path, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded / total * 100
                            print(f"\r   📥 {fmt_size(downloaded)} / {fmt_size(total)} ({pct:.1f}%)", end="", flush=True)
                        else:
                            print(f"\r   📥 {fmt_size(downloaded)}", end="", flush=True)

                print(f"\n   ✅ Tersimpan: {out_path}")


def cmd_delete(args):
    base = get_base_url()
    for file_id in args.ids:
        if not args.yes:
            confirm = input(f"🗑️  Hapus {file_id}? [y/N] ").strip().lower()
            if confirm != "y":
                print("   Dibatalkan.")
                continue

        with httpx.Client(timeout=30) as client:
            resp = client.delete(f"{base}/files/{file_id}")

        if resp.status_code == 200:
            print(f"   ✅ {file_id} dihapus.")
        else:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            print(f"   ❌ Gagal: {detail}")


def main():
    parser = argparse.ArgumentParser(
        prog="telestorage",
        description="TeleStorage CLI — kelola file di Telegram dari terminal",
    )
    parser.add_argument(
        "--url", help="Base URL server (default: http://localhost:8000)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # upload
    p_up = sub.add_parser("upload", aliases=["up"], help="Upload file")
    p_up.add_argument("files", nargs="+", help="File yang akan diupload")

    # list
    sub.add_parser("list", aliases=["ls"], help="Tampilkan semua file")

    # download
    p_dl = sub.add_parser("download", aliases=["dl"], help="Download file")
    p_dl.add_argument("ids", nargs="+", help="ID file yang akan didownload")
    p_dl.add_argument("-o", "--output", help="Folder output (default: direktori saat ini)")

    # delete
    p_rm = sub.add_parser("delete", aliases=["rm"], help="Hapus file")
    p_rm.add_argument("ids", nargs="+", help="ID file yang akan dihapus")
    p_rm.add_argument("-y", "--yes", action="store_true", help="Skip konfirmasi")

    args = parser.parse_args()

    if args.url:
        os.environ["TELESTORAGE_URL"] = args.url

    commands = {
        "upload": cmd_upload, "up": cmd_upload,
        "list": cmd_list, "ls": cmd_list,
        "download": cmd_download, "dl": cmd_download,
        "delete": cmd_delete, "rm": cmd_delete,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
