import os
import sys
import json
import mimetypes
from datetime import datetime
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

FOLDER_ID = "1YmdEHuDQXD0m8Yu_2UXWtBAjfPa6zpTV"
TOKEN_FILE = "/root/accounting-bot/oauth_token.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]

def get_credentials():
    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"]
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(TOKEN_FILE, "w") as f:
            json.dump(token_data, f)

    return creds

def get_or_create_folder(service, name: str, parent_id: str) -> str:
    query = (
        f"name='{name}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    results = service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]

def get_target_folder(service, expense_date: str, category: str) -> str:
    try:
        dt = datetime.strptime(expense_date, "%d/%m/%Y")
        semester = "S1" if dt.month <= 6 else "S2"
        semester_name = f"{dt.year}-{semester}"
    except Exception:
        return FOLDER_ID

    semester_id = get_or_create_folder(service, semester_name, FOLDER_ID)
    category_id = get_or_create_folder(service, category, semester_id)
    return category_id

def upload_image(file_path: str, filename: str, expense_date: str = None, category: str = None) -> str:
    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    mimetype, _ = mimetypes.guess_type(file_path)
    if not mimetype:
        mimetype = "image/jpeg"

    if expense_date and category:
        target_folder = get_target_folder(service, expense_date, category)
    else:
        target_folder = FOLDER_ID

    file_metadata = {
        "name": filename,
        "parents": [target_folder]
    }

    media = MediaFileUpload(file_path, mimetype=mimetype, resumable=False)

    uploaded = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()

    file_id = uploaded.get("id")

    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()

    return f"https://drive.google.com/file/d/{file_id}/view"

if __name__ == "__main__":
    path = sys.argv[1]
    name = sys.argv[2]
    print(upload_image(path, name))
