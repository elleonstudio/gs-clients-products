async def process_cargo_items(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    d = cargo_drafts[str(uid)]
    idx = d.get("current_item_index", 0)
    
    # Пропускаем заполненные товары И товары "В наборе"
    while idx < len(d["items"]) and ("boxes" in d["items"][idx] or d["items"][idx].get("pack_type") == "pk_inset"):
        idx += 1
        
    d["current_item_index"] = idx
    if idx >= len(d["items"]): 
        return await finish_cargo_dims(update, context, uid)
    
    item = d["items"][idx]
    
    # Если упаковка еще не выбрана
    if "pack_type" not in item:
        d["state"] = "CARGO_WAIT_PACK"
        kb = [[InlineKeyboardButton("📦 Мешок/Сборная (+0)", callback_data="pk_sack")],
              [InlineKeyboardButton("📐 Уголки (+1 кг)", callback_data="pk_corners")],
              [InlineKeyboardButton("🪵 Обрешетка (+10кг)", callback_data="pk_crate")],
              [InlineKeyboardButton("🎁 В наборе (Нет места)", callback_data="pk_inset")]]
        return await context.bot.send_message(chat_id=update.effective_chat.id, text=f"📦 Товар: <b>{item['name']}</b>\nКак упакован?", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

    # Если "В наборе", мы сюда даже не должны дойти из-за цикла while выше, 
    # но на всякий случай перебрасываем дальше
    if item["pack_type"] == "pk_inset":
        d["current_item_index"] += 1
        return await process_cargo_items(update, context, uid)

    # Запрос габаритов
    d["state"] = "CARGO_WAIT_DIMS"
    text = f"📐 Габариты: <b>{item['name']} ({item['qty']} шт)</b>\n"
    kb = []
    
    # Пытаемся найти данные в базе Notion
    if item.get("pcs_ctn") and item.get("gw_kg") and item.get("cm"):
        f_box = int(item['qty']) // item['pcs_ctn']
        rem = int(item['qty']) % item['pcs_ctn']
        text += f"\n⚡️ В базе: {item['pcs_ctn']}шт/кор | {item['gw_kg']}кг | {item['cm']}\n"
        if f_box > 0: text += f"👉 Авто-расчет: {f_box} полных коробок.\n"
        kb.append([InlineKeyboardButton("⚡️ Использовать базу", callback_data="cg_use_db")])
    
    text += f"\nВведи данные остатка (Мест Вес Д Ш В) или перешли ответ китайца.\n⏩ Пропустить: /next_product"
    await context.bot.send_message(chat_id=update.effective_chat.id, text=text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb) if kb else None)
