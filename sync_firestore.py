import re
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

PARTNER_COMMISSION_PER_DAY = 50  # DH per day

PAYMENT_STATUS_MAP = {
    "fully_paid": "OUI",
    "partial": "PARTIEL",
    "not_paid": "NON",
    None: "NON",
}

SOURCE_MAP = {
    "personalNetwork": "Bouche à oreille",
    "other": "Direct",
    "online": "Online",
    None: "",
}

# Column indices (0-based), ID is col 0
COL_ID         = 0
COL_START      = 1
COL_END        = 2
COL_DAYS       = 3
COL_RATE       = 4
COL_CAR        = 5
COL_AMOUNT     = 6
COL_CURRENCY   = 7
COL_COMMISSION = 8
COL_PAID       = 9
COL_SOURCE     = 10
COL_CLIENT     = 11
COL_PHONE      = 12
COL_NOTES      = 13

NEW_HEADER = [
    "ID", "Allez", "Retour", "Jours", "Prix(DH)", "Voiture",
    "Vente (DH)", "Currency", "Commissions", "Payé",
    "Provenance du Client", "Nom Client", "Téléphone", "Notes"
]


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


def parse_firestore_date(date_str: str) -> datetime:
    date_str = date_str.strip().split("T")[0]
    return datetime.strptime(date_str, "%Y-%m-%d")


def format_date(date_str: str) -> str:
    try:
        return parse_firestore_date(date_str).strftime("%-d-%b-%Y")
    except Exception:
        return date_str


def parse_sheet_date(date_str: str):
    for fmt in ["%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%B-%Y"]:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except Exception:
            continue
    return None


def normalize_phone(phone: str) -> str:
    return re.sub(r"\s+", "", phone.strip()) if phone else ""


def canonical_car_name(car: dict) -> str:
    make = car.get("make", "").strip()
    model = car.get("model", "").strip()
    plate = car.get("licensePlate", "").strip()
    name = f"{make} {model}".strip()
    if plate:
        name = f"{name} ({plate})"
    return name


def booking_to_row(b: dict, car_name: str, customer: dict, provenance: str, commission: str) -> list:
    return [
        b.get("id", ""),
        format_date(b.get("startDate", "")),
        format_date(b.get("endDate", "")),
        str(b.get("totalDays", "")),
        str(b.get("dailyRate", "")),
        car_name,
        str(b.get("totalAmount", "0")),
        "Dirham",
        commission,
        PAYMENT_STATUS_MAP.get(b.get("paymentStatus"), "NON"),
        provenance,
        customer.get("name", ""),
        normalize_phone(customer.get("phone", "")),
        b.get("comments", ""),
    ]


def col_letter(n: int) -> str:
    """Convert 1-based column index to letter (1=A, 14=N)."""
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def sync_bookings() -> dict:
    db = get_firestore()

    # --- Load reference data ---
    cars = {doc.id: doc.to_dict() for doc in db.collection("cars").stream()}
    carid_to_canonical = {}
    for car_id, car in cars.items():
        carid_to_canonical[car_id] = canonical_car_name(car)

    customers = {doc.id: doc.to_dict() for doc in db.collection("customers").stream()}

    users = {doc.id: doc.to_dict() for doc in db.collection("users").stream()}
    partner_names = {uid: u.get("name", "") for uid, u in users.items() if u.get("role") == "partner"}

    raw_bookings = []
    for doc in db.collection("bookings").stream():
        b = doc.to_dict()
        if not b.get("id"):
            b["id"] = doc.id  # fallback to Firestore document key
        raw_bookings.append(b)

    all_bookings = [
        b for b in raw_bookings
        if b.get("status") not in ("cancelled", "canceled")
        and (b.get("totalDays") or 0) > 0
        and (b.get("totalAmount") or 0) > 0
    ]

    sheet = get_income_sheet()
    existing_rows = sheet.get_all_values()
    header = existing_rows[0] if existing_rows else []
    data_rows = list(existing_rows[1:])

    # Build car plate → canonical name map for dedup of manual rows
    plate_to_canonical = {}
    for car in cars.values():
        plate = car.get("licensePlate", "").strip()
        if plate:
            plate_to_canonical[plate] = canonical_car_name(car)

    # Build index: booking_id → sheet row number (1-based, after header)
    id_to_sheet_row = {}
    # Manual rows (no ID): startDate|plate → row index (so we can update them in-place)
    manual_plate_to_idx = {}
    for i, row in enumerate(data_rows):
        bid = row[COL_ID].strip() if len(row) > COL_ID else ""
        if bid:
            id_to_sheet_row[bid] = i + 2  # 1-indexed + skip header
        else:
            car_cell = row[COL_CAR].strip() if len(row) > COL_CAR else ""
            m = re.search(r'\((\d{5}-[A-Z]-\d{2})\)', car_cell)
            plate = m.group(1) if m else None
            start = row[COL_START].strip() if len(row) > COL_START else ""
            if start and plate:
                manual_plate_to_idx[f"{start}|{plate}"] = i

    # --- Process each booking ---
    # Batch updates: list of {"range": "A5:N5", "values": [[...]]}
    batch_updates = []
    rows_to_insert = []  # (datetime, row_list)
    added = 0
    updated = 0
    skipped = 0

    for b in all_bookings:
        bid = b.get("id", "")
        if not bid:
            skipped += 1
            continue

        car_id = b.get("carId", "")
        car_name = carid_to_canonical.get(car_id) or canonical_car_name(cars.get(car_id, {}))

        customer_id = b.get("customerId", "")
        customer = customers.get(customer_id, {})

        source_raw = b.get("source", "")
        if source_raw == "partner":
            partner_id = customer.get("partnerId", "")
            provenance = partner_names.get(partner_id, "Partenaire") if partner_id else "Partenaire"
        else:
            provenance = SOURCE_MAP.get(source_raw, source_raw or "")

        total_days = b.get("totalDays", 0) or 0
        commission = str(PARTNER_COMMISSION_PER_DAY * total_days) if source_raw == "partner" else "0"

        new_row = booking_to_row(b, car_name, customer, provenance, commission)

        car = cars.get(car_id, {})
        plate = car.get("licensePlate", "").strip()
        start_date = format_date(b.get("startDate", ""))
        manual_key = f"{start_date}|{plate}"

        # If a manual row exists for this booking, update it in-place with the ID
        if manual_key in manual_plate_to_idx and bid not in id_to_sheet_row:
            idx = manual_plate_to_idx[manual_key]
            sheet_row = idx + 2
            range_str = f"A{sheet_row}:{col_letter(COL_NOTES)}{sheet_row}"
            batch_updates.append({"range": range_str, "values": [new_row[:COL_NOTES]]})
            id_to_sheet_row[bid] = sheet_row
            updated += 1
            continue

        if bid in id_to_sheet_row:
            sheet_row = id_to_sheet_row[bid]
            existing = data_rows[sheet_row - 2]

            # Compare — skip cols we never want to overwrite (COL_NOTES = manual)
            changed = False
            for col_idx in range(COL_NOTES):  # cols 0..12, skip notes col 13
                existing_val = existing[col_idx].strip() if col_idx < len(existing) else ""
                if str(new_row[col_idx]).strip() != existing_val:
                    changed = True
                    break

            if changed:
                range_str = f"A{sheet_row}:{col_letter(COL_NOTES)}{sheet_row}"
                batch_updates.append({
                    "range": range_str,
                    "values": [new_row[:COL_NOTES]],  # update all cols except Notes
                })
                updated += 1
        else:
            start_dt = parse_firestore_date(b.get("startDate", "1970-01-01"))
            rows_to_insert.append((start_dt, new_row))
            added += 1

    # --- Apply batch updates (preserves formatting) ---
    if batch_updates:
        sheet.batch_update(batch_updates, value_input_option="USER_ENTERED")

    # --- Append all new rows at once, then re-sort in place ---
    if rows_to_insert:
        rows_to_insert.sort(key=lambda x: x[0])
        sheet.append_rows([r[1] for r in rows_to_insert], value_input_option="USER_ENTERED")

        # Re-read and sort all data rows ascending by start date
        all_rows = sheet.get_all_values()
        header = all_rows[0]
        data = all_rows[1:]

        non_empty = [r for r in data if any(r) and len(r) > COL_START and r[COL_START].strip()]
        empty = [r for r in data if not any(r) or len(r) <= COL_START or not r[COL_START].strip()]
        non_empty.sort(key=lambda r: parse_sheet_date(r[COL_START]) or datetime.min)

        sorted_data = non_empty + empty
        end_col = col_letter(len(header))
        end_row = len(sorted_data) + 1
        sheet.update(
            f"A2:{end_col}{end_row}",
            sorted_data,
            value_input_option="USER_ENTERED"
        )

    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "total_firestore": len(all_bookings),
    }


if __name__ == "__main__":
    result = sync_bookings()
    print(
        f"Sync complete: {result['added']} added, {result['updated']} updated, "
        f"{result['skipped']} skipped ({result['total_firestore']} total in Firestore)"
    )
