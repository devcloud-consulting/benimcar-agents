import os
import time
import tempfile
import asyncio
from datetime import datetime, timedelta
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from langgraph_workflow import (
    process_expense_message, process_expense_image, extract_correction,
    ALLOWED_CARS, ALLOWED_CAR_CATEGORIES, ALLOWED_GENERAL_CATEGORIES,
    WORKERS_ALLOWED_CAR_CATEGORIES, WORKERS_ALLOWED_GENERAL_CATEGORIES,
    ALLOWED_PAYMENTS, ALL_CATEGORIES,
)
from upload_to_firebase import upload_image
from keyboards import (
    kb_initial_choice, kb_categories, kb_cars, kb_payments,
    kb_dates, kb_skip_details, kb_attach, kb_confirm,
)

BOT_TOKEN = "7733678538:AAFOmVlf9NAw2VFXeV1Tz7xOLD-qNoZHaPk"
API_URL = "http://127.0.0.1:8000/add-expense"

WORKERS_GROUP_ID = -5135022095
COMPTA_GROUP_ID = -1003956789017
COMPTA_DEPENSES_THREAD_ID = 6

ALLOWED_CHAT_CONFIGS = {
    WORKERS_GROUP_ID: {"thread_id": None, "allowed_categories": None},
    COMPTA_GROUP_ID: {"thread_id": COMPTA_DEPENSES_THREAD_ID, "allowed_categories": None},
}

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
    if chat_id == WORKERS_GROUP_ID:
        return {
            "allowed_categories": WORKERS_ALLOWED_CAR_CATEGORIES + WORKERS_ALLOWED_GENERAL_CATEGORIES,
            "help_text": HELP_CAR
        }
    return {
        "allowed_categories": ALL_CATEGORIES,
        "help_text": HELP_GENERAL
    }

def is_allowed_chat(update) -> bool:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id if update.message else None

    if chat_id == WORKERS_GROUP_ID:
        return True
    if chat_id == COMPTA_GROUP_ID and thread_id == COMPTA_DEPENSES_THREAD_ID:
        return True
    return False

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

EXT_FROM_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/heic": ".heic", "image/heif": ".heif",
    "application/pdf": ".pdf",
}


async def _process_attachment(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    tg_file, mime: str, caption: str, chat_id: int, config: dict,
) -> None:
    """Download a Telegram file (photo or document), run Gemini extraction,
    upload to Firebase Storage, and reply with the expense summary or a
    fallback prompt."""
    await update.message.reply_text("⏳ Analyse en cours...")

    ext = EXT_FROM_MIME.get(mime, ".jpg")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir="/tmp") as tmp:
        tmp_path = tmp.name

    await tg_file.download_to_drive(tmp_path)

    filename = f"justificatif_{chat_id}_{int(time.time())}{ext}"
    file_url = ""

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: process_expense_image(
                tmp_path,
                extra_info=caption,
                allowed_categories=config["allowed_categories"],
                mime_type=mime,
            ),
        )
        extracted_preview = result.get("extracted") or {}
        try:
            file_url = upload_image(
                tmp_path, filename,
                extracted_preview.get("date"),
                extracted_preview.get("categorie"),
            )
        except Exception as e:
            print(f"DEBUG Storage upload failed: {e}")
    except Exception as e:
        print(f"DEBUG Gemini analysis failed: {e}")
        try:
            file_url = upload_image(tmp_path, filename)
        except Exception as ue:
            print(f"DEBUG Storage upload failed: {ue}")
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

    await _post_extraction_reply(update, chat_id, config, result, file_url)


async def _post_extraction_reply(update, chat_id, config, result, file_url):
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


def _group_caption_check(update, context, caption_or_text):
    """In groups: require either an @mention or a reply-to-bot. Returns the
    cleaned text (mention stripped) or None when the bot should ignore the
    message."""
    if update.effective_chat.type not in ("group", "supergroup"):
        return caption_or_text
    bot_username = context.bot.username
    is_mention = f"@{bot_username}" in caption_or_text
    is_reply_to_bot = (
        update.message.reply_to_message is not None and
        update.message.reply_to_message.from_user is not None and
        update.message.reply_to_message.from_user.username == bot_username
    )
    if not is_mention and not is_reply_to_bot:
        return None
    return caption_or_text.replace(f"@{bot_username}", "").strip()


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return
    if not is_allowed_chat(update):
        return

    chat_id = update.effective_chat.id
    config = get_group_config(chat_id)

    pending = PENDING.get(chat_id)
    in_attach_step = (
        pending and pending.get("flow") == "guided"
        and pending.get("step") == "attach_photo"
    )

    raw_caption = update.message.caption or ""
    if in_attach_step:
        caption = raw_caption  # bypass mention check during guided attach
    else:
        caption = _group_caption_check(update, context, raw_caption)
        if caption is None:
            return

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    if in_attach_step:
        await _attach_to_guided(update, context, file, "image/jpeg", chat_id, pending)
        return

    await _process_attachment(update, context, file, "image/jpeg", caption, chat_id, config)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles PDF justificatifs and images sent as documents (un-compressed)."""
    if not update.message or not update.message.document:
        return
    if not is_allowed_chat(update):
        return

    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    if not (mime == "application/pdf" or mime.startswith("image/")):
        return  # silently ignore other document types

    chat_id = update.effective_chat.id
    config = get_group_config(chat_id)

    pending = PENDING.get(chat_id)
    in_attach_step = (
        pending and pending.get("flow") == "guided"
        and pending.get("step") == "attach_photo"
    )

    raw_caption = update.message.caption or ""
    if in_attach_step:
        caption = raw_caption
    else:
        caption = _group_caption_check(update, context, raw_caption)
        if caption is None:
            return

    file = await context.bot.get_file(doc.file_id)

    if in_attach_step:
        await _attach_to_guided(update, context, file, mime, chat_id, pending)
        return

    await _process_attachment(update, context, file, mime, caption, chat_id, config)


# ── Guided flow ─────────────────────────────────────────────────────────────

GUIDED_TEXT_STEPS = {"amount", "details", "date_custom"}


def _new_guided_pending(config: dict) -> dict:
    return {
        "flow": "guided",
        "step": "category",
        "config": config,
        "date": None,
        "category": None,
        "details": "",
        "amount": None,
        "car": None,
        "payment_type": None,
        "file_url": "",
        "sheet_type": None,
    }


def _parse_amount(text: str) -> str:
    cleaned = text.lower().replace("mad", "").replace("dh", "").replace(",", ".").strip()
    val = float(cleaned)
    if val <= 0:
        raise ValueError("amount must be positive")
    return str(int(val) if val.is_integer() else val)


def _parse_date_custom(text: str) -> str:
    text = text.strip().replace(".", "/").replace("-", "/")
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    raise ValueError("invalid date")


def _format_recap(p: dict) -> str:
    is_car = p.get("sheet_type") == "car"
    lines = [
        "📋 *Récapitulatif:*",
        "",
        f"📅 Date: {p.get('date') or 'N/A'}",
        f"🏷️ Catégorie: {p.get('category') or 'N/A'}",
        f"📝 Détails: {p.get('details') or '—'}",
        f"💰 Montant: {p.get('amount') or 'N/A'} MAD",
    ]
    if is_car:
        lines.append(f"🚗 Voiture: {p.get('car') or 'N/A'}")
    lines.append(f"💳 Paiement: {p.get('payment_type') or 'N/A'}")
    if p.get("file_url"):
        lines.append(f"📎 Justificatif: [Voir]({p['file_url']})")
    return "\n".join(lines)


async def _ask_category(send_func, config):
    await send_func(
        "🏷️ Choisis une *catégorie* :",
        reply_markup=kb_categories(config["allowed_categories"]),
        parse_mode="Markdown",
    )


async def _ask_car(send_func):
    await send_func(
        "🚗 Pour quelle *voiture* ?",
        reply_markup=kb_cars(ALLOWED_CARS),
        parse_mode="Markdown",
    )


async def _ask_amount(send_func):
    await send_func("💰 Tape le *montant* en MAD (ex: 350) :", parse_mode="Markdown")


async def _ask_details(send_func):
    await send_func(
        "📝 Détails de la dépense ? (tape un message ou clique *Passer*)",
        reply_markup=kb_skip_details(),
        parse_mode="Markdown",
    )


async def _ask_date(send_func):
    await send_func("📅 *Date* de la dépense ?", reply_markup=kb_dates(), parse_mode="Markdown")


async def _ask_payment(send_func):
    await send_func(
        "💳 *Mode de paiement* ?",
        reply_markup=kb_payments(ALLOWED_PAYMENTS),
        parse_mode="Markdown",
    )


async def _ask_attach(send_func):
    await send_func(
        "📎 Veux-tu joindre un *justificatif* (photo ou PDF) ?",
        reply_markup=kb_attach(),
        parse_mode="Markdown",
    )


async def _show_recap(send_func, pending: dict):
    await send_func(
        _format_recap(pending),
        reply_markup=kb_confirm(),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def cmd_depense(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    PENDING.pop(chat_id, None)
    await update.message.reply_text(
        "📝 *Nouvelle dépense* — comment veux-tu la saisir ?",
        reply_markup=kb_initial_choice(),
        parse_mode="Markdown",
    )


async def _attach_to_guided(update, context, tg_file, mime, chat_id, pending):
    """Upload the file as the guided-flow justificatif and move to recap."""
    ext = EXT_FROM_MIME.get(mime, ".jpg")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False, dir="/tmp") as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)
    filename = f"justificatif_{chat_id}_{int(time.time())}{ext}"
    try:
        url = upload_image(tmp_path, filename, pending.get("date"), pending.get("category"))
        pending["file_url"] = url
    except Exception as e:
        print(f"DEBUG Storage upload failed (guided attach): {e}")
        pending["file_url"] = ""
        await update.message.reply_text("⚠️ Upload du justificatif échoué, on continue sans.")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    pending["step"] = "recap"
    await _show_recap(update.message.reply_text, pending)


async def _submit_pending(update, chat_id, pending):
    expense = {
        "date": pending.get("date"),
        "category": pending.get("category"),
        "details": pending.get("details") or "",
        "amount": pending.get("amount"),
        "payment_type": pending.get("payment_type"),
        "file_url": pending.get("file_url", ""),
        "sheet_type": pending.get("sheet_type"),
    }
    if pending.get("sheet_type") == "car":
        expense["car"] = pending.get("car")

    try:
        response = requests.post(API_URL, json=expense, timeout=30)
    except requests.RequestException as e:
        await update.message.reply_text(f"❌ Erreur de connexion: {str(e)}")
        return
    try:
        data = response.json()
    except ValueError:
        print(f"DEBUG API non-JSON (status={response.status_code}): {response.text[:500]}")
        await update.message.reply_text(
            f"❌ Erreur serveur (HTTP {response.status_code})."
        )
        return

    if response.status_code == 200 and data.get("success"):
        PENDING.pop(chat_id, None)
        await update.message.reply_text("✅ Dépense enregistrée avec succès!")
    elif data.get("duplicate"):
        await update.message.reply_text(
            "⚠️ Cette dépense existe déjà. Tape /annuler pour abandonner ou /depense pour recommencer."
        )
    else:
        await update.message.reply_text(
            f"❌ Erreur: {data.get('error', 'Erreur inconnue')}"
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("g:"):
        return
    await q.answer()
    chat_id = q.message.chat_id
    config = get_group_config(chat_id)

    parts = q.data.split(":", 2)
    action = parts[1]
    param = parts[2] if len(parts) > 2 else None

    if action == "cancel" or (action == "conf" and param == "no"):
        PENDING.pop(chat_id, None)
        await q.edit_message_text("🚫 Opération annulée.")
        return

    if action == "flow":
        if param == "img":
            PENDING.pop(chat_id, None)
            await q.edit_message_text(
                "📎 Envoie une *photo* ou un *PDF* du justificatif (en réponse à ce message).",
                parse_mode="Markdown",
            )
            return
        if param == "guided":
            PENDING[chat_id] = _new_guided_pending(config)
            await q.edit_message_text("📝 *Saisie guidée*", parse_mode="Markdown")
            await _ask_category(q.message.reply_text, config)
            return

    pending = PENDING.get(chat_id)
    if not pending or pending.get("flow") != "guided":
        await q.edit_message_text("⚠️ Cette action a expiré. Tape /depense pour recommencer.")
        return

    if action == "cat":
        cats = pending["config"]["allowed_categories"]
        idx = int(param)
        if idx >= len(cats):
            return
        cat = cats[idx]
        pending["category"] = cat
        pending["sheet_type"] = get_sheet_type(cat)
        await q.edit_message_text(f"🏷️ Catégorie : *{cat}*", parse_mode="Markdown")
        if pending["sheet_type"] == "car":
            pending["step"] = "car"
            await _ask_car(q.message.reply_text)
        else:
            pending["step"] = "amount"
            await _ask_amount(q.message.reply_text)
        return

    if action == "car":
        idx = int(param)
        if idx >= len(ALLOWED_CARS):
            return
        pending["car"] = ALLOWED_CARS[idx]
        pending["step"] = "amount"
        await q.edit_message_text(f"🚗 Voiture : *{pending['car']}*", parse_mode="Markdown")
        await _ask_amount(q.message.reply_text)
        return

    if action == "det" and param == "skip":
        pending["details"] = ""
        pending["step"] = "date"
        await q.edit_message_text("📝 Détails : *—*", parse_mode="Markdown")
        await _ask_date(q.message.reply_text)
        return

    if action == "date":
        if param == "custom":
            pending["step"] = "date_custom"
            await q.edit_message_text("📅 Tape la date au format *JJ/MM/AAAA* :", parse_mode="Markdown")
            return
        offsets = {"today": 0, "yest": 1, "dby": 2}
        if param not in offsets:
            return
        d = datetime.today() - timedelta(days=offsets[param])
        pending["date"] = d.strftime("%d/%m/%Y")
        pending["step"] = "payment"
        await q.edit_message_text(f"📅 Date : *{pending['date']}*", parse_mode="Markdown")
        await _ask_payment(q.message.reply_text)
        return

    if action == "pay":
        if param not in ALLOWED_PAYMENTS:
            return
        pending["payment_type"] = param
        pending["step"] = "attach"
        await q.edit_message_text(f"💳 Paiement : *{param}*", parse_mode="Markdown")
        await _ask_attach(q.message.reply_text)
        return

    if action == "att":
        if param == "skip":
            pending["file_url"] = ""
            pending["step"] = "recap"
            await q.edit_message_text("📎 Sans justificatif", parse_mode="Markdown")
            await _show_recap(q.message.reply_text, pending)
            return
        if param == "add":
            pending["step"] = "attach_photo"
            await q.edit_message_text(
                "📎 Envoie une *photo* ou un *PDF* (en réponse à ce message ou en mentionnant le bot).",
                parse_mode="Markdown",
            )
            return

    if action == "conf" and param == "yes":
        await _submit_pending(q.message, chat_id, pending)
        return


async def cmd_annuler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_chat(update):
        return
    chat_id = update.effective_chat.id
    if PENDING.pop(chat_id, None):
        await update.message.reply_text("🚫 Opération annulée.")
    else:
        await update.message.reply_text("ℹ️ Aucune opération en cours.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if not is_allowed_chat(update):
        return

    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    config = get_group_config(chat_id)

    pending_pre = PENDING.get(chat_id)
    in_guided_text = (
        pending_pre and pending_pre.get("flow") == "guided"
        and pending_pre.get("step") in GUIDED_TEXT_STEPS
    )

    if update.effective_chat.type in ("group", "supergroup"):
        bot_username = context.bot.username
        is_mention = f"@{bot_username}" in text
        is_reply_to_bot = (
            update.message.reply_to_message is not None and
            update.message.reply_to_message.from_user is not None and
            update.message.reply_to_message.from_user.username == bot_username
        )
        # Guided-flow text inputs (montant, détails, date) are accepted without
        # mention so the user can keep typing answers naturally.
        if not is_mention and not is_reply_to_bot and not in_guided_text:
            return
        text = text.replace(f"@{bot_username}", "").strip()

    # ── Guided flow text inputs ────────────────────────────────────────────
    if in_guided_text:
        step = pending_pre["step"]
        if step == "amount":
            try:
                pending_pre["amount"] = _parse_amount(text)
            except (ValueError, TypeError):
                await update.message.reply_text("⚠️ Montant invalide. Tape un nombre, ex: 350")
                return
            pending_pre["step"] = "details"
            await _ask_details(update.message.reply_text)
            return
        if step == "details":
            pending_pre["details"] = text
            pending_pre["step"] = "date"
            await _ask_date(update.message.reply_text)
            return
        if step == "date_custom":
            try:
                pending_pre["date"] = _parse_date_custom(text)
            except ValueError:
                await update.message.reply_text("⚠️ Format invalide. Ex: 06/05/2026")
                return
            pending_pre["step"] = "payment"
            await _ask_payment(update.message.reply_text)
            return

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
        except requests.RequestException as e:
            await update.message.reply_text(f"❌ Erreur de connexion: {str(e)}")
            return

        try:
            data = response.json()
        except ValueError:
            print(f"DEBUG API non-JSON response (status={response.status_code}): {response.text[:500]}")
            await update.message.reply_text(
                f"❌ Erreur serveur (HTTP {response.status_code}). "
                "Réessaie dans un instant ou contacte l'admin si ça persiste."
            )
            return

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
            if updated_pending.get("amount") is not None:
                updated_pending["amount"] = str(updated_pending["amount"])
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

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("depense", cmd_depense))
    app.add_handler(CommandHandler("annuler", cmd_annuler))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^g:"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
