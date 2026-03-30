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

# Базы данных
NOTION_CACHE_ID = "4547cbb7cbc54138a5ead9f942bd30dc" # Новая база для вечной памяти
AIRTABLE_TOKEN = "patD95Wp6hbmXnSH7.401bcf4ca42844c15f76c8361ddb7d5b7a4551d58c390de27ba3586fdd7d0cc7"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

user_sessions = {}

# ====================================================================
# NOTION CACHE (ВЕЧНАЯ ПАМЯТЬ)
# ====================================================================
def save_to_notion_cache(data, page_id=None):
    """Сохраняем весь чек в Notion в виде JSON. Поддерживает перезапись (edit)."""
    json_str = json.dumps(data, ensure_ascii=False)
    # Разбиваем длинный текст, так как лимит Notion - 2000 символов на блок
    chunks = [json_str[i:i+2000] for i in range(0, len(json_str), 2000)]
    rich_text_array = [{"text": {"content": chunk}} for chunk in chunks]

    payload = {
        "properties": {
            "Order ID": {"title": [{"text": {"content": f"{data['client']} - {datetime.now().strftime('%d.%m.%Y %H:%M')}"}}]},
            "Data_JSON": {"rich_text": rich_text_array}
        }
    }

    if page_id: # Если редактируем старый чек
        url = f"https://api.notion.com/v1/pages/{page_id}"
        requests.patch(url, headers=NOTION_HEADERS, json=payload)
        return page_id
    else: # Если это новый чек
        url = "https://api.notion.com/v1/pages"
        payload["parent"] = {"database_id": NOTION_CACHE_ID}
        response = requests.post(url, headers=NOTION_HEADERS, json=payload).json()
        return response["id"].replace("-", "")

def get_from_notion_cache(page_id):
    """Достаем чек из вечной памяти Notion"""
    url = f"https://api.notion.com/v1/pages/{page_id}"
    response = requests.get(url, headers=NOTION_HEADERS).json()
    rich_text_array = response["properties"]["Data_JSON"]["rich_text"]
    json_str = "".join([block["text"]["content"] for block in rich_text_array])
    return json.loads(json_str)


# ====================================================================
# ФУНКЦИИ NOTION (КАТАЛОГ) И KIMI
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
    idx = session.get("current_item_index", 0)

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

async def generate_final_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, page_id=None):
    session = user_sessions[user_id]
    client_name = session["client"]
    items = session["items"]
    
    client_rate = 58.0
    real_rate = 55.0
    
    subtotal_cny = 0
    purchase_cny = 0
    total_delivery_cny = 0
    inv_lines = ""
    
    for i in items:
        qty = int(i['qty'])
        price = float(i['client_price'])
        gs_price = float(i['gs_price'])
        shipping = float(i['shipping'])
        
        line_total = (price * qty) + shipping
        subtotal_cny += line_total
        purchase_cny += (gs_price * qty)
        total_delivery_cny += shipping
        i['line_total'] = line_total
        
        inv_lines += f"• {i['name']}: — {qty} шт\n"
        inv_lines += f"{qty} × {price} + {shipping} = {line_total:.1f}¥\n\n"

    comm_amd_3pct = (subtotal_cny * 0.03) * client_rate
    rule_applied = comm_amd_3pct < 10000
    actual_comm_amd = 10000 if rule_applied else int(comm_amd_3pct)
    actual_comm_cny = 10000 / client_rate if rule_applied else subtotal_cny * 0.03
    final_total_amd = int((subtotal_cny * client_rate) + actual_comm_amd)

    real_expenses_amd = int((purchase_cny + total_delivery_cny) * real_rate)
    profit_amd = final_total_amd - real_expenses_amd

    session.update({
        "subtotal_cny": subtotal_cny, "actual_comm_cny": actual_comm_cny,
        "actual_comm_amd": actual_comm_amd, "final_total_amd": final_total_amd,
        "purchase_cny": purchase_cny, "total_delivery_cny": total_delivery_cny,
        "profit_amd": profit_amd, "client_rate": client_rate, "real_rate": real_rate
    })
    
    # МАГИЯ: Сохраняем в кэш Notion и получаем ID страницы
    new_page_id = save_to_notion_cache(session, page_id=page_id)

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

    msg_admin = f"""💼 <b>ВНУТРЕННИЙ РАСЧЕТ: {client_name.upper()}</b>

<b>РАСХОДЫ (Курс закупа: {real_rate}):</b>
• Закупка товара: {purchase_cny:.1f}¥
• Доставка по Китаю: {total_delivery_cny:.1f}¥
Итого расход: <b>{real_expenses_amd:,} AMD</b>

<b>ДОХОДЫ:</b>
• Взяли с клиента: <b>{final_total_amd:,} AMD</b>
• Комиссия в чеке: {actual_comm_amd:,} AMD

💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {profit_amd:,} AMD</b>"""

    # Кнопки привязаны к вечному ID из Notion
    keyboard = [
        [InlineKeyboardButton("✏️ Изменить товар", callback_data=f"edit_{new_page_id}")],
        [InlineKeyboardButton("📊 Excel Инвойс", callback_data=f"excel_{new_page_id}")], 
        [InlineKeyboardButton("📑 Отправить в Airtable", callback_data=f"airtable_{new_page_id}")]
    ]

    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg_client, parse_mode='HTML')
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    del user_sessions[user_id] # Очищаем оперативную память, всё надежно в Notion!


# ====================================================================
# EXCEL, AIRTABLE И РЕДАКТИРОВАНИЕ (ОБРАБОТЧИК КНОПОК)
# ====================================================================
async def export_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    action, page_id = query.data.split("_", 1)
    
    # 1. Логика кнопки "Изменить товар"
    if action == "edit":
        try:
            data = get_from_notion_cache(page_id)
            data["page_id"] = page_id # Запоминаем, какой чек мы редактируем
            data["state"] = "WAITING_EDIT_NUM"
            user_sessions[user_id] = data
            await query.message.reply_text(f"✏️ Какой номер товара изменить? (от 1 до {len(data['items'])})\n👉 Напиши цифру:")
        except Exception as e:
            await query.message.reply_text("❌ Ошибка загрузки чека из памяти Notion.")
        return

    # Загружаем данные для Excel и Airtable из вечной памяти
    try:
        data = get_from_notion_cache(page_id)
    except:
        return await query.message.reply_text("❌ Заказ удален из базы Notion.")

    # 2. Логика кнопки "Excel"
    if action == 'excel':
        try:
            items_data = []
            for i, item in enumerate(data['items'], 1):
                items_data.append({
                    "№": i, "Название товара": item['name'], "Кол-во (шт)": item['qty'], 
                    "Цена (¥)": item['client_price'], "Логистика (¥)": float(item['shipping']), "Итого (¥)": item['line_total']
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
            
            filename = f"Invoice_{data['client']}_{datetime.now().strftime('%H%M%S')}.xlsx"
            await context.bot.send_document(chat_id=query.message.chat_id, document=InputFile(output, filename=filename))
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка Excel: {e}")

    # 3. Логика кнопки "Airtable"
    elif action == 'airtable':
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Закупка"
        headers = {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}
        
        payload = {
            "records": [{
                "fields": {
                    "Клиент": data["client"],
                    "Сумма (¥)": data["subtotal_cny"],
                    "Курс Клиент": data["client_rate"],
                    "Курс Реал": data["real_rate"]
                    # Поле Прибыль удалено, Airtable считает сам
                }
            }],
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
    
    # БЛОК РЕДАКТИРОВАНИЯ
    if session["state"] == "WAITING_EDIT_NUM":
        try:
            idx = int(text) - 1
            if 0 <= idx < len(session["items"]):
                session["edit_item_index"] = idx
                session["state"] = "WAITING_EDIT_QTY"
                item = session["items"][idx]
                await update.message.reply_text(f"📦 Товар {idx+1}: {item['name']}\nТекущее количество: {item['qty']}\n👉 Напиши новое количество или отправь - (минус), чтобы не менять:")
            else: await update.message.reply_text("❌ Неверный номер.")
        except: await update.message.reply_text("❌ Напиши просто цифру.")
        return

    if session["state"] == "WAITING_EDIT_QTY":
        idx = session["edit_item_index"]
        if text != "-": session["items"][idx]["qty"] = text
        session["state"] = "WAITING_EDIT_SHIPPING"
        await update.message.reply_text(f"🚚 Текущая доставка: {session['items'][idx]['shipping']}\n👉 Напиши новую доставку или отправь - (минус):")
        return

    if session["state"] == "WAITING_EDIT_SHIPPING":
        idx = session["edit_item_index"]
        if text != "-": session["items"][idx]["shipping"] = text
        await update.message.reply_text("✅ Изменения приняты! Пересчитываю чек...")
        await generate_final_invoice(update, context, user_id, page_id=session.get("page_id"))
        return

    # БЛОК ПЕРВИЧНОГО ОПРОСА
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
    if user_id not in user_sessions or not user_sessions[user_id].get("orders"): return
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
