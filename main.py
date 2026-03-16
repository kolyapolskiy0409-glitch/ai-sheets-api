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

# ---------- ФУНКЦИЯ РАЗБОРА СИНОНИМОВ ----------
def split_aliases(text: str) -> List[str]:
    """
    Разбивает строку на альтернативные варианты по разделителю '/'.
    Удаляет лишние пробелы.
    """
    if not text:
        return [""]
    # Разделяем по '/', убираем пробелы по краям, отбрасываем пустые
    aliases = [alias.strip() for alias in text.split('/') if alias.strip()]
    return aliases if aliases else [text]

# ---------- НАСТРОЙКА ПОДКЛЮЧЕНИЯ К GOOGLE SHEETS ----------
SCOPE = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']

# Путь к файлу с ключами на Render
CREDENTIALS_PATH = '/etc/secrets/credentials.json'
# Для локального теста (раскомментируйте при необходимости)
# CREDENTIALS_PATH = 'credentials.json'

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

# ---------- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ НЕЧЁТКОГО ПОИСКА С УЧЁТОМ СИНОНИМОВ ----------
def get_best_alias_score(query: str, db_value: str, use_normalize: bool = False) -> int:
    """
    Возвращает максимальный балл похожести между запросом и любым из синонимов в db_value.
    Если use_normalize=True, применяет normalize_address к каждому варианту.
    """
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
    """
    Ищет наилучшее совпадение адреса и (опционально) названия с учётом синонимов.
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
        # Получаем максимальный балл среди всех синонимов адреса
        addr_score = get_best_alias_score(query_clean, db_addr, use_normalize=False)  # normalize уже применён к query_clean, к синонимам не применяем, т.к. они уже могут содержать префикс
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
            # Для названия тоже учитываем синонимы
            name_score = get_best_alias_score(query_trade_name, db_name, use_normalize=False)
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
app = FastAPI(title="AI Assistant Sheets API (Fuzzy with Aliases)")

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
            # Для предзагрузки используем только первый вариант (без синонимов), чтобы не раздувать список
            main_addr = split_aliases(addr)[0]
            main_name = split_aliases(name)[0]
            key = (main_addr, main_name)
            if key not in seen:
                seen.add(key)
                result.append(PreloadItem(address=main_addr, trade_name=main_name))
    return result

@app.post("/get_info", response_model=InfoResponse)
async def get_info(request: InfoRequest):
    """
    Выполняет нечёткий поиск с учётом синонимов по адресу и (опционально) торговому наименованию.
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
            for r in [best_row] + candidates[:2]:
                # Для отображения кандидатов используем основной вариант (без синонимов)
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

        # Если всё хорошо, возвращаем данные (все поля, но для адреса и названия можно оставить оригинал)
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
