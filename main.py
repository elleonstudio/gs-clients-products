import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Вставь сюда свои данные
TELEGRAM_TOKEN = "8734915350:AAG_R2_TBwbyqPj-gKE-bjuNH-vy2OSBPUA"
NOTION_TOKEN = "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"
DATABASE_ID = "3328c4d1fb0e80338915c1b18ec915ed"

# Настройки для Notion API
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

# Функция поиска в Notion
def search_in_notion(client_name, item_id):
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    
    # Говорим Notion: "Найди совпадение по Клиенту И по ID"
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
    
    # Если ничего не нашли, возвращаем None
    if not data.get("results"):
        return None
    
    # Если нашли, вытаскиваем данные из колонок
    page = data["results"][0]["properties"]
    
    try:
        # Аккуратно достаем текст из колонок (Notion хранит их сложно)
        name = page["Name"]["rich_text"][0]["plain_text"] if page["Name"]["rich_text"] else "Нет названия"
        client_price = page["Client Price"]["number"] or 0
        gs_price = page["GS Price"]["number"] or 0
        size_weight = page["Size and weight"]["rich_text"][0]["plain_text"] if page["Size and weight"]["rich_text"] else "- - - -"
        
        return {
            "name": name,
            "client_price": client_price,
            "gs_price": gs_price,
            "size_weight": size_weight
        }
    except Exception as e:
        return f"Ошибка чтения данных: {e}"

# Функция обработки команды /find
async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    lines = text.strip().split('\n')
    
    if len(lines) < 3:
        await update.message.reply_text("❌ Ошибка. Пример:\n/find\nZaven8291\nID 12 200")
        return

    client_name = lines[1].strip()
    items_requested = lines[2:] # Все строчки начиная с 3-й
    
    await update.message.reply_text(f"⏳ Ищу товары для {client_name} в Notion...")
    
    result_text = "/calc\n\n"
    result_text += f"Клиент: {client_name}\n\n"
    
    count = 1
    # Проходимся по каждой строчке (например, "ID 12 200")
    for item_line in items_requested:
        # Разбиваем строку: parts[0]="ID", parts[1]="12", parts[2]="200"
        parts = item_line.split()
        if len(parts) >= 3:
            item_id = f"{parts[0]} {parts[1]}" # Склеиваем обратно "ID 12"
            qty = parts[2]
            
            # Идем в Notion
            notion_data = search_in_notion(client_name, item_id)
            
            if notion_data and isinstance(notion_data, dict):
                result_text += f"Tovar {count}:\n"
                result_text += f"Название: {notion_data['name']}\n"
                result_text += f"количество: {qty}\n"
                result_text += f"закупка: {notion_data['gs_price']}\n"
                result_text += f"доставка: -\n"
                result_text += f"размери: {notion_data['size_weight']}\n\n"
            else:
                result_text += f"❌ Товар {item_id} не найден в базе!\n\n"
            count += 1
            
    result_text += f"и сколко товаров ест: {count - 1}\nв конце\n\n"
    result_text += "курс клиенту: 58\nмой курс: 55"
    
    await update.message.reply_text(result_text)

# Запуск бота
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Говорим боту реагировать на команду /find
    app.add_handler(CommandHandler("find", find_command))
    
    print("Бот запущен! Напиши ему /find в Telegram.")
    app.run_polling()
