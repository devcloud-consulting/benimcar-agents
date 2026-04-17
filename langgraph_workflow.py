import os
import json
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from datetime import datetime

# ── LLM Setup ──────────────────────────────────────────────────────────────
llm = ChatOpenAI(
    model="deepseek-chat",
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com/v1"
)

# ── Allowed Values ──────────────────────────────────────────────────────────
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

ALLOWED_PAYMENTS = ["Transfer", "Card", "Cash", "Chèque"]

WORKERS_ALLOWED_CAR_CATEGORIES = [
    "Maintenance", "Loyer", "Assurance", "Fuel",
    "Vignette", "Péage/Parking", "Controle Technique"
]

WORKERS_ALLOWED_GENERAL_CATEGORIES = [
    "Lavage", "Fourniture", "Indrive/Taxi/Transport", "Panier Repas"
]

ALL_CATEGORIES = ALLOWED_CAR_CATEGORIES + ALLOWED_GENERAL_CATEGORIES

# ── State ───────────────────────────────────────────────────────────────────
class ExpenseState(TypedDict):
    user_message: str
    allowed_categories: list[str]
    extracted: Optional[dict]
    errors: list[str]
    summary: Optional[str]

# ── Image Analysis (Gemini Vision) ──────────────────────────────────────────
def analyze_image(image_path: str) -> str:
    from google import genai
    from google.genai import types
    import time

    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    MODELS_TO_TRY = [
        "gemini-2.5-flash",
        "gemini-2.0-flash-001",
        "gemini-2.0-flash-lite-001",
        "gemini-2.5-pro",
    ]

    with open(image_path, "rb") as f:
        image_data = f.read()

    last_error = None
    for model in MODELS_TO_TRY:
        for attempt in range(3):
            try:
                client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
                response = client.models.generate_content(
                    model=model,
                    contents=[
                        types.Part.from_bytes(data=image_data, mime_type="image/jpeg"),
                        "Tu es un assistant comptable. Analyse cette image de recu ou justificatif. Extrait toutes les informations visibles: montant total, date, type de depense, mode de paiement. Reponds en francais avec un resume court. Si tu ne vois pas clairement une information, dis-le."
                    ]
                )
                return response.text
            except Exception as e:
                last_error = e
                if "503" in str(e) or "UNAVAILABLE" in str(e):
                    time.sleep(5)
                    continue
                break

    raise Exception(f"Tous les modeles Gemini ont echoue. Derniere erreur: {last_error}")

# ── Node 1: Extract ─────────────────────────────────────────────────────────
def extract_expense(state: ExpenseState) -> ExpenseState:
    today = datetime.today().strftime("%d/%m/%Y")
    allowed_categories = state["allowed_categories"]

    prompt = f"""
Tu es un assistant comptable pour une entreprise de location de voitures au Maroc.
Aujourd'hui nous sommes le {today}.
Extrait les informations de depense depuis ce message en francais.

Message: {state['user_message']}

Reponds UNIQUEMENT avec un objet JSON valide, sans explication, sans markdown.
Format exact:
{{
  "date": "JJ/MM/AAAA",
  "categorie": "...",
  "details": "...",
  "montant": 000,
  "voiture": "...",
  "type_paiement": "..."
}}

Regles IMPORTANTES:
- date: si non mentionnee, utilise la date d'aujourd'hui ({today})
- montant: nombre entier uniquement, sans symbole
- details: resume brievement la depense en francais
- categorie: doit etre EXACTEMENT une de ces valeurs: {', '.join(allowed_categories)}
  IMPORTANT: Tu DOIS choisir la valeur la plus proche dans cette liste. Ne jamais retourner une valeur qui n'est pas dans cette liste. Si l'utilisateur ecrit "controle technique", "visite technique", "CT" -> tu retournes "Contrôle Technique". Si l'utilisateur ecrit "carburant", "essence", "gasoil" -> tu retournes "Fuel". Toujours mapper vers la valeur exacte de la liste.
  - "carburant", "essence", "gasoil", "fuel" -> "Fuel"
  - "maintenance", "reparation", "vidange" -> "Maintenance"
  - "assurance" -> "Assurance"
  - "loyer" -> "Loyer"
  - "vignette" -> "Vignette"
  - "parking", "peage" -> "Péage/Parking"
  - "achat voiture" -> "Achat Voiture"
  - "controle technique" -> "Controle Technique"
  - "salaire" -> "Salaire"
  - "cnss", "charge sociale" -> "CNSS Dirigeant"
  - "taxi", "indrive", "transport" -> "Indrive/Taxi/Transport"
  - "panier repas", "repas" -> "Panier Repas"
  - "fourniture" -> "Fourniture"
  - "lavage" -> "Lavage"
  - "comptable" -> "Comptable"
  - "frais bancaire", "banque" -> "Frais Bancaire"
  - "prestation" -> "Prestation"
- type_paiement: doit etre EXACTEMENT une de ces valeurs: {', '.join(ALLOWED_PAYMENTS)}
  - "cash", "especes" -> "Cash"
  - "carte", "card" -> "Card"
  - "virement", "transfer" -> "Transfer"
  - "cheque" -> "Cheque"
- voiture: si la categorie est une depense voiture ({', '.join(ALLOWED_CAR_CATEGORIES)}), mets EXACTEMENT une de ces valeurs: {', '.join(ALLOWED_CARS)}. Sinon mets null.
- Si une information est manquante, mets null
"""

    response = llm.invoke(prompt)
    raw = response.content.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        extracted = json.loads(raw)
    except Exception:
        extracted = None

    return {**state, "extracted": extracted}

# ── Node 2: Validate ────────────────────────────────────────────────────────
def validate_expense(state: ExpenseState) -> ExpenseState:
    errors = []
    data = state.get("extracted")
    allowed_categories = state["allowed_categories"]

    if not data:
        return {**state, "errors": ["Impossible d'extraire les donnees du message."]}

    if not data.get("date"):
        errors.append("Date manquante.")
    if not data.get("montant"):
        errors.append("Montant manquant.")
    if not data.get("details"):
        errors.append("Details manquants.")
    if data.get("categorie") not in allowed_categories:
        errors.append(f"Catégorie invalide: '{data.get('categorie')}'.")
    if data.get("type_paiement") not in ALLOWED_PAYMENTS:
        errors.append(f"Type de paiement invalide. Valeurs: Transfer, Card, Cash, Chèque")

    # Car is mandatory only for car categories
    if data.get("categorie") in ALLOWED_CAR_CATEGORIES:
        if data.get("voiture") not in ALLOWED_CARS:
            errors.append(f"Voiture invalide: '{data.get('voiture')}'.")

    return {**state, "errors": errors}

# ── Node 3: Summarize ───────────────────────────────────────────────────────
def summarize_expense(state: ExpenseState) -> ExpenseState:
    if state["errors"]:
        summary = "❌ *Erreurs:*\n" + "\n".join(f"- {e}" for e in state["errors"])
    else:
        d = state["extracted"]
        is_car_expense = d.get("categorie") in ALLOWED_CAR_CATEGORIES
        summary = (
            f"✅ *Dépense extraite:*\n"
            f"📅 Date: {d['date']}\n"
            f"🏷️ Catégorie: {d['categorie']}\n"
            f"📝 Détails: {d['details']}\n"
            f"💰 Montant: {d['montant']} MAD\n"
        )
        if is_car_expense and d.get("voiture"):
            summary += f"🚗 Voiture: {d['voiture']}\n"
        summary += (
            f"💳 Paiement: {d['type_paiement']}\n\n"
            f"Tapez *CONFIRMER* pour enregistrer."
        )
    return {**state, "summary": summary}

# ── Build Graph ─────────────────────────────────────────────────────────────
def build_graph():
    graph = StateGraph(ExpenseState)
    graph.add_node("extract", extract_expense)
    graph.add_node("validate", validate_expense)
    graph.add_node("summarize", summarize_expense)
    graph.set_entry_point("extract")
    graph.add_edge("extract", "validate")
    graph.add_edge("validate", "summarize")
    graph.add_edge("summarize", END)
    return graph.compile()

expense_graph = build_graph()

# ── Public Functions ────────────────────────────────────────────────────────
def process_expense_message(user_message: str, allowed_categories: list = None) -> dict:
    if allowed_categories is None:
        allowed_categories = ALLOWED_CAR_CATEGORIES
    return expense_graph.invoke({
        "user_message": user_message,
        "allowed_categories": allowed_categories,
        "extracted": None,
        "errors": [],
        "summary": None
    })

def process_expense_image(image_path: str, extra_info: str = "", allowed_categories: list = None) -> dict:
    if allowed_categories is None:
        allowed_categories = ALLOWED_CAR_CATEGORIES
    image_description = analyze_image(image_path)
    combined = f"{image_description}\n{extra_info}".strip()
    return process_expense_message(combined, allowed_categories)

def extract_correction(current_expense: dict, correction_text: str) -> dict:
    sheet_type = current_expense.get("sheet_type", "car")
    allowed_categories = ALLOWED_CAR_CATEGORIES if sheet_type == "car" else ALLOWED_GENERAL_CATEGORIES

    prompt = f"""
Tu es un assistant comptable. L'utilisateur veut corriger une depense existante.

Depense actuelle:
- Date: {current_expense.get('date')}
- Categorie: {current_expense.get('category')}
- Details: {current_expense.get('details')}
- Montant: {current_expense.get('amount')} MAD
- Voiture: {current_expense.get('car', 'N/A')}
- Paiement: {current_expense.get('payment_type')}

Message de correction: "{correction_text}"

Voitures autorisees: {', '.join(ALLOWED_CARS)}
Categories autorisees: {', '.join(allowed_categories)}
Paiements autorises: {', '.join(ALLOWED_PAYMENTS)}

Applique la correction et retourne UNIQUEMENT un JSON avec les champs mis a jour.
Ne retourne que les champs modifies.
Format: {{"field": "new_value"}}
Champs possibles: date, category, details, amount, car, payment_type

Si le message n'est pas une correction, retourne: {{}}
"""

    response = llm.invoke(prompt)
    raw = response.content.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        corrections = json.loads(raw)
    except Exception:
        corrections = {}

    if not corrections:
        return {}

    updated = dict(current_expense)
    updated.update(corrections)
    return updated
