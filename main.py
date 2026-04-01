import os, requests, base64, json, io, re
import pandas as pd
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ====================================================================
# НАСТРОЙКИ ОКРУЖЕНИЯ И КОНСТАНТЫ
# ====================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
DATABASE_ID = os.getenv("DATABASE_ID")
KIMI_TOKEN = os.getenv("KIMI_TOKEN")

NOTION_CACHE_ID = "4547cbb7cbc54138a5ead9f942bd30dc" 
AIRTABLE_TOKEN = "patD95Wp6hbmXnSH7.401bcf4ca42844c15f76c8361ddb7d5b7a4551d58c390de27ba3586fdd7d0cc7"
AIRTABLE_BASE_ID = "appRIlSL63Kxh6iWX"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}", 
    "Content-Type": "application/json", 
    "Notion-Version": "2022-06-28"
}

# ====================================================================
# ГЛОБАЛЬНЫЕ СЕССИИ (ПАМЯТЬ БОТА)
# ====================================================================
user_sessions = {}
cargo_drafts = {}
ff_sessions = {}

# ====================================================================
# НАСТРОЙКИ СКЛАДА FF
# ====================================================================
BOX_PRICE_CNY = 7.77 # Цена коробки 60х40х40
MAX_BOX_WEIGHT = 30.0 # Лимит веса на коробку
FF_LIMIT_L, FF_LIMIT_W, FF_LIMIT_H = 60, 40, 40 # Мастер-короб

def update_item_ff_data(page_id, unit_cm, unit_kg, barcodes):
    """Сохраняет данные упаковки в базу AllProducts (Notion)"""
    payload = {"properties": {
        "Unit_cm": {"rich_text": [{"text": {"content": unit_cm}}]},
        "Unit_kg": {"number": unit_kg},
        "Barcodes_pcs": {"number": barcodes}
    }}
    requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS, json=payload)

# ====================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И NOTION API
# ====================================================================
def save_to_notion_cache(data, page_id=None):
    j_str = json.dumps(data, ensure_ascii=False)
    chunks = [j_str[i:i+2000] for i in range(0, len(j_str), 2000)]
    rt_arr = [{"text": {"content": c}} for c in chunks]
    payload = {
        "properties": {
            "Order ID": {"title": [{"text": {"content": f"{data.get('client', 'CARGO')} - {datetime.now().strftime('%d.%m %H:%M')}"}}]}, 
            "Data_JSON": {"rich_text": rt_arr}
        }
    }
    if page_id:
        requests.patch(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS, json=payload)
        return page_id
    payload["parent"] = {"database_id": NOTION_CACHE_ID}
    return requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=payload).json()["id"].replace("-", "")

def get_from_notion_cache(page_id):
    r = requests.get(f"https://api.notion.com/v1/pages/{page_id}", headers=NOTION_HEADERS).json()
    return json.loads("".join([b["text"]["content"] for b in r["properties"]["Data_JSON"]["rich_text"]]))

def get_client_catalog(client_name):
    r = requests.post(f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", headers=NOTION_HEADERS, json={"filter": {"property": "Client", "select": {"equals": client_name}}}).json()
    catalog = []
    for item in r.get("results", []):
        p = item["properties"]
        try:
            i_id = p["ID"]["title"][0]["plain_text"]
            name = p.get("Name", {}).get("rich_text", [])[0]["plain_text"] if p.get("Name", {}).get("rich_text", []) else "Без названия"
            desc = p.get("Описание", {}).get("rich_text", [])
            desc_text = f" (ОПИСАНИЕ: {desc[0]['plain_text']})" if desc else ""
            catalog.append(f"ID: {i_id} | {name}{desc_text}")
        except: continue
    return "\n".join(catalog)

def get_item_details(client_name, item_id):
    r = requests.post(f"https://api.notion.com/v1/databases/{DATABASE_ID}/query", headers=NOTION_HEADERS, json={"filter": {"and": [{"property": "Client", "select": {"equals": client_name}}, {"property": "ID", "title": {"equals": item_id}}]}}).json()
    if not r.get("results"): return None
    p = r["results"][0]["properties"]
    try:
        return {
            "page_id": r["results"][0]["id"],
            "name": p.get("Name", {}).get("rich_text", [])[0]["plain_text"] if p.get("Name", {}).get("rich_text", []) else "Без названия",
            "client_price": float(p.get("Client Price", {}).get("number") or 0.0),
            "gs_price": float(p.get("GS Price", {}).get("number") or 0.0),
            "pcs_ctn": p.get("Pcs/Ctn", {}).get("number"),
            "gw_kg": p.get("GW kg", {}).get("number"),
            "cm": p.get("cm", {}).get("rich_text", [])[0]["plain_text"] if p.get("cm", {}).get("rich_text", []) else None,
            "unit_cm": p.get("Unit_cm", {}).get("rich_text", [])[0]["plain_text"] if p.get("Unit_cm", {}).get("rich_text", []) else None,
            "unit_kg": p.get("Unit_kg", {}).get("number")
        }
    except: return None

# ====================================================================
# ИИ ЛОГИКА (KIMI)
# ====================================================================
def recognize_photos_batch(photo_urls, catalog_text):
    prompt = f"Ты эксперт. Каталог:\n{catalog_text}\n\nНайди ID товаров для этих фото. Верни СТРОГО JSON массив строк."
    content = [{"type": "text", "text": prompt}]
    for url in photo_urls:
        b64 = base64.b64encode(requests.get(url).content).decode('utf-8')
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    r = requests.post("https://api.moonshot.cn/v1/chat/completions", headers={"Authorization": f"Bearer {KIMI_TOKEN}", "Content-Type": "application/json"}, json={"model": "moonshot-v1-32k-vision-preview", "messages": [{"role": "user", "content": content}], "temperature": 0.0}).json()
    try: return json.loads(r["choices"][0]["message"]["content"].replace("```json", "").replace("```", "").strip())
    except: return ["ERROR"] * len(photo_urls)

def parse_logistics_with_kimi(text, photo_url):
    prompt = "Ты логист. Вытащи из данных: штук в кор (pcs_per_ctn), вес кор (gw_kg), размеры (length, width, height). Верни СТРОГО JSON."
    content = [{"type": "text", "text": prompt}]
    if text: content[0]["text"] += f"\n\nДанные: {text}"
    if photo_url:
        b64 = base64.b64encode(requests.get(photo_url).content).decode('utf-8')
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    r = requests.post("https://api.moonshot.cn/v1/chat/completions", headers={"Authorization": f"Bearer {KIMI_TOKEN}", "Content-Type": "application/json"}, json={"model": "moonshot-v1-32k-vision-preview", "messages": [{"role": "user", "content": content}], "temperature": 0.0}).json()
    try: return json.loads(r["choices"][0]["message"]["content"].replace("```json", "").replace("```", "").strip())
    except: return None

# ====================================================================
# МОДУЛЬ ФУЛФИЛМЕНТА (FF)
# ====================================================================
def optimize_boxes_ff(units):
    """Алгоритм укладки в мастер-короба"""
    boxes = []
    for u in units:
        for _ in range(u['qty']):
            l, w, h = u['dims']
            vol = l * w * h
            placed = False
            for b in boxes:
                if b['rem_vol'] >= vol and (b['cur_w'] + u['weight']) <= MAX_BOX_WEIGHT:
                    b['rem_vol'] -= vol
                    b['cur_w'] += u['weight']
                    placed = True
                    break
            if not placed:
                boxes.append({'rem_vol': (FF_LIMIT_L*FF_LIMIT_W*FF_LIMIT_H) - vol, 'cur_w': u['weight']})
    return boxes

async def start_ff_process(update: Update, context: ContextTypes.DEFAULT_TYPE, pid: str):
    uid = update.effective_user.id
    data = get_from_notion_cache(pid)
    ff_sessions[uid] = {"client": data["client"], "pid": pid, "items": data["items"], "current_idx": 0, "units": [], "state": "FF_IDLE"}
    await ask_ff_item(update, context, uid)

async def ask_ff_item(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    s = ff_sessions[uid]
    if s["current_idx"] >= len(s["items"]): 
        return await finish_ff(update, context, uid)
    
    item = s["items"][s["current_idx"]]
    kb = []
    if item.get("unit_cm"):
        kb.append([InlineKeyboardButton("🏷 Использовать базу", callback_data="ff_db")])
    kb.append([
        InlineKeyboardButton("✍️ Ввести размеры", callback_data="ff_man"), 
        InlineKeyboardButton("⏩ Пропустить", callback_data="ff_skip")
    ])
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📦 <b>Фулфилмент: {item['name']}</b>\nКак упакуем?", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

async def finish_ff(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    s = ff_sessions[uid]
    boxes = optimize_boxes_ff(s["units"])
    b_cnt = len(boxes) 
    total_u = sum(u['qty'] for u in s["units"]) 
    
    # Экономика
    cost_cl = total_u * 2.0 
    cost_real = (total_u * 1.5) + (b_cnt * BOX_PRICE_CNY)
    
    msg_cl = f"📦 <b>FF: {s['client'].upper()}</b>\n• Товар: {total_u} ед.\n• Коробки: {b_cnt} шт.\n✅ <b>ИТОГО: {cost_cl:.1f} ¥</b>"
    msg_adm = f"💼 <b>FF ВНУТРЕННИЙ:</b>\n• Прибыль: {cost_cl - cost_real:.1f} ¥"
    
    kb = [
        [InlineKeyboardButton("📑 В Airtable (FF)", callback_data=f"ffair_{save_to_notion_cache(s)}")],
        [InlineKeyboardButton("🧾 ОБЪЕДИНИТЬ ЧЕКИ", callback_data=f"super_{s['pid']}")]
    ]
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg_cl, parse_mode='HTML')
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg_adm, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    
    if uid in ff_sessions: del ff_sessions[uid]

# ====================================================================
# МОДУЛЬ КАРГО (ЛОГИСТИКА)
# ====================================================================
async def process_cargo_items(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    d = cargo_drafts[str(uid)]
    idx = d.get("current_item_index", 0)
    while idx < len(d["items"]) and ("boxes" in d["items"][idx] or d["items"][idx].get("pack_type") == "pk_inset"): 
        idx += 1
    d["current_item_index"] = idx
    if idx >= len(d["items"]): 
        return await finish_cargo_dims(update, context, uid)
    
    item = d["items"][idx]
    if "pack_type" not in item:
        d["state"] = "CARGO_WAIT_PACK"
        kb = [
            [InlineKeyboardButton("📦 Мешок (+0)", callback_data="pk_sack"), InlineKeyboardButton("📐 Уголки (+1кг)", callback_data="pk_corners")], 
            [InlineKeyboardButton("🪵 Обрешетка (+10кг)", callback_data="pk_crate"), InlineKeyboardButton("🎁 В наборе", callback_data="pk_inset")]
        ]
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📦 Товар: <b>{item['name']}</b>\nКак упакуем?", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

    d["state"] = "CARGO_WAIT_DIMS"
    text = f"📐 Габариты: <b>{item['name']} ({item['qty']} шт)</b>\n"
    kb = []
    if item.get("cm"): kb.append([InlineKeyboardButton("⚡️ База Notion", callback_data="cg_use_db")])
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text + "Введи данные остатка или перешли ответ китайца:", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def finish_cargo_dims(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    d = cargo_drafts[str(uid)]
    t_w = t_v = t_p = 0
    for i in d["items"]:
        if i.get("pack_type") == "pk_inset": continue
        for b in i.get("boxes", []):
            q = int(b.get("qty") or 0)
            w = float(b.get("w") or 0.0)
            l = float(b.get("l") or 0.0)
            wd = float(b.get("w_dim") or 0.0)
            h = float(b.get("h") or 0.0)
            
            if i.get("pack_type") == "pk_crate": w += 10; l += 5; wd += 5; h += 5
            elif i.get("pack_type") == "pk_corners": w += 1
            
            t_p += q
            t_w += (w * q)
            t_v += ((l * wd * h) / 1000000) * q
            
    density = int(t_w/t_v) if t_v > 0 else 0
    d.update({"t_weight": t_w, "t_vol": t_v, "t_pieces": t_p, "density": density, "state": "CARGO_WAIT_TARIFF_CG"})
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📊 <b>ИТОГ:</b> {t_w:.1f}кг | {t_v:.2f}м³ | {t_p} мест. Плотность: {density}\n👉 Напиши Тариф Карго ($/кг):", parse_mode='HTML')

# ====================================================================
# МОДУЛЬ ЗАКУПКИ
# ====================================================================
async def ask_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    if uid not in user_sessions: return
    s = user_sessions[uid]
    idx = s.get("current_item_index", 0)
    if idx >= len(s["items"]): 
        return await generate_final_invoice(update, context, uid)
    
    item = s["items"][idx]
    if not item.get("qty_confirmed"):
        s["state"] = "ASKING_QTY"
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=f"❓ Товар: **{item['name']}**\nВведи количество:")
    if item.get("shipping") is None:
        s["state"] = "ASKING_SHIPPING"
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=f"🚚 Цена доставки для: **{item['name']}** ({item['qty']} шт)?")
    
    s["current_item_index"] += 1
    await ask_next_question(update, context, uid)

async def generate_final_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, page_id=None):
    s = user_sessions[uid]
    c_rate = 58.0
    subtotal = 0
    
    now = datetime.now()
    client_code = s['client'].upper()[:4]
    inv_number = f"{client_code}-{now.strftime('%H%M')}"
    date_str = now.strftime('%m.%d.%Y')
    
    inv_text = f"COMMERCIAL INVOICE: {inv_number}\n📅 Date: {date_str}\n\n1. ТОВАРНАЯ ВЕДОМОСТЬ:\n"
    
    for i in s["items"]:
        q, p, sh = int(i.get('qty') or 1), float(i.get('client_price') or 0.0), float(i.get('shipping') or 0.0)
        lt = (q * p) + sh
        subtotal += lt
        inv_text += f"• {i['name']}: — {q} шт\n{q} × {p} + {sh} = {lt:.1f}¥\n\n"

    c_amd = max(10000, int(subtotal * 0.03 * c_rate))
    c_cny = c_amd / c_rate
    tot_cny = subtotal + c_cny
    tot_amd = int((subtotal * c_rate) + c_amd)

    full_msg = (
        f"{inv_text}"
        f"────────────────────────\n"
        f"SUBTOTAL: {subtotal:.1f}¥\n\n"
        f"2. КОМИССИЯ И СЕРВИС\n"
        f"(Минимальная 10000 AMD): {c_cny:.1f}¥\n\n"
        f"3. ИТОГОВЫЙ РАСЧЕТ\n"
        f"• Всего в юанях: {tot_cny:.1f}¥\n"
        f"• Курс: {c_rate}\n\n"
        f"✅ ИТОГО К ОПЛАТЕ: {tot_amd:,} AMD"
    )

    s.update({
        "subtotal_cny": subtotal, 
        "tot_amd": tot_amd, 
        "client_rate": c_rate, 
        "full_invoice": full_msg
    })
    new_pid = save_to_notion_cache(s, page_id=page_id)

    kb = [
        [InlineKeyboardButton("✏️ Изменить товар", callback_data=f"editinit_{new_pid}")], 
        [InlineKeyboardButton("📑 В Airtable", callback_data=f"airtable_{new_pid}"), InlineKeyboardButton("🧮 В Карго", callback_data=f"tocargo_{new_pid}")],
        [InlineKeyboardButton("📊 Excel", callback_data=f"cgexcel_{new_pid}"), InlineKeyboardButton("📦 Склад (FF)", callback_data=f"ffinit_{new_pid}")]
    ]
    
    await context.bot.send_message(chat_id=update.effective_chat.id, text=full_msg, reply_markup=InlineKeyboardMarkup(kb))

# ====================================================================
# MESSAGE HANDLERS
# ====================================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, text = update.message.from_user.id, update.message.text.strip()
    
    # --- FF РУЧНОЙ ВВОД ---
    if uid in ff_sessions and ff_sessions[uid]["state"] == "FF_WAIT_UNIT":
        try:
            n = [float(x) for x in text.replace(',','.').split()]
            s = ff_sessions[uid]
            item = s["items"][s["current_idx"]]
            
            update_item_ff_data(item["page_id"], f"{n[0]}x{n[1]}x{n[2]}", n[3], 1)
            
            s["units"].append({"name": item["name"], "qty": int(item.get("qty", 1)), "dims": (n[0], n[1], n[2]), "weight": n[3]})
            s["current_idx"] += 1
            await ask_ff_item(update, context, uid)
        except: 
            await update.message.reply_text("❌ Ошибка. Формат: Д Ш В Вес (через пробел)")
        return

    # --- ЗАКУПКА ---
    if uid in user_sessions:
        s = user_sessions[uid]
        if s["state"] == "COLLECTING" and text.isdigit():
            s["orders"].append({"type": "id", "val": text})
            return await update.message.reply_text(f"✅ ID {text} добавлен.")
        elif s["state"] == "ASK_EDIT_ID":
            for i, item in enumerate(s["items"]):
                if text == str(i+1):
                    s["current_item_index"], item["qty_confirmed"], item["shipping"] = i, False, None
                    return await ask_next_question(update, context, uid)
            return await update.message.reply_text("❌ Нет такого номера.")
        elif s["state"] == "ASKING_QTY": 
            s["items"][s["current_item_index"]]["qty"] = text
            s["items"][s["current_item_index"]]["qty_confirmed"] = True
        elif s["state"] == "ASKING_SHIPPING": 
            s["items"][s["current_item_index"]]["shipping"] = text
        await ask_next_question(update, context, uid)
    
    # --- КАРГО ---
    elif str(uid) in cargo_drafts:
        d = cargo_drafts[str(uid)]
        if d["state"] == "CARGO_WAIT_DIMS":
            res = parse_logistics_with_kimi(text, None)
            await process_kimi_logistics_result(update, context, uid, res)
        elif d["state"] == "CARGO_WAIT_TARIFF_CG":
            try:
                d["tariff_cg"] = float(text.replace(',','.'))
                d["state"] = "CARGO_WAIT_TARIFF_CL"
                await update.message.reply_text("👉 Тариф Клиенту ($/кг):")
            except: 
                await update.message.reply_text("❌ Введи число.")
        elif d["state"] == "CARGO_WAIT_TARIFF_CL":
            try:
                d["tariff_cl"] = float(text.replace(',','.'))
                d["state"] = "CARGO_WAIT_RATE_AMD"
                await update.message.reply_text("👉 Курс USD -> AMD:")
            except: 
                await update.message.reply_text("❌ Введи число.")
        elif d["state"] == "CARGO_WAIT_RATE_AMD":
            try:
                r_amd = float(text.replace(',','.'))
                t_w, t_cl, t_cg = d['t_weight'], d['tariff_cl'], d['tariff_cg']
                tot_cl_usd = t_w * t_cl
                tot_amd = int(tot_cl_usd * r_amd)
                profit = int((t_cl - t_cg) * t_w * r_amd)
                cny_cargo = int(t_w * t_cg * 7.3)
                
                d["tot_amd"] = tot_amd
                
                await update.message.reply_text(f"🚛 <b>CARGO INVOICE: {d['client'].upper()}</b>\n\nПАРАМЕТРЫ:\n• Вес: {t_w:.1f}кг | {d['t_pieces']} мест\n\nРАСЧЕТ:\n• Доставка: ${tot_cl_usd:.1f}\n✅ <b>К ОПЛАТЕ: {tot_amd:,} AMD</b>", parse_mode='HTML')
                
                pid = save_to_notion_cache(d)
                
                kb = [
                    [InlineKeyboardButton("📊 Excel", callback_data=f"cgexcel_{pid}"), InlineKeyboardButton("📑 Airtable", callback_data=f"cargodb_{pid}")],
                    [InlineKeyboardButton("📦 Расчет FF (Склад)", callback_data=f"ffinit_{pid}")]
                ]
                await update.message.reply_text(f"💼 <b>ВНУТРЕННИЙ РАСЧЕТ ({d['client'].upper()}):</b>\n\n1. В КАРГО:\n• Себестоимость: ${t_w*t_cg:.1f}\n🇨🇳 Перевести: {cny_cargo:,} ¥\n\n2. ПРИБЫЛЬ:\n💰 <b>{profit:,} AMD</b>", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
                del cargo_drafts[str(uid)]
            except: 
                await update.message.reply_text("❌ Введи число.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    url = (await context.bot.get_file(update.message.photo[-1].file_id)).file_path
    
    if uid in user_sessions and user_sessions[uid]["state"] == "COLLECTING":
        user_sessions[uid]["orders"].append({"type": "photo", "val": url})
    elif str(uid) in cargo_drafts and cargo_drafts[str(uid)]["state"] == "CARGO_WAIT_DIMS":
        res = parse_logistics_with_kimi(update.message.caption, url)
        await process_kimi_logistics_result(update, context, uid, res)

async def process_kimi_logistics_result(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, res: dict):
    d = cargo_drafts[str(uid)]
    item = d["items"][d["current_item_index"]]
    if not res: 
        return await update.message.reply_text("❌ ИИ не понял. Введи вручную.")
    
    pcs = int(res.get('pcs_per_ctn') or 1)
    t_qty = int(item.get('qty') or 1)
    res['full_cartons'], res['remainder'] = t_qty // pcs, t_qty % pcs
    res['gw_kg'] = res.get('gw_kg') or 0.0
    res['length'] = res.get('length') or 0
    res['width'] = res.get('width') or 0
    res['height'] = res.get('height') or 0
    d["temp_kimi"] = res
    
    msg = f"🧠 <b>Kimi [{item['name']}]:</b>\nВ кор: {pcs} шт | Вес: {res['gw_kg']}кг | {res['length']}x{res['width']}x{res['height']}\n✅ Полных: {res['full_cartons']} | ⚠️ Остаток: {res['remainder']}"
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Сохранить", callback_data="cg_accept_kimi")]]))

# ====================================================================
# CALLBACK HANDLER
# ====================================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid, data = q.from_user.id, q.data
    
    # --- ЛОГИКА ГЕНЕРАЦИИ EXCEL ---
    if data.startswith("cgexcel_"):
        pid = data.split("_")[1]
        try: 
            d = get_from_notion_cache(pid)
        except: 
            return await q.message.reply_text("❌ Чек удален или устарел.")
        
        items_data = []
        for i, item in enumerate(d.get('items', []), 1):
            pack_str = "Обрешетка" if item.get('pack_type') in ['crate', 'pk_crate'] else \
                       ("Уголки" if item.get('pack_type') in ['corners', 'pk_corners'] else "Мешок/Сборная")
            
            items_data.append({
                "№": i,
                "Название": item.get('name', 'Без названия'),
                "Кол-во (шт)": item.get('qty', 0),
                "Упаковка": pack_str
            })
        
        items_data.extend([
            {"№": "", "Название": "", "Кол-во (шт)": "", "Упаковка": ""},
            {"№": "", "Название": "ИТОГОВЫЙ ВЕС", "Кол-во (шт)": f"{d.get('t_weight', 0):.1f} кг", "Упаковка": ""},
            {"№": "", "Название": "ИТОГОВЫЙ ОБЪЕМ", "Кол-во (шт)": f"{d.get('t_vol', 0):.2f} м³", "Упаковка": ""},
            {"№": "", "Название": "ИТОГО К ОПЛАТЕ", "Кол-во (шт)": f"{d.get('tot_amd', 0):,} AMD", "Упаковка": ""}
        ])
        
        df = pd.DataFrame(items_data)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Cargo Invoice')
        
        output.seek(0)
        await context.bot.send_document(
            chat_id=q.message.chat_id, 
            document=InputFile(output, filename=f"Cargo_{d.get('client', 'Order').upper()}.xlsx")
        )

    # --- FF CALLBACKS ---
    elif data.startswith("ffinit_"):
        await start_ff_process(update, context, data.split("_")[1])
        
    elif data == "ff_db":
        s = ff_sessions[uid]
        item = s["items"][s["current_idx"]]
        d = [float(n) for n in re.findall(r"\d+\.?\d*", item["unit_cm"])]
        s["units"].append({"name": item["name"], "qty": int(item.get("qty", 1)), "dims": (d[0], d[1], d[2]), "weight": item.get("unit_kg", 0)})
        s["current_idx"] += 1
        await ask_ff_item(update, context, uid)
        
    elif data == "ff_man":
        ff_sessions[uid]["state"] = "FF_WAIT_UNIT"
        await q.message.reply_text("Введи размеры и вес одной шт (Д Ш В Вес):")
        
    elif data == "ff_skip":
        ff_sessions[uid]["current_idx"] += 1
        await ask_ff_item(update, context, uid)
        
    # --- ЭКСПОРТ В AIRTABLE ---
    elif data.startswith("airtable_"):
        cache_pid = data.split("_")[1]
        try:
            # Получаем полные данные заказа из Notion Cache
            order_data = get_from_notion_cache(cache_pid)
            
            # Подготовка данных для Airtable
            airtable_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Закупка"
            
            headers = {
                "Authorization": f"Bearer {AIRTABLE_TOKEN}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "records": [
                    {
                        "fields": {
                            "Клиент": order_data["client"],
                            "Заказ": order_data.get("full_invoice", "Текст не найден")
                        }
                    }
                ],
                "typecast": True
            }
            
            # Отправка POST запроса в Airtable
            response = requests.post(airtable_url, headers=headers, json=payload)
            
            if response.status_code == 200:
                await q.message.reply_text("✅ Данные успешно экспортированы в Airtable (Закупка)!")
            else:
                await q.message.reply_text(f"❌ Ошибка Airtable: {response.status_code}\nОтвет: {response.text}")
                
        except Exception as e:
            await q.message.reply_text(f"❌ Сбой при экспорте: {str(e)}")

    # --- EXISTING CALLBACKS ---
    elif data.startswith("editinit_"):
        user_sessions[uid] = get_from_notion_cache(data.split("_")[1])
        user_sessions[uid]["state"] = "ASK_EDIT_ID"
        await q.message.reply_text("Введи номер товара из списка для изменения:")

    elif data.startswith("pk_"):
        cargo_drafts[str(uid)]["items"][cargo_drafts[str(uid)]["current_item_index"]]["pack_type"] = data
        await process_cargo_items(update, context, uid)

    elif data == "cg_accept_kimi":
        d = cargo_drafts[str(uid)]
        i, r = d["items"][d["current_item_index"]], d["temp_kimi"]
        if "boxes" not in i: i["boxes"] = []
        if r["full_cartons"] > 0: 
            i["boxes"].append({"qty": r["full_cartons"], "w": r["gw_kg"], "l": r["length"], "w_dim": r["width"], "h": r["height"]})
        if r["remainder"] == 0: 
            d["current_item_index"] += 1
        else: 
            await q.message.reply_text(f"Введи данные остатка для {i['name']} ({r['remainder']} шт):")
        await process_cargo_items(update, context, uid)

    elif data.startswith("tocargo_"):
        d = get_from_notion_cache(data.split("_")[1])
        cargo_drafts[str(uid)] = {"client": d["client"], "items": d["items"], "current_item_index": 0}
        await process_cargo_items(update, context, uid)

    elif data == "cg_use_db":
        d = cargo_drafts[str(uid)]
        i = d["items"][d["current_item_index"]]
        nums = re.findall(r"[-+]?\d*\.\d+|\d+", str(i.get("cm") or "0x0x0").replace(',', '.'))
        dims = [float(n) for n in nums] if len(nums) >= 3 else [0.0, 0.0, 0.0]
        f_box = int(i.get('qty', 0)) // int(i.get('pcs_ctn', 1) or 1)
        rem = int(i.get('qty', 0)) % int(i.get('pcs_ctn', 1) or 1)
        i["boxes"] = [{"qty": f_box, "w": i.get("gw_kg") or 0.0, "l": dims[0], "w_dim": dims[1], "h": dims[2]}]
        
        if rem == 0: 
            d["current_item_index"] += 1
        else: 
            await q.message.reply_text(f"Введи данные остатка для {i['name']} ({rem} шт):")
        await process_cargo_items(update, context, uid)

# ====================================================================
# КОМАНДЫ БОТА
# ====================================================================
async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(update.message.text.split()) < 2: 
        return await update.message.reply_text("❌ Формат: /client Имя")
    user_sessions[update.message.from_user.id] = {"client": update.message.text.split()[1], "orders": [], "items": [], "current_item_index": 0, "state": "COLLECTING"}
    await update.message.reply_text(f"✅ Клиент {update.message.text.split()[1]}.\nЖду фото или ID.")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in user_sessions: return
    s = user_sessions[uid]
    msg = await update.message.reply_text("⏳ ИИ обрабатывает фото и данные...")
    
    photo_urls = [o["val"] for o in s["orders"] if o["type"] == "photo"]
    if photo_urls:
        ids = recognize_photos_batch(photo_urls, get_client_catalog(s["client"]))
        for rid in ids:
            det = get_item_details(s["client"], rid)
            if det: s["items"].append(det)
            
    for o in s["orders"]:
        if o["type"] == "id":
            det = get_item_details(s["client"], o["val"])
            if det: s["items"].append(det)
            
    await msg.delete()
    await ask_next_question(update, context, uid)

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_sessions.pop(uid, None)
    if str(uid) in cargo_drafts: del cargo_drafts[str(uid)]
    if uid in ff_sessions: del ff_sessions[uid]
    await update.message.reply_text("❌ Все текущие процессы отменены.\nПамять очищена.")

# ====================================================================
# ИНИЦИАЛИЗАЦИЯ И ЗАПУСК (WEBHOOKS / POLLING)
# ====================================================================
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("client", client_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Получаем порт от Railway (или ставим 8080 для локалки)
    PORT = int(os.environ.get('PORT', '8080'))
    
    # Проверяем, есть ли переменная WEBHOOK_URL
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL") 

    if WEBHOOK_URL:
        print(f"ПРОДАКШЕН: Запуск Webhook на порту {PORT}...")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=WEBHOOK_URL
        )
    else:
        print("РАЗРАБОТКА: Запуск локального Polling...")
        app.run_polling()
