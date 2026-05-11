"""
Upload expense justificatifs to Firebase Storage.

Path layout: expenses/YYYY-MM/Category/filename
Returns a public storage.googleapis.com URL.
"""
import os
import sys
import mimetypes
from datetime import datetime
import firebase_admin
from firebase_admin import credentials, storage

BUCKET_NAME = "benim-car-rent.firebasestorage.app"
SA_KEY = "/root/firebase-service-account.json"
APP_NAME = "storage_app"


def _get_bucket():
    try:
        app = firebase_admin.get_app(APP_NAME)
    except ValueError:
        cred = credentials.Certificate(SA_KEY)
        app = firebase_admin.initialize_app(
            cred, {"storageBucket": BUCKET_NAME}, name=APP_NAME
        )
    return storage.bucket(app=app)


def _build_blob_path(filename: str, expense_date: str | None, category: str | None) -> str:
    if expense_date and category:
        try:
            dt = datetime.strptime(expense_date, "%d/%m/%Y")
            ym = dt.strftime("%Y-%m")
        except ValueError:
            ym = "unknown"
        cat = category.strip().replace("/", "-")
        return f"expenses/{ym}/{cat}/{filename}"
    return f"expenses/unknown/{filename}"


def upload_image(file_path: str, filename: str,
                 expense_date: str | None = None,
                 category: str | None = None) -> str:
    bucket = _get_bucket()
    blob_path = _build_blob_path(filename, expense_date, category)
    blob = bucket.blob(blob_path)
    mime, _ = mimetypes.guess_type(file_path)
    blob.upload_from_filename(file_path, content_type=mime or "image/jpeg")
    blob.make_public()
    return blob.public_url


def upload_releve(file_path: str, compte: str, year_month: str) -> str:
    """Upload a monthly bank statement PDF.
    Path: releves/YYYY-MM/{compte}.pdf — one file per (month, account),
    so re-uploading replaces the existing entry."""
    bucket = _get_bucket()
    ext = os.path.splitext(file_path)[1] or ".pdf"
    blob_path = f"releves/{year_month}/{compte.lower()}{ext}"
    blob = bucket.blob(blob_path)
    mime, _ = mimetypes.guess_type(file_path)
    blob.upload_from_filename(file_path, content_type=mime or "application/pdf")
    blob.make_public()
    return blob.public_url


if __name__ == "__main__":
    path = sys.argv[1]
    name = sys.argv[2]
    date = sys.argv[3] if len(sys.argv) > 3 else None
    cat = sys.argv[4] if len(sys.argv) > 4 else None
    print(upload_image(path, name, date, cat))
