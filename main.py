import os
import requests
import base64
import json
import io
import pandas as pd
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")
KIMI_TOKEN = os.getenv("KIMI_TOKEN")

# Новые токены для Airtable
AIRTABLE_TOKEN = "patD95Wp6hbmXnSH7.401bcf4ca42844c15f76c8361ddb7d5b7a4551d58c390de27ba3586fdd7d0cc7"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

user_sessions = {}
completed_orders = {} # Хранилище готовых заказов для кнопок Excel и Airtable

# ====================================================================
# ФУНКЦИИ NOTION И KIMI
# ====================================================================
def get_client_catalog(client_name):
    url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    payload = {"filter": {"property": "Client", "select": {"equals": client_name}}}
    response = requests.post(url, headers=NOTION_HEADERS, json=payload).json()
    
    catalog = []
    for item in response.get("results", []):
        page = item["properties"]
        try:
            item_id = page["ID"]["title"][0]["plain_text"]
            name = page.get("Name", {}).get("rich_text", [])
            name_text = name[0]["plain_text"] if name else "Без названия"
            catalog.append(f"ID: {item_id} | Название: {name_text}")
        except: continue
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
        client_price = page.get("Client Price", {}).get("number")
        gs_price = page.get("GS Price", {}).get("number")
        return {
            "name": name_list[0]["plain_text"] if name_list else "Нет названия",
            "client_price": float(client_price) if client_price is not None else 0.0,
            "gs_price": float(gs_price) if gs_price is not None else 0.0
        }
    except: return None

def recognize_photos_batch(orders, catalog_text):
    url = "https://api.moonshot.cn/v1/chat/completions"
    headers = {"Authorization": f"Bearer {KIMI_TOKEN}", "Content-Type": "application/json"}
    
    num_photos = len(orders)
    prompt = (
        f"Каталог:\n{catalog_text}\n\n"
        f"Я отправляю {num_photos} фото. Сопоставь каждое с ID. "
        "Ответ СТРОГО в формате JSON: массив строк. Если нет - 'ERROR'. Пример: [\"1\", \"ERROR\"]."
    )

    content_list = [{"type": "text", "text": prompt}]
    for order in orders:
        try:
            image_data = requests.get(order["photo_url"]).content
            base64_image = base64.b64encode(image_data).decode('utf-8')
            content_list.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}})
        except:
            content_list.append({"type": "text", "text": "[ОШИБКА]"})

    payload = {"model": "moonshot-v1-32k-vision-preview", "messages": [{"role": "user", "content": content_list}], "temperature": 0.0}
    try:
        response = requests.post(url, headers=headers, json=payload).json()
        if "error" in response: return ["ERROR"] * num_photos
        res_text = response["choices"][0]["message"]["content"].replace("```json", "").replace("```", "").strip()
        recognized_ids = json.loads(res_text)
        while len(recognized_ids) < num_photos: recognized_ids.append("ERROR")
        return recognized_ids
    except: return ["ERROR"] * num_photos

# ====================================================================
# ДИАЛОГОВАЯ ЧАСТЬ И ФИНАЛЬНЫЙ РАСЧЕТ
# ====================================================================
async def ask_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    session = user_sessions[user_id]
    items = session["items"]
    idx = session["current_item_index"]

    if idx >= len(items):
        await generate_final_invoice(update, context, user_id)
        return

    item = items[idx]

    if item["qty"] == "1" and not item.get("qty_confirmed"):
        session["state"] = "ASKING_QTY"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❓ Товар: **{item['name']}**\nКоличество: 1. Напиши правильную цифру:", parse_mode="Markdown")
        return

    if item.get("shipping") is None:
        session["state"] = "ASKING_SHIPPING"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🚚 Цена доставки для: **{item['name']}** ({item['qty']} шт)?", parse_mode="Markdown")
        return

    session["current_item_index"] += 1
    await ask_next_question(update, context, user_id)

async def generate_final_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    session = user_sessions[user_id]
    client_name = session["client"]
    items = session["items"]
    
    # Константы курсов
    client_rate = 58.0
    real_rate = 55.0
    
    subtotal_cny = 0
    purchase_cny = 0
    total_delivery_cny = 0
    
    inv_lines = ""
    
    for i in items:
        qty = int(i['qty'])
        price = i['client_price']
        gs_price = i['gs_price']
        shipping = float(i['shipping'])
        
        line_total = (price * qty) + shipping
        subtotal_cny += line_total
        purchase_cny += (gs_price * qty)
        total_delivery_cny += shipping
        
        # Сохраняем численные значения для Excel
        i['line_total'] = line_total
        
        inv_lines += f"• {i['name']}: — {qty} шт\n"
        inv_lines += f"{qty} × {price} + {shipping} = {line_total:.1f}¥\n\n"

    # Математика: 3% или 10 000 AMD
    comm_amd_3pct = (subtotal_cny * 0.03) * client_rate
    rule_applied = comm_amd_3pct < 10000
    actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
    actual_comm_cny = 10000 / client_rate if rule_applied else subtotal_cny * 0.03
    final_total_amd = int((subtotal_cny * client_rate) + actual_comm_amd)

    # Админка: Чистая прибыль
    real_expenses_amd = int((purchase_cny + total_delivery_cny) * real_rate)
    profit_amd = final_total_amd - real_expenses_amd

    # Сохраняем итоги в сессию для экспорта
    session.update({
        "subtotal_cny": subtotal_cny,
        "actual_comm_cny": actual_comm_cny,
        "actual_comm_amd": actual_comm_amd,
        "final_total_amd": final_total_amd,
        "purchase_cny": purchase_cny,
        "total_delivery_cny": total_delivery_cny,
        "profit_amd": profit_amd,
        "client_rate": client_rate,
        "real_rate": real_rate
    })
    
    completed_orders[user_id] = session # Переносим в готовые заказы

    # Формируем сообщение для клиента
    msg_client = f"""<b>COMMERCIAL INVOICE: {client_name.upper()}</b>
📅 Date: {datetime.now().strftime('%m.%d.%Y')}

<b>1. ТОВАРНАЯ ВЕДОМОСТЬ:</b>
{inv_lines}<code>────────────────────────</code>
<b>SUBTOTAL:</b> {subtotal_cny:.1f}¥

<b>2. КОМИССИЯ И СЕРВИС</b>
({'Минимальная 10000 AMD' if rule_applied else '3%'}): {actual_comm_cny:.1f}¥

<b>3. ИТОГОВЫЙ РАСЧЕТ</b>
• Всего в юанях: {subtotal_cny + actual_comm_cny:.1f}¥
• Курс: {client_rate}

✅ <b>ИТОГО К ОПЛАТЕ: {final_total_amd:,} AMD</b>"""

    # Формируем сообщение для админа
    msg_admin = f"""💼 <b>ВНУТРЕННИЙ РАСЧЕТ: {client_name.upper()}</b>

<b>РАСХОДЫ (Курс закупа: {real_rate}):</b>
• Закупка товара: {purchase_cny:.1f}¥
• Доставка по Китаю: {total_delivery_cny:.1f}¥
Итого расход: <b>{real_expenses_amd:,} AMD</b>

<b>ДОХОДЫ:</b>
• Взяли с клиента: <b>{final_total_amd:,} AMD</b>
• Комиссия в чеке: {actual_comm_amd:,} AMD

💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD</b>"""

    keyboard = [
        [InlineKeyboardButton("📊 Excel Инвойс", callback_data='gen_excel')], 
        [InlineKeyboardButton("📑 Отправить в Airtable", callback_data='export_airtable')]
    ]

    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg_client, parse_mode='HTML')
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    del user_sessions[user_id]

# ====================================================================
# EXCEL И AIRTABLE API (КНОПКИ)
# ====================================================================
async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    
    if user_id not in completed_orders:
        await query.message.reply_text("❌ Заказ устарел или не найден в памяти.")
        return
        
    data = completed_orders[user_id]
    
    if query.data == 'gen_excel':
        try:
            items_data = []
            for i, item in enumerate(data['items'], 1):
                items_data.append({
                    "№": i, 
                    "Название товара": item['name'], 
                    "Кол-во (шт)": item['qty'], 
                    "Цена (¥)": item['client_price'], 
                    "Логистика (¥)": float(item['shipping']), 
                    "Итого (¥)": item['line_total']
                })
                
            items_data.extend([
                {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "", "Итого (¥)": ""},
                {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "SUBTOTAL:", "Итого (¥)": f"{data['subtotal_cny']:.1f} ¥"},
                {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "Комиссия:", "Итого (¥)": f"{data['actual_comm_cny']:.1f} ¥"},
                {"№": "", "Название товара": "", "Кол-во (шт)": "", "Цена (¥)": "", "Логистика (¥)": "ИТОГО К ОПЛАТЕ:", "Итого (¥)": f"{data['final_total_amd']:,} AMD"}
            ])
            
            df = pd.DataFrame(items_data)
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
                df.to_excel(writer, index=False, sheet_name='Invoice')
            output.seek(0)
            
            ts = datetime.now().strftime('%H%M%S')
            filename = f"Invoice_{data['client']}_{ts}.xlsx"
            await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(output, filename=filename))
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка Excel: {e}")

    elif query.data == 'export_airtable':
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Закупка"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }
        
  # Интеграция по структуре Baza 2026
        payload = {
            "records": [
                {
                    "fields": {
                        "Клиент": data["client"],
                        "Сумма (¥)": data["subtotal_cny"],
                        "Курс Клиент": data["client_rate"],
                        "Курс Реал": data["real_rate"]
                        # СТРОЧКУ С ПРИБЫЛЬЮ УДАЛИЛИ! Airtable посчитает её сам.
                    }
                }
            ],
            "typecast": True
        }
        
        try:
            resp = requests.post(url, headers=headers, json=payload)
            if resp.status_code in [200, 201]:
                await query.message.reply_text("✅ Данные успешно записаны в базу Airtable!")
            else:
                await query.message.reply_text(f"❌ Ошибка от Airtable: {resp.text}")
        except Exception as e:
            await query.message.reply_text(f"❌ Сбой соединения: {e}")

# ====================================================================
# ТЕЛЕГРАМ ОБРАБОТЧИКИ
# ====================================================================
async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.split()
    if len(text) < 2: return await update.message.reply_text("❌ Формат: /client Имя")
    user_id = update.message.from_user.id
    user_sessions[user_id] = {"client": text[1], "orders": [], "items": [], "current_item_index": 0, "state": "COLLECTING"}
    await update.message.reply_text(f"✅ Клиент {text[1]} активен. Жду фото.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_sessions or user_sessions[user_id].get("state") != "COLLECTING": return
    photo_url = (await context.bot.get_file(update.message.photo[-1].file_id)).file_path
    qty = update.message.caption or "1"
    user_sessions[user_id]["orders"].append({"photo_url": photo_url, "qty": qty})

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_sessions: return
    session = user_sessions[user_id]
    if session["state"] == "COLLECTING": return
    text = update.message.text.strip()
    idx = session["current_item_index"]
    
    if session["state"] == "ASKING_QTY":
        session["items"][idx]["qty"] = text
        session["items"][idx]["qty_confirmed"] = True
    elif session["state"] == "ASKING_SHIPPING":
        session["items"][idx]["shipping"] = text
        session["current_item_index"] += 1
    await ask_next_question(update, context, user_id)

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_sessions or not user_sessions[user_id]["orders"]: return
    session = user_sessions[user_id]
    msg = await update.message.reply_text("⏳ Kimi распознает фото...")
    
    catalog_text = get_client_catalog(session["client"])
    recognized_ids = recognize_photos_batch(session["orders"], catalog_text)
    
    for order, rec_id in zip(session["orders"], recognized_ids):
        if "ERROR" in rec_id: continue
        details = get_item_details(session["client"], rec_id)
        if details:
            session["items"].append({
                "name": details['name'], "client_price": details['client_price'], 
                "gs_price": details['gs_price'], "qty": order["qty"], 
                "shipping": None, "qty_confirmed": False
            })

    await msg.edit_text("✅ Распознавание завершено! Уточняем детали...")
    await ask_next_question(update, context, user_id)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот готов! Начни с /client Имя")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("client", client_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(export_handler))
    print("Бот успешно запущен!")
    app.run_polling()
