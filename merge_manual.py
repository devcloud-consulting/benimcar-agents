"""
One-time script: fuzzy-match manual rows to Firestore bookings by date + amount + car type.
Updates matched manual rows in-place (adds ID, canonical car name, etc).
Removes resulting duplicates. Inserts any remaining unmatched Firestore bookings.
"""
import re
import firebase_admin
from firebase_admin import credentials, firestore
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from collections import defaultdict

FIREBASE_KEY = "/root/firebase-readonly.json"
GOOGLE_KEY = "/root/google-service-account.json"
SPREADSHEET_NAME = "Benim Car - Fiche Année 2025"
INCOME_SHEET = "Income"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

PAYMENT_STATUS_MAP = {"fully_paid": "OUI", "partial": "PARTIEL", "not_paid": "NON", None: "NON"}
SOURCE_MAP = {"personalNetwork": "Bouche à oreille", "other": "Direct", "online": "Online", None: ""}


def get_firestore():
    if not firebase_admin._apps:
        cred = credentials.Certificate(FIREBASE_KEY)
        firebase_admin.initialize_app(cred)
    return firestore.client()


def get_sheet():
    creds = Credentials.from_service_account_file(GOOGLE_KEY, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open(SPREADSHEET_NAME).worksheet(INCOME_SHEET)


def canonical(car):
    make = car.get("make", "").strip()
    model = car.get("model", "").strip()
    plate = car.get("licensePlate", "").strip()
    return f"{make} {model} ({plate})".strip() if plate else f"{make} {model}".strip()


def parse_date(s):
    for fmt in ["%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%B-%Y"]:
        try:
            return datetime.strptime(s.strip(), fmt)
        except Exception:
            pass
    return None


def norm_amount(s):
    try:
        return int(round(float(str(s).replace(",", "").replace("dh", "").strip())))
    except Exception:
        return -1


def car_type(name):
    n = name.upper()
    if "SANDERO" in n:
        return "sandero"
    if "CLIO" in n:
        return "clio"
    if "KIA" in n or "PICANTO" in n:
        return "kia"
    if "LOGAN" in n:
        return "logan"
    return n.strip()


def fd(s):
    try:
        return datetime.strptime(s.strip().split("T")[0], "%Y-%m-%d").strftime("%-d-%b-%Y")
    except Exception:
        return s


def booking_row(b, car_name, customer, provenance, commission):
    phone = re.sub(r"\s+", "", customer.get("phone", "").strip())
    return [
        b.get("id", ""),
        fd(b.get("startDate", "")),
        fd(b.get("endDate", "")),
        str(b.get("totalDays", "")),
        str(b.get("dailyRate", "")),
        car_name,
        str(b.get("totalAmount", "0")),
        "Dirham",
        commission,
        PAYMENT_STATUS_MAP.get(b.get("paymentStatus"), "NON"),
        provenance,
        customer.get("name", ""),
        phone,
        b.get("comments", ""),
    ]


def main():
    db = get_firestore()
    cars = {doc.id: doc.to_dict() for doc in db.collection("cars").stream()}
    customers = {doc.id: doc.to_dict() for doc in db.collection("customers").stream()}
    users = {doc.id: doc.to_dict() for doc in db.collection("users").stream()}
    partner_names = {uid: u.get("name", "") for uid, u in users.items() if u.get("role") == "partner"}

    bookings = [
        doc.to_dict() for doc in db.collection("bookings").stream()
        if doc.to_dict().get("status") not in ("cancelled", "canceled")
        and (doc.to_dict().get("totalDays") or 0) > 0
        and (doc.to_dict().get("totalAmount") or 0) > 0
    ]

    # Index bookings: (date, amount, car_type) -> booking
    b_index = {}
    for b in bookings:
        bid = b.get("id", "")
        if not bid:
            continue
        d = b.get("startDate", "").split("T")[0]
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue
        amt = norm_amount(b.get("totalAmount", 0))
        car = cars.get(b.get("carId", ""), {})
        ct = car_type(canonical(car))
        key = (dt.date(), amt, ct)
        b_index[key] = b

    sheet = get_sheet()
    rows = sheet.get_all_values()
    header = rows[0]
    data = list(rows[1:])

    existing_ids = set(r[0].strip() for r in data if r[0].strip())
    matched_ids = set()
    updates = []

    for i, row in enumerate(data):
        if not any(row):
            continue
        if row[0].strip():
            continue  # already has Firestore ID

        dt = parse_date(row[1].strip()) if len(row) > 1 else None
        if not dt:
            continue
        amt = norm_amount(row[6]) if len(row) > 6 else -1
        ct = car_type(row[5]) if len(row) > 5 else ""

        key = (dt.date(), amt, ct)
        b = b_index.get(key)
        if not b:
            continue

        bid = b.get("id", "")
        if bid in matched_ids or bid in existing_ids:
            continue
        matched_ids.add(bid)

        car = cars.get(b.get("carId", ""), {})
        car_name = canonical(car)
        customer = customers.get(b.get("customerId", ""), {})
        source_raw = b.get("source", "")
        if source_raw == "partner":
            pid = customer.get("partnerId", "")
            provenance = partner_names.get(pid, "Partenaire") if pid else "Partenaire"
        else:
            provenance = SOURCE_MAP.get(source_raw, source_raw or "")
        commission = str(50 * (b.get("totalDays", 0) or 0)) if source_raw == "partner" else "0"
        new_row = booking_row(b, car_name, customer, provenance, commission)
        updates.append((i, new_row))

    print(f"Matched manual rows: {len(updates)}")

    # Apply updates in-memory
    for i, new_row in updates:
        for col_idx in range(min(len(new_row), 13)):  # preserve Notes col
            while len(data[i]) <= col_idx:
                data[i].append("")
            data[i][col_idx] = str(new_row[col_idx])

    # Insert unmatched Firestore bookings
    all_ids_now = set(r[0].strip() for r in data if r[0].strip())
    new_rows = []
    for b in bookings:
        bid = b.get("id", "")
        if not bid or bid in all_ids_now:
            continue
        car = cars.get(b.get("carId", ""), {})
        car_name = canonical(car)
        customer = customers.get(b.get("customerId", ""), {})
        source_raw = b.get("source", "")
        if source_raw == "partner":
            pid = customer.get("partnerId", "")
            provenance = partner_names.get(pid, "Partenaire") if pid else "Partenaire"
        else:
            provenance = SOURCE_MAP.get(source_raw, source_raw or "")
        commission = str(50 * (b.get("totalDays", 0) or 0)) if source_raw == "partner" else "0"
        new_rows.append(booking_row(b, car_name, customer, provenance, commission))

    print(f"New unmatched rows to insert: {len(new_rows)}")

    all_data = [r for r in data if any(r)] + new_rows

    # Deduplicate: if same ID appears twice, keep only first occurrence
    seen_ids = set()
    deduped = []
    for row in all_data:
        bid = row[0].strip() if row else ""
        if bid:
            if bid in seen_ids:
                continue
            seen_ids.add(bid)
        deduped.append(row)

    # Sort ascending by start date
    deduped.sort(key=lambda r: parse_date(r[1]) if len(r) > 1 and r[1].strip() else datetime.min)

    end_col = chr(65 + len(header) - 1)
    blank = [[""] * len(header) for _ in range(max(0, len(data) - len(deduped)))]
    final = deduped + blank

    sheet.update(values=final, range_name=f"A2:{end_col}{len(final)+1}", value_input_option="USER_ENTERED")

    # Report remaining dups
    groups = defaultdict(list)
    for r in deduped:
        if any(r) and len(r) > 6 and r[1].strip():
            groups[f"{r[1].strip()}|{norm_amount(r[6])}"].append(r[5])
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"Remaining duplicate groups after merge: {len(dups)}")
    for k, v in list(dups.items())[:10]:
        print(f"  {k} -> {v}")
    print("Done.")


if __name__ == "__main__":
    main()
