import os
import requests
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")
KIMI_TOKEN = os.getenv("KIMI_TOKEN")

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

user_sessions = {}

def get_client_catalog(client_name):
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    payload = {
        "filter": {
            "property": "Client",
            "select": {"equals": client_name}
        }
    }
    response = requests.post(url, headers=NOTION_HEADERS, json=payload)
    data = response.json()
    
    catalog = []
    for item in data.get("results", []):
        page = item["properties"]
        try:
            item_id = page["ID"]["title"][0]["plain_text"]
            name = page.get("Name", {}).get("rich_text", [])
            name_text = name[0]["plain_text"] if name else "Без названия"
            catalog.append(f"ID: {item_id} | Название: {name_text}")
        except:
            continue
    return "\n".join(catalog)

def get_item_details(client_name, item_id):
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    payload = {
        "filter": {
            "and": [
                {"property": "Client", "select": {"equals": client_name}},
                {"property": "ID", "title": {"equals": item_id}}
            ]
        }
    }
    response = requests.post(url, headers=NOTION_HEADERS, json=payload).json()
    if not response.get("results"): return None
    
    page = response["results"][0]["properties"]
    try:
        name_list = page.get("Name", {}).get("rich_text", [])
        name = name_list[0]["plain_text"] if name_list else "Нет названия"
        client_price = page.get("Client Price", {}).get("number")
        gs_price = page.get("GS Price", {}).get("number")
        size_list = page.get("Size and weight", {}).get("rich_text", [])
        
        return {
            "name": name,
            "client_price": client_price if client_price is not None else "-",
            "gs_price": gs_price if gs_price is not None else "-",
            "size_weight": size_list[0]["plain_text"] if size_list else "- - - -"
        }
    except:
        return None

def recognize_photo_with_kimi(photo_url, catalog_text):
    url = "https://api.moonshot.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {KIMI_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # СДЕЛАЛИ ПРОМПТ ОЧЕНЬ ЖЕСТКИМ
    prompt = (
        f"Вот список товаров:\n{catalog_text}\n\n"
        "Посмотри на фото и найди этот товар в списке. "
        "ОТВЕТЬ СТРОГО ОДНОЙ ЦИФРОЙ (это ID товара). "
        "Запрещено писать любые слова, точки, пробелы или пояснения. Только цифра. "
        "Если товара нет на фото, напиши слово ERROR"
    )

    payload = {
        "model": "moonshot-v1-32k-vision-preview",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": photo_url}}
                ]
            }
        ],
        "temperature": 0.0 # Ноль фантазии, только факты
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload).json()
        result_text = response["choices"][0]["message"]["content"].strip()
        return result_text
    except Exception as e:
        return f"Ошибка API: {e}"

async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.split()
    if len(text) < 2:
        await update.message.reply_text("❌ Напиши команду так: /client Peto1910")
        return
    
    client_name = text[1]
    user_id = update.message.from_user.id
    user_sessions[user_id] = {"client": client_name, "orders": []}
    await update.message.reply_text(f"✅ Клиент {client_name} активирован!\nПересылай фото с количеством, в конце пиши /done")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_sessions:
        await update.message.reply_text("❌ Сначала выбери клиента командой /client Имя")
        return
        
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_url = file.file_path
    
    qty = update.message.caption
    if not qty:
        qty = "1"
        
    user_sessions[user_id]["orders"].append({"photo_url": photo_url, "qty": qty})

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_sessions or not user_sessions[user_id]["orders"]:
        await update.message.reply_text("❌ Нет сохраненных фотографий для обработки.")
        return
        
    session = user_sessions[user_id]
    client_name = session["client"]
    orders = session["orders"]
    
    msg = await update.message.reply_text(f"⏳ Kimi изучает {len(orders)} фото для {client_name}...")
    
    catalog_text = get_client_catalog(client_name)
    if not catalog_text:
        await msg.edit_text("❌ В Notion нет товаров для этого клиента!")
        return
        
    # ИЗМЕНИЛИ ШАПКУ НА /resultphoto
    result_text = "/resultphoto\n\n"
    result_text += f"Клиент: {client_name}\n\n"
    
    not_found_items = []
    count = 1
    
    for order in orders:
        qty = order["qty"]
        photo_url = order["photo_url"]
        
        recognized_id = recognize_photo_with_kimi(photo_url, catalog_text)
        
        # Проверяем, что ответил Kimi
        details = get_item_details(client_name, recognized_id)
        
        if details:
            result_text += f"Товар {count}:\n"
            result_text += f"Название: {details['name']}\n"
            result_text += f"Количество: {qty}\n"
            result_text += f"Цена клиенту: {details['client_price']}\n"
            result_text += f"Закупка: {details['gs_price']}\n"
            result_text += f"Доставка: -\n"
            result_text += f"Размеры: {details['size_weight']}\n\n"
            count += 1
        else:
            # Сохраняем ТОЧНЫЙ ответ Kimi для диагностики
            not_found_items.append(f"Кол-во: {qty} (Ответ Kimi: '{recognized_id}')")
            
    result_text += "Курс клиенту: 58\nМой курс: 55\n\n"
    
    if not_found_items:
        result_text += "⚠️ Kimi не смог сопоставить эти товары:\n" + "\n".join(not_found_items)
        
    await msg.edit_text(result_text)
    del user_sessions[user_id]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот готов! Начни с /client Имя")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("client", client_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("Бот успешно запущен!")
    app.run_polling()
