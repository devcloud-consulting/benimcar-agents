import os
import re
import json
import time as time_mod
import asyncio
from datetime import datetime, time as dtime, timedelta
from calendar import monthrange
from langchain_openai import ChatOpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import gspread
from google.oauth2.service_account import Credentials


# ── Transient-failure retry for Google Sheets calls ─────────────────────────
# Sheets/Drive occasionally return 503/502/429 during regional hiccups. Wrap
# a closure with this helper and we'll retry up to 4 times with exponential
# backoff before re-raising. Apply at handler boundaries to keep the user
# from seeing raw stack traces on transient outages.
def _sheets_retry(fn, *, retries=4, base_delay=2):
    last = None
    for attempt in range(retries):
        try:
            return fn()
        except gspread.exceptions.APIError as e:
            msg = str(e)
            transient = "[503]" in msg or "[502]" in msg or "[429]" in msg or "[500]" in msg
            if not transient:
                raise
            last = e
            time_mod.sleep(base_delay * (2 ** attempt))
    raise last


async def _sheets_retry_async(blocking_fn):
    """Async wrapper for the sync retry above."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _sheets_retry(blocking_fn))

BOT_TOKEN = "8664821276:AAH_riPofU3TtiAcoVlv5JKa_NRzUoPznaU"
COMPTA_GROUP_ID = -1003956789017
RAPPORTS_THREAD_ID = 14
CAISSES_THREAD_ID = 16
TACHES_THREAD_ID = 17

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3,
    "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "décembre": 12, "decembre": 12
}

MONTH_NAMES_FR = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre"
}

# Amortization & occupancy parameters
AMORTIZATION_TOTAL_RATE = 0.20          # 20% of vehicle value spread over the whole window
AMORTIZATION_MONTHS = 60                # 5 years
LONG_TERM_THRESHOLD_DAYS = 30           # bookings >= this are "long-term"
LONG_TERM_DISCOUNT_FACTOR = 0.70        # long-term days count as 70%

llm = ChatOpenAI(
    model="deepseek-chat",
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com/v1"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_workbook():
    creds = Credentials.from_service_account_file(
        "/root/google-service-account.json",
        scopes=SCOPES
    )
    client = gspread.authorize(creds)
    return client.open("Benim Car - Fiche Année 2025")

def parse_amount(value: str) -> float:
    try:
        return float(str(value).replace("dh", "").replace("DH", "").replace(",", "").strip())
    except Exception:
        return 0.0

def parse_date(value: str) -> datetime | None:
    for fmt in ["%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%B-%Y"]:
        try:
            return datetime.strptime(value.strip(), fmt)
        except Exception:
            continue
    return None

def parse_month_input(text: str) -> tuple[int, int] | None:
    text = text.lower().strip()
    year_match = re.search(r'\b(202\d)\b', text)
    year = int(year_match.group(1)) if year_match else datetime.now().year
    for name, num in FRENCH_MONTHS.items():
        if name in text:
            return (num, year)
    return None


def parse_period_input(text: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """Parse user input into a (start_year, start_month) → (end_year, end_month)
    range. Supports:
      "avril"                          → April current year, single month
      "avril 2026"                     → April 2026, single month
      "avril mai 2026"                 → April-May 2026
      "avril à juin 2026", "avril-juin"→ April-June 2026
      "2025"                           → entire year 2025
      "3 derniers mois"                → last 3 months from today
    Returns None if nothing matches."""
    t = text.lower().strip()
    # Year-only ("2025", "2026")
    year_only = re.fullmatch(r"\s*(202\d)\s*", t)
    if year_only:
        y = int(year_only.group(1))
        return ((y, 1), (y, 12))

    # "N derniers mois" / "last N months"
    last_n = re.search(r"(\d+)\s*derniers?\s*mois", t)
    if last_n:
        n = max(1, min(24, int(last_n.group(1))))
        today = datetime.now()
        end_m, end_y = today.month, today.year
        start_dt = datetime(end_y, end_m, 1)
        for _ in range(n - 1):
            start_dt = (start_dt - timedelta(days=1)).replace(day=1)
        return ((start_dt.year, start_dt.month), (end_y, end_m))

    # Find ALL months and years with their positions in the text, then pair
    # each month with the year that follows it (or with the only year, or
    # default to the current year). This handles inputs like
    #   "juin 2025 à mai 2026"   → ((2025, 6), (2026, 5))
    #   "decembre 2025 mars 2026" → ((2025, 12), (2026, 3))
    #   "juin à mai 2026"        → ((2025, 6), (2026, 5))  (year inferred)
    month_positions = []  # [(pos, month_num)]
    for name, num in FRENCH_MONTHS.items():
        for m in re.finditer(rf"\b{name}\b", t):
            month_positions.append((m.start(), num))
    month_positions.sort()
    if not month_positions:
        return None

    year_positions = [(m.start(), int(m.group(1))) for m in re.finditer(r"\b(202\d)\b", t)]
    default_year = datetime.now().year

    def year_for(pos):
        # First year mentioned AFTER this position (the year the user
        # explicitly attached to that month name). Otherwise the last year
        # mentioned in the text, otherwise current year.
        for yp, yy in year_positions:
            if yp > pos:
                return yy
        if year_positions:
            return year_positions[-1][1]
        return default_year

    paired = [(year_for(pos), num) for pos, num in month_positions]

    if len(paired) == 1:
        return ((paired[0][0], paired[0][1]), (paired[0][0], paired[0][1]))

    start_y, start_m = paired[0]
    end_y, end_m = paired[-1]
    # If user only mentioned one year but the range loops back ("juin à mai
    # 2026"), assume the start belongs to the previous year.
    if (start_y, start_m) > (end_y, end_m):
        start_y = end_y - 1
    return ((start_y, start_m), (end_y, end_m))


def iter_months(start: tuple[int, int], end: tuple[int, int]):
    sy, sm = start
    ey, em = end
    cur_y, cur_m = sy, sm
    while (cur_y, cur_m) <= (ey, em):
        yield cur_y, cur_m
        cur_m += 1
        if cur_m == 13:
            cur_m, cur_y = 1, cur_y + 1


def get_vehicle_purchases(wb) -> list[tuple[datetime, float]]:
    """Returns [(date_achat, prix_dh), ...] from Dépenses Voitures."""
    out = []
    try:
        ws = wb.worksheet("Dépenses Voitures")
    except gspread.WorksheetNotFound:
        return out
    for r in ws.get_all_values()[1:]:
        if len(r) < 5 or r[1].strip() != "Achat Voiture":
            continue
        d = parse_date(r[0])
        if not d:
            continue
        try:
            amt = parse_amount(r[3])
        except Exception:
            amt = 0.0
        if amt > 0:
            out.append((d, amt))
    return out


def compute_amortization(purchases, year: int, month: int) -> tuple[float, int]:
    """Return (monthly_amortization, active_cars) for the given month.
    A car is amortized from its purchase month inclusive over AMORTIZATION_MONTHS.
    """
    from calendar import monthrange
    me = datetime(year, month, monthrange(year, month)[1])
    total = 0.0
    active = 0
    for purchase_date, price in purchases:
        # months elapsed since purchase, inclusive of purchase month
        months_elapsed = (me.year - purchase_date.year) * 12 + (me.month - purchase_date.month) + 1
        if 1 <= months_elapsed <= AMORTIZATION_MONTHS:
            # Total amortization = price × 20% spread over 60 months
            total += price * AMORTIZATION_TOTAL_RATE / AMORTIZATION_MONTHS
            active += 1
        elif months_elapsed > AMORTIZATION_MONTHS:
            active += 1  # car still owned, just fully amortized
        # else: not yet purchased → ignore
    return total, active


def _occupancy_from_rows(inc, year, month, purchases) -> dict:
    """Same as compute_occupancy but takes pre-fetched Income rows."""
    from calendar import monthrange
    ms = datetime(year, month, 1)
    nd = monthrange(year, month)[1]
    nms = ms + timedelta(days=nd)

    per_car_days = {}
    per_car_weighted = {}
    for r in inc[1:]:
        if len(r) < 7:
            continue
        s = parse_date(r[1])
        if not s:
            continue
        e = parse_date(r[2])
        try:
            declared_days = int(r[3].strip()) if r[3].strip() else 0
        except Exception:
            declared_days = 0
        derived_days = (e - s).days if (e and e > s) else 0
        booking_total = declared_days or derived_days
        # Effective end: start + booking_total (or month end for open bookings)
        # so a sheet date span longer than declared Jours doesn't inflate
        # occupancy.
        eff = s + timedelta(days=booking_total) if booking_total > 0 else nms
        o_s = max(s, ms)
        o_e = min(eff, nms)
        od = (o_e - o_s).days
        if od <= 0:
            continue
        factor = LONG_TERM_DISCOUNT_FACTOR if booking_total >= LONG_TERM_THRESHOLD_DAYS else 1.0
        car = (r[5] or "").strip()
        per_car_days[car] = per_car_days.get(car, 0) + od
        per_car_weighted[car] = per_car_weighted.get(car, 0.0) + od * factor

    # Active cars = purchased on or before month end
    me = datetime(year, month, nd)
    n_cars = sum(1 for d, _ in purchases if d <= me)
    capacity = n_cars * nd

    raw = sum(per_car_days.values())
    capped = sum(min(v, nd) for v in per_car_days.values())
    weighted = sum(min(v, nd * LONG_TERM_DISCOUNT_FACTOR) if v > nd else v
                   for v in per_car_weighted.values())
    # ^ for capped weighted: a single car can't physically exceed nd*1.0 raw,
    # so its weighted contribution caps at nd * factor at worst.

    suspects = [(c, v) for c, v in per_car_days.items() if v > nd]

    return {
        "raw_days": raw,
        "capped_days": capped,
        "weighted_days": weighted,
        "capacity": capacity,
        "suspects": suspects,
        "n_cars": n_cars,
    }


def compute_occupancy(wb, year: int, month: int, purchases) -> dict:
    """Single-month convenience wrapper — reads Income on the fly."""
    try:
        inc = wb.worksheet("Income").get_all_values()
    except gspread.WorksheetNotFound:
        return {"raw_days": 0, "capped_days": 0, "weighted_days": 0.0,
                "capacity": 0, "suspects": [], "n_cars": 0}
    return _occupancy_from_rows(inc, year, month, purchases)


def _expenses_from_rows(rows, year, month, exclude=None):
    """Sum expenses in (year, month) from pre-fetched expense rows.
    Returns dict of {category: amount} (matches get_monthly_*_expenses)."""
    out = {}
    for r in rows[1:]:
        if len(r) < 4:
            continue
        d = parse_date(r[0])
        if not d or d.year != year or d.month != month:
            continue
        cat = r[1].strip()
        if exclude and cat == exclude:
            continue
        out[cat] = out.get(cat, 0.0) + parse_amount(r[3])
    return out


def compute_period_stats(wb, start: tuple[int, int], end: tuple[int, int]) -> dict:
    """Aggregates monthly stats over an inclusive (start_year, start_month) →
    (end_year, end_month) range. Returns dict with totals + per-month rows.

    Reads each Sheets tab ONCE (3 reads total: Income, Dépenses Voitures,
    Dépense Général) regardless of period length, then filters in Python.
    Prevents the Sheets per-minute quota from being hit on long periods."""
    purchases = get_vehicle_purchases(wb)
    # Prefetch — these are the only Sheets reads in the whole computation.
    income_rows = wb.worksheet("Income").get_all_values()
    try:
        car_exp_rows = wb.worksheet("Dépenses Voitures").get_all_values()
    except gspread.WorksheetNotFound:
        car_exp_rows = [[]]
    try:
        gen_exp_rows = wb.worksheet("Dépense Général").get_all_values()
    except gspread.WorksheetNotFound:
        gen_exp_rows = [[]]
    rows = []
    for y, m in iter_months(start, end):
        rev = _monthly_revenue_from_rows(income_rows, y, m)
        car_exp = sum(_expenses_from_rows(car_exp_rows, y, m, exclude="Achat Voiture").values())
        gen_exp = sum(_expenses_from_rows(gen_exp_rows, y, m).values())
        amort, _ = compute_amortization(purchases, y, m)
        occ = _occupancy_from_rows(income_rows, y, m, purchases)
        net_rev = rev["total_ventes"] - rev["commissions"]
        benefit = net_rev - car_exp - gen_exp - amort
        rows.append({
            "year": y, "month": m,
            "revenue": rev["total_ventes"],
            "commissions": rev["commissions"],
            "net_revenue": net_rev,
            "car_exp": car_exp,
            "general_exp": gen_exp,
            "amortization": amort,
            "benefit": benefit,
            "occ_capped": occ["capped_days"] / occ["capacity"] * 100 if occ["capacity"] else 0,
            "occ_weighted": occ["weighted_days"] / occ["capacity"] * 100 if occ["capacity"] else 0,
            "rented_days": occ["capped_days"],
            "capacity": occ["capacity"],
            "n_cars": occ["n_cars"],
            "suspects": occ["suspects"],
        })

    totals = {
        "revenue": sum(r["revenue"] for r in rows),
        "commissions": sum(r["commissions"] for r in rows),
        "net_revenue": sum(r["net_revenue"] for r in rows),
        "car_exp": sum(r["car_exp"] for r in rows),
        "general_exp": sum(r["general_exp"] for r in rows),
        "amortization": sum(r["amortization"] for r in rows),
        "benefit": sum(r["benefit"] for r in rows),
        "rented_days": sum(r["rented_days"] for r in rows),
        "capacity": sum(r["capacity"] for r in rows),
        "weighted_days": sum(r["occ_weighted"] / 100 * r["capacity"] for r in rows),
    }
    totals["occ_capped"] = totals["rented_days"] / totals["capacity"] * 100 if totals["capacity"] else 0
    totals["occ_weighted"] = totals["weighted_days"] / totals["capacity"] * 100 if totals["capacity"] else 0

    return {"rows": rows, "totals": totals}


def generate_period_report(start: tuple[int, int], end: tuple[int, int]) -> str:
    """Single multi-month report (or single month if start == end)."""
    wb = get_workbook()
    stats = compute_period_stats(wb, start, end)
    rows = stats["rows"]
    t = stats["totals"]

    def label(y, m):
        return f"{MONTH_NAMES_FR[m]} {y}"

    if start == end:
        header = f"📊 *Rapport — {label(*start)}*"
    else:
        n = len(rows)
        header = f"📊 *Rapport — {label(*start)} → {label(*end)}* ({n} mois)"

    lines = [header, ""]
    lines.append("💰 *Revenus*")
    lines.append(f"  Chiffre d'affaires : *{t['revenue']:,.0f} DH*")
    if t["commissions"]:
        lines.append(f"  Commissions        : −{t['commissions']:,.0f} DH")
        lines.append(f"  Net revenus        : *{t['net_revenue']:,.0f} DH*")
    lines.append("")
    lines.append("💸 *Dépenses*")
    lines.append(f"  Voitures           : {t['car_exp']:,.0f} DH")
    lines.append(f"  Générales          : {t['general_exp']:,.0f} DH")
    lines.append(f"  Amortissement      : {t['amortization']:,.0f} DH  _(20% total sur 5 ans)_")
    total_costs = t["car_exp"] + t["general_exp"] + t["amortization"]
    lines.append(f"  *Total dépenses*    : *{total_costs:,.0f} DH*")
    lines.append("")
    lines.append(f"✅ *Bénéfice net : {t['benefit']:,.0f} DH*")
    lines.append("")
    lines.append("📈 *Occupation*")
    lines.append(f"  Brute (cappée)   : *{t['occ_capped']:.1f}%*  ({t['rented_days']}/{t['capacity']} j)")
    lines.append(f"  Pondérée long-terme : *{t['occ_weighted']:.1f}%*  _(long-term ≥ {LONG_TERM_THRESHOLD_DAYS}j × {int(LONG_TERM_DISCOUNT_FACTOR*100)}%)_")

    if len(rows) > 1:
        lines.append("")
        lines.append("*Détail mensuel :*")
        for r in rows:
            lines.append(
                f"  {MONTH_NAMES_FR[r['month']][:3]} {r['year']}  "
                f"Rev {r['net_revenue']:>8,.0f}  "
                f"Dép {r['car_exp']+r['general_exp']+r['amortization']:>8,.0f}  "
                f"Bén {r['benefit']:>+9,.0f}  "
                f"Occ {r['occ_capped']:>4.0f}%/{r['occ_weighted']:.0f}%"
            )

    # Surface over-booking suspects
    all_suspects = []
    for r in rows:
        for car, days in r["suspects"]:
            all_suspects.append((r["year"], r["month"], car, days))
    if all_suspects:
        lines.append("")
        lines.append("⚠️ *Sur-bookings détectés (à vérifier dans Firestore)* :")
        for y, m, car, d in all_suspects[:6]:
            short = car.split("(")[0].strip() or car
            lines.append(f"  {MONTH_NAMES_FR[m][:3]} {y}: {short} {d}j (max {monthrange_days(y, m)})")

    return "\n".join(lines)


def monthrange_days(year: int, month: int) -> int:
    from calendar import monthrange
    return monthrange(year, month)[1]

# ── Data Fetchers ─────────────────────────────────────────────────────────────

def _monthly_revenue_from_rows(income_rows, year, month) -> dict:
    """Same as get_monthly_revenue but takes pre-fetched Income rows so
    period reports can iterate many months without re-reading the sheet."""
    month_start = datetime(year, month, 1)
    next_month_start = month_start + timedelta(days=monthrange(year, month)[1])

    total_ventes = 0.0
    commissions = 0.0
    jours_location = 0

    for row in income_rows[1:]:
        if len(row) < 7:
            continue
        start = parse_date(row[1])
        if not start:
            continue
        end = parse_date(row[2])

        # Booking total days: prefer the declared Jours column (col 3),
        # fall back to (Retour − Allez).days otherwise.
        try:
            declared_days = int(str(row[3]).strip()) if len(row) > 3 and str(row[3]).strip() else 0
        except ValueError:
            declared_days = 0
        derived_days = (end - start).days if (end and end > start) else 0
        booking_days = declared_days or derived_days

        # The effective end date for overlap purposes is `start + booking_days`,
        # NOT the raw Retour. This keeps the sum of monthly contributions equal
        # to Vente exactly, even when the sheet's Jours column is off by a day
        # vs. (Retour − Allez).days. For open-ended bookings (no Retour AND no
        # declared days) we treat them as ongoing through the requested month.
        if booking_days > 0:
            effective_end = start + timedelta(days=booking_days)
        else:
            effective_end = next_month_start

        overlap_start = max(start, month_start)
        overlap_end = min(effective_end, next_month_start)
        overlap_days = (overlap_end - overlap_start).days
        if overlap_days <= 0:
            continue

        # Daily rate: Vente(DH) / booking_days. Fall back to Prix(DH) when
        # the division would be invalid (open booking, missing Vente).
        vente = parse_amount(row[6]) if len(row) > 6 else 0.0
        if booking_days > 0 and vente > 0:
            daily_rate = vente / booking_days
        else:
            daily_rate = parse_amount(row[4]) if len(row) > 4 else 0.0

        total_ventes += overlap_days * daily_rate
        jours_location += overlap_days
        if len(row) > 8 and booking_days > 0:
            commissions += parse_amount(row[8]) * (overlap_days / booking_days)

    return {
        "total_ventes": total_ventes,
        "commissions": commissions,
        "jours_location": jours_location,
        "occupation": "",
        "moyenne_jour": "",
    }


def get_monthly_revenue(wb, month: int, year: int) -> dict:
    """Single-month convenience wrapper — reads the Income sheet on the fly.
    Period reports go through compute_period_stats which prefetches once."""
    income_rows = wb.worksheet("Income").get_all_values()
    return _monthly_revenue_from_rows(income_rows, year, month)

def get_monthly_car_expenses(wb, month: int, year: int) -> dict:
    """Single-month convenience wrapper. Period reports prefetch the sheet once."""
    rows = wb.worksheet("Dépenses Voitures").get_all_values()
    return _expenses_from_rows(rows, year, month, exclude="Achat Voiture")


def get_monthly_general_expenses(wb, month: int, year: int) -> dict:
    """Single-month convenience wrapper. Period reports prefetch the sheet once."""
    rows = wb.worksheet("Dépense Général").get_all_values()
    return _expenses_from_rows(rows, year, month)

# ── Report Generator ──────────────────────────────────────────────────────────

def generate_monthly_report(month: int, year: int) -> str:
    wb = get_workbook()

    revenue = get_monthly_revenue(wb, month, year)
    car_expenses = get_monthly_car_expenses(wb, month, year)
    gen_expenses = get_monthly_general_expenses(wb, month, year)

    total_ventes = revenue["total_ventes"]
    commissions = revenue["commissions"]
    net_revenue = total_ventes - commissions
    total_car = sum(car_expenses.values())
    total_gen = sum(gen_expenses.values())
    benefice = net_revenue - total_car - total_gen

    month_name = MONTH_NAMES_FR[month]
    lines = [f"📊 *Rapport Mensuel — {month_name} {year}*\n"]

    # Revenue
    lines.append("💰 *REVENUS*")
    lines.append(f"  Total location : {total_ventes:,.0f} DH")
    lines.append(f"  Commissions    : -{commissions:,.0f} DH")
    lines.append(f"  *Net revenu    : {net_revenue:,.0f} DH*\n")

    # Car expenses
    lines.append("🚗 *DÉPENSES VOITURES*")
    if car_expenses:
        for cat, amt in sorted(car_expenses.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat:<22}: {amt:,.0f} DH")
        lines.append(f"  {'─' * 30}")
        lines.append(f"  *Total : {total_car:,.0f} DH*\n")
    else:
        lines.append("  Aucune dépense\n")

    # General expenses
    lines.append("📋 *DÉPENSES GÉNÉRALES*")
    if gen_expenses:
        for cat, amt in sorted(gen_expenses.items(), key=lambda x: -x[1]):
            lines.append(f"  {cat:<22}: {amt:,.0f} DH")
        lines.append(f"  {'─' * 30}")
        lines.append(f"  *Total : {total_gen:,.0f} DH*\n")
    else:
        lines.append("  Aucune dépense\n")

    # Bottom line
    emoji = "✅" if benefice > 0 else "❌"
    lines.append(f"{emoji} *BÉNÉFICE NET : {benefice:,.0f} DH*")

    if revenue["occupation"]:
        lines.append(f"\n📈 Occupation : *{revenue['occupation']}* ({revenue['jours_location']} jours / 7 voitures)")
    if revenue["moyenne_jour"]:
        lines.append(f"💵 Moyenne/Jour : {revenue['moyenne_jour']}")

    return "\n".join(lines)

# ── DeepSeek Q&A ──────────────────────────────────────────────────────────────

def answer_question(question: str) -> str:
    wb = get_workbook()

    # Build prorata-correct monthly summaries from Income (NOT from the
    # manually-maintained TOTAL Incomes sheet, which attributed each booking
    # to its start month entirely and inflated long-rental months). Cover
    # every month from 2025-01 up to next month so DeepSeek can answer
    # questions about any period without us going back to the wrong source.
    # Prefetch each sheet ONCE so we don't burn through the per-minute quota.
    today = datetime.now()
    end_y = today.year + (1 if today.month == 12 else 0)
    end_m = 1 if today.month == 12 else today.month + 1
    purchases = get_vehicle_purchases(wb)
    income_rows = wb.worksheet("Income").get_all_values()
    car_rows = wb.worksheet("Dépenses Voitures").get_all_values()
    gen_rows = wb.worksheet("Dépense Général").get_all_values()

    summaries = []
    for y, m in iter_months((2025, 1), (end_y, end_m)):
        rev = _monthly_revenue_from_rows(income_rows, y, m)
        car_exp = sum(_expenses_from_rows(car_rows, y, m, exclude="Achat Voiture").values())
        gen_exp = sum(_expenses_from_rows(gen_rows, y, m).values())
        amort, _ = compute_amortization(purchases, y, m)
        net_rev = rev["total_ventes"] - rev["commissions"]
        benefit = net_rev - car_exp - gen_exp - amort
        summaries.append({
            "mois": f"{MONTH_NAMES_FR[m]} {y}",
            "revenus": round(rev["total_ventes"]),
            "commissions": round(rev["commissions"]),
            "net_revenus": round(net_rev),
            "depenses_voitures": round(car_exp),
            "depenses_generales": round(gen_exp),
            "amortissement": round(amort),
            "benefice_net": round(benefit),
            "jours_loues": rev["jours_location"],
        })

    context = f"""
Récapitulatif mensuel (calculé avec pro-rata journalier — chaque réservation
contribue à chaque mois proportionnellement aux jours qui y tombent):
{json.dumps(summaries, ensure_ascii=False)}

Dépenses Voitures (hors Achat Voiture):
{json.dumps([r for r in car_rows if len(r) > 1 and r[1] != 'Achat Voiture'], ensure_ascii=False)}

Dépenses Générales:
{json.dumps(gen_rows, ensure_ascii=False)}

Note: Les montants sont déjà en dirhams. L'amortissement est de 20% étalé
sur 5 ans (60 mois), calculé à partir du prix d'achat de chaque véhicule.
"""

    prompt = f"""Tu es un assistant comptable pour BenimCar, une société de location de voitures à Agadir, Maroc.
Réponds à la question suivante en français, avec des chiffres clairs et précis.
Utilise des emojis pour rendre la réponse lisible.

Question: {question}

Données disponibles:
{context}

Réponds directement et de façon concise."""

    response = llm.invoke(prompt)
    return response.content.strip()

# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Bonjour\\! Je suis le bot de rapports *BenimCar*\\.\n\n"
        "📊 *Commandes:*\n"
        "• `/rapport juin` — rapport mensuel\n"
        "• `/rapport juin 2025` — avec année\n\n"
        "💬 Ou posez une question en français:\n"
        "_\"Quel est le bénéfice de juillet?\"_\n"
        "_\"Combien de fuel en août?\"_",
        parse_mode="MarkdownV2"
    )

async def rapport_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id if update.message else None
    if update.effective_chat.type in ("group", "supergroup"):
        if chat_id != COMPTA_GROUP_ID or thread_id != RAPPORTS_THREAD_ID:
            return

    args = " ".join(context.args) if context.args else ""
    if not args:
        await update.message.reply_text(
            "Usage: `/rapport juin` ou `/rapport juin 2025`",
            parse_mode="Markdown"
        )
        return

    parsed = parse_period_input(args)
    if not parsed:
        await update.message.reply_text(
            "❌ Période non reconnue. Exemples :\n"
            "  `/rapport juin 2025`\n"
            "  `/rapport avril mai 2026`\n"
            "  `/rapport avril à juin 2026`\n"
            "  `/rapport 2025`\n"
            "  `/rapport 3 derniers mois`",
            parse_mode="Markdown"
        )
        return

    start, end = parsed
    await update.message.reply_text("⏳ Génération du rapport...")

    try:
        report = await _sheets_retry_async(lambda: generate_period_report(start, end))
        await update.message.reply_text(report, parse_mode="Markdown")
    except gspread.exceptions.APIError as e:
        msg = str(e)[:200]
        await update.message.reply_text(
            f"⚠️ Google Sheets indisponible (`{msg}`). Réessaie dans une minute.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur: {str(e)}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id

    if update.effective_chat.type in ("group", "supergroup"):
        thread_id = update.message.message_thread_id if update.message else None
        if chat_id != COMPTA_GROUP_ID or thread_id != RAPPORTS_THREAD_ID:
            return
        bot_username = context.bot.username
        is_mention = f"@{bot_username}" in text
        is_reply_to_bot = (
            update.message.reply_to_message is not None and
            update.message.reply_to_message.from_user is not None and
            update.message.reply_to_message.from_user.username == bot_username
        )
        if not is_mention and not is_reply_to_bot:
            return
        text = text.replace(f"@{bot_username}", "").strip()

    # If message looks like a report request → generate period report
    parsed = parse_period_input(text)
    if parsed:
        start, end = parsed
        await update.message.reply_text("⏳ Génération du rapport...")
        try:
            report = await _sheets_retry_async(lambda: generate_period_report(start, end))
            await update.message.reply_text(report, parse_mode="Markdown")
        except gspread.exceptions.APIError as e:
            await update.message.reply_text(
                f"⚠️ Google Sheets indisponible (`{str(e)[:200]}`). Réessaie dans une minute.",
                parse_mode="Markdown",
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Erreur: {str(e)}")
        return

    # Otherwise → DeepSeek Q&A
    await update.message.reply_text("⏳ Analyse en cours...")
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, lambda: answer_question(text))
        await update.message.reply_text(answer)
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur: {str(e)}")

# ── Scheduled Monthly Report ──────────────────────────────────────────────────

async def scheduled_monthly_report(context) -> None:
    today = datetime.now()
    if today.day != 1:
        return

    if today.month == 1:
        month, year = 12, today.year - 1
    else:
        month, year = today.month - 1, today.year

    try:
        report = generate_period_report((year, month), (year, month))
        await context.bot.send_message(
            chat_id=COMPTA_GROUP_ID,
            message_thread_id=RAPPORTS_THREAD_ID,
            text=f"📅 *Rapport automatique du mois écoulé*\n\n{report}",
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error sending scheduled report: {e}")

# ── Sync Handler ─────────────────────────────────────────────────────────────

async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id if update.message else None
    if update.effective_chat.type in ("group", "supergroup"):
        if chat_id != COMPTA_GROUP_ID or thread_id != CAISSES_THREAD_ID:
            return

    await update.message.reply_text("⏳ Synchronisation Firestore en cours...")
    try:
        from sync_firestore import sync_bookings
        result = await _sheets_retry_async(sync_bookings)
        await update.message.reply_text(
            f"✅ *Synchronisation terminée*\n\n"
            f"📥 Added: *{result['added']}*\n"
            f"🔄 Updated: *{result.get('updated', 0)}*\n"
            f"🗑️ Deleted (orphans): *{result.get('deleted', 0)}*\n"
            f"📊 Total Firestore: *{result['total_firestore']}*",
            parse_mode="Markdown"
        )
    except gspread.exceptions.APIError as e:
        await update.message.reply_text(
            f"⚠️ Google Sheets indisponible (`{str(e)[:200]}`). Réessaie dans une minute.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur sync: {str(e)}")

async def scheduled_sync(context) -> None:
    try:
        from sync_firestore import sync_bookings
        result = sync_bookings()
        if result["added"] > 0 or result.get("deleted", 0) > 0:
            parts = []
            if result["added"]:
                parts.append(f"📥 {result['added']} added")
            if result.get("updated"):
                parts.append(f"🔄 {result['updated']} updated")
            if result.get("deleted"):
                parts.append(f"🗑️ {result['deleted']} deleted (orphans)")
            await context.bot.send_message(
                chat_id=COMPTA_GROUP_ID,
                message_thread_id=CAISSES_THREAD_ID,
                text=f"🔄 *Sync automatique Firestore*\n\n" + ", ".join(parts),
                parse_mode="Markdown"
            )
    except Exception as e:
        print(f"Scheduled sync error: {e}")

async def scheduled_payment_reminder(context) -> None:
    """
    Runs daily. On the 5th of each month, sends to Tâches channel a recap of all
    bookings longer than 30 days whose payment status is not fully paid,
    covering the period of the previous month (last 30 days of the booking window).
    """
    now = datetime.now()
    if now.day != 5:
        return

    try:
        creds = Credentials.from_service_account_file("/root/google-service-account.json", scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open("Benim Car - Fiche Année 2025").worksheet("Income")
        rows = sheet.get_all_values()

        def parse_date(s):
            for fmt in ["%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%B-%Y"]:
                try:
                    return datetime.strptime(s.strip(), fmt)
                except Exception:
                    pass
            return None

        def norm_amount(s):
            try:
                return float(str(s).replace(",", "").replace("dh", "").strip())
            except Exception:
                return 0.0

        # Period: previous calendar month
        if now.month == 1:
            period_year, period_month = now.year - 1, 12
        else:
            period_year, period_month = now.year, now.month - 1

        import calendar
        period_start = datetime(period_year, period_month, 1)
        period_end = datetime(period_year, period_month, calendar.monthrange(period_year, period_month)[1], 23, 59, 59)

        pending = []
        for row in rows[1:]:
            if not any(row) or len(row) < 10:
                continue
            start_dt = parse_date(row[1]) if len(row) > 1 else None
            end_dt = parse_date(row[2]) if len(row) > 2 else None
            if not start_dt or not end_dt:
                continue

            # Only bookings longer than 30 days
            total_days = (end_dt - start_dt).days
            if total_days <= 30:
                continue

            # Booking must overlap with previous month window
            if end_dt < period_start or start_dt > period_end:
                continue

            paid_status = row[9].strip().upper() if len(row) > 9 else ""
            if paid_status == "OUI":
                continue

            amount = norm_amount(row[6]) if len(row) > 6 else 0
            client_name = row[11].strip() if len(row) > 11 else ""
            car = row[5].strip() if len(row) > 5 else ""
            phone = row[12].strip() if len(row) > 12 else ""

            pending.append({
                "client": client_name or "Inconnu",
                "car": car,
                "start": row[1].strip(),
                "end": row[2].strip(),
                "days": total_days,
                "amount": amount,
                "paid": paid_status or "NON",
                "phone": phone,
            })

        month_name = period_start.strftime("%B %Y")

        if not pending:
            msg = f"✅ *Rappel Paiements — {month_name}*\n\nAucun paiement en attente pour les réservations longues durée."
        else:
            lines = [f"⚠️ *Rappel Paiements — {month_name}*", f"_{len(pending)} réservation(s) longue durée à vérifier :_\n"]
            for p in pending:
                lines.append(
                    f"👤 *{p['client']}*"
                    + (f" — {p['phone']}" if p['phone'] else "")
                )
                lines.append(f"🚗 {p['car']}")
                lines.append(f"📅 {p['start']} → {p['end']} ({p['days']} jours)")
                lines.append(f"💰 {p['amount']:,.0f} DH — Payé: *{p['paid']}*")
                lines.append("")
            msg = "\n".join(lines)

        await context.bot.send_message(
            chat_id=COMPTA_GROUP_ID,
            message_thread_id=TACHES_THREAD_ID,
            text=msg,
            parse_mode="Markdown",
        )

    except Exception as e:
        print(f"Payment reminder error: {e}")


# ── Caisses: monthly bank statements + balance tracking ────────────────────

CAISSES_CAPTION_RE = re.compile(
    r"^\s*(officiel|black)\s+(\d{1,2}/\d{1,2}/\d{4})\s+([\d\s.,]+)\s*$",
    re.IGNORECASE,
)
RELEVES_SHEET = "Relevés"
RELEVES_HEADER = ["Date Fin", "Compte", "Solde Fin (DH)", "Fichier", "Uploadé par", "Uploadé le"]


def _in_caisses_thread(update) -> bool:
    if not update.effective_chat or update.effective_chat.id != COMPTA_GROUP_ID:
        return False
    thread_id = update.message.message_thread_id if update.message else None
    return thread_id == CAISSES_THREAD_ID


def _ensure_releves_sheet(wb):
    """Return the Relevés worksheet, creating it with a header on first call."""
    try:
        return wb.worksheet(RELEVES_SHEET)
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title=RELEVES_SHEET, rows=200, cols=len(RELEVES_HEADER))
        ws.update(values=[RELEVES_HEADER], range_name="A1")
        return ws


def _last_day_of_month(year: int, month: int) -> datetime:
    from calendar import monthrange
    return datetime(year, month, monthrange(year, month)[1])


def _normalize_to_month_end(date_str: str) -> tuple[datetime, str]:
    """Caption gives a date like 30/04/2026. We normalize to the last day of
    that month so re-uploads always overwrite the same Storage path
    (releves/YYYY-MM/...). Returns (normalized_datetime, "YYYY-MM")."""
    dt = datetime.strptime(date_str, "%d/%m/%Y")
    ym = dt.strftime("%Y-%m")
    return _last_day_of_month(dt.year, dt.month), ym


async def handle_caisses_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pick up PDFs/images posted in the Caisses topic with a valid caption
    and store them as the monthly bank statement."""
    if not update.message:
        return
    if not _in_caisses_thread(update):
        return

    doc = update.message.document
    photo = update.message.photo
    if not doc and not photo:
        return

    caption = (update.message.caption or "").strip()
    if not caption:
        return  # no caption → not a relevé upload

    m = CAISSES_CAPTION_RE.match(caption)
    if not m:
        await update.message.reply_text(
            "ℹ️ Pour enregistrer un relevé, ajoute en légende :\n"
            "`officiel JJ/MM/AAAA SOLDE`  ou  `black JJ/MM/AAAA SOLDE`\n"
            "Ex : `officiel 30/04/2026 47300`",
            parse_mode="Markdown",
        )
        return

    compte = m.group(1).lower()
    date_str = m.group(2)
    solde_raw = m.group(3)

    try:
        end_date, ym = _normalize_to_month_end(date_str)
    except ValueError:
        await update.message.reply_text("⚠️ Date invalide. Format attendu : JJ/MM/AAAA.")
        return

    solde = parse_amount(solde_raw)
    if solde <= 0:
        await update.message.reply_text("⚠️ Solde invalide.")
        return

    if doc:
        mime = (doc.mime_type or "").lower()
        if mime != "application/pdf":
            await update.message.reply_text("⚠️ Merci d'envoyer le relevé en PDF.")
            return
        tg_file = await context.bot.get_file(doc.file_id)
        ext = ".pdf"
    else:
        tg_file = await context.bot.get_file(photo[-1].file_id)
        ext = ".jpg"  # in theory PDF-only, but tolerate a photo of the statement

    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir="/tmp") as tmp:
        tmp_path = tmp.name
    try:
        await tg_file.download_to_drive(tmp_path)
        from upload_to_firebase import upload_releve
        url = upload_releve(tmp_path, compte, ym)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)

    user = update.effective_user
    user_label = (user.full_name if user else "?") or "?"
    wb = get_workbook()
    ws = _ensure_releves_sheet(wb)

    rows = ws.get_all_values()
    end_iso = end_date.strftime("%Y-%m-%d")
    existing_row = None
    for i, row in enumerate(rows[1:], start=2):
        if len(row) >= 2 and row[0] == end_iso and row[1].lower() == compte:
            existing_row = i
            break

    new_row = [
        end_iso, compte, str(int(solde)) if solde.is_integer() else str(solde),
        url, user_label, datetime.now().strftime("%Y-%m-%d %H:%M"),
    ]
    if existing_row:
        ws.update(values=[new_row], range_name=f"A{existing_row}:F{existing_row}")
        action = "remplacé"
    else:
        ws.append_row(new_row)
        action = "enregistré"

    await update.message.reply_text(
        f"✅ Relevé *{compte}* {action} pour *{end_date.strftime('%B %Y')}*\n"
        f"💰 Solde fin de mois : *{solde:,.0f} DH*\n"
        f"📎 [Fichier]({url})",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


def _last_balance(ws, compte: str) -> tuple[datetime | None, float, str]:
    """Returns (anchor_date, balance, file_url) for the most recent relevé
    of the given account, or (None, 0, '') if none."""
    rows = ws.get_all_values()
    best_dt = None
    best_solde = 0.0
    best_url = ""
    for row in rows[1:]:
        if len(row) < 3 or row[1].lower() != compte:
            continue
        try:
            dt = datetime.strptime(row[0], "%Y-%m-%d")
        except ValueError:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_solde = parse_amount(row[2])
            best_url = row[3] if len(row) > 3 else ""
    return best_dt, best_solde, best_url


def _movements_since(wb, anchor: datetime) -> dict:
    """Sum movements after the anchor date, split by payment-method bucket.
    Returns {'officiel': delta, 'black': delta}.

    Mapping:
      Cash → black
      Card / Transfer / Chèque → officiel
    """
    out = {"officiel": 0.0, "black": 0.0}

    def bucket(method: str) -> str | None:
        m = (method or "").strip().lower()
        if m == "cash":
            return "black"
        if m in ("card", "transfer", "chèque", "cheque"):
            return "officiel"
        return None

    # Income (encaissements) — only count fully paid bookings, otherwise we'd
    # double-count partials. Vente (DH) is in column 6, payment in col 9 (Payé),
    # but we don't have a method on Income; we approximate by saying Income
    # encaissements always credit officiel unless the Notes say "cash".
    # In practice the boss says everything Card/Transfer/Chèque maps to
    # officiel; Income doesn't expose method directly, so we skip Income
    # here and rely on Dépenses for now. Document this limitation.
    # (Booking-level method tracking can be added later if needed.)

    # Dépenses Voitures
    try:
        ws_v = wb.worksheet("Dépenses Voitures")
        for r in ws_v.get_all_values()[1:]:
            if len(r) < 6:
                continue
            d = parse_date(r[0])
            if not d or d <= anchor:
                continue
            b = bucket(r[5])
            if b:
                out[b] -= parse_amount(r[3])
    except gspread.WorksheetNotFound:
        pass

    # Dépense Général
    try:
        ws_g = wb.worksheet("Dépense Général")
        for r in ws_g.get_all_values()[1:]:
            if len(r) < 5:
                continue
            d = parse_date(r[0])
            if not d or d <= anchor:
                continue
            b = bucket(r[4])
            if b:
                out[b] -= parse_amount(r[3])
    except gspread.WorksheetNotFound:
        pass

    return out


async def cmd_solde(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_caisses_thread(update):
        return
    wb = get_workbook()
    ws = _ensure_releves_sheet(wb)

    lines = ["💼 *Solde des comptes*\n"]
    for compte, label in [("officiel", "Officiel"), ("black", "Black")]:
        anchor, base, _ = _last_balance(ws, compte)
        if anchor is None:
            lines.append(f"*{label}* : pas encore de relevé enregistré.")
            continue
        movements = _movements_since(wb, anchor).get(compte, 0.0)
        current = base + movements
        lines.append(
            f"*{label}* : {current:,.0f} DH\n"
            f"  _Ancré au {anchor.strftime('%d/%m/%Y')} = {base:,.0f} DH_\n"
            f"  _Mouvements depuis : {movements:+,.0f} DH_"
        )
    lines.append("\n_Mouvements pris en compte : dépenses uniquement (Cash→Black, Card/Transfer/Chèque→Officiel)._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_releves(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _in_caisses_thread(update):
        return
    wb = get_workbook()
    ws = _ensure_releves_sheet(wb)
    rows = ws.get_all_values()
    entries = []
    for row in rows[1:]:
        if len(row) < 4:
            continue
        try:
            dt = datetime.strptime(row[0], "%Y-%m-%d")
        except ValueError:
            continue
        entries.append((dt, row[1], parse_amount(row[2]), row[3]))
    entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
    if not entries:
        await update.message.reply_text("ℹ️ Aucun relevé enregistré pour l'instant.")
        return
    lines = ["📚 *Derniers relevés*\n"]
    for dt, compte, solde, url in entries[:12]:
        lines.append(
            f"• {dt.strftime('%b %Y')} — *{compte}* — {solde:,.0f} DH — [PDF]({url})"
        )
    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown", disable_web_page_preview=True
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rapport", rapport_command))
    app.add_handler(CommandHandler("sync", sync_command))
    app.add_handler(CommandHandler("solde", cmd_solde))
    app.add_handler(CommandHandler("releves", cmd_releves))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_caisses_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Monthly report: 1st of each month at 08:00 UTC
    app.job_queue.run_daily(
        scheduled_monthly_report,
        time=dtime(hour=8, minute=0)
    )

    # Daily Firestore sync at 07:00 UTC
    app.job_queue.run_daily(
        scheduled_sync,
        time=dtime(hour=7, minute=0)
    )

    # Payment reminder: runs daily, fires on 5th of each month at 09:00 UTC
    app.job_queue.run_daily(
        scheduled_payment_reminder,
        time=dtime(hour=9, minute=0)
    )

    app.run_polling()

if __name__ == "__main__":
    main()
