import os
import re
import json
from datetime import datetime
from langchain_openai import ChatOpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import gspread
from google.oauth2.service_account import Credentials

BOT_TOKEN = "8664821276:AAH_riPofU3TtiAcoVlv5JKa_NRzUoPznaU"
MANAGEMENT_GROUP_ID = -5117263813

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

EUR_TO_MAD = 10.0

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

    # Fallback: calculate from Income sheet
    income_ws = wb.worksheet("Income")
    income_rows = income_ws.get_all_values()
    total_ventes = 0.0
    commissions = 0.0
    jours_location = 0

    for row in income_rows[1:]:
        if len(row) < 6:
            continue
        d = parse_date(row[0])
        if not d or d.month != month or d.year != year:
            continue
        currency = row[6].strip() if len(row) > 6 else "Dirham"
        vente = parse_amount(row[5])
        if currency.lower() == "euro":
            vente *= EUR_TO_MAD
        commission = parse_amount(row[7]) if len(row) > 7 else 0.0
        if currency.lower() == "euro":
            commission *= EUR_TO_MAD
        total_ventes += vente
        commissions += commission
        try:
            jours_location += int(row[2])
        except Exception:
            pass

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
    if update.effective_chat.type in ("group", "supergroup") and chat_id != MANAGEMENT_GROUP_ID:
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
        if chat_id != MANAGEMENT_GROUP_ID:
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

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rapport", rapport_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
