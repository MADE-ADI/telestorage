# TeleStorage 📦

Simpan file **tanpa batas** di **Telegram** — gratis. Web app + CLI untuk upload, download, dan kelola file menggunakan Telegram sebagai backend storage.

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-blue?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Telegram-MTProto-229ED9?logo=telegram&logoColor=white" alt="Telegram">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
</p>

---

## ✨ Fitur

- 🚀 **Upload hingga 2 GB** per file via Pyrogram (MTProto)
- 📦 **Chunked upload** otomatis — file besar dipecah & diupload paralel
- 🖱️ **Drag & drop** + klik untuk pilih file, bisa pilih banyak sekaligus
- 📊 **Progress real-time** — kecepatan upload, persentase, dan estimasi waktu
- ⬇️ **Download/stream** langsung dari Telegram
- 🗑️ **Hapus file** dari daftar dan Telegram sekaligus
- 🗄️ **SQLite database** — metadata tersimpan lokal dengan WAL mode
- 💻 **CLI tool** lengkap — upload, list, download, delete dari terminal
- 🔗 **Copy link** — bagikan direct download link

---

## 🏗️ Arsitektur

```
┌─────────────┐     HTTP      ┌──────────────┐    MTProto     ┌──────────┐
│  Browser /  │◄────────────►│   FastAPI     │◄─────────────►│ Telegram │
│  CLI (httpx)│              │   (main.py)   │   (Pyrogram)   │  Cloud   │
└─────────────┘              └──────┬───────┘               └──────────┘
                                    │
                              ┌─────┴─────┐
                              │  SQLite   │
                              │ (metadata)│
                              └───────────┘
```

---

## 🚀 Quick Start

### 1. Buat Telegram Bot

1. Buka Telegram, cari **@BotFather**
2. Kirim `/newbot` → ikuti instruksi → catat **Bot Token**

### 2. Dapatkan API Credentials

1. Buka **https://my.telegram.org/apps**
2. Login → buat application → catat **API ID** dan **API Hash**

### 3. Dapatkan Chat ID

**Opsi A — Channel/Group (recommended):**
1. Buat channel/group di Telegram
2. Tambahkan bot sebagai **Admin** (izin: Post Messages + Delete Messages)
3. Kirim pesan ke channel/group
4. Buka: `https://api.telegram.org/bot<TOKEN>/getUpdates`
5. Cari `"chat":{"id":...}` — biasanya negatif, misal `-1001234567890`

**Opsi B — Chat pribadi bot:**
1. Buka bot → klik Start
2. Buka: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Cari `"from":{"id":...}`

### 4. Konfigurasi

```bash
cp .env.example .env
```

Edit `.env`:
```env
TELEGRAM_BOT_TOKEN=1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
```

### 5. Install & Jalankan

```bash
python -m venv venv
source venv/bin/activate     # Linux/Mac
# venv\Scripts\activate      # Windows

pip install -r requirements.txt

uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Buka **http://localhost:8000** 🎉

> **Reverse proxy?** Pastikan max upload size ≥ 2 GB.
> Nginx: `client_max_body_size 2G;`

---

## 📁 Struktur Proyek

```
.
├── main.py              # FastAPI server + Pyrogram MTProto client
├── cli.py               # CLI tool (upload, list, download, delete)
├── requirements.txt     # Python dependencies
├── .env.example         # Template konfigurasi
├── templates/
│   └── index.html       # Web UI (drag & drop, progress bar)
└── static/              # Static assets (opsional)
```

---

## 🔌 API Endpoints

| Method   | Endpoint               | Deskripsi                      |
|----------|------------------------|--------------------------------|
| `GET`    | `/`                    | Web UI                         |
| `POST`   | `/upload`              | Upload file (multipart/form)   |
| `GET`    | `/files`               | List semua file (JSON)         |
| `GET`    | `/files/{id}/download` | Download/stream file           |
| `DELETE` | `/files/{id}`          | Hapus file + pesan di Telegram |

**Contoh upload via cURL:**

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@video.mp4"
```

---

## 💻 CLI

`cli.py` menyediakan antarmuka terminal lengkap.

### Upload

```bash
python cli.py upload file.pdf
python cli.py up video.mp4 dokumen.pdf    # banyak file
python cli.py up *.zip                     # glob
```

### List

```bash
python cli.py list    # atau: python cli.py ls
```

```
📦 3 file tersimpan:

  ID                                      NAMA                            UKURAN  TANGGAL
  ────────────────────────────────────── ────────────────────────────── ──────────  ────────────────────
  a1b2c3d4-...                            video.mp4                      1.2 GB  2026-03-02 01:55:00
  e5f6g7h8-...                            dokumen.pdf                    2.3 MB  2026-03-02 01:50:00
```

### Download

```bash
python cli.py download <ID>
python cli.py dl <ID> -o ~/Downloads      # ke folder tertentu
python cli.py dl <ID1> <ID2> <ID3>        # banyak file
```

### Hapus

```bash
python cli.py delete <ID>
python cli.py rm <ID1> <ID2> -y           # skip konfirmasi
```

### Custom Server URL

```bash
python cli.py --url http://myserver:8000 list

# atau via environment variable
export TELESTORAGE_URL=http://myserver:8000
python cli.py ls
```

---

## ⚙️ Konfigurasi

| Variabel | Deskripsi |
|----------|-----------|
| `TELEGRAM_BOT_TOKEN` | Token dari @BotFather |
| `TELEGRAM_CHAT_ID` | ID channel/group/user untuk menyimpan file |
| `TELEGRAM_API_ID` | API ID dari my.telegram.org |
| `TELEGRAM_API_HASH` | API Hash dari my.telegram.org |

---

## 📝 Catatan

- Telegram MTProto mendukung file hingga **2 GB** (4 GB untuk Premium)
- File besar otomatis dipecah menjadi chunk ≤ 1.9 GB dan diupload paralel
- Metadata file disimpan di **SQLite** (`telestorage.db`) — bukan JSON
- File yang dihapus juga dihapus dari Telegram
- **Jangan commit** file `.env`, `telestorage.db`, dan `*.session`

---

## 📄 License

MIT
