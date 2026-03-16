import gspread
from oauth2client.service_account import ServiceAccountCredentials

scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
client = gspread.authorize(creds)

print("Пытаюсь открыть таблицу по ID...")
try:
    # ВСТАВЬТЕ СЮДА ВАШ РЕАЛЬНЫЙ ID
    sh = client.open_by_key("1zIqd833LNjc5spvoDmqIIwabhU8jhu-KaTf8bn0cnq8")
    print("Таблица успешно открыта!")
    print("Название:", sh.title)
    print("Доступные листы:", [ws.title for ws in sh.worksheets()])
except Exception as e:
    print("Ошибка при открытии по ID:", e)

print("\nПытаюсь открыть таблицу по имени...")
try:
    sh = client.open("Экосистема Рабочая")
    print("Таблица успешно открыта по имени!")
    print("Доступные листы:", [ws.title for ws in sh.worksheets()])
except Exception as e:
    print("Ошибка при открытии по имени:", e)