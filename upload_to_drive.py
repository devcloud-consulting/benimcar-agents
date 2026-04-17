import os
import sys
import json
import mimetypes
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

def upload_image(file_path: str, filename: str) -> str:
    creds = get_credentials()
    service = build("drive", "v3", credentials=creds)

    mimetype, _ = mimetypes.guess_type(file_path)
    if not mimetype:
        mimetype = "image/jpeg"

    file_metadata = {
        "name": filename,
        "parents": [FOLDER_ID]
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

    link = f"https://drive.google.com/file/d/{file_id}/view"
    return link

if __name__ == "__main__":
    path = sys.argv[1]
    name = sys.argv[2]
    print(upload_image(path, name))
