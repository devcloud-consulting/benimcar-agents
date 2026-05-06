"""Inline-keyboard builders for the guided-expense flow.

Callback data scheme — keep prefixes short (Telegram caps at 64 bytes).
All guided-flow callbacks are namespaced under "g:".

  g:flow:img | guided      — initial branch choice
  g:cat:<idx>              — category index in the chat's allowed list
  g:car:<idx>              — car index in ALLOWED_CARS
  g:pay:<value>            — payment label (Cash/Card/Transfer/Chèque)
  g:date:today|yest|dby|custom
  g:det:skip               — skip details
  g:att:add|skip           — attach photo step
  g:conf:yes|no|edit       — confirmation step
  g:cancel                 — abort current flow
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _grid(buttons, cols=2):
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def kb_initial_choice():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📎 Avec justificatif", callback_data="g:flow:img"),
        InlineKeyboardButton("⌨️ Saisie guidée", callback_data="g:flow:guided"),
    ], [InlineKeyboardButton("🚫 Annuler", callback_data="g:cancel")]])


def kb_categories(allowed):
    btns = [InlineKeyboardButton(c, callback_data=f"g:cat:{i}") for i, c in enumerate(allowed)]
    rows = _grid(btns, cols=2)
    rows.append([InlineKeyboardButton("🚫 Annuler", callback_data="g:cancel")])
    return InlineKeyboardMarkup(rows)


def kb_cars(cars):
    btns = [InlineKeyboardButton(c, callback_data=f"g:car:{i}") for i, c in enumerate(cars)]
    rows = _grid(btns, cols=1)  # car names are long, single column
    rows.append([InlineKeyboardButton("🚫 Annuler", callback_data="g:cancel")])
    return InlineKeyboardMarkup(rows)


def kb_payments(payments):
    btns = [InlineKeyboardButton(p, callback_data=f"g:pay:{p}") for p in payments]
    rows = _grid(btns, cols=2)
    rows.append([InlineKeyboardButton("🚫 Annuler", callback_data="g:cancel")])
    return InlineKeyboardMarkup(rows)


def kb_dates():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Aujourd'hui", callback_data="g:date:today"),
         InlineKeyboardButton("Hier", callback_data="g:date:yest")],
        [InlineKeyboardButton("Avant-hier", callback_data="g:date:dby"),
         InlineKeyboardButton("📅 Autre", callback_data="g:date:custom")],
        [InlineKeyboardButton("🚫 Annuler", callback_data="g:cancel")],
    ])


def kb_skip_details():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ Passer", callback_data="g:det:skip")],
        [InlineKeyboardButton("🚫 Annuler", callback_data="g:cancel")],
    ])


def kb_attach():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📷 Ajouter photo/PDF", callback_data="g:att:add"),
         InlineKeyboardButton("⏭️ Sans justificatif", callback_data="g:att:skip")],
        [InlineKeyboardButton("🚫 Annuler", callback_data="g:cancel")],
    ])


def kb_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmer", callback_data="g:conf:yes"),
         InlineKeyboardButton("🚫 Annuler", callback_data="g:conf:no")],
    ])
