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
    """Достаем каталог клиента из Notion"""
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
    """Берем цены и размеры для финального чека"""
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
    """Мега-функция: отправляет все фотки в Kimi за ОДИН раз"""
    url = "https://api.moonshot.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {KIMI_TOKEN}",
        "Content-Type": "application/json"
    }
    
    num_photos = len(orders)
    prompt = (
        f"Вот каталог товаров:\n{catalog_text}\n\n"
        f"Я отправляю тебе {num_photos} фотографий по порядку. Сопоставь КАЖДУЮ фотографию с ID товара из каталога. "
        "Твой ответ должен быть СТРОГО в формате JSON: массив строк, где каждая строка — это ID товара для соответствующей фотографии. "
        "Если товара нет, пиши 'ERROR'. "
        f"Пример ответа для 3 фото: [\"1\", \"5\", \"ERROR\"]. "
        "Никакого лишнего текста, только валидный JSON массив!"
    )

    # Собираем контент: 1 текстовое задание + много картинок
    content_list = [{"type": "text", "text": prompt}]
    
    for order in orders:
        try:
            # Скачиваем фото и кодируем в текст прямо перед отправкой
            image_data = requests.get(order["photo_url"]).content
            base64_image = base64.b64encode(image_data).decode('utf-8')
            content_list.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
            })
        except Exception as e:
            print(f"Ошибка скачивания фото: {e}")
            # Если фото не скачалось, шлем пустышку, чтобы не сбить порядок
            content_list.append({"type": "text", "text": "[ФОТО НЕ ЗАГРУЖЕНО]"})

    payload = {
        "model": "moonshot-v1-32k-vision-preview",
        "messages": [
            {
                "role": "user",
                "content": content_list
            }
        ],
        "temperature": 0.0
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload).json()
        
        if "error" in response:
            return [f"API_ERROR: {response['error'].get('message')}"] * num_photos
            
        result_text = response["choices"][0]["message"]["content"].strip()
        
        # Очищаем ответ от маркдауна, если Kimi все-таки его добавит
        result_text = result_text.replace("```json", "").replace("```", "").strip()
        
        # Превращаем текст в настоящий список Python
        recognized_ids = json.loads(result_text)
        
        # Защита от дурака: если Kimi вернул меньше ответов, чем было фоток
        while len(recognized_ids) < num_photos:
            recognized_ids.append("ERROR")
            
        return recognized_ids
        
    except Exception as e:
        print(f"Сбой парсинга Kimi: {e}")
        return ["CRITICAL_ERROR"] * num_photos

async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.split()
    if len(text) < 2:
        await update.message.reply_text("❌ Напиши команду так: /client Peto1910")
        return
    
    client_name = text[1]
    user_id = update.message.from_user.id
    user_sessions[user_id] = {"client": client_name, "orders": []}
    await update.message.reply_text(f"✅ Клиент {client_name} активирован!\nПересылай фото с подписью (количеством), в конце пиши /done")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_sessions:
        await update.message.reply_text("❌ Сначала выбери клиента командой /client Имя")
        return
        
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_url = file.file_path
    
    # Читаем количество ИЗ ПОДПИСИ к фотографии
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
    
    msg = await update.message.reply_text(f"⏳ Kimi изучает ВСЕ {len(orders)} фото одним запросом...")
    
    catalog_text = get_client_catalog(client_name)
    if not catalog_text:
        await msg.edit_text("❌ В Notion нет товаров для этого клиента!")
        return
        
    # Отправляем весь пакет в Kimi
    recognized_ids = recognize_photos_batch(orders, catalog_text)
    
    result_text = "/resultphoto\n\n"
    result_text += f"Клиент: {client_name}\n\n"
    
    not_found_items = []
    count = 1
    
    # Склеиваем ответы Kimi и наши заказы
    for order, recognized_id in zip(orders, recognized_ids):
        qty = order["qty"]
        
        # Если Kimi вернул ошибку или не нашел
        if recognized_id == "ERROR" or "ERROR" in recognized_id:
            not_found_items.append(f"Кол-во: {qty} (Ответ системы: '{recognized_id}')")
            continue
            
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
            not_found_items.append(f"Кол-во: {qty} (Ответ Kimi: '{recognized_id}' - ID не найден в базе)")
            
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
