#!/usr/bin/env python3
"""
Automatizált Gmail PDF Betakarító és Beolvasó (Batch Harvester & Ingest Pipeline)
================================================================================
Kikeresi és letölti a megadott Gmail fiókból (kismenti@gmail.com) az összes
számlát, közüzemi számlát, bizonylatot, nyugtát, jótállási jegyet, rendelés visszaigazolást,
biztosítási kötvényt és pénzügyi dokumentumot, majd ellenőrzött kötegekben (batch)
átadja a Paperless-ngx és a Háztartási AI Daemon feldolgozó rendszerének.
"""

import sys
import os
import json
import time
import email
import hashlib
import re
import imaplib
import subprocess
import datetime
from pathlib import Path
from email.header import decode_header
import urllib.request
import base64

# --- Konfiguráció ---
CONSUME_DIR = Path("/mnt/hermes-workdir/DOKUMENTUMOK/01_BEOLVASAS_IDE/00_Automata_AI_Kategorizalas")
JSON_OUTPUT_DIR = Path("/mnt/hermes-workdir/DOKUMENTUMOK/03_JSON_ADATOK")
CACHE_FILE = JSON_OUTPUT_DIR / ".gmail_ingested_cache.json"
STATUS_FILE = JSON_OUTPUT_DIR / "feldolgozas_statusz.json"
HEADERS_FILE = JSON_OUTPUT_DIR / ".gmail_pdf_headers.json"

UNRAID_HOST = "192.168.0.32"
HA_HOST = "192.168.0.99"
PAPERLESS_URL = "http://192.168.0.32:8010"
PAPERLESS_USER = "admin"
PAPERLESS_PASS = "menti2026"

DEFAULT_BATCH_SIZE = 15

# Kulcsszó szűrők a releváns levelek kiválasztásához
KW_INVOICE = re.compile(r"száml|szaml|faktur|faktúr|invoice|rechnung|účt|paragon", re.I)
KW_UTILITY = re.compile(r"mvm|dmrv|díjnet|dijnet|villany|földgáz|foldgaz|távhő|tavho|víz|vizdíj|vizmu|hulladék|szemét|e\.on|eon|elmu|emasz|tigaz|invitel|telekom|yettel|vodafone|telenor|upc|digi|konfer", re.I)
KW_WARRANTY = re.compile(r"jótáll|jotall|garanci|warranty", re.I)
KW_ORDER = re.compile(r"megrendel|rendel|vásárl|vasarl|order|potvrdenie|bestellung|alza|emag|allegro|ikea|mall\.hu|aqua|edigital|euronics|mediamarkt|decathlon|bauhaus|obi|praktiker|ticketbase|jegy", re.I)
KW_FINANCIAL = re.compile(r"kivonat|elszámol|elszamol|egyenleg|nyugta|bizonylat|fizetés|fizetes|biztosít|biztosit|kötvény|kotveny|díjbekér|dijbeker|otp|cib|erste|k&h|kh bank|mbh|revolut|allianz|generali|uniqa|union|aegon|posta|groupama|nav |nemzeti adó|szerződés|szerzodes|zmluva|igazolás|igazolas|adó|ado|tbsz|lightyear|casco|kgfb", re.I)
KW_JUNK = re.compile(r"mat_probazh|szoftver labor|fizika felzark|rosaries for the world|party invitation|hazi feladat|orarend", re.I)


def log(msg: str):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def decode_str(s) -> str:
    if not s:
        return ""
    res = []
    try:
        parts = decode_header(s)
        for text, enc in parts:
            if isinstance(text, bytes):
                try:
                    res.append(text.decode(enc or "utf-8", errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    res.append(text.decode("latin1", errors="replace"))
            else:
                res.append(str(text))
    except Exception:
        return str(s)
    return "".join(res).strip()


def slugify(text: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^\w\-_.]", "_", text)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:max_len]


def get_gmail_credentials() -> tuple[str, str]:
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if user and pwd:
        return user, pwd

    try:
        res = subprocess.run(
            ["ssh", "-o", "GSSAPIAuthentication=no", f"root@{HA_HOST}", "cat /config/.storage/core.config_entries"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        data = json.loads(res.stdout)
        for e in data.get("data", {}).get("entries", []):
            if e.get("domain") == "smtp":
                d = e.get("data", {})
                user = d.get("username", "kismenti@gmail.com")
                pwd = d.get("password")
                if user and pwd:
                    return user, pwd
    except Exception as e:
        log(f"Nem sikerült lekérni a jelszót a Home Assistantból: {e}")

    raise RuntimeError("Hiányzik a Gmail hozzáférés (GMAIL_USER / GMAIL_APP_PASSWORD).")


def connect_imap(user: str, pwd: str) -> imaplib.IMAP4_SSL:
    for attempt in range(5):
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mail.login(user, pwd)
            mail.select("\"[Gmail]/All Mail\"", readonly=True)
            return mail
        except Exception as e:
            log(f"IMAP csatlakozási hiba (próbálkozás {attempt+1}/5): {e}")
            time.sleep(3)
    raise RuntimeError("Nem sikerült csatlakozni az IMAP szerverhez.")


def get_paperless_existing_checksums() -> set[str]:
    try:
        res = subprocess.run(
            ["ssh", f"root@{UNRAID_HOST}", "docker exec paperless-db psql -U paperless -d paperless -t -A -c 'SELECT checksum FROM documents_document;'"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        checksums = set(line.strip().lower() for line in res.stdout.splitlines() if line.strip())
        return checksums
    except Exception as e:
        log(f"Figyelem: PostgreSQL checksum lekérdezési hiba: {e}")
        return set()


def get_paperless_unanalyzed_count() -> int:
    try:
        creds = base64.b64encode(f"{PAPERLESS_USER}:{PAPERLESS_PASS}".encode()).decode()
        headers = {"Authorization": f"Basic {creds}", "Accept": "application/json"}
        req_docs = urllib.request.Request(f"{PAPERLESS_URL}/api/documents/?tags__id__none=1", headers=headers)
        with urllib.request.urlopen(req_docs, timeout=10) as r:
            docs_data = json.loads(r.read().decode())
            return docs_data.get("count", 0)
    except Exception as e:
        log(f"Paperless API ellenőrzési hiba: {e}")
        return 0


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def save_status(status_data: dict):
    STATUS_FILE.write_text(json.dumps(status_data, ensure_ascii=False, indent=2), encoding="utf-8")


def trigger_household_agent():
    trig_file = JSON_OUTPUT_DIR / f".trigger_sync_{int(time.time())}"
    try:
        trig_file.touch()
    except Exception:
        pass


def is_candidate(h: dict) -> tuple[bool, list[str]]:
    text = (h.get("subject") or "") + " " + (h.get("from") or "") + " " + " ".join(h.get("attachments", []))
    if KW_JUNK.search(text):
        return False, []

    reasons = []
    if KW_INVOICE.search(text):
        reasons.append("szamla")
    if KW_UTILITY.search(text):
        reasons.append("rezsi")
    if KW_WARRANTY.search(text):
        reasons.append("jotallas")
    if KW_ORDER.search(text):
        reasons.append("megrendeles")
    if KW_FINANCIAL.search(text):
        reasons.append("penzugy")

    sender = (h.get("from") or "").lower()
    if ("kismenti" in sender or "papdanna" in sender or "kasnyik" in sender) and bool(h.get("attachments")):
        reasons.append("csaladi_tovabbitott")

    return bool(reasons), reasons


def parse_date_to_sort_key(date_str: str) -> str:
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d_%H%M%S")
    except Exception:
        m = re.search(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})", date_str or "")
        if m:
            months = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
                      "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}
            d, mon, y = m.group(1).zfill(2), months.get(m.group(2), "01"), m.group(3)
            return f"{y}-{mon}-{d}_000000"
        return "1970-01-01_000000"


def fetch_live_candidates(mail: imaplib.IMAP4_SSL, days: int = 14) -> list[dict]:
    """Közvetlen IMAP keresés az elmúlt N nap PDF-mellékletes leveleire (X-GM-RAW)."""
    query = f'"has:attachment filename:pdf newer_than:{days}d"'
    log(f"Élő Gmail IMAP keresés indítása: {query}...")
    typ, data = mail.search(None, "X-GM-RAW", query)
    if typ != "OK" or not data or not data[0]:
        return []
    msg_ids = [mid.decode("utf-8") if isinstance(mid, bytes) else str(mid) for mid in data[0].split()]
    log(f"Talált PDF-mellékletes levelek az elmúlt {days} napból: {len(msg_ids)} db")
    headers_list = []
    for mid in msg_ids:
        try:
            res, h_data = mail.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
            if res != "OK" or not h_data or not isinstance(h_data[0], tuple):
                continue
            hdr_msg = email.message_from_bytes(h_data[0][1])
            subj = decode_str(hdr_msg.get("Subject", ""))
            sender = decode_str(hdr_msg.get("From", ""))
            date_str = decode_str(hdr_msg.get("Date", ""))
            headers_list.append({
                "id": mid,
                "subject": subj,
                "from": sender,
                "date": date_str,
                "attachments": ["attachment.pdf"],
            })
        except Exception as e:
            log(f"  Fejléc lekérdezési hiba #{mid}: {e}")
    return headers_list


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Gmail PDF Számla Betakarító")
    parser.add_argument("--live", action="store_true", help="Élő IMAP keresés az elmúlt N nap leveleire (nem a statikus fejlécfájlból)")
    parser.add_argument("--days", type=int, default=14, help="Hány napra visszamenőleg keressen élő módban (alapértelmezett: 14)")
    args, _ = parser.parse_known_args()

    log("================================================================================")
    log("GMAIL TELJES DOKUMENTUM BETAKARÍTÓ ÉS BEOLVASÓ PIPELINE INDÍTÁSA")
    log("================================================================================")

    CONSUME_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cache = load_cache()
    log(f"Helyi gyorsítótárban nyilvántartott fájlok: {len(cache)} db")

    existing_db_checksums = get_paperless_existing_checksums()
    log(f"Paperless adatbázisban már meglévő dokumentumok: {len(existing_db_checksums)} db")

    user, pwd = get_gmail_credentials()
    log(f"IMAP csatlakozás a(z) {user} fiókhoz...")
    mail = connect_imap(user, pwd)
    log("IMAP kapcsolat sikeresen felépült!")

    if args.live or not HEADERS_FILE.exists():
        headers_data = fetch_live_candidates(mail, days=args.days)
    else:
        headers_data = json.loads(HEADERS_FILE.read_text(encoding="utf-8"))
        log(f"Összes PDF-et tartalmazó email a fiókban: {len(headers_data)} db")

    candidates = []
    for h in headers_data:
        ok, reasons = is_candidate(h)
        if ok:
            h["sort_key"] = parse_date_to_sort_key(h.get("date", ""))
            h["match_reasons"] = reasons
            candidates.append(h)

    # Legújabbaktól a legrégebbiek felé (2026 -> 2008)
    candidates.sort(key=lambda x: x["sort_key"], reverse=True)
    log(f"Kiválasztott releváns számla/rezsi/jótállás/megrendelés emailek: {len(candidates)} db")

    batch_size = int(os.environ.get("BATCH_SIZE", DEFAULT_BATCH_SIZE))
    limit = int(os.environ.get("LIMIT", "0"))
    if limit > 0:
        candidates = candidates[:limit]
        log(f"LIMIT beállítva: csak az első {limit} email kerül feldolgozásra.")

    total_candidates = len(candidates)
    ingested_count = 0
    skipped_count = 0
    active_batch_files = []

    status = {
        "status": "running",
        "started_at": datetime.datetime.now().isoformat(),
        "total_target_emails": total_candidates,
        "processed_emails": 0,
        "ingested_pdfs": 0,
        "skipped_existing_pdfs": 0,
        "last_email": None,
    }
    save_status(status)

    for idx, cand in enumerate(candidates, 1):
        mid = cand["id"]
        subj = cand.get("subject") or "Nincs tárgy"
        sender = cand.get("from") or "Ismeretlen feladó"
        date_str = cand.get("date") or ""
        sort_date = cand.get("sort_key", "")[:10]

        log(f"[{idx}/{total_candidates}] #{mid} ({sort_date}) | {sender[:30]} | {subj[:50]}")

        # IMAP letöltés újrapróbálkozással és reconnecttel
        data_fetch = None
        for fetch_attempt in range(3):
            try:
                res, data_fetch = mail.fetch(mid, "(RFC822)")
                if res == "OK" and data_fetch and isinstance(data_fetch[0], tuple):
                    break
            except Exception as e:
                log(f"  IMAP hiba #{mid} letöltésekor: {e}, újracsatlakozás...")
                time.sleep(2)
                try:
                    mail = connect_imap(user, pwd)
                except Exception:
                    pass

        if not data_fetch or not isinstance(data_fetch[0], tuple):
            log(f"  ! Nem sikerült letölteni a levelet (#{mid}) - kihagyva.")
            continue

        try:
            msg = email.message_from_bytes(data_fetch[0][1])

            for part in msg.walk():
                ct = (part.get_content_type() or "").lower()
                raw_fn = part.get_filename() or part.get_param("name") or ""
                decoded_fn = decode_str(raw_fn)

                is_pdf = ct == "application/pdf" or decoded_fn.lower().endswith(".pdf")
                if not is_pdf:
                    continue

                payload = part.get_payload(decode=True)
                if not payload or len(payload) < 200:
                    continue
                if not payload.startswith(b"%PDF"):
                    idx_magic = payload.find(b"%PDF")
                    if idx_magic != -1 and idx_magic < 1024:
                        payload = payload[idx_magic:]
                    else:
                        continue

                sha256 = hashlib.sha256(payload).hexdigest()

                if sha256 in existing_db_checksums or (sha256 in cache and cache[sha256].get("status") in ("ingested", "processed")):
                    skipped_count += 1
                    cache[sha256] = {
                        "status": "ingested",
                        "msg_id": mid,
                        "date": date_str,
                        "filename": decoded_fn,
                        "subject": subj,
                        "sender": sender,
                    }
                    continue

                clean_partner = slugify(re.sub(r"<.*?>", "", sender).replace('"', '').strip(), max_len=20)
                clean_subj = slugify(subj, max_len=30)
                base_fn = slugify(Path(decoded_fn).stem, max_len=25)
                out_filename = f"mail_{sort_date}_{clean_partner}_{clean_subj}_{base_fn}_{sha256[:6]}.pdf"
                out_path = CONSUME_DIR / out_filename

                out_path.write_bytes(payload)
                try:
                    out_path.chmod(0o666)
                except Exception:
                    pass

                log(f"  >>> ÚJ PDF BEOLVASÁSRA ELHELYEZVE: {out_filename} ({len(payload)} bájt)")
                ingested_count += 1
                active_batch_files.append(out_filename)
                existing_db_checksums.add(sha256)
                cache[sha256] = {
                    "status": "queued",
                    "path": str(out_path),
                    "filename": decoded_fn,
                    "msg_id": mid,
                    "date": date_str,
                    "subject": subj,
                    "sender": sender,
                    "queued_at": datetime.datetime.now().isoformat(),
                }

        except Exception as err:
            log(f"  ! Hiba az email csatolmány feldolgozása közben (#{mid}): {err}")

        # Köteg ellenőrzése
        if len(active_batch_files) >= batch_size:
            log(f"--- KÖTEG ELÉRVE ({len(active_batch_files)} fájl) ---")
            save_cache(cache)
            status["processed_emails"] = idx
            status["ingested_pdfs"] = ingested_count
            status["skipped_existing_pdfs"] = skipped_count
            status["last_email"] = f"{mid} - {subj}"
            save_status(status)

            trigger_household_agent()

            log("Várakozás a Paperless beolvasó motorjára...")
            wait_start = time.time()
            while True:
                remaining_in_consume = len(list(CONSUME_DIR.glob("*.pdf")))
                if remaining_in_consume == 0:
                    log("  ✓ Paperless beolvasta a köteg fájljait.")
                    break
                if time.time() - wait_start > 120:
                    log(f"  Figyelem: {remaining_in_consume} fájl még a consume mappában van, folytatás...")
                    break
                time.sleep(4)

            unanalyzed = get_paperless_unanalyzed_count()
            log(f"Jelenleg AI elemzésre / OCR-re váró dokumentumok a Paperless-ben: {unanalyzed} db")
            while unanalyzed > 12:
                log(f"  Várakozás a háttérfolyamatokra ({unanalyzed} db aktív)...")
                trigger_household_agent()
                time.sleep(10)
                unanalyzed = get_paperless_unanalyzed_count()

            active_batch_files = []

        # Rendszeres állapot mentés minden 10. email után
        if idx % 10 == 0:
            save_cache(cache)
            status["processed_emails"] = idx
            status["ingested_pdfs"] = ingested_count
            status["skipped_existing_pdfs"] = skipped_count
            save_status(status)

    if active_batch_files:
        save_cache(cache)
        trigger_household_agent()
        active_batch_files = []

    log("================================================================================")
    log("MINDEN KIVÁLASZTOTT EMAIL ÁTVIZSGÁLVA ÉS BETAKARÍTVA!")
    log(f"Összesen beolvasott új PDF: {ingested_count} db")
    log(f"Kihagyott már meglévő PDF: {skipped_count} db")
    log("Várakozás a háttérben futó OCR és Háztartási AI folyamatok teljes lefutására...")

    if ingested_count > 0:
        final_wait_start = time.time()
        while True:
            remaining_consume = len(list(CONSUME_DIR.glob("*.pdf")))
            unanalyzed = get_paperless_unanalyzed_count()
            if remaining_consume == 0 and unanalyzed == 0:
                log("✓ Minden dokumentum sikeresen OCR-ezve, AI-val kategorizálva és JSON-be exportálva!")
                break
            log(f"Hátralévő feldolgozandó elemek: {remaining_consume} consume, {unanalyzed} AI elemzésre vár...")
            trigger_household_agent()
            time.sleep(12)
            if time.time() - final_wait_start > 180:
                log("Időtúllépés a záró várakozásnál, a daemon a háttérben önállóan befejezi a munkát.")
                break

    try:
        mail.logout()
    except Exception:
        pass

    now_iso = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status["status"] = "completed"
    status["completed_at"] = now_iso
    status["processed_emails"] = total_candidates
    status["ingested_pdfs"] = ingested_count
    status["skipped_existing_pdfs"] = skipped_count
    save_status(status)
    try:
        (JSON_OUTPUT_DIR / "last_gmail_sync.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    log("================================================================================")
    log("GMAIL PDF PIPELINE TELJESEN ÉS SIKERESEN BEFEJEZŐDÖTT!")
    log("================================================================================")


if __name__ == "__main__":
    main()
