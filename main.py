import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

# --- НОВАЯ КОМАНДА ДЛЯ ДИАГНОСТИКИ ---
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.split()
    if len(text) < 2:
        await update.message.reply_text("Напиши команду так: /check 1")
        return
        
    item_id = text[1]
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    
    # Ищем ТОЛЬКО по ID, игнорируя имя клиента
    payload = {
        "filter": {
            "property": "ID",
            "title": {"equals": item_id}
        }
    }
    
    await update.message.reply_text(f"Стучусь в базу {DATABASE_ID} за товаром {item_id}...")
    
    response = requests.post(url, headers=NOTION_HEADERS, json=payload)
    data = response.json()
    
    if "results" not in data:
        await update.message.reply_text(f"❌ Ошибка от самого Notion:\n{data}")
        return
        
    if not data["results"]:
        await update.message.reply_text(f"❌ Товар '{item_id}' не найден! Значит, DATABASE_ID указывает не на ту таблицу, или ID написан иначе.")
        return
        
    page = data["results"][0]["properties"]
    
    # Пытаемся прочитать, как Notion видит Клиента
    client_data = page.get("Client", {})
    if "select" in client_data and client_data["select"]:
        client_name = client_data["select"].get("name", "Пусто")
    else:
        client_name = "Ничего не выбрано или тип не Select"
        
    await update.message.reply_text(f"✅ УРА! Товар {item_id} существует в базе!\nВот что записано в колонке Клиент: '{client_name}'\n(Если там есть лишние пробелы, бот его не находил).")

# --- СТАРАЯ ФУНКЦИЯ ПОИСКА ---
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
        name_list = page.get("Name", {}).get("rich_text", [])
        name = name_list[0]["plain_text"] if name_list else "Нет названия"
        
        client_price = page.get("Client Price", {}).get("number")
        client_price_text = client_price if client_price is not None else "-"
        
        gs_price = page.get("GS Price", {}).get("number")
        gs_price_text = gs_price if gs_price is not None else "-"
        
        size_list = page.get("Size and weight", {}).get("rich_text", [])
        size_weight = size_list[0]["plain_text"] if size_list else "- - - -"
        
        return {
            "name": name,
            "client_price": client_price_text,
            "gs_price": gs_price_text,
            "size_weight": size_weight
        }
    except Exception as e:
        print(f"Ошибка: {e}")
        return None

async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    lines = text.strip().split('\n')
    
    if len(lines) < 3:
        await update.message.reply_text("❌ Ошибка формата. Пример:\n/find\nPeto1910\n1 200")
        return

    client_name = lines[1].strip()
    items_requested = lines[2:]
    
    await update.message.reply_text(f"⏳ Ищу товары для {client_name}...")
    
    result_text = "/paste\n\n"
    result_text += f"Клиент: {client_name}\n\n"
    
    count = 1
    for item_line in items_requested:
        parts = item_line.split()
        if len(parts) >= 2:
            item_id = parts[0]
            qty = parts[1]
            
            notion_data = search_in_notion(client_name, item_id)
            
            if notion_data:
                result_text += f"Товар {count}:\n"
                result_text += f"Название: {notion_data['name']}\n"
                result_text += f"Количество: {qty}\n"
                result_text += f"Цена клиенту: {notion_data['client_price']}\n"
                result_text += f"Закупка: {notion_data['gs_price']}\n"
                result_text += f"Доставка: -\n"
                result_text += f"Размеры: {notion_data['size_weight']}\n\n"
            else:
                result_text += f"Товар {count}:\n"
                result_text += f"❌ Товар {item_id} не найден!\n\n"
            count += 1
            
    result_text += "Курс клиенту: 58\nМой курс: 55"
    await update.message.reply_text(result_text)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот работает! Отправь /find или /check 1")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("check", check_command))
    print("Бот успешно запущен!")
    app.run_polling()
