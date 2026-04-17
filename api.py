from fastapi import FastAPI
from pydantic import BaseModel, Field
from typing import Optional
import subprocess
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = FastAPI()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

WORKERS_GROUP_ID = -5135022095
MANAGEMENT_GROUP_ID = -5117263813

class CarExpense(BaseModel):
    date: str
    category: str
    details: str
    amount: str
    car: str
    payment_type: str
    file_url: str
    sheet_type: str = "car"

class GeneralExpense(BaseModel):
    date: str
    category: str
    details: str
    amount: str
    payment_type: str
    file_url: str
    sheet_type: str = "general"

def get_sheet(worksheet_name: str):
    creds = Credentials.from_service_account_file(
        "/root/google-service-account.json",
        scopes=SCOPES
    )
    client = gspread.authorize(creds)
    return client.open("Benim Car - Fiche Année 2025").worksheet(worksheet_name)

def check_duplicate_car(date: str, category: str, amount: str, car: str) -> bool:
    try:
        sheet = get_sheet("Dépenses Voitures")
        rows = sheet.get_all_values()
        try:
            normalized_date = datetime.strptime(date, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            normalized_date = date
        for row in rows[1:]:
            if len(row) < 5:
                continue
            if (row[0].strip() == normalized_date and
                row[1].strip() == category and
                str(row[3]).strip() == str(amount) and
                row[4].strip() == car):
                return True
        return False
    except Exception:
        return False

def check_duplicate_general(date: str, category: str, amount: str) -> bool:
    try:
        sheet = get_sheet("Dépense Général")
        rows = sheet.get_all_values()
        try:
            normalized_date = datetime.strptime(date, "%d/%m/%Y").strftime("%Y-%m-%d")
        except ValueError:
            normalized_date = date
        for row in rows[1:]:
            if len(row) < 4:
                continue
            if (row[0].strip() == normalized_date and
                row[1].strip() == category and
                str(row[3]).strip() == str(amount)):
                return True
        return False
    except Exception:
        return False

@app.post("/add-expense")
def add_expense(expense: dict):
    print(f"DEBUG expense received: {expense}")
    sheet_type = expense.get("sheet_type", "car")

    if sheet_type == "car":
        if check_duplicate_car(expense["date"], expense["category"], expense["amount"], expense["car"]):
            return {"success": False, "duplicate": True, "error": "Cette dépense existe déjà."}

        result = subprocess.run(
            [
                "/root/accounting-bot/add_expense.sh",
                "car",
                expense["date"],
                expense["category"],
                expense["details"],
                expense["amount"],
                expense["car"],
                expense["payment_type"],
                expense.get("file_url", ""),
            ],
            capture_output=True, text=True
        )

    elif sheet_type == "general":
        if check_duplicate_general(expense["date"], expense["category"], expense["amount"]):
            return {"success": False, "duplicate": True, "error": "Cette dépense existe déjà."}

        result = subprocess.run(
            [
                "/root/accounting-bot/add_expense.sh",
                "general",
                expense["date"],
                expense["category"],
                expense["details"],
                expense["amount"],
                expense["payment_type"],
                expense.get("file_url", ""),
            ],
            capture_output=True, text=True
        )
    else:
        return {"success": False, "error": f"Type de feuille inconnu: {sheet_type}"}

    if result.returncode != 0:
        return {"success": False, "duplicate": False, "error": result.stderr.strip()}

    return {"success": True, "duplicate": False, "output": result.stdout.strip()}

@app.get("/health")
def health():
    return {"status": "ok"}
