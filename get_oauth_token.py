from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = ["https://www.googleapis.com/auth/drive"]

flow = InstalledAppFlow.from_client_secrets_file(
    "/root/accounting-bot/oauth_client.json",
    scopes=SCOPES
)

flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
auth_url, _ = flow.authorization_url(prompt="consent")
print(f"\nOpen this URL in your browser:\n\n{auth_url}\n")
code = input("Paste the authorization code here: ")

flow.fetch_token(code=code)
creds = flow.credentials

token_data = {
    "token": creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri": creds.token_uri,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "scopes": list(creds.scopes)
}

with open("/root/accounting-bot/oauth_token.json", "w") as f:
    json.dump(token_data, f)

print("✅ Token saved to oauth_token.json")
