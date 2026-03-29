import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Система сама возьмет твои ключи из настроек Railway
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

def search_in_notion(client_name, item_id):
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    
    payload = {
        "filter": {
            "and": [
                {"property": "Client", "select": {"equals": client_name}},
                {"property": "ID", "title": {"equals": item_id}}
            ]
        }
    }
    
    response = requests.post(url, headers=NOTION_HEADERS, json=payload)
    data = response.json()
    
    if not data.get("results"):
        return None
    
    page = data["results"][0]["properties"]
    
    try:
        # Читаем твои точные колонки из Notion
        name = page["Name"]["rich_text"][0]["plain_text"] if page["Name"]["rich_text"] else "Нет названия"
        gs_price = page["GS Price"]["number"] or 0
        size_weight = page["Size and weight"]["rich_text"][0]["plain_text"] if page["Size and weight"]["rich_text"] else "- - - -"
        
        return {
            "name": name,
            "gs_price": gs_price,
            "size_weight": size_weight
        }
    except Exception as e:
        print(f"Ошибка парсинга Notion: {e}")
        return None

async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    lines = text.strip().split('\n')
    
    if len(lines) < 3:
        await update.message.reply_text("❌ Ошибка формата. Пример:\n/find\nZaven8291\nID 12 200")
        return

    client_name = lines[1].strip()
    items_requested = lines[2:]
    
    await update.message.reply_text(f"⏳ Ищу товары для {client_name}...")
    
    result_text = "/calc\n\n"
    result_text += f"Клиент: {client_name}\n\n"
    
    count = 1
    for item_line in items_requested:
        parts = item_line.split()
        if len(parts) >= 3:
            item_id = f"{parts[0]} {parts[1]}"
            qty = parts[2]
            
            notion_data = search_in_notion(client_name, item_id)
            
            if notion_data:
                result_text += f"Tovar {count}:\n"
                result_text += f"Название: {notion_data['name']}\n"
                result_text += f"количество: {qty}\n"
                result_text += f"закупка: {notion_data['gs_price']}\n"
                result_text += f"доставка: -\n"
                result_text += f"размери: {notion_data['size_weight']}\n\n"
            else:
                result_text += f"❌ Товар {item_id} не найден!\n\n"
            count += 1
            
    result_text += f"и сколко товаров ест: {count - 1}\nв конце\n\n"
    result_text += "курс клиенту: 58\nмой курс: 55"
    
    await update.message.reply_text(result_text)

if __name__ == '__main__':
    # Если ключей нет (например, забыли добавить в Railway), бот выдаст ошибку в логах
    if not TELEGRAM_TOKEN or not NOTION_TOKEN or not DATABASE_ID:
        print("ВНИМАНИЕ: Не найдены секретные ключи (Tokens)!")
    else:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        app.add_handler(CommandHandler("find", find_command))
        print("Бот успешно запущен!")
        app.run_polling()
