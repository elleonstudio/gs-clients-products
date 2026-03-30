import os
import requests
import base64
import json
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

def recognize_photos_batch(orders, catalog_text):
    url = "https://api.moonshot.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {KIMI_TOKEN}",
        "Content-Type": "application/json"
    }
    
    num_photos = len(orders)
    prompt = (
        f"Вот каталог товаров:\n{catalog_text}\n\n"
        f"Я отправляю тебе {num_photos} фотографий. Сопоставь КАЖДУЮ фотографию с ID товара из каталога. "
        "Твой ответ должен быть СТРОГО в формате JSON: массив строк, где каждая строка — это ID товара. "
        "Если товара нет, пиши 'ERROR'. "
        f"Пример ответа: [\"1\", \"5\", \"ERROR\"]. "
        "Никакого лишнего текста, только JSON массив!"
    )

    content_list = [{"type": "text", "text": prompt}]
    for order in orders:
        try:
            image_data = requests.get(order["photo_url"]).content
            base64_image = base64.b64encode(image_data).decode('utf-8')
            content_list.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
            })
        except:
            content_list.append({"type": "text", "text": "[ОШИБКА ФОТО]"})

    payload = {
        "model": "moonshot-v1-32k-vision-preview",
        "messages": [{"role": "user", "content": content_list}],
        "temperature": 0.0
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload).json()
        if "error" in response: return [f"API_ERROR"] * num_photos
        result_text = response["choices"][0]["message"]["content"].strip()
        result_text = result_text.replace("```json", "").replace("```", "").strip()
        recognized_ids = json.loads(result_text)
        while len(recognized_ids) < num_photos: recognized_ids.append("ERROR")
        return recognized_ids
    except:
        return ["CRITICAL_ERROR"] * num_photos

# --- ДИАЛОГОВАЯ ЧАСТЬ ---

async def ask_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Умный опросник: идет по списку товаров и задает вопросы"""
    session = user_sessions[user_id]
    items = session["items"]
    idx = session["current_item_index"]

    # Если мы прошли все товары, выдаем финальный результат
    if idx >= len(items):
        await generate_final_paste(update, context, user_id)
        return

    item = items[idx]

    # Проверка 1: Если количество '1' и мы еще не переспрашивали
    if item["qty"] == "1" and not item.get("qty_confirmed"):
        session["state"] = "ASKING_QTY"
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❓ Товар: **{item['name']}**\nКоличество стоит: 1. Это верно?\n👉 *Напиши правильную цифру:*",
            parse_mode="Markdown"
        )
        return

    # Проверка 2: Если цена доставки еще не указана
    if item.get("shipping") is None:
        session["state"] = "ASKING_SHIPPING"
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🚚 Цена доставки для: **{item['name']}** (Кол-во: {item['qty']} шт)\n👉 *Напиши стоимость доставки:*",
            parse_mode="Markdown"
        )
        return

    # Если всё заполнено, переходим к следующему товару
    session["current_item_index"] += 1
    await ask_next_question(update, context, user_id)

async def generate_final_paste(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Сборка финального чека /paste"""
    session = user_sessions[user_id]
    client_name = session["client"]
    items = session["items"]
    not_found_items = session["not_found_items"]
    
    result_text = "/paste\n\n"
    result_text += f"Клиент: {client_name}\n\n"
    
    count = 1
    for item in items:
        result_text += f"Товар {count}:\n"
        result_text += f"Название: {item['name']}\n"
        result_text += f"Количество: {item['qty']}\n"
        result_text += f"Цена клиенту: {item['client_price']}\n"
        result_text += f"Закупка: {item['gs_price']}\n"
        result_text += f"Доставка: {item['shipping']}\n" # Теперь доставка из опроса!
        result_text += f"Размеры: {item['size_weight']}\n\n"
        count += 1
        
    result_text += "Курс клиенту: 58\nМой курс: 55\n\n"
    
    if not_found_items:
        result_text += "⚠️ Kimi не смог сопоставить эти товары:\n" + "\n".join(not_found_items)
        
    await context.bot.send_message(chat_id=update.effective_chat.id, text=result_text)
    
    # Очищаем память
    del user_sessions[user_id]

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловим текстовые ответы (количество и цену доставки)"""
    user_id = update.message.from_user.id
    text = update.message.text.strip()
    
    # Если бот не в режиме опроса, просто игнорируем текст
    if user_id not in user_sessions or user_sessions[user_id].get("state") == "COLLECTING":
        return

    session = user_sessions[user_id]
    idx = session["current_item_index"]
    item = session["items"][idx]

    if session["state"] == "ASKING_QTY":
        item["qty"] = text # Сохраняем новую цифру
        item["qty_confirmed"] = True
        await ask_next_question(update, context, user_id)

    elif session["state"] == "ASKING_SHIPPING":
        item["shipping"] = text # Сохраняем цену доставки
        session["current_item_index"] += 1 # Товар полностью готов, идем дальше
        await ask_next_question(update, context, user_id)


# --- ОСНОВНЫЕ КОМАНДЫ ---

async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.split()
    if len(text) < 2:
        await update.message.reply_text("❌ Напиши команду так: /client Peto1910")
        return
    client_name = text[1]
    user_id = update.message.from_user.id
    
    # Инициализируем новую структуру сессии
    user_sessions[user_id] = {
        "client": client_name, 
        "orders": [],
        "items": [],
        "not_found_items": [],
        "current_item_index": 0,
        "state": "COLLECTING"
    }
    await update.message.reply_text(f"✅ Клиент {client_name} активирован!\nПересылай фото, в конце пиши /done")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_sessions or user_sessions[user_id].get("state") != "COLLECTING":
        return
        
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_url = file.file_path
    
    qty = update.message.caption
    if not qty: qty = "1"
        
    user_sessions[user_id]["orders"].append({"photo_url": photo_url, "qty": qty})

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_sessions or not user_sessions[user_id]["orders"]:
        await update.message.reply_text("❌ Нет сохраненных фотографий для обработки.")
        return
        
    session = user_sessions[user_id]
    client_name = session["client"]
    orders = session["orders"]
    
    msg = await update.message.reply_text(f"⏳ Kimi распознает {len(orders)} фото. Ждем...")
    
    catalog_text = get_client_catalog(client_name)
    recognized_ids = recognize_photos_batch(orders, catalog_text)
    
    # Собираем успешные товары в список items
    for order, recognized_id in zip(orders, recognized_ids):
        qty = order["qty"]
        
        if recognized_id == "ERROR" or "ERROR" in recognized_id:
            session["not_found_items"].append(f"Кол-во: {qty} (Ответ: '{recognized_id}')")
            continue
            
        details = get_item_details(client_name, recognized_id)
        if details:
            session["items"].append({
                "name": details['name'],
                "client_price": details['client_price'],
                "gs_price": details['gs_price'],
                "size_weight": details['size_weight'],
                "qty": qty,
                "shipping": None,
                "qty_confirmed": False # По умолчанию считаем не подтвержденным
            })
        else:
            session["not_found_items"].append(f"Кол-во: {qty} (Ответ: '{recognized_id}' - нет в базе)")

    await msg.edit_text("✅ Распознавание завершено! Уточняем детали...")
    
    # Запускаем цепочку вопросов
    await ask_next_question(update, context, user_id)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот готов! Начни с /client Имя")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("client", client_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)) # Слушатель ответов
    print("Бот успешно запущен!")
    app.run_polling()
