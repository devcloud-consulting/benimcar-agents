"""
One-time cleanup: removes rows appended by the broken sync.
Detects them by car name format "Make Model (plate)" which was never
in the sheet before the bad sync. Rows with the old short names
(SANDERO, CLIO 5, etc.) are kept intact.
"""
import gspread
from google.oauth2.service_account import Credentials

GOOGLE_KEY = "/root/google-service-account.json"
SPREADSHEET_NAME = "Benim Car - Fiche Année 2025"
INCOME_SHEET = "Income"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def looks_like_new_format(car_name: str) -> bool:
    """Returns True if car name looks like it came from the broken sync (e.g. 'Dacia Sandero (57972-B-33)')."""
    name = car_name.strip()
    # New format: contains a plate in parentheses with dashes like 57972-B-33
    import re
    return bool(re.search(r'\(\d{5}-[A-Z]-\d{2}\)', name))

def cleanup():
    creds = Credentials.from_service_account_file(GOOGLE_KEY, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open(SPREADSHEET_NAME).worksheet(INCOME_SHEET)

    all_rows = sheet.get_all_values()
    header = all_rows[0]
    data_rows = all_rows[1:]

    kept = []
    removed = 0
    for row in data_rows:
        car_name = row[4].strip() if len(row) > 4 else ""
        if looks_like_new_format(car_name):
            removed += 1
        else:
            kept.append(row)

    print(f"Rows to remove: {removed}, rows to keep: {len(kept)}")

    # Rewrite sheet with only kept rows
    sheet.clear()
    sheet.update([header] + kept, value_input_option="USER_ENTERED")
    print("Done.")

if __name__ == "__main__":
    cleanup()
