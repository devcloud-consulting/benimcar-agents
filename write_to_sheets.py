import sys
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials

SHEETS_EPOCH = datetime(1899, 12, 30)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

ALLOWED_CAR_CATEGORIES = [
    "Achat Voiture", "Maintenance", "Loyer", "Assurance",
    "Fuel", "Vignette", "Péage/Parking", "Controle Technique"
]

ALLOWED_GENERAL_CATEGORIES = [
    "Salaire", "Loyer", "Lavage", "Comptable", "Frais Bancaire",
    "CNSS Dirigeant", "Prestation", "Fourniture",
    "Indrive/Taxi/Transport", "Panier Repas"
]

ALLOWED_CARS = [
    "Sandero Noir : 57972-B-33",
    "Logan Grise - 57970-B-33",
    "Logan Grise - 57971-B-33",
    "Logan Noir -57981-B-33",
    "Clio V - 57937-B-33",
    "Kia Bleu - 57906-B-33",
    "Kia Verte -57908-B-33"
]

ALLOWED_PAYMENT_TYPES = ["Transfer", "Card", "Cash", "Chèque"]

def get_sheet(worksheet_name: str):
    creds = Credentials.from_service_account_file(
        "/root/google-service-account.json",
        scopes=SCOPES
    )
    client = gspread.authorize(creds)
    return client.open("Benim Car - Fiche Année 2025").worksheet(worksheet_name)


def sort_by_date(sheet) -> None:
    """Defensive utility: sort everything below the header by column A
    ascending. Not used in the normal write path — kept for one-off
    cleanups or if the sheet is ever edited manually."""
    last_row = len(sheet.get_all_values())
    if last_row <= 1:
        return
    end_col = gspread.utils.rowcol_to_a1(1, sheet.col_count).rstrip("0123456789")
    sheet.sort((1, "asc"), range=f"A2:{end_col}{last_row}")


def _cell_to_date(cell) -> datetime | None:
    """Parse a column-A cell (Sheets serial int/float or string) to a
    datetime. Returns None if unparseable."""
    if cell == "" or cell is None:
        return None
    if isinstance(cell, (int, float)):
        return SHEETS_EPOCH + timedelta(days=int(cell))
    s = str(cell).strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d/%m/%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def find_insert_row(sheet, new_date_iso: str) -> int:
    """Returns the 1-based row index at which to insert a new dépense so
    the sheet stays sorted by column A ascending. If no existing row has a
    later date, returns the row after the last data row (i.e. append)."""
    new_dt = datetime.strptime(new_date_iso, "%Y-%m-%d")
    rows = sheet.get("A2:A", value_render_option="UNFORMATTED_VALUE")
    for i, row in enumerate(rows, start=2):
        cell = row[0] if row else None
        cell_dt = _cell_to_date(cell)
        if cell_dt is None:
            continue
        if cell_dt > new_dt:
            return i
    return len(rows) + 2

def write_car_expense(date_raw, categorie, details, montant, voiture, paiement, lien):
    try:
        date = datetime.strptime(date_raw, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        date = date_raw

    if categorie not in ALLOWED_CAR_CATEGORIES:
        raise ValueError(f"Catégorie non autorisée: {categorie}")
    if voiture not in ALLOWED_CARS:
        raise ValueError(f"Voiture non autorisée: {voiture}")
    if paiement not in ALLOWED_PAYMENT_TYPES:
        raise ValueError(f"Type de paiement non autorisé: {paiement}")

    sheet = get_sheet("Dépenses Voitures")
    insert_at = find_insert_row(sheet, date)
    sheet.insert_rows([[date, categorie, details, montant, voiture, paiement, lien]], row=insert_at)
    print("OK")

def write_general_expense(date_raw, categorie, details, montant, paiement, lien):
    try:
        date = datetime.strptime(date_raw, "%d/%m/%Y").strftime("%Y-%m-%d")
    except ValueError:
        date = date_raw

    if categorie not in ALLOWED_GENERAL_CATEGORIES:
        raise ValueError(f"Catégorie non autorisée: {categorie}")
    if paiement not in ALLOWED_PAYMENT_TYPES:
        raise ValueError(f"Type de paiement non autorisé: {paiement}")

    sheet = get_sheet("Dépense Général")
    insert_at = find_insert_row(sheet, date)
    sheet.insert_rows([[date, categorie, details, montant, paiement, lien]], row=insert_at)
    print("OK")

if __name__ == "__main__":
    sheet_type = sys.argv[1]  # "car" or "general"
    if sheet_type == "car":
        write_car_expense(
            sys.argv[2], sys.argv[3], sys.argv[4],
            sys.argv[5], sys.argv[6], sys.argv[7], sys.argv[8]
        )
    elif sheet_type == "general":
        write_general_expense(
            sys.argv[2], sys.argv[3], sys.argv[4],
            sys.argv[5], sys.argv[6], sys.argv[7]
        )
    else:
        raise ValueError(f"Type de feuille inconnu: {sheet_type}")
