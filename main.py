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
NOTION_CACHE_ID = "4547cbb7cbc54138a5ead9f942bd30dc" 
AIRTABLE_TOKEN = "patD95Wp6hbmXnSH7.401bcf4ca42844c15f76c8361ddb7d5b7a4551d58c390de27ba3586fdd7d0cc7"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28"
}

user_sessions = {}
cargo_drafts = {}

# ====================================================================
# NOTION CACHE (ВЕЧНАЯ ПАМЯТЬ)
# ====================================================================
def save_to_notion_cache(data, page_id=None):
    json_str = json.dumps(data, ensure_ascii=False)
    chunks = [json_str[i:i+2000] for i in range(0, len(json_str), 2000)]
    rich_text_array = [{"text": {"content": chunk}} for chunk in chunks]
    payload = {
        "properties": {
            "Order ID": {"title": [{"text": {"content": f"{data.get('client', 'CARGO')} - {datetime.now().strftime('%d.%m.%Y %H:%M')}"}}]},
            "Data_JSON": {"rich_text": rich_text_array}
        }
    }
    if page_id:
        requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS, json=payload)
        return page_id
    else:
        payload["parent"] = {"database_id": NOTION_CACHE_ID}
        return requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=payload).json()["id"].replace("-", "")

def get_from_notion_cache(page_id):
    resp = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS).json()
    return json.loads("".join([b["text"]["content"] for b in resp["properties"]["Data_JSON"]["rich_text"]]))


# ====================================================================
# NOTION API: КАТАЛОГ И BIG DATA
# ====================================================================
def get_client_catalog(client_name):
    payload = {"filter": {"property": "Client", "select": {"equals": client_name}}}
    resp = requests.post(f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", headers=NOTION_HEADERS, json=payload).json()
    catalog = []
    for item in resp.get("results", []):
        p = item["properties"]
        try:
            i_id = p["ID"]["title"][0]["plain_text"]
            name = p.get("Name", {}).get("rich_text", [])[0]["plain_text"]
            desc = p.get("Описание", {}).get("rich_text", [])
            desc_text = f" (Вид: {desc[0]['plain_text']})" if desc else ""
            catalog.append(f"ID: {i_id} | {name}{desc_text}")
        except: continue
    return "\n".join(catalog)

def get_item_details(client_name, item_id):
    payload = {"filter": {"and": [{"property": "Client", "select": {"equals": client_name}}, {"property": "ID", "title": {"equals": item_id}}]}}
    resp = requests.post(f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", headers=NOTION_HEADERS, json=payload).json()
    if not resp.get("results"): return None
    page = resp["results"][0]
    p = page["properties"]
    try:
        return {
            "page_id": page["id"],
            "name": p.get("Name", {}).get("rich_text", [])[0]["plain_text"],
            "client_price": float(p.get("Client Price", {}).get("number") or 0.0),
            "gs_price": float(p.get("GS Price", {}).get("number") or 0.0),
            "pcs_ctn": p.get("Pcs/Ctn", {}).get("number"),
            "gw_kg": p.get("GW kg", {}).get("number"),
            "cm": p.get("cm", {}).get("rich_text", [])[0]["plain_text"] if p.get("cm", {}).get("rich_text", []) else None
        }
    except: return None

def update_item_big_data(page_id, pcs, gw, cm):
    """Сохраняет новые габариты в каталог Notion (Big Data)"""
    payload = {"properties": {"Pcs/Ctn": {"number": pcs}, "GW kg": {"number": gw}, "cm": {"rich_text": [{"text": {"content": cm}}]}}}
    requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS, json=payload)


# ====================================================================
# ИИ: KIMI VISION И ЛОГИСТ
# ====================================================================
def recognize_photos_batch(orders, catalog_text):
    prompt = f"Каталог:\n{catalog_text}\n\nЯ отправляю {len(orders)} фото. Сопоставь с ID. Верни JSON массив строк. Пример: [\"1\", \"ERROR\"]."
    content = [{"type": "text", "text": prompt}]
    for o in orders:
        try:
            b64 = base64.b64encode(requests.get(o["photo_url"]).content).decode('utf-8')
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        except: content.append({"type": "text", "text": "[ОШИБКА]"})
    
    resp = requests.post("https://api.moonshot.cn/v1/chat/completions", headers={"Authorization": f"Bearer {KIMI_TOKEN}", "Content-Type": "application/json"}, json={"model": "moonshot-v1-32k-vision-preview", "messages": [{"role": "user", "content": content}], "temperature": 0.0}).json()
    try: return json.loads(resp["choices"][0]["message"]["content"].replace("```json", "").replace("```", "").strip())
    except: return ["ERROR"] * len(orders)

def parse_logistics_with_kimi(text, photo_url, qty):
    """Умный ИИ-Логист вытаскивает габариты из ответа поставщика"""
    prompt = (
        f"Ты Senior Logistics Analyst. У нас заказ на {qty} шт товара.\n"
        "Вытащи из сообщения поставщика: штук в 1 коробке (pcs_per_ctn), вес брутто 1 коробки (gw_kg) и габариты в см (length, width, height).\n"
        "Верни СТРОГО JSON: {\"pcs_per_ctn\": 0, \"gw_kg\": 0.0, \"length\": 0, \"width\": 0, \"height\": 0} Без текста."
    )
    content = [{"type": "text", "text": prompt}]
    if text: content[0]["text"] += f"\n\nСообщение: {text}"
    if photo_url:
        b64 = base64.b64encode(requests.get(photo_url).content).decode('utf-8')
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        
    resp = requests.post("https://api.moonshot.cn/v1/chat/completions", headers={"Authorization": f"Bearer {KIMI_TOKEN}", "Content-Type": "application/json"}, json={"model": "moonshot-v1-32k-vision-preview", "messages": [{"role": "user", "content": content}], "temperature": 0.0}).json()
    try: return json.loads(resp["choices"][0]["message"]["content"].replace("```json", "").replace("```", "").strip())
    except: return None


# ====================================================================
# МОДУЛЬ 1: ЗАКУПКА (ПЕРВИЧНЫЙ ОПРОС)
# ====================================================================
async def ask_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    session = user_sessions[user_id]
    items = session["items"]
    idx = session.get("current_item_index", 0)

    if idx >= len(items): return await generate_final_invoice(update, context, user_id)
    item = items[idx]

    if item["qty"] == "1" and not item.get("qty_confirmed"):
        session["state"] = "ASKING_QTY"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❓ Товар: **{item['name']}**\nКоличество: 1. Напиши правильную цифру:")
        return

    if item.get("shipping") is None:
        session["state"] = "ASKING_SHIPPING"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🚚 Цена доставки для: **{item['name']}** ({item['qty']} шт)?")
        return

    session["current_item_index"] += 1
    await ask_next_question(update, context, user_id)

async def generate_final_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, page_id=None):
    session = user_sessions[user_id]
    c_rate, r_rate = 58.0, 55.0
    subtotal = purchase = shipping_total = 0
    inv_lines = ""
    
    for i in session["items"]:
        q, p, s = int(i['qty']), float(i['client_price']), float(i['shipping'])
        lt = (p * q) + s
        subtotal += lt
        purchase += (float(i['gs_price']) * q)
        shipping_total += s
        i['line_total'] = lt
        inv_lines += f"• {i['name']}: — {q} шт\n{q} × {p} + {s} = {lt:.1f}¥\n\n"

    c_cny = 10000 / c_rate if (subtotal * 0.03 * c_rate < 10000) else subtotal * 0.03
    c_amd = 10000 if (subtotal * 0.03 * c_rate < 10000) else int(subtotal * 0.03 * c_rate)
    tot_amd = int((subtotal * c_rate) + c_amd)

    session.update({"subtotal_cny": subtotal, "actual_comm_cny": c_cny, "actual_comm_amd": c_amd, "final_total_amd": tot_amd, "purchase_cny": purchase, "total_delivery_cny": shipping_total, "profit_amd": tot_amd - int((purchase + shipping_total) * r_rate), "client_rate": c_rate, "real_rate": r_rate})
    new_pid = save_to_notion_cache(session, page_id=page_id)

    msg_client = f"<b>COMMERCIAL INVOICE: {session['client'].upper()}</b>\n📅 {datetime.now().strftime('%m.%d.%Y')}\n\n<b>1. ТОВАРНАЯ ВЕДОМОСТЬ:</b>\n{inv_lines}<code>────────────────────────</code>\n<b>SUBTOTAL:</b> {subtotal:.1f}¥\n\n<b>2. КОМИССИЯ:</b> {c_cny:.1f}¥\n<b>3. ИТОГОВЫЙ РАСЧЕТ:</b>\n• Всего в юанях: {subtotal + c_cny:.1f}¥\n• Курс: {c_rate}\n✅ <b>ИТОГО К ОПЛАТЕ: {tot_amd:,} AMD</b>"
    
    kb = [
        [InlineKeyboardButton("✏️ Изменить товар", callback_data=f"edit_{new_pid}"), InlineKeyboardButton("📊 Excel", callback_data=f"excel_{new_pid}")],
        [InlineKeyboardButton("📑 В Airtable (Закупка)", callback_data=f"airtable_{new_pid}")],
        [InlineKeyboardButton("🧮 Рассчитать Карго", callback_data=f"tocargo_{new_pid}")] # НОВАЯ КНОПКА
    ]
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg_client, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    del user_sessions[user_id]


# ====================================================================
# МОДУЛЬ 2: КАРГО ЛОГИСТИКА
# ====================================================================
async def process_cargo_items(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    draft = cargo_drafts[str(user_id)]
    idx = draft.get("current_item_index", 0)
    
    if idx >= len(draft["items"]): return await finish_cargo_dims(update, context, user_id)
    item = draft["items"][idx]
    
    # 1. Спрашиваем тип упаковки
    if "pack_type" not in item:
        draft["state"] = "CARGO_WAIT_PACK"
        kb = [[InlineKeyboardButton("📦 Мешок/Сборная (+0 кг)", callback_data="pk_sack")], [InlineKeyboardButton("📐 Уголки (+1 кг)", callback_data="pk_corners")], [InlineKeyboardButton("🪵 Обрешетка (+10 кг, +5см)", callback_data="pk_crate")]]
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📦 <b>Товар: {item['name']} ({item['qty']} шт)</b>\nКак будем упаковывать?", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        return

    # 2. Кнопка-молния (Big Data) или ИИ
    if "boxes" not in item:
        draft["state"] = "CARGO_WAIT_DIMS"
        text = f"📐 <b>Габариты для: {item['name']} (Нужно {item['qty']} шт)</b>\n"
        
        # Если есть Big Data в Notion
        if item.get("pcs_ctn") and item.get("gw_kg") and item.get("cm"):
            full_boxes = int(item['qty']) // item['pcs_ctn']
            rem = int(item['qty']) % item['pcs_ctn']
            text += f"\n⚡️ <b>Найдено в базе Notion:</b>\n• В 1 коробке: {item['pcs_ctn']} шт\n• Вес: {item['gw_kg']} кг | Габариты: {item['cm']}\n"
            if full_boxes > 0: text += f"👉 <b>Это {full_boxes} полных коробок.</b> (Остаток: {rem} шт)\n"
            kb = [[InlineKeyboardButton("⚡️ Использовать базу", callback_data="cg_use_db")], [InlineKeyboardButton("🤖 Спросить Kimi (Китайца)", callback_data="cg_ask_kimi")]]
        else:
            text += "\nДанных в базе нет. Отправь габариты вручную (Коробок Вес Д Ш В) ИЛИ спроси ИИ:"
            kb = [[InlineKeyboardButton("🤖 Извлечь ответ китайца (Kimi)", callback_data="cg_ask_kimi")]]
            
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        return

    draft["current_item_index"] += 1
    await process_cargo_items(update, context, user_id)

async def finish_cargo_dims(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    draft = cargo_drafts[str(user_id)]
    t_weight = t_vol = t_pieces = 0
    
    for i in draft["items"]:
        for box in i.get("boxes", []):
            qty, w, l, w_dim, h = box["qty"], box["w"], box["l"], box["w_dim"], box["h"]
            # Математика тары
            if i["pack_type"] == "crate": w += 10; l += 5; w_dim += 5; h += 5
            elif i["pack_type"] == "corners": w += 1
            
            t_pieces += qty
            t_weight += (w * qty)
            t_vol += ((l * w_dim * h) / 1000000) * qty
            
    draft.update({"t_weight": t_weight, "t_vol": t_vol, "t_pieces": t_pieces, "density": int(t_weight/t_vol) if t_vol > 0 else 0})
    save_to_notion_cache(draft, page_id=draft.get("page_id"))
    
    draft["state"] = "CARGO_WAIT_TARIFF_CG"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📊 <b>СВОДКА КАРГО:</b>\n• Вес: {t_weight} кг\n• Объем: {t_vol:.2f} м³\n• Мест: {t_pieces}\n• Плотность: <b>{draft['density']} кг/м³</b>\n\n👉 Напиши Тариф Карго ($/кг):", parse_mode='HTML')

async def generate_cargo_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    d = cargo_drafts[str(user_id)]
    cg_cost = d['t_weight'] * d['tariff_cg']
    cl_cost = d['t_weight'] * d['tariff_cl']
    cny_cost = cg_cost * 7.3 # Упрощенный кросс-курс, можно спросить
    amd_profit = (cl_cost - cg_cost) * 400 # Упрощенный курс, лучше просить
    tot_amd = cl_cost * d.get('rate_usd_amd', 400)
    
    d.update({"tot_amd": int(tot_amd), "cg_cny": int(cny_cost), "net_profit": int(amd_profit)})
    pid = save_to_notion_cache(d, page_id=d.get("page_id"))
    
    msg = f"🚛 <b>CARGO INVOICE: {d['client']}</b>\n\n<b>ПАРАМЕТРЫ:</b>\n• Вес: {d['t_weight']} кг | Объем: {d['t_vol']:.2f} м³\n• Плотность: {d['density']} кг/м³\n\n<b>РАСЧЕТ:</b>\n• Логистика: ${cl_cost:.1f} (по ${d['tariff_cl']})\n\n✅ <b>К ОПЛАТЕ: {int(tot_amd):,} AMD</b>\n\n💰 Прибыль: {int(amd_profit):,} AMD"
    
    kb = [[InlineKeyboardButton("📑 В Airtable (Карго)", callback_data=f"cargodb_{pid}")]]
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    del cargo_drafts[str(user_id)]


# ====================================================================
# ОБРАБОТЧИКИ СООБЩЕНИЙ И КНОПОК
# ====================================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    # Ветка КАРГО
    if str(uid) in cargo_drafts:
        d = cargo_drafts[str(uid)]
        st = d.get("state")
        
        if st == "CARGO_WAIT_DIMS":
            # Ввод вручную: Кол-во Вес Д Ш В
            try:
                parts = list(map(float, text.split()))
                if len(parts) == 5:
                    idx = d["current_item_index"]
                    if "boxes" not in d["items"][idx]: d["items"][idx]["boxes"] = []
                    d["items"][idx]["boxes"].append({"qty": int(parts[0]), "w": parts[1], "l": parts[2], "w_dim": parts[3], "h": parts[4]})
                    await update.message.reply_text("✅ Коробки добавлены! Если есть остаток, введи его габариты так же. Если всё, жми /next")
                else: await update.message.reply_text("❌ Формат: Штук Вес Длина Ширина Высота (через пробел)")
            except: await update.message.reply_text("❌ Только цифры через пробел.")
            return
            
        elif st == "CARGO_WAIT_KIMI_REPLY":
            await update.message.reply_text("⏳ Анализирую текст ИИ-Логистом...")
            idx = d["current_item_index"]
            res = parse_logistics_with_kimi(text, None, d["items"][idx]["qty"])
            await process_kimi_logistics_result(update, context, uid, res)
            return
            
        elif st == "CARGO_WAIT_TARIFF_CG":
            d["tariff_cg"] = float(text.replace(',', '.'))
            d["state"] = "CARGO_WAIT_TARIFF_CL"
            await update.message.reply_text("👉 Напиши Тариф Клиенту ($/кг):")
            return
            
        elif st == "CARGO_WAIT_TARIFF_CL":
            d["tariff_cl"] = float(text.replace(',', '.'))
            await generate_cargo_invoice(update, context, uid)
            return

    # Ветка ЗАКУПКИ
    if uid in user_sessions:
        s = user_sessions[uid]
        if s["state"] == "COLLECTING": return
        idx = s.get("current_item_index", 0)
        
        if s["state"] == "ASKING_QTY": s["items"][idx]["qty"] = text; s["items"][idx]["qty_confirmed"] = True
        elif s["state"] == "ASKING_SHIPPING": s["items"][idx]["shipping"] = text; s["current_item_index"] += 1
        await ask_next_question(update, context, uid)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    url = (await context.bot.get_file(update.message.photo[-1].file_id)).file_path
    
    if str(uid) in cargo_drafts and cargo_drafts[str(uid)].get("state") == "CARGO_WAIT_KIMI_REPLY":
        await update.message.reply_text("⏳ Читаю скриншот ИИ-Логистом...")
        d = cargo_drafts[str(uid)]
        idx = d["current_item_index"]
        res = parse_logistics_with_kimi(update.message.caption, url, d["items"][idx]["qty"])
        await process_kimi_logistics_result(update, context, uid, res)
        return

    if uid in user_sessions and user_sessions[uid].get("state") == "COLLECTING":
        user_sessions[uid]["orders"].append({"photo_url": url, "qty": update.message.caption or "1"})

async def process_kimi_logistics_result(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, res: dict):
    d = cargo_drafts[str(uid)]
    idx = d["current_item_index"]
    item = d["items"][idx]
    
    if not res: return await update.message.reply_text("❌ Kimi не смог понять данные. Введи вручную (Кол Вес Д Ш В):")
    
    msg = f"🧠 **Kimi проанализировал данные:**\nВ коробке: {res['pcs_per_ctn']} шт | Вес: {res['gw_kg']} кг | Габариты: {res['length']}x{res['width']}x{res['height']}\n"
    
    # Фрод-контроль (Big Data)
    if item.get("gw_kg"):
        diff = ((item["gw_kg"] - res["gw_kg"]) / item["gw_kg"]) * 100
        if abs(diff) > 10: msg += f"\n⚠️ **ВНИМАНИЕ:** В базе Notion вес {item['gw_kg']} кг. Расхождение {diff:.1f}%!\n"
    
    msg += f"\n🧮 **РАСЧЕТ:**\n✅ Полных коробок: {res['full_cartons']} шт.\n⚠️ Остаток: {res['remainder']} шт."
    d["temp_kimi"] = res
    
    kb = [[InlineKeyboardButton("✅ Сохранить в Карго и Базу", callback_data="cg_accept_kimi")], [InlineKeyboardButton("❌ Ошибка ИИ, введу руками", callback_data="cg_reject_kimi")]]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

# ====================================================================
# CALLBACK HANDLER (КНОПКИ)
# ====================================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data
    
    # --- КНОПКИ ЗАКУПКИ ---
    if data.startswith("airtable_"):
        pid = data.split("_")[1]
        try: d = get_from_notion_cache(pid)
        except: return await q.message.reply_text("❌ Чек удален из Notion.")
        
        inv_text = f"COMMERCIAL INVOICE: {d['client']}\n" + "".join([f"• {i['name']}: {i['qty']} шт\n" for i in d['items']]) + f"\nИТОГО: {d['final_total_amd']} AMD"
        payload = {"records": [{"fields": {"Клиент": d["client"], "Сумма (¥)": d["subtotal_cny"], "Курс Клиент": d["client_rate"], "Курс Реал": d["real_rate"], "Реал Цена Закупки (¥)": d["purchase_cny"], "Заказ": inv_text}}], "typecast": True}
        
        resp = requests.post(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Закупка", headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}, json=payload)
        await q.message.reply_text("✅ Закупка записана в Airtable!" if resp.status_code in [200, 201] else f"❌ Ошибка Airtable: {resp.text}")

    elif data.startswith("tocargo_"):
        pid = data.split("_")[1]
        try: d = get_from_notion_cache(pid)
        except: return await q.message.reply_text("❌ Чек удален из Notion.")
        
        cargo_drafts[str(uid)] = {"client": d["client"], "page_id": pid, "items": d["items"], "current_item_index": 0}
        await q.message.reply_text(f"🚀 Запускаем Карго для {d['client']} ({len(d['items'])} позиций).")
        await process_cargo_items(update, context, uid)

    # --- КНОПКИ КАРГО ЛОГИСТИКИ ---
    elif data.startswith("pk_"):
        cargo_drafts[str(uid)]["items"][cargo_drafts[str(uid)]["current_item_index"]]["pack_type"] = data.split("_")[1]
        await process_cargo_items(update, context, uid)
        
    elif data == "cg_use_db":
        d = cargo_drafts[str(uid)]
        i = d["items"][d["current_item_index"]]
        full = int(i['qty']) // i['pcs_ctn']
        rem = int(i['qty']) % i['pcs_ctn']
        dims = list(map(float, i["cm"].lower().replace("x", "х").split("х")))
        i["boxes"] = [{"qty": full, "w": i["gw_kg"], "l": dims[0], "w_dim": dims[1], "h": dims[2]}]
        
        if rem > 0:
            d["state"] = "CARGO_WAIT_DIMS"
            await q.message.reply_text(f"✅ {full} коробок добавлено из базы.\n⚠️ Осталось {rem} шт! Введи габариты для остатка (1 Вес Д Ш В):")
        else:
            await q.message.reply_text("✅ Товар полностью рассчитан по базе.")
            d["current_item_index"] += 1
            await process_cargo_items(update, context, uid)
            
    elif data == "cg_ask_kimi":
        cargo_drafts[str(uid)]["state"] = "CARGO_WAIT_KIMI_REPLY"
        await q.message.reply_text("🤖 Перешли мне сообщение от китайца текстом или скриншотом:")
        
    elif data == "cg_accept_kimi":
        d = cargo_drafts[str(uid)]
        i = d["items"][d["current_item_index"]]
        res = d["temp_kimi"]
        
        # Сохраняем Big Data в Notion!
        if i.get("page_id"): update_item_big_data(i["page_id"], res["pcs_per_ctn"], res["gw_kg"], f"{res['length']}x{res['width']}x{res['height']}")
        
        if "boxes" not in i: i["boxes"] = []
        if res["full_cartons"] > 0: i["boxes"].append({"qty": res["full_cartons"], "w": res["gw_kg"], "l": res["length"], "w_dim": res["width"], "h": res["height"]})
        
        if res["remainder"] > 0:
            d["state"] = "CARGO_WAIT_DIMS"
            await q.message.reply_text(f"✅ Полные коробки в Карго и база Notion обновлена!\n⚠️ Осталось {res['remainder']} шт. Введи габариты остатка:")
        else:
            d["current_item_index"] += 1
            await process_cargo_items(update, context, uid)
            
    elif data.startswith("cargodb_"):
        pid = data.split("_")[1]
        d = get_from_notion_cache(pid)
        payload = {"records": [{"fields": {
            "Party_ID": f"CARGO-{str(uid)[-4:]}-{datetime.now().strftime('%M%S')}",
            "Total_Weight_KG": float(d["t_weight"]),
            "Total_Volume_CBM": float(d["t_vol"]),
            "Total_Pieces": int(d["t_pieces"]),
            "Density": int(d["density"]),
            "Packaging_Type": "Сборная",
            "Tariff_Cargo_USD": float(d["tariff_cg"]),
            "Tariff_Client_USD": float(d["tariff_cl"]),
            "Rate_USD_CNY": 7.3, "Rate_USD_AMD": d.get("rate_usd_amd", 400),
            "Total_Client_AMD": d["tot_amd"], "Total_Cargo_CNY": d["cg_cny"], "Net_Profit_AMD": d["net_profit"],
            "Logistics_Status": "Подтвержден"
        }}], "typecast": True}
        
        resp = requests.post(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Логистика Карго", headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}, json=payload)
        await q.message.reply_text("✅ Карго записано в Airtable!" if resp.status_code in [200, 201] else f"❌ Ошибка Airtable: {resp.text}")

# ====================================================================
# БАЗОВЫЕ КОМАНДЫ
# ====================================================================
async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(update.message.text.split()) < 2: return await update.message.reply_text("❌ Формат: /client Имя")
    user_sessions[update.message.from_user.id] = {"client": update.message.text.split()[1], "orders": [], "items": [], "current_item_index": 0, "state": "COLLECTING"}
    await update.message.reply_text("✅ Жду фото.")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in user_sessions or not user_sessions[uid].get("orders"): return
    s = user_sessions[uid]
    msg = await update.message.reply_text("⏳ Распознаю фото...")
    
    r_ids = recognize_photos_batch(s["orders"], get_client_catalog(s["client"]))
    for o, rid in zip(s["orders"], r_ids):
        if "ERROR" in rid: continue
        det = get_item_details(s["client"], rid)
        if det: s["items"].append({"name": det['name'], "client_price": det['client_price'], "gs_price": det['gs_price'], "qty": o["qty"], "shipping": None, "page_id": det["page_id"], "pcs_ctn": det["pcs_ctn"], "gw_kg": det["gw_kg"], "cm": det["cm"]})

    await msg.edit_text("✅ Готово! Уточняем детали...")
    await ask_next_question(update, context, uid)

async def next_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Позволяет пропустить остаток в Карго, если ввели все данные"""
    uid = update.message.from_user.id
    if str(uid) in cargo_drafts and cargo_drafts[str(uid)].get("state") == "CARGO_WAIT_DIMS":
        cargo_drafts[str(uid)]["current_item_index"] += 1
        await process_cargo_items(update, context, uid)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("client", client_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("next", next_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback_handler))
    print("ERP Бот запущен!")
    app.run_polling()
