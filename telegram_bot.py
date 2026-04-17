import os
import time
import tempfile
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from langgraph_workflow import (
    process_expense_message, process_expense_image, extract_correction,
    ALLOWED_CARS, ALLOWED_CAR_CATEGORIES, ALLOWED_GENERAL_CATEGORIES,
    WORKERS_ALLOWED_CAR_CATEGORIES, WORKERS_ALLOWED_GENERAL_CATEGORIES,
    ALLOWED_PAYMENTS, ALL_CATEGORIES
)
from upload_to_drive import upload_image

BOT_TOKEN = "7733678538:AAFOmVlf9NAw2VFXeV1Tz7xOLD-qNoZHaPk"
API_URL = "http://127.0.0.1:8000/add-expense"

WORKERS_GROUP_ID = -5135022095
MANAGEMENT_GROUP_ID = -5117263813

PENDING = {}

CARS_LIST = "\n".join(f"• {c}" for c in ALLOWED_CARS)

HELP_CAR = """Voici comment utiliser le bot BenimCar :

🗣️ *Message texte:*
_"J'ai payé 350 MAD de carburant pour la Clio V aujourd'hui en cash"_

📸 *Photo du justificatif:*
Envoie une photo avec ou sans légende.

✏️ *Corriger:* Réponds directement au message du bot.

✅ *CONFIRMER* pour enregistrer.
🚫 *ANNULER* pour annuler.
"""

HELP_GENERAL = """Voici comment utiliser le bot BenimCar :

🗣️ *Message texte:*
_"Salaire Ahmed 5000 MAD cash"_
_"Fuel 350 MAD Clio V cash"_

📸 *Photo du justificatif:*
Envoie une photo avec ou sans légende.

✏️ *Corriger:* Réponds directement au message du bot.

✅ *CONFIRMER* pour enregistrer.
🚫 *ANNULER* pour annuler.
"""

# ── Group Config ─────────────────────────────────────────────────────────────

def get_group_config(chat_id: int) -> dict:
    if chat_id == MANAGEMENT_GROUP_ID:
        return {
            "allowed_categories": ALL_CATEGORIES,
            "help_text": HELP_GENERAL
        }
    else:
        return {
            "allowed_categories": WORKERS_ALLOWED_CAR_CATEGORIES + WORKERS_ALLOWED_GENERAL_CATEGORIES,
            "help_text": HELP_CAR
        }

# ── Helpers ──────────────────────────────────────────────────────────────────

def get_sheet_type(category: str) -> str:
    if category in ALLOWED_CAR_CATEGORIES:
        return "car"
    return "general"

def match_car(text: str) -> str | None:
    for car in ALLOWED_CARS:
        parts = [p for p in car.split() if len(p) > 3]
        if any(part.lower() in text.lower() for part in parts):
            return car
    return None

def match_payment(text: str) -> str | None:
    t = text.upper()
    if any(w in t for w in ["CASH", "ESPECE", "ESPÈCE"]):
        return "Cash"
    if any(w in t for w in ["CARD", "CARTE"]):
        return "Card"
    if any(w in t for w in ["TRANSFER", "VIREMENT"]):
        return "Transfer"
    if any(w in t for w in ["CHEQUE", "CHÈQUE"]):
        return "Chèque"
    return None

def format_expense_summary(extracted: dict, file_url: str = "") -> str:
    is_car = extracted.get("categorie") in ALLOWED_CAR_CATEGORIES
    summary = (
        f"📋 *Récapitulatif de la dépense:*\n\n"
        f"📅 Date: {extracted.get('date', 'N/A')}\n"
        f"🏷️ Catégorie: {extracted.get('categorie', 'N/A')}\n"
        f"📝 Détails: {extracted.get('details', 'N/A')}\n"
        f"💰 Montant: {extracted.get('montant', 'N/A')} MAD\n"
    )
    if is_car:
        summary += f"🚗 Voiture: {extracted.get('voiture', 'N/A')}\n"
    summary += f"💳 Paiement: {extracted.get('type_paiement', 'N/A')}\n"
    if file_url:
        summary += f"📎 Justificatif: [Voir]({file_url})\n"
    summary += f"\nTapez *CONFIRMER* pour enregistrer ou *ANNULER* pour annuler."
    return summary

def build_pending(extracted: dict, file_url: str) -> dict:
    category = extracted.get("categorie")
    sheet_type = get_sheet_type(category)

    pending = {
        "date": extracted.get("date"),
        "category": category,
        "details": extracted.get("details"),
        "amount": str(extracted.get("montant")),
        "payment_type": extracted.get("type_paiement"),
        "file_url": file_url,
        "sheet_type": sheet_type,
    }
    if sheet_type == "car":
        pending["car"] = extracted.get("voiture")
    return pending

# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = get_group_config(update.effective_chat.id)
    await update.message.reply_text(
        "Bonjour 👋 Je suis le bot comptable *BenimCar*.\n\n" + config["help_text"],
        parse_mode="Markdown"
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    chat_id = update.effective_chat.id
    config = get_group_config(chat_id)

    if update.effective_chat.type in ("group", "supergroup"):
        caption = update.message.caption or ""
        bot_username = context.bot.username
        is_mention = f"@{bot_username}" in caption
        is_reply_to_bot = (
            update.message.reply_to_message is not None and
            update.message.reply_to_message.from_user is not None and
            update.message.reply_to_message.from_user.username == bot_username
        )
        if not is_mention and not is_reply_to_bot:
            return
        caption = caption.replace(f"@{bot_username}", "").strip()
    else:
        caption = update.message.caption or ""

    await update.message.reply_text("⏳ Analyse en cours...")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    tmp_path = None
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False, dir="/tmp") as tmp:
        tmp_path = tmp.name

    await file.download_to_drive(tmp_path)

    filename = f"justificatif_{chat_id}_{int(time.time())}.jpg"
    file_url = ""

    # Analyze image with Gemini Vision
    try:
        import asyncio
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: process_expense_image(
                tmp_path,
                extra_info=caption,
                allowed_categories=config["allowed_categories"]
            )
        )
        # Upload to Drive with date/category from extraction
        extracted_preview = result.get("extracted") or {}
        try:
            file_url = upload_image(
                tmp_path, filename,
                extracted_preview.get("date"),
                extracted_preview.get("categorie")
            )
        except Exception as e:
            print(f"DEBUG Drive upload failed: {e}")
    except Exception as e:
        # Gemini failed — upload to root folder as fallback
        try:
            file_url = upload_image(tmp_path, filename)
        except Exception as ue:
            print(f"DEBUG Drive upload failed: {ue}")
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        PENDING[chat_id] = {"file_url": file_url, "waiting_description": True, "config": config}
        await update.message.reply_text(
            "⚠️ Impossible d'analyser le reçu.\n\n"
            "Décris la dépense en français et je l'enregistrerai avec ce justificatif."
        )
        return
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    extracted = result.get("extracted") or {}
    errors = result.get("errors", [])

    non_critical_errors = [
        e for e in errors
        if "Voiture" not in e and "Type de paiement" not in e
    ]

    if non_critical_errors:
        PENDING[chat_id] = {"file_url": file_url, "waiting_description": True, "config": config}
        await update.message.reply_text(
            "⚠️ Je n'ai pas pu extraire toutes les informations essentielles du reçu.\n\n"
            "Décris la dépense en français et je l'enregistrerai avec ce justificatif."
        )
        return

    if errors:
        PENDING[chat_id] = {
            "file_url": file_url,
            "waiting_car": True,
            "partial": extracted,
            "config": config
        }

        missing_parts = []
        if extracted.get("categorie") in ALLOWED_CAR_CATEGORIES and not extracted.get("voiture"):
            missing_parts.append(f"🚗 *Pour quelle voiture?*\n{CARS_LIST}")
        if not extracted.get("type_paiement"):
            missing_parts.append("💳 *Quel type de paiement?*\nTransfer, Card, Cash, Chèque")

        await update.message.reply_text(
            f"✅ Reçu analysé!\n\n"
            f"📅 Date: {extracted.get('date')}\n"
            f"🏷️ Catégorie: {extracted.get('categorie')}\n"
            f"📝 Détails: {extracted.get('details')}\n"
            f"💰 Montant: {extracted.get('montant')} MAD\n\n"
            + "\n\n".join(missing_parts),
            parse_mode="Markdown"
        )
        return

    PENDING[chat_id] = build_pending(extracted, file_url)

    await update.message.reply_text(
        format_expense_summary(extracted, file_url),
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    config = get_group_config(chat_id)

    if update.effective_chat.type in ("group", "supergroup"):
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

    # ── CONFIRMER ────────────────────────────────────────────────────────────
    if "CONFIRMER" in text.upper():
        expense = PENDING.get(chat_id)
        if not expense:
            await update.message.reply_text("ℹ️ Aucune dépense en attente de confirmation.")
            return
        if expense.get("waiting_description") or expense.get("waiting_car"):
            await update.message.reply_text("⚠️ Veuillez d'abord compléter les informations manquantes.")
            return
        try:
            response = requests.post(API_URL, json=expense, timeout=30)
            data = response.json()
            if response.status_code == 200 and data.get("success"):
                PENDING.pop(chat_id, None)
                await update.message.reply_text("✅ Dépense enregistrée avec succès!")
            elif data.get("duplicate"):
                await update.message.reply_text(
                    "⚠️ *Dépense en double détectée!*\n"
                    "Cette dépense existe déjà.\n\n"
                    "Tapez *ANNULER* si c'est un doublon, ou corrigez les informations.",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    f"❌ Erreur: {data.get('error', 'Erreur inconnue')}"
                )
        except Exception as e:
            await update.message.reply_text(f"❌ Erreur de connexion: {str(e)}")
        return

    # ── ANNULER ───────────────────────────────────────────────────────────────
    if "ANNULER" in text.upper():
        if PENDING.pop(chat_id, None):
            await update.message.reply_text("🚫 Dépense annulée.")
        else:
            await update.message.reply_text("ℹ️ Aucune dépense en cours.")
        return

    pending = PENDING.get(chat_id)

    # ── Waiting for car/payment after OCR ────────────────────────────────────
    if pending and pending.get("waiting_car"):
        partial = pending["partial"]
        is_car = partial.get("categorie") in ALLOWED_CAR_CATEGORIES

        matched_car = partial.get("voiture") or (match_car(text) if is_car else None)
        matched_payment = partial.get("type_paiement") or match_payment(text)

        if not matched_car or not matched_payment:
            temp_expense = {
                "date": partial.get("date"),
                "category": partial.get("categorie"),
                "details": partial.get("details"),
                "amount": str(partial.get("montant")),
                "car": partial.get("voiture"),
                "payment_type": partial.get("type_paiement"),
                "file_url": pending.get("file_url", ""),
                "sheet_type": get_sheet_type(partial.get("categorie")),
            }
            try:
                updated = extract_correction(temp_expense, text)
                if updated:
                    partial["montant"] = updated.get("amount", partial.get("montant"))
                    partial["date"] = updated.get("date", partial.get("date"))
                    partial["details"] = updated.get("details", partial.get("details"))
                    partial["categorie"] = updated.get("category", partial.get("categorie"))
                    if updated.get("car"):
                        matched_car = updated.get("car")
                    if updated.get("payment_type"):
                        matched_payment = updated.get("payment_type")
                    PENDING[chat_id]["partial"] = partial
            except Exception:
                pass

        if is_car and not matched_car:
            await update.message.reply_text(
                f"❌ Voiture non reconnue.\n\nChoisissez parmi:\n{CARS_LIST}",
                parse_mode="Markdown"
            )
            return

        if not matched_payment:
            await update.message.reply_text(
                "❌ Type de paiement non reconnu.\n\nChoisissez parmi: Transfer, Card, Cash, Chèque"
            )
            return

        updated_pending = {
            "date": partial["date"],
            "category": partial["categorie"],
            "details": partial["details"],
            "amount": str(partial["montant"]),
            "payment_type": matched_payment,
            "file_url": pending["file_url"],
            "sheet_type": get_sheet_type(partial["categorie"]),
        }
        if is_car:
            updated_pending["car"] = matched_car

        PENDING[chat_id] = updated_pending

        display_extracted = {
            "date": partial["date"],
            "categorie": partial["categorie"],
            "details": partial["details"],
            "montant": partial["montant"],
            "voiture": matched_car,
            "type_paiement": matched_payment,
        }

        await update.message.reply_text(
            format_expense_summary(display_extracted, pending["file_url"]),
            parse_mode="Markdown"
        )
        return

    # ── Waiting for description after failed OCR ──────────────────────────────
    if pending and pending.get("waiting_description"):
        await update.message.reply_text("⏳ Analyse en cours...")
        pending_config = pending.get("config", config)

        try:
            result = process_expense_message(text, pending_config["allowed_categories"])
        except Exception as e:
            await update.message.reply_text(f"❌ Erreur lors de l'analyse: {str(e)}")
            return

        if result["errors"]:
            await update.message.reply_text(result["summary"], parse_mode="Markdown")
            return

        extracted = result["extracted"]
        PENDING[chat_id] = build_pending(extracted, pending["file_url"])

        await update.message.reply_text(
            format_expense_summary(extracted, pending["file_url"]),
            parse_mode="Markdown"
        )
        return

    # ── AI Correction of pending expense ─────────────────────────────────────
    if pending and not pending.get("waiting_description") and not pending.get("waiting_car"):
        await update.message.reply_text("⏳ Application de la correction...")
        try:
            updated_pending = extract_correction(pending, text)
        except Exception:
            updated_pending = {}

        if updated_pending:
            PENDING[chat_id] = updated_pending
            fake_extracted = {
                "date": updated_pending.get("date"),
                "categorie": updated_pending.get("category"),
                "details": updated_pending.get("details"),
                "montant": updated_pending.get("amount"),
                "voiture": updated_pending.get("car"),
                "type_paiement": updated_pending.get("payment_type"),
            }
            await update.message.reply_text(
                "✏️ *Dépense mise à jour:*\n\n" +
                format_expense_summary(fake_extracted, updated_pending.get("file_url", "")),
                parse_mode="Markdown"
            )
            return

    # ── Normal natural language extraction ────────────────────────────────────
    await update.message.reply_text("⏳ Analyse en cours...")

    try:
        result = process_expense_message(text, config["allowed_categories"])
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur lors de l'analyse: {str(e)}")
        return

    if result["errors"]:
        await update.message.reply_text(result["summary"], parse_mode="Markdown")
        return

    extracted = result["extracted"]
    existing_file_url = PENDING.get(chat_id, {}).get("file_url", "")
    PENDING[chat_id] = build_pending(extracted, existing_file_url)

    await update.message.reply_text(
        format_expense_summary(extracted, existing_file_url),
        parse_mode="Markdown"
    )

# ── Main ──────────────────────────────────────────────────────────────────────

async def debug_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.message
    if m:
        print(f"DEBUG_TOPIC chat_id={m.chat.id} thread_id={m.message_thread_id} text={str(m.text or '')[:50]}")

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, debug_all), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
