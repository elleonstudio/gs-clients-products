import os, requests, base64, json, io, re
import pandas as pd
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# Конфигурация
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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И NOTION
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
            "cm": p.get("cm", {}).get("rich_text", [])[0]["plain_text"] if p.get("cm", {}).get("rich_text", []) else None
        }
    except: return None

# ====================================================================
# ИИ ЛОГИКА
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
# МОДУЛЬ КАРГО
# ====================================================================
async def process_cargo_items(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    d = cargo_drafts[str(uid)]
    idx = d.get("current_item_index", 0)
    while idx < len(d["items"]) and ("boxes" in d["items"][idx] or d["items"][idx].get("pack_type") == "pk_inset"): idx += 1
    d["current_item_index"] = idx
    if idx >= len(d["items"]): return await finish_cargo_dims(update, context, uid)
    
    item = d["items"][idx]
    if "pack_type" not in item:
        d["state"] = "CARGO_WAIT_PACK"
        kb = [[InlineKeyboardButton("📦 Мешок (+0)", callback_data="pk_sack"), InlineKeyboardButton("📐 Уголки (+1кг)", callback_data="pk_corners")], [InlineKeyboardButton("🪵 Обрешетка (+10кг)", callback_data="pk_crate")], [InlineKeyboardButton("🎁 В наборе", callback_data="pk_inset")]]
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📦 Товар: <b>{item['name']}</b>\nКак упакуем?", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

    d["state"] = "CARGO_WAIT_DIMS"
    text = f"📐 Габариты: <b>{item['name']} ({item['qty']} шт)</b>\n"
    kb = []
    if item.get("cm") and item.get("gw_kg"): kb.append([InlineKeyboardButton("⚡️ База Notion", callback_data="cg_use_db")])
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text + "Введи данные остатка или перешли ответ китайца:", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def finish_cargo_dims(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    d = cargo_drafts[str(uid)]
    t_w = t_v = t_p = 0
    for i in d["items"]:
        if i.get("pack_type") == "pk_inset": continue
        for b in i.get("boxes", []):
            q = float(b.get("qty", 0))
            w = float(b.get("w") or 0)
            l = float(b.get("l") or 0)
            wd = float(b.get("w_dim") or 0)
            h = float(b.get("h") or 0)
            
            if i.get("pack_type") == "pk_crate": w += 10; l += 5; wd += 5; h += 5
            elif i.get("pack_type") == "pk_corners": w += 1
            
            t_p += q
            t_w += (w * q)
            t_v += ((l * wd * h) / 1000000) * q
            
    d.update({"t_weight": t_w, "t_vol": t_v, "t_pieces": int(t_p), "density": int(t_w/t_v) if t_v > 0 else 0, "state": "CARGO_WAIT_TARIFF_CG"})
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📊 <b>ИТОГ:</b> {t_w:.1f}кг | {t_v:.2f}м³ | {int(t_p)} мест. Плотность: {d['density']}\n👉 Напиши Тариф Карго ($/кг):", parse_mode='HTML')

# ====================================================================
# МОДУЛЬ ЗАКУПКИ
# ====================================================================
async def ask_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    s = user_sessions[uid]
    idx = s.get("current_item_index", 0)
    if idx >= len(s["items"]): return await generate_final_invoice(update, context, uid)
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
    inv_text = "<b>ТОВАРНАЯ ВЕДОМОСТЬ:</b>\n"
    for i in s["items"]:
        q, p, sh = int(i['qty']), float(i['client_price']), float(i['shipping'])
        lt = (q * p) + sh
        subtotal += lt
        inv_text += f"• {i['name']}: — {q} шт\n{q} × {p} + {sh} = {lt:.1f}¥\n\n"

    c_amd = 10000 if (subtotal * 0.03 * c_rate < 10000) else int(subtotal * 0.03 * c_rate)
    tot_amd = int((subtotal * c_rate) + c_amd)
    s.update({"subtotal_cny": subtotal, "tot_amd": tot_amd, "client_rate": c_rate, "inv_text": inv_text})
    new_pid = save_to_notion_cache(s, page_id=page_id)

    full_msg = f"{inv_text}────────────────────────\n<b>SUBTOTAL:</b> {subtotal:.1f}¥\n\n<b>2. КОМИССИЯ:</b> {c_amd / c_rate:.1f}¥\n<b>3. ИТОГОВЫЙ РАСЧЕТ:</b>\n• Итого юаней: {subtotal + (c_amd/c_rate):.1f}¥\n• Курс: {c_rate}\n✅ <b>К ОПЛАТЕ: {tot_amd:,} AMD</b>"
    kb = [[InlineKeyboardButton("✏️ Изменить товар", callback_data=f"editinit_{new_pid}")], [InlineKeyboardButton("📑 В Airtable", callback_data=f"airtable_{new_pid}"), InlineKeyboardButton("🧮 В Карго", callback_data=f"tocargo_{new_pid}")]]
    await context.bot.send_message(chat_id=update.effective_chat.id, text=full_msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

# ====================================================================
# MESSAGE HANDLERS
# ====================================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, text = update.message.from_user.id, update.message.text.strip()
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
        elif s["state"] == "ASKING_QTY": s["items"][s["current_item_index"]]["qty"] = text; s["items"][s["current_item_index"]]["qty_confirmed"] = True
        elif s["state"] == "ASKING_SHIPPING": s["items"][s["current_item_index"]]["shipping"] = text
        await ask_next_question(update, context, uid)
    
    elif str(uid) in cargo_drafts:
        d = cargo_drafts[str(uid)]
        if d["state"] == "CARGO_WAIT_DIMS":
            res = parse_logistics_with_kimi(text, None)
            await process_kimi_logistics_result(update, context, uid, res)
        elif d["state"] == "CARGO_WAIT_TARIFF_CG":
            d["tariff_cg"], d["state"] = float(text.replace(',','.')), "CARGO_WAIT_TARIFF_CL"
            await update.message.reply_text("👉 Тариф Клиенту ($/кг):")
        elif d["state"] == "CARGO_WAIT_TARIFF_CL":
            d["tariff_cl"], d["state"] = float(text.replace(',','.')), "CARGO_WAIT_RATE_AMD"
            await update.message.reply_text("👉 Курс USD -> AMD:")
        elif d["state"] == "CARGO_WAIT_RATE_AMD":
            r_amd = float(text.replace(',','.'))
            t_w, t_cl, t_cg = d['t_weight'], d['tariff_cl'], d['tariff_cg']
            tot_cl_usd = t_w * t_cl
            tot_amd = int(tot_cl_usd * r_amd)
            profit = int((t_cl - t_cg) * t_w * r_amd)
            cny_cargo = int(t_w * t_cg * 7.3)
            
            await update.message.reply_text(f"🚛 <b>CARGO INVOICE: {d['client'].upper()}</b>\n\nПАРАМЕТРЫ:\n• Вес: {t_w:.1f}кг | {d['t_pieces']} мест\n\nРАСЧЕТ:\n• Доставка: ${tot_cl_usd:.1f}\n✅ <b>К ОПЛАТЕ: {tot_amd:,} AMD</b>", parse_mode='HTML')
            await update.message.reply_text(f"💼 <b>ВНУТРЕННИЙ РАСЧЕТ ({d['client'].upper()}):</b>\n\n1. В КАРГО:\n• Себестоимость: ${t_w*t_cg:.1f}\n🇨🇳 Перевести: {cny_cargo:,} ¥\n\n2. ПРИБЫЛЬ:\n💰 <b>{profit:,} AMD</b>", parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📊 Excel", callback_data=f"cgexcel_{save_to_notion_cache(d)}"), InlineKeyboardButton("📑 Airtable", callback_data=f"cargodb_{save_to_notion_cache(d)}")]]))
            del cargo_drafts[str(uid)]

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
    if not res: return await update.message.reply_text("❌ ИИ не понял. Введи вручную.")
    pcs = int(res.get('pcs_per_ctn', 1)) or 1
    t_qty = int(item['qty'])
    res['full_cartons'], res['remainder'] = t_qty // pcs, t_qty % pcs
    d["temp_kimi"] = res
    msg = f"🧠 <b>Kimi [{item['name']}]:</b>\nВ кор: {pcs} шт | Вес: {res.get('gw_kg', 0)}кг | {res.get('length', 0)}x{res.get('width', 0)}x{res.get('height', 0)}\n✅ Полных: {res['full_cartons']} | ⚠️ Остаток: {res['remainder']}"
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Сохранить", callback_data="cg_accept_kimi")]]))

# ====================================================================
# CALLBACK HANDLER
# ====================================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid, data = q.from_user.id, q.data
    
    if data.startswith("editinit_"):
        user_sessions[uid] = get_from_notion_cache(data.split("_")[1])
        user_sessions[uid]["state"] = "ASK_EDIT_ID"
        await q.message.reply_text("Введи номер товара из списка для изменения:")
    
    elif data.startswith("airtable_"):
        d = get_from_notion_cache(data.split("_")[1])
        requests.post(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/Закупка", headers={"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}, json={"records": [{"fields": {"Клиент": d["client"], "Заказ": d["inv_text"] + f"\nИТОГО: {d['tot_amd']} AMD"}}], "typecast": True})
        await q.message.reply_text("✅ В Airtable!")

    elif data.startswith("pk_"):
        cargo_drafts[str(uid)]["items"][cargo_drafts[str(uid)]["current_item_index"]]["pack_type"] = data
        await process_cargo_items(update, context, uid)

    elif data == "cg_accept_kimi":
        d = cargo_drafts[str(uid)]
        i, r = d["items"][d["current_item_index"]], d["temp_kimi"]
        if "boxes" not in i: i["boxes"] = []
        if r["full_cartons"] > 0: i["boxes"].append({"qty": r["full_cartons"], "w": r.get("gw_kg", 0), "l": r.get("length", 0), "w_dim": r.get("width", 0), "h": r.get("height", 0)})
        if r["remainder"] == 0: d["current_item_index"] += 1
        else: await q.message.reply_text(f"Введи данные остатка для {i['name']} ({r['remainder']} шт):")
        await process_cargo_items(update, context, uid)

    elif data.startswith("tocargo_"):
        d = get_from_notion_cache(data.split("_")[1])
        cargo_drafts[str(uid)] = {"client": d["client"], "items": d["items"], "current_item_index": 0}
        await process_cargo_items(update, context, uid)

    elif data == "cg_use_db":
        d = cargo_drafts[str(uid)]
        i = d["items"][d["current_item_index"]]
        nums = re.findall(r"[-+]?\d*\.\d+|\d+", i.get("cm", "").replace(',', '.'))
        dims = [float(n) for n in nums] if len(nums) >= 3 else [0,0,0]
        f_box, rem = (int(i['qty']) // i['pcs_ctn']) if i.get('pcs_ctn') else (0, int(i['qty']))
        i["boxes"] = [{"qty": f_box, "w": i.get("gw_kg", 0), "l": dims[0], "w_dim": dims[1], "h": dims[2]}]
        if rem == 0: d["current_item_index"] += 1
        else: await q.message.reply_text(f"Введи данные остатка для {i['name']} ({rem} шт):")
        await process_cargo_items(update, context, uid)

# ====================================================================
# КОМАНДЫ
# ====================================================================
async def client_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(update.message.text.split()) < 2: return await update.message.reply_text("❌ /client Имя")
    user_sessions[update.message.from_user.id] = {"client": update.message.text.split()[1], "orders": [], "items": [], "current_item_index": 0, "state": "COLLECTING"}
    await update.message.reply_text(f"✅ Клиент {update.message.text.split()[1]}. Жду фото или ID.")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in user_sessions: return
    s = user_sessions[uid]
    msg = await update.message.reply_text("⏳ Обработка...")
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

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("client", client_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(callback_handler))
    print("Бот запущен!"); app.run_polling()
