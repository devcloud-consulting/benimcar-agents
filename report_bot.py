import os
import re
import json
from datetime import datetime, time as dtime, timedelta
from calendar import monthrange
from langchain_openai import ChatOpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import gspread
from google.oauth2.service_account import Credentials

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

# ── Data Fetchers ─────────────────────────────────────────────────────────────

def get_monthly_revenue(wb, month: int, year: int) -> dict:
    """Get revenue from TOTAL Incomes sheet, fallback to Income sheet."""
    ws = wb.worksheet("TOTAL Incomes")
    rows = ws.get_all_values()

    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        d = parse_date(row[0])
        if d and d.month == month and d.year == year:
            return {
                "total_ventes": parse_amount(row[5]) if len(row) > 5 else 0.0,
                "commissions": parse_amount(row[6]) if len(row) > 6 else 0.0,
                "jours_location": int(row[2]) if len(row) > 2 and row[2].strip().isdigit() else 0,
                "occupation": row[3].strip() if len(row) > 3 else "",
                "moyenne_jour": row[4].strip() if len(row) > 4 else "",
            }

    # Fallback: calculate from Income sheet, prorated by day so a
    # multi-month booking only contributes the days that fall inside the
    # requested month.
    #
    # Income columns: 0:ID, 1:Allez, 2:Retour, 3:Jours, 4:Prix(DH),
    # 5:Voiture, 6:Vente(DH), 7:Currency, 8:Commissions, 9:Payé, ...
    # Vente (DH) and Prix(DH) are both in dirhams.
    income_ws = wb.worksheet("Income")
    income_rows = income_ws.get_all_values()

    month_start = datetime(year, month, 1)
    next_month_start = month_start + timedelta(days=monthrange(year, month)[1])

    total_ventes = 0.0
    commissions = 0.0
    jours_location = 0

    for row in income_rows[1:]:
        if len(row) < 7:
            continue
        start = parse_date(row[1])
        end = parse_date(row[2])
        if not start or not end or end <= start:
            continue
        # Day-by-day overlap with the requested month (end is exclusive,
        # matching the convention Jours = (Retour - Allez).days).
        overlap_start = max(start, month_start)
        overlap_end = min(end, next_month_start)
        overlap_days = (overlap_end - overlap_start).days
        if overlap_days <= 0:
            continue

        booking_days = (end - start).days
        daily_rate = parse_amount(row[4]) if len(row) > 4 else 0.0
        if daily_rate == 0 and booking_days > 0:
            # Fallback: derive daily rate from total Vente.
            daily_rate = parse_amount(row[6]) / booking_days

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

def get_monthly_car_expenses(wb, month: int, year: int) -> dict:
    """Get car expenses grouped by category, excluding Achat Voiture."""
    ws = wb.worksheet("Dépenses Voitures")
    rows = ws.get_all_values()
    expenses = {}
    for row in rows[1:]:
        if len(row) < 4:
            continue
        d = parse_date(row[0])
        if not d or d.month != month or d.year != year:
            continue
        category = row[1].strip()
        if category == "Achat Voiture":
            continue
        amount = parse_amount(row[3])
        expenses[category] = expenses.get(category, 0.0) + amount
    return expenses

def get_monthly_general_expenses(wb, month: int, year: int) -> dict:
    """Get general expenses grouped by category."""
    ws = wb.worksheet("Dépense Général")
    rows = ws.get_all_values()
    expenses = {}
    for row in rows[1:]:
        if len(row) < 4:
            continue
        d = parse_date(row[0])
        if not d or d.month != month or d.year != year:
            continue
        category = row[1].strip()
        amount = parse_amount(row[3])
        expenses[category] = expenses.get(category, 0.0) + amount
    return expenses

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

    total_rows = wb.worksheet("TOTAL Incomes").get_all_values()
    car_rows = wb.worksheet("Dépenses Voitures").get_all_values()
    gen_rows = wb.worksheet("Dépense Général").get_all_values()

    context = f"""
Résumés mensuels (TOTAL Incomes):
{json.dumps(total_rows[:20], ensure_ascii=False)}

Dépenses Voitures (hors Achat Voiture):
{json.dumps([r for r in car_rows if len(r) > 1 and r[1] != 'Achat Voiture'], ensure_ascii=False)}

Dépenses Générales:
{json.dumps(gen_rows, ensure_ascii=False)}

Note: Les montants en euros sont convertis à 1 EUR = 10 MAD.
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

    parsed = parse_month_input(args)
    if not parsed:
        await update.message.reply_text(
            "❌ Mois non reconnu. Exemple: `/rapport juin 2025`",
            parse_mode="Markdown"
        )
        return

    month, year = parsed
    await update.message.reply_text("⏳ Génération du rapport...")

    try:
        report = generate_monthly_report(month, year)
        await update.message.reply_text(report, parse_mode="Markdown")
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

    # If message contains a month → generate monthly report
    parsed = parse_month_input(text)
    if parsed:
        month, year = parsed
        await update.message.reply_text("⏳ Génération du rapport...")
        try:
            report = generate_monthly_report(month, year)
            await update.message.reply_text(report, parse_mode="Markdown")
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
        report = generate_monthly_report(month, year)
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
        import asyncio
        from sync_firestore import sync_bookings
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, sync_bookings)
        await update.message.reply_text(
            f"✅ *Synchronisation terminée*\n\n"
            f"📥 Nouvelles réservations ajoutées : *{result['added']}*\n"
            f"⏭️ Déjà existantes : *{result['skipped']}*\n"
            f"📊 Total Firestore : *{result['total_firestore']}*",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur sync: {str(e)}")

async def scheduled_sync(context) -> None:
    try:
        from sync_firestore import sync_bookings
        result = sync_bookings()
        if result["added"] > 0:
            await context.bot.send_message(
                chat_id=COMPTA_GROUP_ID,
                message_thread_id=CAISSES_THREAD_ID,
                text=(
                    f"🔄 *Sync automatique Firestore*\n\n"
                    f"📥 {result['added']} nouvelle(s) réservation(s) ajoutée(s) au tableau Income."
                ),
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rapport", rapport_command))
    app.add_handler(CommandHandler("sync", sync_command))
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
