import firebase_admin
from firebase_admin import credentials, firestore
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

FIREBASE_KEY = "/root/firebase-readonly.json"
GOOGLE_KEY = "/root/google-service-account.json"
SPREADSHEET_NAME = "Benim Car - Fiche Année 2025"
INCOME_SHEET = "Income"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

PAYMENT_STATUS_MAP = {
    "fully_paid": "OUI",
    "partial": "PARTIEL",
    "not_paid": "NON",
    None: "NON",
}

SOURCE_MAP = {
    "partner": "Partenaire",
    "personalNetwork": "Bouche à oreille",
    "other": "Direct",
    "online": "Online",
    None: "",
}

def get_firestore():
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_KEY)
        firebase_admin.initialize_app(cred)
    elif "default" not in firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_KEY)
        firebase_admin.initialize_app(cred)
    return firestore.client()

def get_income_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_KEY, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME).worksheet(INCOME_SHEET)

def format_date(date_str: str) -> str:
    """Convert YYYY-MM-DD to DD-Mon-YYYY to match existing sheet format."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%-d-%b-%Y")
    except Exception:
        return date_str

def sync_bookings() -> dict:
    db = get_firestore()

    # Load all cars and customers for lookup
    cars = {doc.id: doc.to_dict() for doc in db.collection("cars").stream()}
    customers = {doc.id: doc.to_dict() for doc in db.collection("customers").stream()}

    # Load all bookings (exclude cancelled)
    all_bookings = [
        doc.to_dict() for doc in db.collection("bookings").stream()
        if doc.to_dict().get("status") not in ("cancelled", "canceled")
    ]

    # Load existing income sheet rows to detect duplicates
    sheet = get_income_sheet()
    existing_rows = sheet.get_all_values()

    # Build dedup set: startDate + carId + totalAmount
    existing_keys = set()
    for row in existing_rows[1:]:
        if len(row) >= 6:
            existing_keys.add(f"{row[0]}|{row[4]}|{row[5]}")

    new_rows = []
    skipped = 0

    for b in all_bookings:
        car_id = b.get("carId", "")
        car = cars.get(car_id, {})
        car_name = f"{car.get('make', '')} {car.get('model', '')} ({car.get('licensePlate', '')})".strip()

        customer_id = b.get("customerId", "")
        customer = customers.get(customer_id, {})
        customer_name = customer.get("name", "")
        customer_phone = customer.get("phone", "")

        start_date = format_date(b.get("startDate", ""))
        end_date = format_date(b.get("endDate", ""))
        total_amount = str(b.get("totalAmount", "0"))
        daily_rate = str(b.get("dailyRate", ""))
        total_days = str(b.get("totalDays", ""))
        paye = PAYMENT_STATUS_MAP.get(b.get("paymentStatus"), "NON")
        source = SOURCE_MAP.get(b.get("source"), b.get("source", ""))
        comments = b.get("comments", "")

        # Commission: only set if source is partner
        commission = "0"

        dedup_key = f"{start_date}|{car_name}|{total_amount}"
        if dedup_key in existing_keys:
            skipped += 1
            continue

        new_rows.append([
            start_date,
            end_date,
            total_days,
            daily_rate,
            car_name,
            total_amount,
            "Dirham",
            commission,
            paye,
            source,
            customer_name,
            customer_phone,
            comments,
        ])
        existing_keys.add(dedup_key)

    if new_rows:
        sheet.append_rows(new_rows, value_input_option="USER_ENTERED")

    return {
        "added": len(new_rows),
        "skipped": skipped,
        "total_firestore": len(all_bookings),
    }

if __name__ == "__main__":
    result = sync_bookings()
    print(f"Sync complete: {result['added']} added, {result['skipped']} already existed ({result['total_firestore']} total in Firestore)")
