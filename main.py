import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Tuple
import uvicorn
from thefuzz import fuzz

# ---------- ФУНКЦИЯ НОРМАЛИЗАЦИИ АДРЕСА (удаляем "г. Красноярск, ") ----------
def normalize_address(addr: str) -> str:
    """
    Удаляет общий префикс 'г. Красноярск, ' из адреса для более точного сравнения.
    Регистр не важен.
    """
    prefix = "г. Красноярск, "
    if addr.lower().startswith(prefix.lower()):
        return addr[len(prefix):].strip()
    return addr.strip()

# ---------- НАСТРОЙКА ПОДКЛЮЧЕНИЯ К GOOGLE SHEETS ----------
SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

# Путь к файлу с ключами на Render (секретный файл)
CREDENTIALS_PATH = '/etc/secrets/credentials.json'
# Для локального теста (закомментируйте перед загрузкой на GitHub)
# CREDENTIALS_PATH = 'credentials.json'

creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, SCOPE)
client = gspread.authorize(creds)

# ID таблицы читаем из переменной окружения (её зададим на Render)
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

# ---------- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ НЕЧЁТКОГО ПОИСКА ----------
def find_best_match(query_address: str, query_trade_name: Optional[str] = None, threshold: int = 75) -> Tuple[Optional[Dict], List[Dict], bool]:
    """
    Ищет наилучшее совпадение адреса и (опционально) названия.
    Возвращает (лучшая_строка, список_кандидатов, флаг_множественности)
    """
    all_data = sheet.get_all_records()
    if not all_data:
        return None, [], False

    # Нормализуем запрос (убираем город)
    query_clean = normalize_address(query_address)

    # Оцениваем похожесть адреса для каждой записи
    scored = []
    for row in all_data:
        db_addr = row.get('full_address', '')
        if not db_addr:
            continue
        db_addr_clean = normalize_address(db_addr)
        addr_score = fuzz.partial_ratio(query_clean.lower(), db_addr_clean.lower())
        if addr_score >= threshold:
            scored.append((addr_score, row))

    # Сортируем по убыванию балла
    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        return None, [], False

    # Если передан trade_name, дополнительно фильтруем по названию
    if query_trade_name and scored:
        name_scored = []
        for score, row in scored:
            db_name = row.get('trade_name', '')
            name_score = fuzz.partial_ratio(query_trade_name.lower(), db_name.lower())
            combined = (score + name_score) / 2  # среднее арифметическое
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
app = FastAPI(title="AI Assistant Sheets API (Fuzzy)")

@app.get("/preload", response_model=List[PreloadItem])
async def preload():
    """Возвращает список уникальных пар адрес+название для быстрой предзагрузки"""
    all_data = sheet.get_all_records()
    seen = set()
    result = []
    for row in all_data:
        addr = row.get('full_address')
        name = row.get('trade_name')
        if addr and name:
            key = (addr, name)
            if key not in seen:
                seen.add(key)
                result.append(PreloadItem(address=addr, trade_name=name))
    return result

@app.post("/get_info", response_model=InfoResponse)
async def get_info(request: InfoRequest):
    """
    Выполняет нечёткий поиск по адресу и (опционально) торговому наименованию.
    Возвращает лучшую найденную запись или список кандидатов для уточнения.
    """
    try:
        best_row, candidates, multiple = find_best_match(
            request.address,
            request.trade_name,
            threshold=75
        )

        if not best_row:
            return InfoResponse(
                success=False,
                message="По вашему запросу ничего не найдено. Попробуйте уточнить адрес или название."
            )

        # Если есть несколько кандидатов и название не указано, возвращаем список для уточнения
        if multiple and not request.trade_name:
            candidate_list = []
            for r in [best_row] + candidates[:2]:  # лучший + ещё два
                candidate_list.append({
                    "address": r.get('full_address'),
                    "trade_name": r.get('trade_name')
                })
            return InfoResponse(
                success=False,
                message="Найдено несколько заведений. Уточните название.",
                multiple=True,
                candidates=candidate_list
            )

        # Если всё хорошо, возвращаем данные
        return InfoResponse(
            success=True,
            data=dict(best_row),
            message="Найдено несколько записей, выбрана наиболее подходящая." if multiple else None
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка сервера: {str(e)}")

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)