import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Tuple
import uvicorn
from thefuzz import fuzz

# ---------- ФУНКЦИЯ НОРМАЛИЗАЦИИ АДРЕСА ----------
def normalize_address(addr: str) -> str:
    prefix = "г. Красноярск, "
    if addr.lower().startswith(prefix.lower()):
        return addr[len(prefix):].strip()
    return addr.strip()

# ---------- ФУНКЦИЯ РАЗБОРА СИНОНИМОВ ----------
def split_aliases(text: str) -> List[str]:
    if not text:
        return [""]
    aliases = [alias.strip() for alias in text.split('/') if alias.strip()]
    return aliases if aliases else [text]

# ---------- НАСТРОЙКА ПОДКЛЮЧЕНИЯ К GOOGLE SHEETS ----------
SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
CREDENTIALS_PATH = '/etc/secrets/credentials.json'
# Для локального теста: CREDENTIALS_PATH = 'credentials.json'

creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, SCOPE)
client = gspread.authorize(creds)

SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID')
if not SPREADSHEET_ID:
    raise ValueError("Не задана переменная окружения SPREADSHEET_ID")
WORKSHEET_NAME = os.environ.get('WORKSHEET_NAME', 'API_DATA')
sheet = client.open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)

# ---------- МОДЕЛИ ДАННЫХ ----------
class InfoRequest(BaseModel):
    address: str
    trade_name: Optional[str] = None

class InfoResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    message: Optional[str] = None
    multiple: bool = False
    candidates: Optional[List[Dict[str, Any]]] = None

class PreloadItem(BaseModel):
    address: str
    trade_name: str

# ---------- ФУНКЦИИ ПОИСКА ----------
def get_best_alias_score(query: str, db_value: str, use_normalize: bool = False) -> int:
    aliases = split_aliases(db_value)
    max_score = 0
    for alias in aliases:
        if use_normalize:
            alias = normalize_address(alias)
        score = fuzz.partial_ratio(query.lower(), alias.lower())
        if score > max_score:
            max_score = score
    return max_score

def find_best_match(query_address: str, query_trade_name: Optional[str] = None, threshold: int = 75) -> Tuple[Optional[Dict], List[Dict], bool]:
    all_data = sheet.get_all_records()
    if not all_data:
        return None, [], False

    query_clean = normalize_address(query_address)
    scored = []
    for row in all_data:
        db_addr = row.get('full_address', '')
        if not db_addr:
            continue
        addr_score = get_best_alias_score(query_clean, db_addr, use_normalize=False)
        if addr_score >= threshold:
            scored.append((addr_score, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return None, [], False

    if query_trade_name and scored:
        name_scored = []
        for score, row in scored:
            db_name = row.get('trade_name', '')
            name_score = get_best_alias_score(query_trade_name, db_name, use_normalize=False)
            combined = (score + name_score) / 2
            name_scored.append((combined, row))
        name_scored.sort(key=lambda x: x[0], reverse=True)
        best_row = name_scored[0][1]
        other_candidates = [r for _, r in name_scored[1:]] if len(name_scored) > 1 else []
        return best_row, other_candidates, len(name_scored) > 1
    else:
        best_row = scored[0][1]
        other_candidates = [r for _, r in scored[1:]] if len(scored) > 1 else []
        return best_row, other_candidates, len(scored) > 1

# ---------- ЭНДПОИНТЫ ----------
app = FastAPI(title="AI Assistant Sheets API (Fuzzy with Aliases)")

@app.get("/preload", response_model=List[PreloadItem])
async def preload():
    all_data = sheet.get_all_records()
    seen = set()
    result = []
    for row in all_data:
        addr = row.get('full_address')
        name = row.get('trade_name')
        if addr and name:
            main_addr = split_aliases(addr)[0]
            main_name = split_aliases(name)[0]
            key = (main_addr, main_name)
            if key not in seen:
                seen.add(key)
                result.append(PreloadItem(address=main_addr, trade_name=main_name))
    return result

# Новый GET-эндпоинт для LPTracker
@app.get("/get_info", response_model=InfoResponse)
async def get_info_get(
    address: str = Query(..., description="Адрес заведения"),
    trade_name: Optional[str] = Query(None, description="Торговое наименование")
):
    """
    Выполняет нечёткий поиск с учётом синонимов по адресу и (опционально) торговому наименованию.
    Параметры передаются в URL (GET).
    """
    try:
        best_row, candidates, multiple = find_best_match(
            address,
            trade_name,
            threshold=75
        )

        if not best_row:
            return InfoResponse(
                success=False,
                message="По вашему запросу ничего не найдено. Попробуйте уточнить адрес или название."
            )

        if multiple and not trade_name:
            candidate_list = []
            for r in [best_row] + candidates[:2]:
                candidate_list.append({
                    "address": split_aliases(r.get('full_address', ''))[0],
                    "trade_name": split_aliases(r.get('trade_name', ''))[0]
                })
            return InfoResponse(
                success=False,
                message="Найдено несколько заведений. Уточните название.",
                multiple=True,
                candidates=candidate_list
            )

        return InfoResponse(
            success=True,
            data=dict(best_row),
            message="Найдено несколько записей, выбрана наиболее подходящая." if multiple else None
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {str(e)}")

# Оставляем POST для совместимости (может пригодиться)
@app.post("/get_info", response_model=InfoResponse)
async def get_info_post(request: InfoRequest):
    return await get_info_get(request.address, request.trade_name)

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
