#!/usr/bin/env python3
"""
Háztartási Dokumentum AI Kategorizáló, OpenAI OAuth Proxy és JSON Export Daemon
Támogatott AI motorok:
  1. openai-oauth (ChatGPT Plus/Pro előfizetés OAuth-on keresztül, alapértelmezett: gpt-6-luna)
  2. ollama (Helyi AI Core: http://192.168.0.77:11434, gemma3:4b)
  3. openai (Klasszikus fizetős OpenAI API kulcs)
  4. gemini (Google Gemini API)

Beépített OpenAI-kompatibilis OAuth Proxy:
  http://paperless-household-agent:3006/v1 (Paperless-AI és egyéb konténerek számára)
"""

import base64
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PAPERLESS_URL = os.environ.get("PAPERLESS_URL", "http://paperless-ngx:8000").rstrip("/")
PAPERLESS_USER = os.environ.get("PAPERLESS_ADMIN_USER", "admin")
PAPERLESS_PASS = os.environ.get("PAPERLESS_ADMIN_PASSWORD", "menti2026")
PAPERLESS_TOKEN = os.environ.get("PAPERLESS_API_TOKEN", "").strip()

AI_PROVIDER = os.environ.get("AI_PROVIDER", "openai-oauth").lower()  # openai-oauth | auto | ollama | openai | gemini

CODEX_AUTH_FILE = Path(os.environ.get("CODEX_AUTH_FILE", "/oauth/auth.json"))
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-6-luna")
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
OAUTH_PROXY_PORT = int(os.environ.get("OAUTH_PROXY_PORT", "3006"))

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://192.168.0.77:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:4b")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

JSON_OUTPUT_DIR = Path(os.environ.get("JSON_OUTPUT_DIR", "/json_output"))
CONSUME_DIR = Path(os.environ.get("CONSUME_DIR", "/consume"))
UNRAID_PUBLIC_BASE = os.environ.get("UNRAID_PUBLIC_BASE", "/mnt/user/_HERMES_WORKDIR/DOKUMENTUMOK")
PAPERLESS_PUBLIC_URL = os.environ.get("PAPERLESS_PUBLIC_URL", "http://192.168.0.32:8010").rstrip("/")
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL_SECONDS", "15"))

_oauth_lock = threading.Lock()

# Többoldalas szkenner puffer mappák és zárolás (Paperless consume-on KÍVÜL, hogy ne egye meg korán!)
STAGING_DIR = Path(os.environ.get("STAGING_DIR", "/staging"))
STAGING_PAGES_DIR = STAGING_DIR / "pages"
STAGING_FINISH_DIR = STAGING_DIR / "finish"
TARGET_CONSUME_DIR = CONSUME_DIR / "00_Automata_AI_Kategorizalas"
_staging_lock = threading.RLock()

# pypdf, pypdfium2 és pillow dinamikus biztosítása az összefűzéshez és előnézethez
pypdf_dir = Path("/tmp/pylib")
pypdf_dir.mkdir(parents=True, exist_ok=True)
import sys
if str(pypdf_dir) not in sys.path:
    sys.path.insert(0, str(pypdf_dir))

for mod_name, pkg_name in [("pypdf", "pypdf"), ("pypdfium2", "pypdfium2"), ("PIL", "pillow")]:
    try:
        __import__(mod_name)
    except ImportError:
        try:
            import subprocess
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--target", str(pypdf_dir), pkg_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            __import__(mod_name)
        except Exception as _e:
            log(f"Hiba a {pkg_name} dinamikus telepítésekor: {_e}")

# Háztartási mappák a consume könyvtárban
CONSUME_SUBFOLDERS = [
    "00_Automata_AI_Kategorizalas",
    "01_Rezsi_es_Szamlak",
    "02_Bank_Hitel_Penzugy",
    "03_Ado_es_Hivatalos_Iratok",
    "04_Haz_Ingatlan_Felujitas",
    "05_Garancia_Blokk_Keszulekek",
    "06_Auto_es_Kozlekedes",
    "07_Biztositasok",
    "08_Egeszsegugy_Orvosi_Leletek",
    "09_Munka_Ber_Oktatas",
    "10_Szemelyes_Okiratok_Egyeb",
]

FOLDER_TAG_TO_DOCTYPE = {
    "01_rezsi_es_szamlak": "Rezsi és Közüzemi számla",
    "02_bank_hitel_penzugy": "Bank, Hitel és Pénzügy",
    "03_ado_es_hivatalos_iratok": "Adó és Hivatalos irat",
    "04_haz_ingatlan_felujitas": "Ház, Ingatlan és Felújítás",
    "05_garancia_blokk_keszulekek": "Garancialevél és Készülékek",
    "06_auto_es_kozlekedes": "Autó és Közlekedés",
    "07_biztositasok": "Biztosítás",
    "08_egeszsegugy_orvosi_leletek": "Egészségügy és Orvosi lelet",
    "09_munka_ber_oktatas": "Munka, Bér és Oktatás",
    "10_szemelyes_okiratok_egyeb": "Személyes okirat és Egyéb",
}

HOUSEHOLD_DOCUMENT_TYPES = [
    "Rezsi és Közüzemi számla",
    "Bank, Hitel és Pénzügy",
    "Adó és Hivatalos irat",
    "Ház, Ingatlan és Felújítás",
    "Garancialevél és Készülékek",
    "Autó és Közlekedés",
    "Biztosítás",
    "Egészségügy és Orvosi lelet",
    "Munka, Bér és Oktatás",
    "Személyes okirat és Egyéb",
]

HOUSEHOLD_TAGS = [
    ("ai-feldolgozva", "#1d7afc"),
    ("fizetendo", "#e53935"),
    ("befizetve", "#43a047"),
    ("fontos", "#fb8c00"),
    ("garancia", "#8e24aa"),
    ("rezsi", "#00acc1"),
    ("szamla", "#3949ab"),
    ("szerzodes", "#5e35b1"),
    ("orvosi", "#d81b60"),
    ("auto", "#546e7a"),
    ("ingatlan", "#6d4c41"),
    ("bank", "#2e7d32"),
    ("ado", "#c62828"),
]

HOUSEHOLD_CORRESPONDENTS = [
    "MVM Next Energiakereskedelmi Zrt.",
    "Magyar Telekom Nyrt.",
    "Yettel Magyarország Zrt.",
    "One Magyarország Zrt.",
    "MOHU Hulladékgazdálkodási Zrt.",
    "Fővárosi Vízművek / Regionális Vízművek",
    "Nemzeti Adó- és Vámhivatal (NAV)",
    "OTP Bank Nyrt.",
    "MBH Bank Nyrt.",
    "Revolut Bank UAB",
    "Allianz Hungária Zrt.",
    "Generali Biztosító Zrt.",
    "EESZT / Egészségügyi Intézmény",
    "Magyar Államkincstár",
]

CUSTOM_FIELD_DEFS = [
    ("Összeg", "string"),
    ("Összeg (Ft)", "integer"),
    ("Fizetési határidő", "date"),
    ("Garancia lejárata", "date"),
    ("Számlaszám / Ügyiratszám", "string"),
    ("Variabilný symbol / Közlemény", "string"),
    ("AI Összefoglaló", "string"),
]


def log(msg: str):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def slugify(value: str, max_len: int = 70) -> str:
    value = unicodedata.normalize("NFKD", str(value or "dokumentum"))
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")
    return (value[:max_len] or "dokumentum").strip("_")


def safe_folder_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", str(value or "Egyeb")).strip()
    return cleaned or "Egyeb"


def ensure_consume_folders():
    try:
        CONSUME_DIR.mkdir(parents=True, exist_ok=True)
        for sub in CONSUME_SUBFOLDERS:
            d = CONSUME_DIR / sub
            d.mkdir(parents=True, exist_ok=True)
            keep = d / ".keep"
            if not keep.exists():
                keep.write_text("keep\n", encoding="utf-8")
        STAGING_PAGES_DIR.mkdir(parents=True, exist_ok=True)
        STAGING_FINISH_DIR.mkdir(parents=True, exist_ok=True)
        TARGET_CONSUME_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f"Consume mappa ellenőrzési hiba: {e}")


# ============================================================================
# EPSON TÖBBOLDALAS SZKENNER PUFFER ÉS ÖSSZEFŰZŐ LOGIKA (NINCS TIMEOUT!)
# ============================================================================

LAST_EPSON_SCAN_FILE = JSON_OUTPUT_DIR / "last_epson_scan.json"
LAST_GMAIL_SYNC_FILE = JSON_OUTPUT_DIR / "last_gmail_sync.json"
_last_buffer_action = "Készenlétben"


def load_last_epson_scan() -> dict:
    try:
        if LAST_EPSON_SCAN_FILE.exists():
            return json.loads(LAST_EPSON_SCAN_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_last_epson_scan(data: dict):
    try:
        JSON_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        LAST_EPSON_SCAN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"Hiba a last_epson_scan.json mentésekor: {e}")


def load_last_gmail_sync() -> dict:
    try:
        if LAST_GMAIL_SYNC_FILE.exists():
            return json.loads(LAST_GMAIL_SYNC_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def get_buffer_pages() -> list[Path]:
    with _staging_lock:
        if not STAGING_PAGES_DIR.exists():
            return []
        return sorted(
            [f for f in STAGING_PAGES_DIR.iterdir() if f.is_file() and not f.name.startswith(".")],
            key=lambda x: x.name,
        )


def get_empty_placeholder_jpeg(text: str = "A puffer üres") -> bytes:
    try:
        import io
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (700, 360), color=(26, 28, 30))
        d = ImageDraw.Draw(img)
        d.text((40, 160), text, fill=(180, 190, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return b""


def render_page_thumbnail(page_path: Path, scale: float = 1.3) -> bytes:
    try:
        import io, sys
        pypdf_dir = Path("/tmp/pylib")
        if str(pypdf_dir) not in sys.path:
            sys.path.insert(0, str(pypdf_dir))
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(page_path))
        if len(pdf) == 0:
            raise ValueError("Üres PDF oldal")
        page = pdf[0]
        image = page.render(scale=scale).to_pil()
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=82)
        return buf.getvalue()
    except Exception as e:
        log(f"Előnézet renderelési hiba ({page_path}): {e}")
        return get_empty_placeholder_jpeg(f"Renderelési hiba: {e}")


def get_merged_buffer_pdf() -> bytes:
    with _staging_lock:
        pages = get_buffer_pages()
        import io, pypdf
        writer = pypdf.PdfWriter()
        if not pages:
            writer.add_blank_page(width=595, height=842)
            buf = io.BytesIO()
            writer.write(buf)
            return buf.getvalue()
        for p in pages:
            try:
                reader = pypdf.PdfReader(str(p))
                for pg in reader.pages:
                    writer.add_page(pg)
            except Exception as e:
                log(f"PDF összefűzési hiba előnézethez ({p.name}): {e}")
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()


def rotate_page_by_index(page_num: int) -> dict:
    """1-indexed oldal forgatása 180 fokkal."""
    global _last_buffer_action
    with _staging_lock:
        pages = get_buffer_pages()
        if page_num < 1 or page_num > len(pages):
            return {"status": "error", "message": f"Érvénytelen oldalszám: {page_num}"}
        target_pdf = pages[page_num - 1]
        try:
            import pypdf
            reader = pypdf.PdfReader(str(target_pdf))
            writer = pypdf.PdfWriter()
            for pg in reader.pages:
                pg.rotate(180)
                writer.add_page(pg)
            tmp_pdf = target_pdf.with_suffix(".rot.pdf")
            with open(tmp_pdf, "wb") as f_out:
                writer.write(f_out)
            tmp_pdf.replace(target_pdf)
            _last_buffer_action = f"{page_num}. oldal 180°-kal elforgatva"
            log(f"[Szkenner Puffer] {_last_buffer_action}: {target_pdf.name}")
            return {"status": "rotated", "page": page_num, "filename": target_pdf.name, "message": _last_buffer_action}
        except Exception as e:
            _last_buffer_action = f"Forgatási hiba ({page_num}. oldal): {e}"
            log(f"[Szkenner Puffer] {e}")
            return {"status": "error", "error": str(e), "message": _last_buffer_action}


def delete_page_by_index(page_num: int) -> dict:
    """1-indexed oldal törlése."""
    global _last_buffer_action
    with _staging_lock:
        pages = get_buffer_pages()
        if page_num < 1 or page_num > len(pages):
            return {"status": "error", "message": f"Érvénytelen oldalszám: {page_num}"}
        target_pdf = pages[page_num - 1]
        name = target_pdf.name
        try:
            target_pdf.unlink()
            remaining = len(pages) - 1
            _last_buffer_action = f"{page_num}. oldal törölve ({remaining} maradt)"
            log(f"[Szkenner Puffer] {_last_buffer_action}: {name}")
            return {"status": "deleted", "page": page_num, "deleted_file": name, "remaining": remaining, "message": _last_buffer_action}
        except Exception as e:
            _last_buffer_action = f"Törlési hiba ({page_num}. oldal): {e}"
            log(f"[Szkenner Puffer] {e}")
            return {"status": "error", "error": str(e), "message": _last_buffer_action}


def move_page_by_index(page_num: int, direction: str) -> dict:
    """1-indexed oldal mozgatása ('up' = előbbre, 'down' = hátrébb)."""
    global _last_buffer_action
    with _staging_lock:
        pages = get_buffer_pages()
        total = len(pages)
        if page_num < 1 or page_num > total:
            return {"status": "error", "message": f"Érvénytelen oldalszám: {page_num}"}
        idx = page_num - 1
        if direction == "up" and idx > 0:
            other_idx = idx - 1
        elif direction == "down" and idx < total - 1:
            other_idx = idx + 1
        else:
            return {"status": "noop", "message": "Nem mozgatható tovább"}

        p1, p2 = pages[idx], pages[other_idx]
        b1, b2 = p1.read_bytes(), p2.read_bytes()
        p1.write_bytes(b2)
        p2.write_bytes(b1)
        _last_buffer_action = f"{page_num}. oldal felcserélve ({direction})"
        log(f"[Szkenner Puffer] {_last_buffer_action}: {p1.name} <-> {p2.name}")
        return {"status": "moved", "from": page_num, "to": other_idx + 1, "message": _last_buffer_action}


def generate_preview_html() -> str:
    pages = get_buffer_pages()
    total_pages = len(pages)
    ts = int(time.time())

    cards_html = []
    for i, p in enumerate(pages, 1):
        size_kb = max(1, round(p.stat().st_size / 1024)) if p.exists() else 0
        up_disabled = "disabled style='opacity:0.3;pointer-events:none;'" if i == 1 else ""
        down_disabled = "disabled style='opacity:0.3;pointer-events:none;'" if i == total_pages else ""

        card = f"""
        <div class="page-card" id="page-card-{i}">
            <div class="page-header">
                <div class="page-badge">📄 <span>{i}. oldal</span></div>
                <div class="page-meta">{p.name} · {size_kb} KB</div>
            </div>
            <div class="img-wrapper" onclick="openModal('/api/scan/page/{i}/image?t={ts}')">
                <img src="/api/scan/page/{i}/image?t={ts}" alt="{i}. oldal előnézete" loading="lazy" />
                <div class="img-zoom-hint">🔍 Nagyítás érintéssel</div>
            </div>
            <div class="page-toolbar">
                <div class="toolbar-group">
                    <button class="btn btn-secondary" onclick="postAction('/api/scan/page/{i}/rotate')">🔄 180° Forgatás</button>
                    <button class="btn btn-red" onclick="postAction('/api/scan/page/{i}/delete', 'Biztosan törölni szeretnéd a(z) {i}. oldalt?')">🗑️ Lap törlése</button>
                </div>
                <div class="toolbar-group">
                    <button class="btn btn-secondary btn-icon" {up_disabled} onclick="postAction('/api/scan/page/{i}/move_up')" title="Előbbre mozgatás">⬆️</button>
                    <button class="btn btn-secondary btn-icon" {down_disabled} onclick="postAction('/api/scan/page/{i}/move_down')" title="Hátrébb mozgatás">⬇️</button>
                </div>
            </div>
        </div>
        """
        cards_html.append(card)

    if not cards_html:
        content_body = """
        <div class="empty-state">
            <div class="empty-icon">🖨️</div>
            <div class="empty-title">A szkenner puffer üres</div>
            <div class="empty-desc">
                Helyezz egy dokumentumot az Epson szkenner síkágyára, majd a nyomtató kijelzőjén válaszd a <b>+ Oldal hozzaad</b> opciót a többoldalas gyűjtéshez!
            </div>
            <div style="display:flex;gap:12px;justify-content:center;flex-wrap:wrap;">
                <button class="btn btn-blue" onclick="location.reload()">🔄 Frissítés</button>
                <a class="btn btn-secondary" href="http://192.168.0.99:8123/szkenner">⬅️ Vissza a Home Assistanthoz</a>
            </div>
        </div>
        """
        header_actions = '<a class="btn btn-secondary" href="http://192.168.0.99:8123/szkenner">⬅️ Vissza</a>'
    else:
        content_body = f"""
        <div class="top-actions">
            <button class="btn btn-green" onclick="postAction('/api/scan/finish', 'Biztosan lezárod és beküldöd mind a(z) {total_pages} oldalt a Paperless-be?')">✅ Befejezés & Beküldés ({total_pages} oldal)</button>
            <a class="btn btn-blue" href="/api/scan/preview.pdf" target="_blank">📄 Teljes összefűzött PDF</a>
            <button class="btn btn-secondary" onclick="postAction('/api/scan/clear', 'Biztosan törölni szeretnéd a TELJES puffert?')">🗑️ Puffer ürítése</button>
            <button class="btn btn-secondary btn-icon" onclick="location.reload()" title="Frissítés">🔄</button>
        </div>
        <div class="pages-feed">
            {''.join(cards_html)}
        </div>
        <div style="text-align:center;margin-top:20px;">
            <button class="btn btn-green" style="padding:14px 24px;font-size:16px;width:100%;max-width:400px;" onclick="postAction('/api/scan/finish', 'Biztosan lezárod és beküldöd mind a(z) {total_pages} oldalt a Paperless-be?')">
                ✅ Összes oldal összefűzése és beküldése
            </button>
        </div>
        """
        header_actions = f"""
        <div style="display:flex;gap:8px;">
            <button class="btn btn-green" onclick="postAction('/api/scan/finish')">✅ Befejezés</button>
            <a class="btn btn-secondary" href="http://192.168.0.99:8123/szkenner">⬅️ Vissza</a>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="hu">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
    <title>Szkenner Puffer Előnézet ({total_pages} oldal)</title>
    <style>
        :root {{
            --bg: #111114;
            --card-bg: #1c1c1e;
            --card-sub: #2c2c2e;
            --border: #38383a;
            --accent: #2196f3;
            --green: #34c759;
            --red: #ff3b30;
            --orange: #ff9500;
            --text: #ffffff;
            --text-muted: #8e8e93;
        }}
        * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
        body {{
            margin: 0;
            padding: 0;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            padding-bottom: 60px;
        }}
        .header {{
            position: sticky;
            top: 0;
            z-index: 100;
            background: rgba(28, 28, 30, 0.90);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-bottom: 1px solid var(--border);
            padding: 12px 16px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .header-title {{
            font-size: 17px;
            font-weight: 700;
            margin: 0;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .header-subtitle {{
            font-size: 12px;
            color: var(--text-muted);
            margin: 2px 0 0 0;
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
            padding: 9px 14px;
            border-radius: 10px;
            font-size: 14px;
            font-weight: 600;
            border: none;
            cursor: pointer;
            text-decoration: none;
            transition: transform 0.1s, opacity 0.1s;
        }}
        .btn:active {{ transform: scale(0.96); opacity: 0.85; }}
        .btn-green {{ background: var(--green); color: #fff; }}
        .btn-blue {{ background: var(--accent); color: #fff; }}
        .btn-red {{ background: var(--red); color: #fff; }}
        .btn-secondary {{ background: var(--card-sub); color: var(--text); }}
        .btn-icon {{ padding: 8px 12px; font-size: 16px; }}

        .container {{
            max-width: 760px;
            margin: 0 auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}
        .top-actions {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            align-items: center;
        }}
        .pages-feed {{
            display: flex;
            flex-direction: column;
            gap: 20px;
        }}
        .page-card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 4px 16px rgba(0,0,0,0.3);
        }}
        .page-header {{
            padding: 12px 16px;
            background: var(--card-sub);
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid var(--border);
        }}
        .page-badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            font-weight: 700;
            font-size: 15px;
        }}
        .page-meta {{
            font-size: 12px;
            color: var(--text-muted);
        }}
        .img-wrapper {{
            position: relative;
            background: #000;
            text-align: center;
            min-height: 250px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: zoom-in;
            padding: 8px;
        }}
        .img-wrapper img {{
            max-width: 100%;
            height: auto;
            max-height: 75vh;
            border-radius: 4px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.5);
        }}
        .img-zoom-hint {{
            position: absolute;
            bottom: 12px;
            right: 12px;
            background: rgba(0,0,0,0.65);
            color: #fff;
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 11px;
            backdrop-filter: blur(4px);
        }}
        .page-toolbar {{
            padding: 10px 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            flex-wrap: wrap;
            background: var(--card-bg);
            border-top: 1px solid var(--border);
        }}
        .toolbar-group {{
            display: flex;
            gap: 8px;
            align-items: center;
        }}
        .empty-state {{
            text-align: center;
            padding: 50px 20px;
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 20px;
            margin-top: 10px;
        }}
        .empty-icon {{ font-size: 54px; margin-bottom: 12px; }}
        .empty-title {{ font-size: 20px; font-weight: 700; margin-bottom: 8px; }}
        .empty-desc {{ color: var(--text-muted); font-size: 14px; max-width: 440px; margin: 0 auto 24px; line-height: 1.5; }}

        .modal {{
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.95);
            z-index: 999;
            justify-content: center;
            align-items: center;
            padding: 10px;
        }}
        .modal.active {{ display: flex; }}
        .modal img {{ max-width: 100%; max-height: 100%; object-fit: contain; }}
        .modal-close {{
            position: absolute;
            top: 16px; right: 16px;
            background: rgba(255,255,255,0.25);
            color: #fff;
            border: none;
            border-radius: 50%;
            width: 44px; height: 44px;
            font-size: 24px;
            cursor: pointer;
        }}
        .toast {{
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%);
            background: #323236;
            color: #fff;
            padding: 10px 20px;
            border-radius: 24px;
            font-size: 14px;
            font-weight: 600;
            box-shadow: 0 4px 14px rgba(0,0,0,0.6);
            z-index: 1000;
            opacity: 0;
            transition: opacity 0.25s;
            pointer-events: none;
        }}
        .toast.show {{ opacity: 1; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="header-left">
            <div>
                <h1 class="header-title">📄 Szkenner Puffer Előnézet</h1>
                <div class="header-subtitle">{total_pages} oldal várakozik · Élő ellenőrzés</div>
            </div>
        </div>
        {header_actions}
    </div>

    <div class="container">
        {content_body}
    </div>

    <div class="modal" id="imageModal" onclick="closeModal()">
        <button class="modal-close" onclick="closeModal()">&times;</button>
        <img id="modalImg" src="" alt="Nagyított oldal" />
    </div>

    <div class="toast" id="toast"></div>

    <script>
        function showToast(msg) {{
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.classList.add('show');
            setTimeout(() => t.classList.remove('show'), 2000);
        }}

        async function postAction(url, confirmText) {{
            if (confirmText && !confirm(confirmText)) return;
            try {{
                const res = await fetch(url, {{ method: 'POST' }});
                const data = await res.json();
                if (data.message) showToast(data.message);
                if (url.includes('/finish')) {{
                    setTimeout(() => {{
                        window.location.href = 'http://192.168.0.99:8123/szkenner';
                    }}, 1200);
                }} else {{
                    setTimeout(() => location.reload(), 400);
                }}
            }} catch (e) {{
                alert('Műveleti hiba: ' + e);
            }}
        }}

        function openModal(src) {{
            document.getElementById('modalImg').src = src;
            document.getElementById('imageModal').classList.add('active');
        }}

        function closeModal() {{
            document.getElementById('imageModal').classList.remove('active');
        }}
    </script>
</body>
</html>
"""


def get_staging_status() -> dict:
    with _staging_lock:
        pages = []
        if STAGING_PAGES_DIR.exists():
            pages += [f.name for f in STAGING_PAGES_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
        if STAGING_FINISH_DIR.exists():
            pages += [f.name for f in STAGING_FINISH_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
        count = len(pages)
        last_scan = load_last_epson_scan()
        gmail_sync = load_last_gmail_sync()
        return {
            "count": count,
            "status": f"{count} oldal várakozik" if count > 0 else "Üres",
            "pages": sorted(pages),
            "last_action": _last_buffer_action,
            "last_scan_id": last_scan.get("id"),
            "last_scan_asn": last_scan.get("asn"),
            "last_scan_title": last_scan.get("title"),
            "last_scan_category": last_scan.get("category"),
            "last_scan_correspondent": last_scan.get("correspondent"),
            "last_scan_amount": last_scan.get("amount"),
            "last_scan_at": last_scan.get("processed_at"),
            "last_scan_url": last_scan.get("paperless_url"),
            "gmail_last_sync_at": gmail_sync.get("completed_at"),
            "gmail_last_ingested": gmail_sync.get("ingested_pdfs", 0),
            "timestamp": datetime.datetime.now().isoformat(),
        }


def clear_multipage_buffer() -> dict:
    global _last_buffer_action
    with _staging_lock:
        deleted = 0
        for d in [STAGING_PAGES_DIR, STAGING_FINISH_DIR]:
            if d.exists():
                for f in d.iterdir():
                    if f.is_file() and not f.name.startswith("."):
                        try:
                            f.unlink()
                            deleted += 1
                        except Exception:
                            pass
        _last_buffer_action = f"Puffer kiürítve ({deleted} oldal törölve)"
        log(f"[Szkenner Puffer] {_last_buffer_action}.")
        return {"status": "cleared", "deleted_files": deleted, "message": _last_buffer_action}


def pop_last_page_from_buffer() -> dict:
    global _last_buffer_action
    with _staging_lock:
        if not STAGING_PAGES_DIR.exists():
            _last_buffer_action = "A puffer üres (nincs törölhető oldal)"
            return {"status": "empty", "message": _last_buffer_action}
        pages = sorted(
            [f for f in STAGING_PAGES_DIR.iterdir() if f.is_file() and not f.name.startswith(".")],
            key=lambda x: x.name,
        )
        if not pages:
            _last_buffer_action = "A puffer üres (nincs törölhető oldal)"
            return {"status": "empty", "message": _last_buffer_action}
        last_file = pages[-1]
        name = last_file.name
        last_file.unlink()
        remaining = len(pages) - 1
        _last_buffer_action = f"Utolsó (#{len(pages)}.) oldal törölve ({remaining} maradt)"
        log(f"[Szkenner Puffer] {_last_buffer_action}: {name}")
        return {"status": "popped", "deleted": name, "remaining": remaining, "message": _last_buffer_action}


def swap_last_two_pages_in_buffer() -> dict:
    global _last_buffer_action
    with _staging_lock:
        if not STAGING_PAGES_DIR.exists():
            _last_buffer_action = "Legalább 2 oldal szükséges a cseréhez"
            return {"status": "not_enough_pages", "message": _last_buffer_action}
        pages = sorted(
            [f for f in STAGING_PAGES_DIR.iterdir() if f.is_file() and not f.name.startswith(".")],
            key=lambda x: x.name,
        )
        if len(pages) < 2:
            _last_buffer_action = f"Csak {len(pages)} oldal van a pufferben (min. 2 kell)"
            return {"status": "not_enough_pages", "message": _last_buffer_action}
        p1, p2 = pages[-2], pages[-1]
        b1 = p1.read_bytes()
        b2 = p2.read_bytes()
        p1.write_bytes(b2)
        p2.write_bytes(b1)
        _last_buffer_action = f"Utolsó 2 oldal (#{len(pages)-1} ↔ #{len(pages)}) felcserélve"
        log(f"[Szkenner Puffer] {_last_buffer_action}: {p1.name} <-> {p2.name}")
        return {"status": "swapped", "page_a": p1.name, "page_b": p2.name, "message": _last_buffer_action}


def rotate_last_page_in_buffer() -> dict:
    global _last_buffer_action
    with _staging_lock:
        if not STAGING_PAGES_DIR.exists():
            _last_buffer_action = "A puffer üres (nincs forgatható oldal)"
            return {"status": "empty", "message": _last_buffer_action}
        pages = sorted(
            [f for f in STAGING_PAGES_DIR.iterdir() if f.is_file() and not f.name.startswith(".")],
            key=lambda x: x.name,
        )
        if not pages:
            _last_buffer_action = "A puffer üres (nincs forgatható oldal)"
            return {"status": "empty", "message": _last_buffer_action}
        target_pdf = pages[-1]
        try:
            import pypdf
            reader = pypdf.PdfReader(str(target_pdf))
            writer = pypdf.PdfWriter()
            for pg in reader.pages:
                pg.rotate(180)
                writer.add_page(pg)
            tmp_pdf = target_pdf.with_suffix(".rot.pdf")
            with open(tmp_pdf, "wb") as f_out:
                writer.write(f_out)
            tmp_pdf.replace(target_pdf)
            _last_buffer_action = f"Utolsó (#{len(pages)}.) oldal 180°-kal elforgatva"
            log(f"[Szkenner Puffer] {_last_buffer_action}: {target_pdf.name}")
            return {"status": "rotated", "page": target_pdf.name, "message": _last_buffer_action}
        except Exception as e:
            _last_buffer_action = f"Forgatási hiba: {e}"
            log(f"[Szkenner Puffer] Hiba a forgatáskor: {e}")
            return {"status": "error", "error": str(e), "message": _last_buffer_action}


def merge_multipage_batch() -> dict:
    with _staging_lock:
        STAGING_PAGES_DIR.mkdir(parents=True, exist_ok=True)
        STAGING_FINISH_DIR.mkdir(parents=True, exist_ok=True)
        TARGET_CONSUME_DIR.mkdir(parents=True, exist_ok=True)

        # 1. Ha a finish mappában van lap, áthúzzuk a pages mappába
        finish_files = [f for f in STAGING_FINISH_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
        for f in finish_files:
            target = STAGING_PAGES_DIR / f.name
            counter = 1
            while target.exists():
                stem = f.stem
                suffix = f.suffix
                target = STAGING_PAGES_DIR / f"{stem}_{counter}{suffix}"
                counter += 1
            f.rename(target)

        # 2. Összegyűjtjük a pages-ben lévő lapokat
        pages = sorted(
            [f for f in STAGING_PAGES_DIR.iterdir() if f.is_file() and not f.name.startswith(".")],
            key=lambda x: x.name,
        )

        if not pages:
            log("[Szkenner Összefűző] A puffer üres, nincs összefűzendő oldal.")
            return {"status": "empty", "count": 0, "message": "A puffer üres."}

        # 3. Külön izolált munkakönyvtárba mozgatjuk (versenyhelyzetek és részleges írások kizárása)
        proc_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        proc_dir = STAGING_DIR / f"_processing_{proc_ts}"
        proc_dir.mkdir(parents=True, exist_ok=True)

        proc_pages = []
        for p in pages:
            dest = proc_dir / p.name
            p.rename(dest)
            proc_pages.append(dest)

        proc_pages.sort(key=lambda x: x.name)

        # 4. Összefűzés PDF-be
        try:
            import pypdf
            writer = pypdf.PdfWriter()
            page_count = 0
            for pdf_path in proc_pages:
                try:
                    reader = pypdf.PdfReader(str(pdf_path))
                    for pg in reader.pages:
                        writer.add_page(pg)
                        page_count += 1
                except Exception as pe:
                    log(f"[Szkenner Összefűző] Hiba a(z) {pdf_path.name} olvasásakor: {pe}")

            if page_count == 0:
                raise RuntimeError("Egyetlen oldalt sem sikerült beolvasni a forrásokból.")

            out_filename = f"szkennelt_tobboldalas_{proc_ts}.pdf"
            out_path = TARGET_CONSUME_DIR / out_filename

            with open(out_path, "wb") as out_f:
                writer.write(out_f)

            # Sikeres mentés után töröljük az átmeneti fájlokat
            for pdf_path in proc_pages:
                try:
                    pdf_path.unlink()
                except Exception:
                    pass
            try:
                proc_dir.rmdir()
            except Exception:
                pass

            global _last_buffer_action
            _last_buffer_action = f"{page_count} oldal összefűzve és beküldve ({out_filename})"
            log(f"[Szkenner Összefűző] SIKERES ÖSSZEFŰZÉS: {page_count} oldal -> {out_filename}")
            return {
                "status": "success",
                "count": page_count,
                "filename": out_filename,
                "message": f"{page_count} oldal sikeresen összefűzve és elküldve a Paperlessbe!",
            }
        except Exception as e:
            log(f"[Szkenner Összefűző] Hiba az összefűzés során: {e}")
            # Visszahelyezzük az oldalakat a pages mappába, hogy ne vesszenek el
            for p in proc_pages:
                try:
                    p.rename(STAGING_PAGES_DIR / p.name)
                except Exception:
                    pass
            try:
                proc_dir.rmdir()
            except Exception:
                pass
            return {"status": "error", "error": str(e)}


def staging_buffer_monitor_loop():
    log("Szkenner többoldalas puffer figyelő elindult (KESZ / Utolso oldal és Home Assistant vezérlés)...")
    while True:
        try:
            if STAGING_FINISH_DIR.exists():
                finish_files = [f for f in STAGING_FINISH_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
                if finish_files:
                    # Rövid várakozás, hogy a fájl lezárása és kiírása biztosan befejeződjön
                    time.sleep(1.5)
                    merge_multipage_batch()
        except Exception as e:
            log(f"[Szkenner Puffer Hiba] {e}")
        time.sleep(1)


def start_staging_watcher_thread():
    t = threading.Thread(target=staging_buffer_monitor_loop, daemon=True)
    t.start()


# ============================================================================
# OPENAI CODEX OAUTH TOKEN KEZELÉS ÉS HÍVÁS (gpt-6-luna)
# ============================================================================

def _jwt_exp(token: str) -> float:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return 0.0
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return float(claims.get("exp") or 0.0)
    except Exception:
        return 0.0


def get_valid_codex_oauth_credentials(force_refresh: bool = False) -> tuple[str, str]:
    """Visszaadja a (access_token, account_id) párost, szükség esetén frissítve a tokent."""
    with _oauth_lock:
        if not CODEX_AUTH_FILE.exists():
            raise RuntimeError(f"Codex OAuth fájl nem található: {CODEX_AUTH_FILE}")

        data = json.loads(CODEX_AUTH_FILE.read_text(encoding="utf-8"))
        tokens = data.get("tokens") or {}
        access_token = (tokens.get("access_token") or "").strip()
        refresh_token = (tokens.get("refresh_token") or "").strip()
        account_id = (tokens.get("account_id") or "").strip()

        exp = _jwt_exp(access_token)
        needs_refresh = force_refresh or not access_token or (exp > 0 and time.time() + 180 >= exp)

        if needs_refresh and refresh_token:
            log("OpenAI OAuth access_token frissítése a refresh_token segítségével...")
            form_body = urllib.parse.urlencode(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                CODEX_OAUTH_TOKEN_URL,
                data=form_body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                    "User-Agent": "codex_cli_rs/0.0.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                ref_data = json.loads(resp.read().decode("utf-8"))
                new_at = (ref_data.get("access_token") or "").strip()
                new_rt = (ref_data.get("refresh_token") or "").strip()
                if new_at:
                    tokens["access_token"] = new_at
                    access_token = new_at
                if new_rt:
                    tokens["refresh_token"] = new_rt
                if ref_data.get("id_token"):
                    tokens["id_token"] = ref_data["id_token"]
                data["tokens"] = tokens
                data["last_refresh"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                CODEX_AUTH_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
                log("OpenAI OAuth access_token sikeresen frissítve és elmentve!")

        return access_token, account_id


def call_codex_oauth_messages(messages: list[dict], model: str | None = None) -> str:
    """Meghívja a ChatGPT Codex OAuth Responses végpontot (pl. gpt-6-luna)."""
    chosen_model = (model or CODEX_MODEL or "gpt-6-luna").strip()
    instructions_parts = []
    input_items = []

    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            text_val = "\n".join(
                part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
            )
        else:
            text_val = str(content or "")

        if role in ("system", "developer"):
            instructions_parts.append(text_val)
        else:
            msg_role = "assistant" if role == "assistant" else "user"
            content_type = "output_text" if msg_role == "assistant" else "input_text"
            input_items.append(
                {
                    "type": "message",
                    "role": msg_role,
                    "content": [{"type": content_type, "text": text_val}],
                }
            )

    instructions = "\n\n".join(instructions_parts).strip() or "You are a helpful assistant."
    if not input_items:
        input_items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello"}]}]

    for attempt in range(2):
        access_token, account_id = get_valid_codex_oauth_credentials(force_refresh=(attempt == 1))
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "codex_cli_rs/0.0.0",
            "originator": "codex_cli_rs",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id

        payload = {
            "model": chosen_model,
            "instructions": instructions,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        req = urllib.request.Request(
            CODEX_RESPONSES_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                chunks = []
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        ev = json.loads(data_str)
                        ev_type = ev.get("type", "")
                        if ev_type == "response.output_text.delta":
                            chunks.append(ev.get("delta", ""))
                        elif ev_type == "response.output_text.done" and not chunks:
                            chunks.append(ev.get("text", ""))
                    except Exception:
                        pass
                return "".join(chunks).strip()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and attempt == 0:
                log(f"OpenAI OAuth HTTP {e.code}, token kényszerített frissítése...")
                continue
            err_body = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"OpenAI OAuth HTTP {e.code}: {err_body}") from e


# ============================================================================
# BEÉPÍTETT OPENAI-KOMPATIBILIS PROXY SZERVER (:3006/v1) PAPERLESS-AI SZÁMÁRA
# ============================================================================

class OpenAIOAuthProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Csendes naplózás

    def _send_json(self, status: int, data: dict):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/scan/status":
            self._send_json(200, get_staging_status())
            return
        if self.path.startswith("/api/scan/preview.pdf"):
            pdf_bytes = get_merged_buffer_pdf()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'inline; filename="szkenner_elonezet.pdf"')
            self.send_header("Content-Length", str(len(pdf_bytes)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(pdf_bytes)
            return
        if self.path in ("/api/scan/preview", "/preview") or self.path.startswith("/api/scan/preview?"):
            html_content = generate_preview_html()
            raw_html = html_content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw_html)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(raw_html)
            return
        if self.path.startswith("/api/scan/page/"):
            clean_path = self.path.split("?")[0].strip("/")
            parts = clean_path.split("/")
            if len(parts) >= 5 and parts[-1] == "image":
                target = parts[3]
                pages = get_buffer_pages()
                if not pages:
                    img_bytes = get_empty_placeholder_jpeg("A puffer üres")
                else:
                    if target == "last":
                        target_page = pages[-1]
                    else:
                        try:
                            idx = int(target) - 1
                            if idx < 0 or idx >= len(pages):
                                idx = 0
                            target_page = pages[idx]
                        except ValueError:
                            target_page = pages[-1]
                    img_bytes = render_page_thumbnail(target_page)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img_bytes)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
                self.end_headers()
                self.wfile.write(img_bytes)
                return
        if self.path.startswith("/v1/models") or self.path.startswith("/models"):
            models = [CODEX_MODEL, "gpt-6-luna", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4-mini"]
            seen = []
            for m in models:
                if m not in seen:
                    seen.append(m)
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": m, "object": "model", "created": 1790000000, "owned_by": "openai-oauth"}
                        for m in seen
                    ],
                },
            )
            return
        if self.path in ("/health", "/"):
            prov, mod = active_ai_provider_with_model()
            self._send_json(200, {"status": "ok", "provider": prov, "default_model": mod})
            return
        self._send_json(404, {"error": {"message": "Not found"}})

    def do_POST(self):
        if self.path == "/api/scan/finish":
            res = merge_multipage_batch()
            self._send_json(200, res)
            return
        if self.path == "/api/scan/clear":
            res = clear_multipage_buffer()
            self._send_json(200, res)
            return
        if self.path == "/api/scan/pop":
            res = pop_last_page_from_buffer()
            self._send_json(200, res)
            return
        if self.path == "/api/scan/swap":
            res = swap_last_two_pages_in_buffer()
            self._send_json(200, res)
            return
        if self.path == "/api/scan/rotate":
            res = rotate_last_page_in_buffer()
            self._send_json(200, res)
            return
        if self.path.startswith("/api/scan/page/"):
            clean_path = self.path.split("?")[0].strip("/")
            parts = clean_path.split("/")
            if len(parts) >= 5:
                target = parts[3]
                action = parts[4]
                pages = get_buffer_pages()
                try:
                    target_num = len(pages) if target == "last" else int(target)
                    if action == "rotate":
                        res = rotate_page_by_index(target_num)
                        self._send_json(200, res)
                        return
                    elif action == "delete":
                        res = delete_page_by_index(target_num)
                        self._send_json(200, res)
                        return
                    elif action in ("move_up", "up"):
                        res = move_page_by_index(target_num, "up")
                        self._send_json(200, res)
                        return
                    elif action in ("move_down", "down"):
                        res = move_page_by_index(target_num, "down")
                        self._send_json(200, res)
                        return
                except Exception as pe:
                    self._send_json(500, {"status": "error", "message": str(pe)})
                    return
        if "/chat/completions" not in self.path:
            self._send_json(404, {"error": {"message": f"Unsupported endpoint: {self.path}"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            req_model = body.get("model") or OPENAI_MODEL or CODEX_MODEL
            messages = body.get("messages") or []
            prov = active_ai_provider()
            if prov == "openai" and OPENAI_API_KEY:
                reply_text = call_openai_api_messages(messages, model=req_model)
            else:
                reply_text = call_codex_oauth_messages(messages, model=req_model)
            resp_obj = {
                "id": f"chatcmpl-{int(time.time()*1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": req_model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            self._send_json(200, resp_obj)
        except Exception as e:
            log(f"[AI Proxy Hiba] {e}")
            self._send_json(500, {"error": {"message": str(e), "type": "ai_proxy_error"}})


def start_oauth_proxy_server():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", OAUTH_PROXY_PORT), OpenAIOAuthProxyHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        log(f"OpenAI OAuth Proxy elindult a 0.0.0.0:{OAUTH_PROXY_PORT}/v1 végponton (alapértelmezett modell: {CODEX_MODEL})")
    except Exception as e:
        log(f"OpenAI OAuth Proxy indítási hiba: {e}")


class PaperlessClient:
    def __init__(self):
        self.base_url = PAPERLESS_URL
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if PAPERLESS_TOKEN:
            self.headers["Authorization"] = f"Token {PAPERLESS_TOKEN}"
        else:
            creds = base64.b64encode(f"{PAPERLESS_USER}:{PAPERLESS_PASS}".encode()).decode()
            self.headers["Authorization"] = f"Basic {creds}"

    def request(self, method: str, path: str, data=None):
        url = f"{self.base_url}{path}" if path.startswith("/") else path
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"HTTP {e.code} ({method} {path}): {err_body}") from e

    def get_all(self, endpoint: str):
        results = []
        url = f"{endpoint}?page_size=500"
        while url:
            res = self.request("GET", url)
            results.extend(res.get("results", []))
            next_url = res.get("next")
            if next_url:
                parsed = urllib.parse.urlparse(next_url)
                url = parsed.path + ("?" + parsed.query if parsed.query else "")
            else:
                url = None
        return results


def load_runtime_ai_config() -> dict:
    cfg_path = JSON_OUTPUT_DIR / "ai_beallitas.json"
    default_cfg = {
        "active_provider": "openai" if AI_PROVIDER == "openai" else ("openai-oauth" if AI_PROVIDER in ("openai-oauth", "codex-oauth", "codex") else AI_PROVIDER),
        "openai_model": OPENAI_MODEL or "gpt-6-luna",
        "codex_oauth_model": CODEX_MODEL or "gpt-6-luna",
        "ollama_model": OLLAMA_MODEL or "gemma3:4b",
        "gemini_model": GEMINI_MODEL or "gemini-2.5-flash",
        "auto_fallback": True,
        "available_options": {
            "openai": "Hivatalos OpenAI API kulcs (Home Assistantbol atvett, gpt-6-luna)",
            "openai-oauth": "ChatGPT / Codex OAuth elofizetes (auth.json, gpt-6-luna)",
            "ollama": "Helyi ingyenes AI Core (192.168.0.77:11434, gemma3:4b)",
            "gemini": "Google Gemini API"
        },
        "leiras": "Az active_provider erteket barmikor atirhatod ('openai', 'openai-oauth', 'ollama', 'gemini'), a rendszer ujrainditas nelkul azonnal atvalt!"
    }
    if not cfg_path.exists():
        try:
            JSON_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(default_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return default_cfg
    try:
        loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
        for k, v in default_cfg.items():
            if k not in loaded:
                loaded[k] = v
        return loaded
    except Exception:
        return default_cfg


def active_ai_provider() -> str:
    cfg = load_runtime_ai_config()
    p = str(cfg.get("active_provider") or AI_PROVIDER).strip().lower()
    if p in ("openai-oauth", "codex-oauth", "codex"):
        return "openai-oauth"
    if p in ("openai", "gemini", "ollama"):
        return p
    if OPENAI_API_KEY:
        return "openai"
    if CODEX_AUTH_FILE.exists():
        return "openai-oauth"
    if GEMINI_API_KEY:
        return "gemini"
    return "ollama"


def active_ai_provider_with_model() -> tuple[str, str]:
    cfg = load_runtime_ai_config()
    prov = active_ai_provider()
    if prov == "openai":
        return prov, str(cfg.get("openai_model") or OPENAI_MODEL or "gpt-6-luna")
    if prov == "openai-oauth":
        return prov, str(cfg.get("codex_oauth_model") or CODEX_MODEL or "gpt-6-luna")
    if prov == "gemini":
        return prov, str(cfg.get("gemini_model") or GEMINI_MODEL or "gemini-2.5-flash")
    return "ollama", str(cfg.get("ollama_model") or OLLAMA_MODEL or "gemma3:4b")


def call_openai_api_messages(messages: list[dict], model: str | None = None, json_mode: bool = False) -> str:
    """Meghívja a hivatalos OpenAI API végpontot (https://api.openai.com/v1/chat/completions)."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY nincs beállítva.")
    chosen_model = (model or OPENAI_MODEL or "gpt-6-luna").strip()
    url = "https://api.openai.com/v1/chat/completions"
    payload: dict = {
        "model": chosen_model,
        "messages": messages,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {OPENAI_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


def build_ai_prompt(ocr_text: str, orig_filename: str, existing_tags: list[str]) -> str:
    categories_str = "\n".join(f"- {c}" for c in HOUSEHOLD_DOCUMENT_TYPES)
    excerpt = (ocr_text or "")[:8000]
    return f"""Te egy magyar/szlovákiai otthoni háztartás precíz dokumentum-kategorizáló AI asszisztense vagy.
A dokumentumok lehetnek magyar, szlovák vagy angol nyelvűek, de a választ (a címet és az összefoglalót) MINDIG MAGYAR NYELVEN add meg, KIZÁRÓLAG érvényes JSON objektumként!

Eredeti fájlnév: {orig_filename}
Meglévő címkék: {", ".join(existing_tags) if existing_tags else "nincs"}

Választható dokumentum kategóriák (PONTOSAN egyet válassz az alábbi listából a `document_type` mezőbe):
{categories_str}

A visszaküldendő JSON sémája:
{{
  "title": "Rövid, beszédes magyar cím (pl. KONFERnet Internet számla - 2026.11)",
  "document_type": "Pontosan a fenti 10 kategória egyike",
  "correspondent": "A feladó / szolgáltató / intézmény rövid neve (pl. KONFER net, s. r. o., MVM Next, OTP Bank)",
  "created_date": "A dokumentum kiállítási dátuma YYYY-MM-DD formátumban, vagy null ha nem található",
  "tags": ["max 4 releváns magyar címke kisbetűvel, ékezet nélkül, pl. rezsi, internet, szamla, fizetendo, fontos, garancia, orvosi, auto, bank, ado"],
  "summary": "1-2 mondatos, informatív magyar összefoglaló a dokumentum lényegéről (tárgy, időszak, fizetendő összeg és pénznem, határidő)",
  "amount": 11.99,
  "currency": "EUR vagy HUF (vagy null ha nincs összeg)",
  "amount_huf": null,
  "due_date": "YYYY-MM-DD vagy null (fizetési határidő / Dátum splatnosti, ha van)",
  "warranty_until": "YYYY-MM-DD vagy null (garancia lejárata, ha van)",
  "reference_number": "Számlaszám (Číslo faktúry), szerződésszám vagy ügyiratszám, ha van, különben null",
  "variable_symbol": "Variabilný symbol (Var. sym.) vagy fizetési közlemény, ha van, különben null",
  "iban": "Kedvezményezett IBAN számlaszáma, ha szerepel a dokumentumon, különben null"
}}

Szabályok:
- Az `amount` mező szám legyen (pl. 11.99 vagy 18450) vagy null.
- Ha a pénznem HUF/Ft, akkor az `amount_huf` is legyen kitöltve egész számmal, EUR esetén az `amount_huf` legyen null.
- Minden dátum `YYYY-MM-DD` formátumú legyen (pl. 01.11.2026 -> 2026-11-01) vagy null.
- Csak a nyers JSON objektumot add vissza, markdown kódblokk (```json) nélkül!

A dokumentum OCR szövege:
---
{excerpt}
---"""


def _extract_json_dict(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def call_llm_json(prompt: str) -> tuple[dict, str]:
    provider, chosen_model = active_ai_provider_with_model()
    cfg = load_runtime_ai_config()
    auto_fb = bool(cfg.get("auto_fallback", True))

    if provider == "openai" and OPENAI_API_KEY:
        try:
            txt = call_openai_api_messages(
                [
                    {"role": "system", "content": "You output only valid JSON objects in Hungarian."},
                    {"role": "user", "content": prompt},
                ],
                model=chosen_model,
                json_mode=True,
            )
            return _extract_json_dict(txt), f"openai:{chosen_model}"
        except Exception as e:
            log(f"Hivatalos OpenAI API ({chosen_model}) hiba: {e}")
            if not auto_fb:
                raise
            log("Automatikus átváltás tartalékra (openai-oauth / ollama)...")
            provider = "openai-oauth"
            chosen_model = str(cfg.get("codex_oauth_model") or CODEX_MODEL or "gpt-6-luna")

    if provider == "openai-oauth":
        try:
            raw_reply = call_codex_oauth_messages(
                [
                    {"role": "system", "content": "You output only valid JSON objects in Hungarian without markdown fences."},
                    {"role": "user", "content": prompt},
                ],
                model=chosen_model,
            )
            return _extract_json_dict(raw_reply), f"openai-oauth:{chosen_model}"
        except Exception as e:
            log(f"OpenAI OAuth ({chosen_model}) hiba, visszaállás tartalékra: {e}")
            if not auto_fb:
                raise
            if OPENAI_API_KEY:
                try:
                    fb_model = str(cfg.get("openai_model") or OPENAI_MODEL or "gpt-6-luna")
                    txt = call_openai_api_messages(
                        [
                            {"role": "system", "content": "You output only valid JSON objects in Hungarian."},
                            {"role": "user", "content": prompt},
                        ],
                        model=fb_model,
                        json_mode=True,
                    )
                    return _extract_json_dict(txt), f"openai:{fb_model}"
                except Exception as e2:
                    log(f"Tartalék OpenAI API hiba: {e2}")

    if provider == "gemini" and GEMINI_API_KEY:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{chosen_model}:generateContent?key={GEMINI_API_KEY}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            txt = data["candidates"][0]["content"]["parts"][0]["text"]
            return _extract_json_dict(txt), f"gemini:{chosen_model}"

    # Fallback / Local Ollama
    url = f"{OLLAMA_URL}/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_ctx": 4096},
        "messages": [
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        txt = data.get("message", {}).get("content", "{}")
        return _extract_json_dict(txt), f"ollama:{OLLAMA_MODEL}"


def valid_date(val) -> str | None:
    if not val or not isinstance(val, str):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", val.strip())
    if not m:
        return None
    try:
        datetime.date.fromisoformat(m.group(1))
        return m.group(1)
    except ValueError:
        return None


def seed_household_taxonomy(client: PaperlessClient):
    log("Háztartási kategóriák, címkék és egyedi mezők ellenőrzése / létrehozása...")

    existing_types = {t["name"].lower(): t for t in client.get_all("/api/document_types/")}
    for dt_name in HOUSEHOLD_DOCUMENT_TYPES:
        if dt_name.lower() not in existing_types:
            try:
                created = client.request(
                    "POST",
                    "/api/document_types/",
                    {"name": dt_name, "matching_algorithm": 6, "is_insensitive": True},
                )
                existing_types[dt_name.lower()] = created
                log(f"  + Kategória létrehozva: {dt_name}")
            except Exception as e:
                log(f"  ! Kategória hiba ({dt_name}): {e}")

    existing_tags = {t["name"].lower(): t for t in client.get_all("/api/tags/")}
    for tag_name, color in HOUSEHOLD_TAGS:
        if tag_name.lower() not in existing_tags:
            try:
                created = client.request(
                    "POST",
                    "/api/tags/",
                    {"name": tag_name, "color": color, "matching_algorithm": 0, "is_insensitive": True},
                )
                existing_tags[tag_name.lower()] = created
                log(f"  + Címke létrehozva: {tag_name}")
            except Exception as e:
                log(f"  ! Címke hiba ({tag_name}): {e}")

    existing_corrs = {c["name"].lower(): c for c in client.get_all("/api/correspondents/")}
    for corr_name in HOUSEHOLD_CORRESPONDENTS:
        if corr_name.lower() not in existing_corrs:
            try:
                created = client.request(
                    "POST",
                    "/api/correspondents/",
                    {"name": corr_name, "matching_algorithm": 6, "is_insensitive": True},
                )
                existing_corrs[corr_name.lower()] = created
                log(f"  + Partner létrehozva: {corr_name}")
            except Exception as e:
                log(f"  ! Partner hiba ({corr_name}): {e}")

    existing_fields = {f["name"].lower(): f for f in client.get_all("/api/custom_fields/")}
    for f_name, f_type in CUSTOM_FIELD_DEFS:
        if f_name.lower() not in existing_fields:
            try:
                created = client.request(
                    "POST",
                    "/api/custom_fields/",
                    {"name": f_name, "data_type": f_type},
                )
                existing_fields[f_name.lower()] = created
                log(f"  + Egyedi mező létrehozva: {f_name} ({f_type})")
            except Exception as e:
                log(f"  ! Egyedi mező hiba ({f_name}): {e}")

    ensure_gmail_mail_rule(client)


def ensure_gmail_mail_rule(client: PaperlessClient):
    try:
        accounts = client.get_all("/api/mail_accounts/")
        if not accounts:
            return
        gmail_acc = None
        for acc in accounts:
            if "gmail" in acc.get("name", "").lower() or acc.get("account_type") == 2 or "menti.todo" in acc.get("username", "").lower():
                gmail_acc = acc
                break
        if not gmail_acc:
            gmail_acc = accounts[0]

        rules = client.get_all("/api/mail_rules/")
        rule_exists = any(r.get("account") == gmail_acc["id"] for r in rules)
        if not rule_exists:
            rule_payload = {
                "name": "Menti Todo Paperless Számlák",
                "account": gmail_acc["id"],
                "enabled": True,
                "folder": "INBOX",
                "filter_to": "menti.todo+paperless@gmail.com",
                "action": 3,  # Mark as read
                "attachment_type": 1,  # Only process attachments
                "consumption_scope": 1,  # Only process attachments
                "assign_title_from": 3,  # Do not assign title from rule (AI will title it)
                "order": 1,
            }
            client.request("POST", "/api/mail_rules/", rule_payload)
            log("  + Automatikus E-mail szabály létrehozva a menti.todo+paperless@gmail.com címre (Mark as read, AI kategorizálás)!")
    except Exception as e:
        log(f"  ! Mail rule ellenőrzési hiba: {e}")


def process_and_sync(client: PaperlessClient, exported_cache: dict[int, str]):
    doc_types_by_id = {t["id"]: t for t in client.get_all("/api/document_types/")}
    doc_types_by_name = {t["name"].lower(): t for t in doc_types_by_id.values()}

    tags_by_id = {t["id"]: t for t in client.get_all("/api/tags/")}
    tags_by_name = {t["name"].lower(): t for t in tags_by_id.values()}

    corrs_by_id = {c["id"]: c for c in client.get_all("/api/correspondents/")}
    corrs_by_name = {c["name"].lower(): c for c in corrs_by_id.values()}

    fields_by_id = {f["id"]: f for f in client.get_all("/api/custom_fields/")}
    fields_by_name = {f["name"].lower(): f for f in fields_by_id.values()}

    ai_tag_id = tags_by_name.get("ai-feldolgozva", {}).get("id")

    documents = client.get_all("/api/documents/")
    JSON_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (JSON_OUTPUT_DIR / "osszes").mkdir(parents=True, exist_ok=True)
    (JSON_OUTPUT_DIR / "kategorizalt").mkdir(parents=True, exist_ok=True)

    ai_meta_path = JSON_OUTPUT_DIR / ".ai_meta_cache.json"
    ai_meta_store = {}
    if ai_meta_path.exists():
        try:
            ai_meta_store = json.loads(ai_meta_path.read_text(encoding="utf-8"))
        except Exception:
            ai_meta_store = {}

    for trig in JSON_OUTPUT_DIR.glob(".trigger_sync_*"):
        try:
            trig.unlink()
        except OSError:
            pass

    catalog_entries = []
    any_updated = False

    # Legmagasabb meglévő ASN (Archive Serial Number / Irattári sorszám) lekérése
    current_max_asn = 0
    try:
        resp = client.request("GET", "/api/documents/?archive_serial_number__isnull=false&ordering=-archive_serial_number&page_size=1")
        results = resp.get("results", []) if isinstance(resp, dict) else []
        if results and results[0].get("archive_serial_number"):
            current_max_asn = int(results[0]["archive_serial_number"])
    except Exception as e:
        log(f"  ! Legmagasabb ASN sorszám lekérési hiba: {e}")

    # Rendezzük a dokumentumokat hozzáadás/ID szerint növekvő sorrendbe az ASN sorszámozás folytonosságához
    sorted_docs = sorted(documents, key=lambda d: (d.get("added") or "", d.get("id") or 0))

    for doc in sorted_docs:
        doc_id = doc["id"]
        doc_tags = list(doc.get("tags", []))
        doc_tag_names = [tags_by_id[tid]["name"] for tid in doc_tags if tid in tags_by_id]
        content = (doc.get("content") or "").strip()

        orig_fn = (doc.get("original_file_name") or "").lower()
        is_epson_scan = (
            orig_fn.startswith("scan_")
            or orig_fn.startswith("szkennelt_")
            or "szkennelt" in orig_fn
            or "epson" in orig_fn
        ) and not orig_fn.startswith("mail_")

        # Ha Epson szkennerből származik és még nincs hozzárendelve ASN irattári sorszám
        assigned_new_asn = None
        if is_epson_scan and not doc.get("archive_serial_number"):
            current_max_asn += 1
            assigned_new_asn = current_max_asn
            doc["archive_serial_number"] = assigned_new_asn

        if ai_tag_id and ai_tag_id not in doc_tags:
            log(f"AI elemzés indítása a(z) #{doc_id} dokumentumon ({doc.get('original_file_name')})...")
            try:
                content_for_prompt = (
                    content
                    if content
                    else f"[Nincs kinyerhető OCR szöveg vagy védett/grafikus PDF. Fájlnév: {doc.get('original_file_name')}]"
                )
                prompt = build_ai_prompt(content_for_prompt, doc.get("original_file_name") or "", doc_tag_names)
                ai_res, provider_used = call_llm_json(prompt)
                log(f"  -> AI eredmény ({provider_used}): {json.dumps(ai_res, ensure_ascii=False)}")

                ai_meta_store[str(doc_id)] = {
                    "provider": provider_used,
                    "analyzed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "extracted": ai_res,
                }
                ai_meta_path.write_text(json.dumps(ai_meta_store, ensure_ascii=False, indent=2), encoding="utf-8")

                patch_data = {}
                if assigned_new_asn:
                    patch_data["archive_serial_number"] = assigned_new_asn
                    log(f"  -> Epson szkenner észlelve: Új ASN sorszám #{assigned_new_asn} hozzárendelve a(z) #{doc_id} dokumentumhoz!")

                new_title = (ai_res.get("title") or "").strip()
                if new_title:
                    patch_data["title"] = new_title[:127]

                chosen_dt_name = None
                for tname in doc_tag_names:
                    if tname.lower() in FOLDER_TAG_TO_DOCTYPE:
                        chosen_dt_name = FOLDER_TAG_TO_DOCTYPE[tname.lower()]
                        break
                if not chosen_dt_name:
                    ai_dt = (ai_res.get("document_type") or "").strip()
                    for valid_dt in HOUSEHOLD_DOCUMENT_TYPES:
                        if valid_dt.lower() in ai_dt.lower() or ai_dt.lower() in valid_dt.lower():
                            chosen_dt_name = valid_dt
                            break
                    if not chosen_dt_name:
                        chosen_dt_name = "Személyes okirat és Egyéb"

                dt_obj = doc_types_by_name.get(chosen_dt_name.lower())
                if dt_obj:
                    patch_data["document_type"] = dt_obj["id"]

                corr_name = (ai_res.get("correspondent") or "").strip()
                if corr_name and corr_name.lower() not in ("null", "none", "ismeretlen", "n/a"):
                    corr_obj = corrs_by_name.get(corr_name.lower())
                    if not corr_obj:
                        for existing_c_name, existing_c in corrs_by_name.items():
                            if corr_name.lower() in existing_c_name or existing_c_name in corr_name.lower():
                                corr_obj = existing_c
                                break
                    if not corr_obj:
                        try:
                            corr_obj = client.request(
                                "POST",
                                "/api/correspondents/",
                                {"name": corr_name[:127], "matching_algorithm": 0, "is_insensitive": True},
                            )
                            corrs_by_id[corr_obj["id"]] = corr_obj
                            corrs_by_name[corr_obj["name"].lower()] = corr_obj
                        except Exception:
                            corr_obj = None
                    if corr_obj:
                        patch_data["correspondent"] = corr_obj["id"]

                c_date = valid_date(ai_res.get("created_date"))
                if c_date:
                    patch_data["created"] = c_date

                new_tag_ids = set(doc_tags)
                new_tag_ids.add(ai_tag_id)
                for raw_tag in ai_res.get("tags") or []:
                    clean_tag = slugify(str(raw_tag).lower(), max_len=30).replace("_", "-")
                    if not clean_tag or clean_tag in ("null", "none"):
                        continue
                    t_obj = tags_by_name.get(clean_tag)
                    if not t_obj:
                        try:
                            t_obj = client.request(
                                "POST",
                                "/api/tags/",
                                {"name": clean_tag, "matching_algorithm": 0, "is_insensitive": True},
                            )
                            tags_by_id[t_obj["id"]] = t_obj
                            tags_by_name[clean_tag] = t_obj
                        except Exception:
                            t_obj = None
                    if t_obj:
                        new_tag_ids.add(t_obj["id"])
                patch_data["tags"] = sorted(new_tag_ids)

                custom_fields_payload = []
                existing_cf = {cf["field"]: cf["value"] for cf in doc.get("custom_fields", [])}

                def set_cf(field_name: str, val):
                    f_obj = fields_by_name.get(field_name.lower())
                    if f_obj and val is not None:
                        existing_cf[f_obj["id"]] = val

                amt_val = ai_res.get("amount")
                curr_val = (ai_res.get("currency") or "").strip().upper()
                if amt_val is not None and str(amt_val).strip() not in ("", "null", "None"):
                    amt_str = f"{amt_val} {curr_val}".strip()
                    set_cf("Összeg", amt_str[:128])

                amt = ai_res.get("amount_huf")
                if amt is not None and str(amt).strip() not in ("", "null", "None"):
                    try:
                        set_cf("Összeg (Ft)", int(float(str(amt).replace(" ", "").replace("Ft", ""))))
                    except ValueError:
                        pass

                due = valid_date(ai_res.get("due_date"))
                if due:
                    set_cf("Fizetési határidő", due)

                warr = valid_date(ai_res.get("warranty_until"))
                if warr:
                    set_cf("Garancia lejárata", warr)

                ref_num = ai_res.get("reference_number")
                if ref_num and str(ref_num).strip().lower() not in ("", "null", "none"):
                    set_cf("Számlaszám / Ügyiratszám", str(ref_num).strip()[:128])

                var_sym = ai_res.get("variable_symbol")
                iban_val = ai_res.get("iban")
                vs_parts = []
                if var_sym and str(var_sym).strip().lower() not in ("", "null", "none"):
                    vs_parts.append(f"VS: {str(var_sym).strip()}")
                if iban_val and str(iban_val).strip().lower() not in ("", "null", "none"):
                    vs_parts.append(f"IBAN: {str(iban_val).strip()}")
                if vs_parts:
                    set_cf("Variabilný symbol / Közlemény", " | ".join(vs_parts)[:128])

                summary_txt = (ai_res.get("summary") or "").strip()
                if summary_txt:
                    set_cf("AI Összefoglaló", summary_txt[:128])

                for fid, fval in existing_cf.items():
                    custom_fields_payload.append({"field": fid, "value": fval})
                if custom_fields_payload:
                    patch_data["custom_fields"] = custom_fields_payload

                doc = client.request("PATCH", f"/api/documents/{doc_id}/", patch_data)
                try:
                    if summary_txt:
                        client.request(
                            "POST",
                            f"/api/documents/{doc_id}/notes/",
                            {"note": f"[{provider_used}] {summary_txt}"},
                        )
                except Exception:
                    pass
                any_updated = True
                log(f"  -> #{doc_id} sikeresen frissítve a Paperless-ben ({provider_used})!")
            except Exception as e:
                log(f"  ! AI feldolgozási hiba #{doc_id} dokumentumnál: {e}")
        elif assigned_new_asn:
            # Már korábban AI által feldolgozott Epson dokumentum utólagos sorszámozása
            try:
                doc = client.request("PATCH", f"/api/documents/{doc_id}/", {"archive_serial_number": assigned_new_asn})
                any_updated = True
                log(f"  -> Epson szkenner észlelve: Utólagos ASN sorszám #{assigned_new_asn} mentve a(z) #{doc_id} dokumentumhoz ({doc.get('title')})!")
            except Exception as ae:
                log(f"  ! Hiba az ASN #{assigned_new_asn} mentésekor #{doc_id} dokumentumnál: {ae}")

        dt_id = doc.get("document_type")
        dt_name = doc_types_by_id.get(dt_id, {}).get("name") if dt_id else "Kategorizálatlan"
        corr_id = doc.get("correspondent")
        corr_name = corrs_by_id.get(corr_id, {}).get("name") if corr_id else None
        tag_names = [tags_by_id[tid]["name"] for tid in doc.get("tags", []) if tid in tags_by_id]

        cf_readable = {}
        for cf in doc.get("custom_fields", []):
            fname = fields_by_id.get(cf["field"], {}).get("name", f"field_{cf['field']}")
            cf_readable[fname] = cf["value"]

        created_str = (doc.get("created") or doc.get("added") or "")[:10]
        year_str = created_str[:4] if len(created_str) >= 4 else "Ismeretlen_Ev"
        ai_info = ai_meta_store.get(str(doc_id))

        doc_json = {
            "id": doc_id,
            "title": doc.get("title"),
            "original_filename": doc.get("original_file_name"),
            "archived_filename": doc.get("archived_file_name"),
            "created_date": created_str,
            "added_at": doc.get("added"),
            "modified_at": doc.get("modified"),
            "category": dt_name,
            "correspondent": corr_name,
            "tags": tag_names,
            "archive_serial_number": doc.get("archive_serial_number"),
            "custom_fields": cf_readable,
            "ai_analysis": ai_info,
            "page_count": doc.get("page_count"),
            "ocr_text": doc.get("content") or "",
            "paths": {
                "paperless_web_url": f"{PAPERLESS_PUBLIC_URL}/documents/{doc_id}/details",
                "paperless_api_url": f"{PAPERLESS_PUBLIC_URL}/api/documents/{doc_id}/",
                "unraid_archive_pdf": (
                    f"{UNRAID_PUBLIC_BASE}/02_ARCHIVUM_PDF/documents/archive/{doc.get('archived_file_name')}"
                    if doc.get("archived_file_name")
                    else None
                ),
                "unraid_original_file": (
                    f"{UNRAID_PUBLIC_BASE}/02_ARCHIVUM_PDF/documents/originals/{doc.get('original_file_name')}"
                    if doc.get("original_file_name")
                    else None
                ),
            },
        }

        doc_asn = doc.get("archive_serial_number")
        if is_epson_scan and doc_asn:
            prev_last = load_last_epson_scan()
            prev_asn = prev_last.get("asn") or 0
            if assigned_new_asn or int(doc_asn) >= int(prev_asn):
                new_last_scan = {
                    "id": doc_id,
                    "asn": int(doc_asn),
                    "title": doc.get("title") or doc.get("original_file_name"),
                    "category": dt_name,
                    "correspondent": corr_name or "Ismeretlen",
                    "amount": cf_readable.get("Összeg") or (f"{cf_readable.get('Összeg (Ft)')} Ft" if cf_readable.get("Összeg (Ft)") else None),
                    "created_date": created_str,
                    "processed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if assigned_new_asn else (prev_last.get("processed_at") or created_str),
                    "paperless_url": f"{PAPERLESS_PUBLIC_URL}/documents/{doc_id}/details",
                }
                if new_last_scan != prev_last:
                    save_last_epson_scan(new_last_scan)

        mod_sig = f"{doc.get('modified')}_{dt_name}_{doc.get('title')}_{len(cf_readable)}_{doc.get('archive_serial_number')}"
        if exported_cache.get(doc_id) != mod_sig:
            flat_path = JSON_OUTPUT_DIR / "osszes" / f"{doc_id:05d}.json"
            flat_path.write_text(json.dumps(doc_json, ensure_ascii=False, indent=2), encoding="utf-8")

            for old_f in (JSON_OUTPUT_DIR / "kategorizalt").rglob(f"{doc_id:05d}_*.json"):
                try:
                    old_f.unlink()
                except OSError:
                    pass

            cat_dir = JSON_OUTPUT_DIR / "kategorizalt" / safe_folder_name(dt_name) / safe_folder_name(year_str)
            cat_dir.mkdir(parents=True, exist_ok=True)
            cat_filename = f"{doc_id:05d}_{created_str}_{slugify(doc.get('title') or 'dok')}.json"
            cat_path = cat_dir / cat_filename
            cat_path.write_text(json.dumps(doc_json, ensure_ascii=False, indent=2), encoding="utf-8")

            exported_cache[doc_id] = mod_sig
            any_updated = True
            log(f"  [JSON EXPORT] #{doc_id} -> {cat_path.relative_to(JSON_OUTPUT_DIR)}")

        catalog_entries.append(
            {
                "id": doc_id,
                "title": doc.get("title"),
                "created_date": created_str,
                "category": dt_name,
                "correspondent": corr_name,
                "tags": tag_names,
                "archive_serial_number": doc.get("archive_serial_number"),
                "custom_fields": cf_readable,
                "json_file": f"osszes/{doc_id:05d}.json",
                "archive_pdf": doc_json["paths"]["unraid_archive_pdf"],
            }
        )

    index_path = JSON_OUTPUT_DIR / "katalogus_index.json"
    prov_now, mod_now = active_ai_provider_with_model()
    active_str = f"{prov_now}:{mod_now}"
    prev_prov = None
    if index_path.exists():
        try:
            prev_prov = json.loads(index_path.read_text(encoding="utf-8")).get("active_ai_provider")
        except Exception:
            pass
    if any_updated or not index_path.exists() or prev_prov != active_str:
        master_index = {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "total_documents": len(catalog_entries),
            "active_ai_provider": active_str,
            "documents": sorted(catalog_entries, key=lambda x: x["id"], reverse=True),
        }
        index_path.write_text(json.dumps(master_index, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    prov_init, mod_init = active_ai_provider_with_model()
    log(f"Háztartási AI Kategorizáló és JSON Export Daemon indul (Aktív motor: {prov_init}:{mod_init})...")
    start_oauth_proxy_server()
    start_staging_watcher_thread()
    ensure_consume_folders()
    client = PaperlessClient()

    while True:
        try:
            client.request("GET", "/api/documents/?page_size=1")
            log("Paperless-ngx API kapcsolat él!")
            break
        except Exception as e:
            log(f"Várakozás a Paperless-ngx elindulására... ({e})")
            time.sleep(5)

    seed_household_taxonomy(client)

    exported_cache: dict[int, str] = {}
    while True:
        try:
            ensure_consume_folders()
            process_and_sync(client, exported_cache)
        except Exception as e:
            log(f"Szinkronizációs ciklus hiba: {e}")

        for _ in range(SYNC_INTERVAL):
            if any(JSON_OUTPUT_DIR.glob(".trigger_sync_*")):
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
