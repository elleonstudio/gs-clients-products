import os, requests, base64, json, io
import pandas as pd
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")
KIMI_TOKEN = os.getenv("KIMI_TOKEN")

NOTION_CACHE_ID = "4547cbb7cbc54138a5ead9f942bd30dc" 
AIRTABLE_TOKEN = "patD95Wp6hbmXnSH7.401bcf4ca42844c15f76c8361ddb7d5b7a4551d58c390de27ba3586fdd7d0cc7"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

NOTION_HEADERS = {"Authorization": f"Bearer {NOTION_TOKEN}", "Content-Type": "application/json", "Notion-Version": "2022-06-28"}

user_sessions = {}
cargo_drafts = {}

# ====================================================================
# NOTION CACHE (ВЕЧНАЯ ПАМЯТЬ)
# ====================================================================
def save_to_notion_cache(data, page_id=None):
    j_str = json.dumps(data, ensure_ascii=False)
    chunks = [j_str[i:i+2000] for i in range(0, len(j_str), 2000)]
    rt_arr = [{"text": {"content": c}} for c in chunks]
    payload = {"properties": {"Order ID": {"title": [{"text": {"content": f"{data.get('client', 'CARGO')} - {datetime.now().strftime('%d.%m %H:%M')}"}}]}, "Data_JSON": {"rich_text": rt_arr}}}
    
    if page_id:
        requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS, json=payload)
        return page_id
    payload["parent"] = {"database_id": NOTION_CACHE_ID}
    return requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=payload).json()["id"].replace("-", "")

def get_from_notion_cache(page_id):
    r = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS).json()
    return json.loads("".join([b["text"]["content"] for b in r["properties"]["Data_JSON"]["rich_text"]]))

# ====================================================================
# NOTION API: КАТАЛОГ И BIG DATA
# ====================================================================
def get_client_catalog(client_name):
    r = requests.post(f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", headers=NOTION_HEADERS, json={"filter": {"property": "Client", "select": {"equals": client_name}}}).json()
    catalog = []
    for item in r.get("results", []):
        p = item["properties"]
        try:
            i_id = p["ID"]["title"][0]["plain_text"]
            n_list = p.get("Name", {}).get("rich_text", [])
            name = n_list[0]["plain_text"] if n_list else "Без названия"
            desc = p.get("Описание", {}).get("rich_text", [])
            desc_text = f" (ВИЗУАЛЬНОЕ ОПИСАНИЕ: {desc[0]['plain_text']})" if desc else ""
            catalog.append(f"ID: {i_id} | Название: {name}{desc_text}")
        except: continue
    return "\n".join(catalog)

def get_item_details(client_name, item_id):
    r = requests.post(f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", headers=NOTION_HEADERS, json={"filter": {"and": [{"property": "Client", "select": {"equals": client_name}}, {"property": "ID", "title": {"equals": item_id}}]}}).json()
    if not r.get("results"): return None
    p = r["results"][0]["properties"]
    try:
        n_list = p.get("Name", {}).get("rich_text", [])
        return {
            "page_id": r["results"][0]["id"],
            "name": n_list[0]["plain_text"] if n_list else "Без названия",
            "client_price": float(p.get("Client Price", {}).get("number") or 0.0),
            "gs_price": float(p.get("GS Price", {}).get("number") or 0.0),
            "pcs_ctn": p.get("Pcs/Ctn", {}).get("number"),
            "gw_kg": p.get("GW kg", {}).get("number"),
            "cm": p.get("cm", {}).get("rich_text", [])[0]["plain_text"] if p.get("cm", {}).get("rich_text", []) else None
        }
    except: return None

def update_item_big_data(page_id, pcs, gw, cm):
    requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS, json={"properties": {"Pcs/Ctn": {"number": pcs}, "GW kg": {"number": gw}, "cm": {"rich_text": [{"text": {"content": cm}}]}}})

# ====================================================================
# ИИ: KIMI VISION И ЛОГИСТ
# ====================================================================
def recognize_photos_batch(orders, catalog_text):
    prompt = (f"Ты эксперт по закупкам. Вот каталог товаров с их ВИЗУАЛЬНЫМ ОПИСАНИЕМ:\n{catalog_text}\n\n"
              f"Я отправляю {len(orders)} фото. Твоя задача: ВНИМАТЕЛЬНО прочитать 'ВИЗУАЛЬНОЕ ОПИСАНИЕ' из каталога и найти точное совпадение на фото. "
              f"Верни СТРОГО JSON массив строк (ID товаров). Пример: [\"1\", \"ERROR\"]. Без лишнего текста.")
    content = [{"type": "text", "text": prompt}]
    for o in orders:
        try:
            b64 = base64.b64encode(requests.get(o["photo_url"]).content).decode('utf-8')
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        except: content.append({"type": "text", "text": "[ОШИБКА]"})
    
    r = requests.post("https://api.moonshot.cn/v1/chat/completions", headers={"Authorization": f"Bearer {KIMI_TOKEN}", "Content-Type": "application/json"}, json={"model": "moonshot-v1-32k-vision-preview", "messages": [{"role": "user", "content": content}], "temperature": 0.0}).json()
    try: return json.loads(r["choices"][0]["message"]["content"].replace("```json", "").replace("```", "").strip())
    except: return ["ERROR"] * len(orders)

def parse_logistics_with_kimi(text, photo_url):
    prompt = ("Ты логист. Вытащи из сообщения поставщика ТОЛЬКО данные для СТАНДАРТНОЙ ПОЛНОЙ коробки:\n"
              "1. Штук в 1 коробке (pcs_per_ctn)\n2. Вес брутто 1 коробки (gw_kg)\n3. Габариты (length, width, height)\n"
              "Верни СТРОГО JSON: {\"pcs_per_ctn\": 120, \"gw_kg\": 18.5, \"length\": 50, \"width\": 30, \"height\": 40} Без текста.")
    content = [{"type": "text", "text": prompt}]
    if text: content[0]["text"] += f"\n\nСообщение: {text}"
    if photo_url:
        b64 = base64.b64encode(requests.get(photo_url).content).decode('utf-8')
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        
    r = requests.post("https://api.moonshot.cn/v1/chat/completions", headers={"Authorization": f"Bearer {KIMI_TOKEN}", "Content-Type": "application/json"}, json={"model": "moonshot-v1-32k-vision-preview", "messages": [{"role": "user", "content": content}], "temperature": 0.0}).json()
    try: return json.loads(r["choices"][0]["message"]["content"].replace("```json", "").replace("```", "").strip())
    except: return None

# ====================================================================
# МОДУЛЬ 1: ЗАКУПКА
# ====================================================================
async def ask_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    s = user_sessions[uid]
    idx = s.get("current_item_index", 0)
    if idx >= len(s["items"]): return await generate_final_invoice(update, context, uid)
    
    item = s["items"][idx]
    if item["qty"] == "1" and not item.get("qty_confirmed"):
        s["state"] = "ASKING_QTY"
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❓ Товар: **{item['name']}**\nКоличество: 1. Напиши правильную цифру:")
    if item.get("shipping") is None:
        s["state"] = "ASKING_SHIPPING"
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🚚 Цена доставки для: **{item['name']}** ({item['qty']} шт)?")
    
    s["current_item_index"] += 1
    await ask_next_question(update, context, uid)

async def generate_final_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, page_id=None):
    s = user_sessions[uid]
    c_rate, r_rate = 58.0, 55.0
    subtotal = purchase = shipping_total = 0
    inv_lines = ""
    
    for i in s["items"]:
        q, p, sh = int(i['qty']), float(i['client_price']), float(i['shipping'])
        lt = (p * q) + sh
        subtotal += lt
        purchase += (float(i['gs_price']) * q)
        shipping_total += sh
        i['line_total'] = lt
        inv_lines += f"• {i['name']}: — {q} шт\n{q} × {p} + {sh} = {lt:.1f}¥\n\n"

    c_cny = 10000 / c_rate if (subtotal * 0.03 * c_rate < 10000) else subtotal * 0.03
    c_amd = 10000 if (subtotal * 0.03 * c_rate < 10000) else int(subtotal * 0.03 * c_rate)
    tot_amd = int((subtotal * c_rate) + c_amd)

    s.update({"subtotal_cny": subtotal, "actual_comm_cny": c_cny, "actual_comm_amd": c_amd, "final_total_amd": tot_amd, "purchase_cny": purchase, "total_delivery_cny": shipping_total, "profit_amd": tot_amd - int((purchase + shipping_total) * r_rate), "client_rate": c_rate, "real_rate": r_rate})
    new_pid = save_to_notion_cache(s, page_id=page_id)

    msg = f"<b>COMMERCIAL INVOICE: {s['client'].upper()}</b>\n📅 {datetime.now().strftime('%m.%d.%Y')}\n\n<b>1. ТОВАРНАЯ ВЕДОМОСТЬ:</b>\n{inv_lines}<code>────────────────────────</code>\n<b>SUBTOTAL:</b> {subtotal:.1f}¥\n\n<b>2. КОМИССИЯ:</b> {c_cny:.1f}¥\n<b>3. ИТОГОВЫЙ РАСЧЕТ:</b>\n• Итого юаней: {subtotal + c_cny:.1f}¥\n• Курс: {c_rate}\n✅ <b>К ОПЛАТЕ: {tot_amd:,} AMD</b>"
    kb = [[InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_{new_pid}"), InlineKeyboardButton("📊 Excel", callback_data=f"excel_{new_pid}")],
          [InlineKeyboardButton("📑 В Airtable", callback_data=f"airtable_{new_pid}")], [InlineKeyboardButton("🧮 В Карго", callback_data=f"tocargo_{new_pid}")]]
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    del user_sessions[uid]

# ====================================================================
# МОДУЛЬ 2: КАРГО ЛОГИСТИКА
# ====================================================================
async def process_cargo_items(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    d = cargo_drafts[str(uid)]
    idx = d.get("current_item_index", 0)
    
    while idx < len(d["items"]) and ("boxes" in d["items"][idx] and not d["items"][idx].get("waiting_data")): idx += 1
    d["current_item_index"] = idx
    if idx >= len(d["items"]): return await finish_cargo_dims(update, context, uid)
    
    item = d["items"][idx]
    if "pack_type" not in item:
        d["state"] = "CARGO_WAIT_PACK"
        kb = [[InlineKeyboardButton("📦 Мешок/Сборная (+0)", callback_data="pk_sack")], [InlineKeyboardButton("📐 Уголки (+1 кг)", callback_data="pk_corners")], [InlineKeyboardButton("🪵 Обрешетка (+10кг, +5см)", callback_data="pk_crate")]]
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📦 Товар: <b>{item['name']} ({item['qty']} шт)</b>\nУпаковка?", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

    if "boxes" not in item or item.get("waiting_data"):
        d["state"] = "CARGO_WAIT_DIMS"
        text = f"📐 Габариты: <b>{item['name']} (Нужно {item['qty']} шт)</b>\n"
        kb = []
        if item.get("pcs_ctn") and item.get("gw_kg") and item.get("cm"):
            f_box = int(item['qty']) // item['pcs_ctn']
            rem = int(item['qty']) % item['pcs_ctn']
            text += f"\n⚡️ В базе: {item['pcs_ctn']} шт/кор | {item['gw_kg']} кг | {item['cm']}\n👉 Это {f_box} полных кор. (Остаток: {rem} шт)\n"
            kb.append([InlineKeyboardButton("⚡️ Использовать базу", callback_data="cg_use_db")])
        
        text += f"\n🇨🇳 Скопируй китайцу:\n`你好！我需要 {item['name']} {item['qty']}个。请问一整箱装多少个？一整箱的毛重是多少公斤？外箱尺寸是多少（长x宽x高）？`\n\n⏳ Перешли ответ сюда (текст/фото) или введи (Шт Вес Д Ш В).\n⏩ Пропустить: /next"
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def finish_cargo_dims(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    d = cargo_drafts[str(uid)]
    waiting = [i['name'] for i in d["items"] if i.get("waiting_data")]
    if waiting:
        d["state"] = "CARGO_DRAFT_WAITING"
        if "page_id" in d: save_to_notion_cache(d, page_id=d.get("page_id"))
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=f"💾 <b>ЧЕРНОВИК СОХРАНЕН</b>\nЖдем данные для: {', '.join(waiting)}\nВернуться: /cargo", parse_mode='HTML')

    if d.get("is_independent"):
        kb = [[InlineKeyboardButton("➕ Добавить товар", callback_data="cg_indep_add")], [InlineKeyboardButton("🧮 Итог", callback_data="cg_indep_calc")]]
        return await context.bot.send_message(chat_id=update.effective_chat.id, text="✅ Добавлено! Дальше?", reply_markup=InlineKeyboardMarkup(kb))

    t_w = t_v = t_p = 0
    for i in d["items"]:
        for b in i.get("boxes", []):
            q, w, l, wd, h = b["qty"], b["w"], b["l"], b["w_dim"], b["h"]
            if i.get("pack_type") in ["crate", "pk_crate"]: w += 10; l += 5; wd += 5; h += 5
            elif i.get("pack_type") in ["corners", "pk_corners"]: w += 1
            t_p += q; t_w += (w * q); t_v += ((l * wd * h) / 1000000) * q
            
    d.update({"t_weight": t_w, "t_vol": t_v, "t_pieces": t_p, "density": int(t_w/t_v) if t_v > 0 else 0})
    if "page_id" in d: save_to_notion_cache(d, page_id=d.get("page_id"))
    d["state"] = "CARGO_WAIT_TARIFF_CG"
    msg = f"📊 <b>СВОДКА:</b>\nВес: {t_w:.1f} кг | Объем: {t_v:.2f} м³ | Мест: {t_p} | Плотность: <b>{d['density']}</b>\n👉 Тариф Карго ($/кг):"
    
    if update.callback_query: await update.callback_query.message.reply_text(msg, parse_mode='HTML')
    else: await context.bot.send_message(chat_id=update.effective_chat.id, text=msg, parse_mode='HTML')

# ====================================================================
# ТЕЛЕГРАМ ОБРАБОТЧИКИ СООБЩЕНИЙ
# ====================================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()
    if text.lower() == "/next":
        if str(uid) in cargo_drafts and cargo_drafts[str(uid)].get("state") == "CARGO_WAIT_DIMS":
            cargo_drafts[str(uid)]["items"][cargo_drafts[str(uid)]["current_item_index"]]["waiting_data"] = True
            cargo_drafts[str(uid)]["current_item_index"] += 1
            await update.message.reply_text("⏭ Отложено в черновик.")
            return await process_cargo_items(update, context, uid)

    if str(uid) in cargo_drafts:
        d = cargo_drafts[str(uid)]
        st = d.get("state")
        
        if st == "CG_NEW_CLIENT":
            d["client"] = text; d["state"] = "CG_NEW_ITEM"
            return await update.message.reply_text("📦 Название и кол-во (Конусы 350):")
            
        elif st == "CG_NEW_ITEM":
            pts = text.split()
            qty, name = ("1", text) if not pts[-1].isdigit() else (pts[-1], " ".join(pts[:-1]))
            d["items"].append({"name": name, "qty": qty})
            return await process_cargo_items(update, context, uid)

        elif st == "CARGO_WAIT_DIMS":
            is_m, boxes = True, []
            for line in text.split('\n'):
                if not line.strip(): continue
                try:
                    p = list(map(float, line.replace(',', '.').split()))
                    if len(p) == 5: boxes.append({"qty": int(p[0]), "w": p[1], "l": p[2], "w_dim": p[3], "h": p[4]})
                    else: is_m = False
                except: is_m = False
            
        if is_m and boxes:
                d["items"][d["current_item_index"]]["boxes"] = boxes
                d["items"][d["current_item_index"]]["waiting_data"] = False
                d["current_item_index"] += 1
                return await process_cargo_items(update, context, uid)
            
        await update.message.reply_text("⏳ Читаю текст ИИ-Логистом...")
        res = parse_logistics_with_kimi(text, None)
        return await process_kimi_logistics_result(update, context, uid, res)
            
        elif st == "CARGO_WAIT_TARIFF_CG":
            try:
                d["tariff_cg"] = float(text.replace(',', '.'))
                d["state"] = "CARGO_WAIT_TARIFF_CL"
                await update.message.reply_text("👉 Тариф Клиенту ($/кг):")
            except:
                await update.message.reply_text("❌ Введи число.")
            return
            
        elif st == "CARGO_WAIT_TARIFF_CL":
            try:
                d["tariff_cl"] = float(text.replace(',', '.'))
                d["state"] = "CARGO_WAIT_RATE_AMD"
                return await update.message.reply_text("👉 Напиши курс USD -> AMD (например, 395):")
            except: return await update.message.reply_text("❌ Введи число.")
            
        elif st == "CARGO_WAIT_RATE_AMD":
            try:
                d["rate_usd_amd"] = float(text.replace(',', '.'))
                
                # Математика для чека
                t_w = d['t_weight']
                t_v = d['t_vol']
                t_p = d['t_pieces']
                t_cl = d['tariff_cl']
                t_cg = d['tariff_cg']
                r_amd = d['rate_usd_amd']
                r_cny = 7.3 # Фиксированный курс юаня
                
                delivery_cl = t_w * t_cl
                pack_cost = 0.0 # Стоимость упаковки (пока 0, так как мы считали только вес)
                
                client_total_usd = delivery_cl + pack_cost
                cargo_total_usd = (t_w * t_cg) + pack_cost
                
                cargo_total_cny = int(cargo_total_usd * r_cny)
                tot_amd = int(client_total_usd * r_amd)
                net_profit = int((client_total_usd - cargo_total_usd) * r_amd)
                
                d.update({
                    "tot_amd": tot_amd, 
                    "cg_cny": cargo_total_cny, 
                    "net_profit": net_profit
                })
                pid = save_to_notion_cache(d, page_id=d.get("page_id"))
                
                # СООБЩЕНИЕ 1 (КЛИЕНТ)
                msg_client = f"""🚛 <b>CARGO INVOICE: {d.get('client', 'CLIENT').upper()}</b>

<b>ПАРАМЕТРЫ ГРУЗА:</b>
• Вес брутто: {t_w:.1f} кг
• Объем: {t_v:.2f} м³
• Мест: {t_p} шт

<b>РАСЧЕТ СТОИМОСТИ:</b>
• Доставка ({t_w:.1f} кг × ${t_cl}): ${delivery_cl:.1f}
• Упаковка и выгрузка: ${pack_cost:.1f}

💵 Итого логистика: ${client_total_usd:.1f}
🔄 Конвертация: ${client_total_usd:.1f} × {r_cny} ¥ × {r_amd} AMD
✅ <b>К ОПЛАТЕ: {tot_amd:,} AMD</b>"""

                # СООБЩЕНИЕ 2 (АДМИН)
                msg_admin = f"""💼 <b>ВНУТРЕННИЙ РАСЧЕТ (CARGO-{str(uid)[-4:]}):</b>

<b>1. ОТДАЕМ В КАРГО:</b>
• Себестоимость (${t_cg}/кг + Услуги): ${cargo_total_usd:.1f}
🇨🇳 Перевести Карго: {cargo_total_cny:,} ¥ (по курсу {r_cny})

<b>2. ПРИБЫЛЬ:</b>
💰 <b>ЧИСТАЯ ПРИБЫЛЬ: {net_profit:,} AMD</b>"""

                kb = [
                    [InlineKeyboardButton("📊 Excel Карго", callback_data=f"cgexcel_{pid}")],
                    [InlineKeyboardButton("📑 В Airtable", callback_data=f"cargodb_{pid}")]
                ]
                
                await update.message.reply_text(msg_client, parse_mode='HTML')
                await update.message.reply_text(msg_admin, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
                
                del cargo_drafts[str(uid)]
            except Exception as e: 
                await update.message.reply_text(f"❌ Ошибка расчета: {e}")
            return
    if uid in user_sessions and user_sessions[uid]["state"] != "COLLECTING":
        s = user_sessions[uid]
        idx = s.get("current_item_index", 0)
        if s["state"] == "ASKING_QTY": s["items"][idx]["qty"] = text; s["items"][idx]["qty_confirmed"] = True
        elif s["state"] == "ASKING_SHIPPING": s["items"][idx]["shipping"] = text; s["current_item_index"] += 1
        await ask_next_question(update, context, uid)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    url = (await context.bot.get_file(update.message.photo[-1].file_id)).file_path
    
    if str(uid) in cargo_drafts and cargo_drafts[str(uid)].get("state") == "CARGO_WAIT_DIMS":
        await update.message.reply_text("⏳ Читаю скриншот ИИ-Логистом...")
        res = parse_logistics_with_kimi(update.message.caption, url)
        return await process_kimi_logistics_result(update, context, uid, res)

    if uid in user_sessions and user_sessions[uid].get("state") == "COLLECTING":
        user_sessions[uid]["orders"].append({"photo_url": url, "qty": update.message.caption or "1"})

async def process_kimi_logistics_result(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, res: dict):
    d = cargo_drafts[str(uid)]
    item = d["items"][d["current_item_index"]]
    if not res: return await update.message.reply_text("❌ ИИ не понял. Введи вручную (Коробок Вес Д Ш В):")
    
    # Питоновская математика (надежная)
    pcs = int(res.get('pcs_per_ctn', 1))
    if pcs == 0: pcs = 1
    t_qty = int(item['qty'])
    res['full_cartons'] = t_qty // pcs
    res['remainder'] = t_qty % pcs

    msg = f"🧠 **Kimi:** В коробке {pcs} шт | Вес {res['gw_kg']} кг | {res['length']}x{res['width']}x{res['height']}\n"
    if item.get("gw_kg"):
        diff = ((item["gw_kg"] - res["gw_kg"]) / item["gw_kg"]) * 100
        if abs(diff) > 10: msg += f"\n⚠️ **ВНИМАНИЕ:** База={item['gw_kg']} кг. Расхождение {diff:.1f}%!\n"
    
    msg += f"\n🧮 **РАСЧЕТ:**\n✅ Полных: {res['full_cartons']} шт.\n⚠️ Остаток: {res['remainder']} шт."
    d["temp_kimi"] = res
    kb = [[InlineKeyboardButton("✅ Сохранить", callback_data="cg_accept_kimi")], [InlineKeyboardButton("❌ Ошибка ИИ", callback_data="cg_reject_kimi")]]
    await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(kb))

# ====================================================================
# CALLBACK HANDLER
# ====================================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data
    
    if data == "cg_resume_draft":
        d = cargo_drafts[str(uid)]
        for i in d["items"]:
            if i.get("waiting_data"): i.pop("waiting_data", None); i.pop("boxes", None)
        idx = 0
        while idx < len(d["items"]) and "boxes" in d["items"][idx]: idx += 1
        d["current_item_index"] = idx
        await q.message.edit_text(f"📂 Возврат к черновику {d.get('client', '')}...")
        await process_cargo_items(update, context, uid)

    elif data == "cg_indep_new":
        cargo_drafts[str(uid)] = {"items": [], "current_item_index": 0, "is_independent": True, "state": "CG_NEW_CLIENT"}
        await q.message.edit_text("👤 Имя клиента (Aram_YVN):")

    elif data == "cg_indep_add":
        cargo_drafts[str(uid)]["state"] = "CG_NEW_ITEM"
        await q.message.edit_text("📦 Товар и кол-во (Конусы 350):")

    elif data == "cg_indep_calc":
        await finish_cargo_dims(update, context, uid)

    elif data.startswith("airtable_"):
        pid = data.split("_")[1]
        try: d = get_from_notion_cache(pid)
        except: return await q.message.reply_text("❌ Чек удален.")
        inv = f"COMMERCIAL INVOICE: {d['client']}\n" + "".join([f"• {i['name']}: {i['qty']} шт\n" for i in d['items']]) + f"\nИТОГО: {d['final_total_amd']} AMD"
        resp = requests.post(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Закупка", headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}, json={"records": [{"fields": {"Клиент": d["client"], "Сумма (¥)": d["subtotal_cny"], "Курс Клиент": d["client_rate"], "Курс Реал": d["real_rate"], "Реал Цена Закупки (¥)": d["purchase_cny"], "Заказ": inv}}], "typecast": True})
        await q.message.reply_text("✅ В Airtable!" if resp.status_code in [200, 201] else f"❌ Ошибка Airtable: {resp.text}")

    elif data.startswith("tocargo_"):
        pid = data.split("_")[1]
        try: d = get_from_notion_cache(pid)
        except: return await q.message.reply_text("❌ Чек удален.")
        cargo_drafts[str(uid)] = {"client": d["client"], "page_id": pid, "items": d["items"], "current_item_index": 0}
        await q.message.reply_text(f"🚀 Карго для {d['client']}")
        await process_cargo_items(update, context, uid)

    elif data.startswith("pk_"):
        cargo_drafts[str(uid)]["items"][cargo_drafts[str(uid)]["current_item_index"]]["pack_type"] = data.split("_")[1]
        await process_cargo_items(update, context, uid)
        
    elif data == "cg_use_db":
        d = cargo_drafts[str(uid)]
        i = d["items"][d["current_item_index"]]
        f_box, rem = int(i['qty']) // i['pcs_ctn'], int(i['qty']) % i['pcs_ctn']
        dims = list(map(float, i["cm"].lower().replace("x", "х").split("х")))
        i["boxes"] = [{"qty": f_box, "w": i["gw_kg"], "l": dims[0], "w_dim": dims[1], "h": dims[2]}]
        i["waiting_data"] = False
        if rem > 0:
            d["state"] = "CARGO_WAIT_DIMS"
            await q.message.reply_text(f"✅ База: {f_box} кор.\n⚠️ Остаток: {rem} шт! Введи габариты или жми /next:")
        else:
            d["current_item_index"] += 1
            await process_cargo_items(update, context, uid)
            
    elif data == "cg_accept_kimi":
        d = cargo_drafts[str(uid)]
        i = d["items"][d["current_item_index"]]
        r = d["temp_kimi"]
        if i.get("page_id"): update_item_big_data(i["page_id"], r["pcs_per_ctn"], r["gw_kg"], f"{r['length']}x{r['width']}x{r['height']}")
        if "boxes" not in i: i["boxes"] = []
        if r["full_cartons"] > 0: i["boxes"].append({"qty": r["full_cartons"], "w": r["gw_kg"], "l": r["length"], "w_dim": r["width"], "h": r["height"]})
        i["waiting_data"] = False
        if r["remainder"] > 0:
            d["state"] = "CARGO_WAIT_DIMS"
            await q.message.reply_text(f"✅ Сохранено!\n⚠️ Остаток: {r['remainder']} шт. Введи габариты ИЛИ нажми /next:")
        else:
            d["current_item_index"] += 1
            await process_cargo_items(update, context, uid)
            
    elif data == "cg_reject_kimi":
        cargo_drafts[str(uid)]["state"] = "CARGO_WAIT_DIMS"
        await q.message.reply_text("❌ Окей, введи (Штук Вес Д Ш В):")
            
    elif data.startswith("cargodb_"):
        pid = data.split("_")[1]
        d = get_from_notion_cache(pid)
        payload = {"records": [{"fields": {
            "Party_ID": f"CG-{str(uid)[-4:]}-{datetime.now().strftime('%M%S')}", "Total_Weight_KG": float(d["t_weight"]), "Total_Volume_CBM": float(d["t_vol"]),
            "Density": int(d["density"]), "Tariff_Cargo_USD": float(d["tariff_cg"]), "Tariff_Client_USD": float(d["tariff_cl"]),
            "Total_Client_AMD": d["tot_amd"], "Total_Cargo_CNY": d["cg_cny"], "Net_Profit_AMD": d["net_profit"]
        }}], "typecast": True}
        resp = requests.post(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Логистика Карго", headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}, json=payload)
        await q.message.reply_text("✅ В Airtable!" if resp.status_code in [200, 201] else f"❌ Ошибка Airtable: {resp.text}")

    elif data.startswith("cgexcel_"):
        pid = data.split("_")[1]
        try: d = get_from_notion_cache(pid)
        except: return await q.message.reply_text("❌ Чек удален.")
        items_data = []
        for i, item in enumerate(d['items'], 1):
            pack_str = "Обрешетка" if item.get('pack_type') in ['crate', 'pk_crate'] else ("Уголки" if item.get('pack_type') in ['corners', 'pk_corners'] else "Мешок/Сборная")
            items_data.append({"№": i, "Название": item['name'], "Кол-во (шт)": item['qty'], "Упаковка": pack_str})
        items_data.extend([
            {"№": "", "Название": "", "Кол-во (шт)": "", "Упаковка": ""},
            {"№": "", "Название": "ИТОГОВЫЙ ВЕС", "Кол-во (шт)": f"{d['t_weight']:.1f} кг", "Упаковка": ""},
            {"№": "", "Название": "ИТОГОВЫЙ ОБЪЕМ", "Кол-во (шт)": f"{d['t_vol']:.2f} м³", "Упаковка": ""},
            {"№": "", "Название": "ИТОГО К ОПЛАТЕ", "Кол-во (шт)": f"{d['tot_amd']:,} AMD", "Упаковка": ""}
        ])
        df = pd.DataFrame(items_data)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Cargo Invoice')
        output.seek(0)
        await context.bot.send_document(chat_id=q.message.chat_id, document=InputFile(output, filename=f"Cargo_{d.get('client', 'Order')}.xlsx"))

# ====================================================================
# БАЗОВЫЕ КОМАНДЫ
# ====================================================================
async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(update.message.text.split()) < 2: return await update.message.reply_text("❌ /client Имя")
    user_sessions[update.message.from_user.id] = {"client": update.message.text.split()[1], "orders": [], "items": [], "current_item_index": 0, "state": "COLLECTING"}
    await update.message.reply_text("✅ Жду фото.")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in user_sessions or not user_sessions[uid].get("orders"): return
    s = user_sessions[uid]
    msg = await update.message.reply_text("⏳ Читаю фото...")
    
    r_ids = recognize_photos_batch(s["orders"], get_client_catalog(s["client"]))
    for o, rid in zip(s["orders"], r_ids):
        if "ERROR" in rid: continue
        det = get_item_details(s["client"], rid)
        if det: s["items"].append({"name": det['name'], "client_price": det['client_price'], "gs_price": det['gs_price'], "qty": o["qty"], "shipping": None, "page_id": det["page_id"], "pcs_ctn": det["pcs_ctn"], "gw_kg": det["gw_kg"], "cm": det["cm"]})

    if not s["items"]: return await msg.edit_text("❌ ОШИБКА: Kimi не понял фото. Проверь клиента.")
    await msg.edit_text("✅ Уточняем детали...")
    await ask_next_question(update, context, uid)

async def cargo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    kb = [[InlineKeyboardButton("➕ Новый Карго-расчет", callback_data="cg_indep_new")]]
    if uid in cargo_drafts:
        d = cargo_drafts[uid]
        waiting = [i['name'] for i in d.get("items", []) if i.get("waiting_data") or "boxes" not in i]
        if waiting: kb.insert(0, [InlineKeyboardButton(f"📂 Черновик: {d.get('client', 'Без имени')} (Ждет: {', '.join(waiting)})", callback_data="cg_resume_draft")])
    await update.message.reply_text("📦 Меню Карго:", reply_markup=InlineKeyboardMarkup(kb))

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in user_sessions: del user_sessions[uid]
    if str(uid) in cargo_drafts: del cargo_drafts[str(uid)]
    await update.message.reply_text("❌ Отменено.")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("client", client_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("cargo", cargo_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback_handler))
    print("Бот запущен!")
    app.run_polling()
