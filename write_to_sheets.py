import sys
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

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
    next_row = len(sheet.get_all_values()) + 1
    sheet.insert_rows([[date, categorie, details, montant, voiture, paiement, lien]], row=next_row)
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
    next_row = len(sheet.get_all_values()) + 1
    sheet.insert_rows([[date, categorie, details, montant, paiement, lien]], row=next_row)
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
