"""
One-shot migration: download every Drive justificatif referenced in the Google
Sheets, re-upload it to Firebase Storage at expenses/YYYY-MM/Category/, and
rewrite the sheet cell with the new public URL.

Idempotent: rows already pointing at storage.googleapis.com are skipped.

Usage:
    python migrate_drive_to_firebase.py --dry   # report only
    python migrate_drive_to_firebase.py         # actually migrate
"""
import io
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime

import gspread
import requests
from google.oauth2.service_account import Credentials as ServiceCreds
from google.oauth2.credentials import Credentials as UserCreds
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import firebase_admin
from firebase_admin import credentials, storage

GOOGLE_KEY = "/root/google-service-account.json"
TOKEN_FILE = "/root/accounting-bot/oauth_token.json"
SA_KEY = "/root/firebase-service-account.json"
BUCKET_NAME = "benim-car-rent.firebasestorage.app"
SPREADSHEET = "Benim Car - Fiche Année 2025"

# 1-based column indices
SHEETS = {
    "Dépenses Voitures": {"date_col": 1, "cat_col": 2, "url_col": 7},
    "Dépense Général":   {"date_col": 1, "cat_col": 2, "url_col": 6},
}

SCOPES_SHEETS = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

DRIVE_RE = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)([A-Za-z0-9_-]+)")
FB_RE = re.compile(r"storage\.googleapis\.com/.*benim-car-rent")


def get_drive_service():
    """Returns a Drive service, or None if OAuth token is unusable."""
    try:
        with open(TOKEN_FILE) as f:
            td = json.load(f)
        creds = UserCreds(
            token=td["token"], refresh_token=td["refresh_token"],
            token_uri=td["token_uri"], client_id=td["client_id"],
            client_secret=td["client_secret"], scopes=td["scopes"],
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            td["token"] = creds.token
            with open(TOKEN_FILE, "w") as f:
                json.dump(td, f)
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"WARN: Drive OAuth unavailable ({e}); will fall back to public HTTP download")
        return None


def get_book():
    creds = ServiceCreds.from_service_account_file(GOOGLE_KEY, scopes=SCOPES_SHEETS)
    return gspread.authorize(creds).open(SPREADSHEET)


def get_bucket():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.Certificate(SA_KEY),
            {"storageBucket": BUCKET_NAME},
        )
    return storage.bucket()


def parse_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def safe_cat(c):
    return (c or "Unknown").strip().replace("/", "-") or "Unknown"


def ext_from_mime(mime):
    if not mime:
        return ".jpg"
    if "jpeg" in mime or "jpg" in mime:
        return ".jpg"
    if "png" in mime:
        return ".png"
    if "pdf" in mime:
        return ".pdf"
    if "heic" in mime:
        return ".heic"
    return ".bin"


def download_drive(service, file_id, target_dir):
    """Download via OAuth API (private files); requires a valid token."""
    meta = service.files().get(fileId=file_id, fields="mimeType,name").execute()
    ext = ext_from_mime(meta.get("mimeType"))
    target = os.path.join(target_dir, f"{file_id}{ext}")
    req = service.files().get_media(fileId=file_id)
    with io.FileIO(target, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return target, ext, meta.get("mimeType")


def download_drive_public(file_id, target_dir):
    """Download a public Drive file (anyone-with-link reader) over plain HTTP."""
    r = requests.get(
        f"https://drive.google.com/uc?export=download&id={file_id}",
        allow_redirects=True, timeout=60,
    )
    r.raise_for_status()
    mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
    ext = ext_from_mime(mime)
    target = os.path.join(target_dir, f"{file_id}{ext}")
    with open(target, "wb") as fh:
        fh.write(r.content)
    return target, ext, mime


def upload_fb(bucket, src, blob_path, content_type):
    blob = bucket.blob(blob_path)
    blob.upload_from_filename(src, content_type=content_type or "image/jpeg")
    blob.make_public()
    return blob.public_url


def migrate(dry=False):
    drive = get_drive_service()
    book = get_book()
    bucket = get_bucket() if not dry else None

    summary = {"scanned": 0, "drive": 0, "already_fb": 0, "no_url": 0,
               "migrated": 0, "errors": 0}
    tmpdir = tempfile.mkdtemp(prefix="drive_migr_")

    for ws_name, info in SHEETS.items():
        ws = book.worksheet(ws_name)
        rows = ws.get_all_values()
        print(f"\n=== {ws_name}: {len(rows)} rows ===")
        updates = []  # batch updates to reduce API calls

        for i, row in enumerate(rows, start=1):
            if i == 1:
                continue  # header
            summary["scanned"] += 1

            url_idx = info["url_col"] - 1
            if url_idx >= len(row):
                summary["no_url"] += 1
                continue
            url = (row[url_idx] or "").strip()
            if not url:
                summary["no_url"] += 1
                continue
            if FB_RE.search(url):
                summary["already_fb"] += 1
                continue
            m = DRIVE_RE.search(url)
            if not m:
                continue

            summary["drive"] += 1
            file_id = m.group(1)

            date = parse_date(row[info["date_col"] - 1] if len(row) > info["date_col"] - 1 else "")
            ym = date.strftime("%Y-%m") if date else "unknown"
            cat = safe_cat(row[info["cat_col"] - 1] if len(row) > info["cat_col"] - 1 else "")

            if dry:
                print(f"  row {i}: {file_id} → expenses/{ym}/{cat}/{file_id}.<ext>")
                continue

            try:
                if drive is not None:
                    try:
                        local, ext, mime = download_drive(drive, file_id, tmpdir)
                    except Exception as e:
                        print(f"    OAuth download failed ({e}); trying public URL")
                        local, ext, mime = download_drive_public(file_id, tmpdir)
                else:
                    local, ext, mime = download_drive_public(file_id, tmpdir)
                blob_path = f"expenses/{ym}/{cat}/{file_id}{ext}"
                new_url = upload_fb(bucket, local, blob_path, mime)
                updates.append({
                    "range": gspread.utils.rowcol_to_a1(i, info["url_col"]),
                    "values": [[new_url]],
                })
                os.unlink(local)
                summary["migrated"] += 1
                print(f"  row {i}: {file_id} → {blob_path}")
            except Exception as e:
                summary["errors"] += 1
                print(f"  row {i}: ERROR {file_id}: {e}")

            # batch every 50 updates to avoid quota
            if len(updates) >= 50:
                ws.batch_update(updates)
                updates.clear()
                time.sleep(1)

        if updates:
            ws.batch_update(updates)

    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    migrate(dry="--dry" in sys.argv)
