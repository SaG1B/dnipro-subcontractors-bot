import asyncio
import logging
import sqlite3
import io
import requests
from pypdf import PdfReader
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

API_TOKEN = "8942982040:AAGo781dK6bUOyPDjp2vfHBJNrAcxAO-D3A"
ADMIN_ID = 255027851  # Ваш Telegram ID

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# Строительные ключевые слова
KEYWORDS = [
    "ремонт", "будівництв", "утеплен", "фасад", "плитк", "покрівл", 
    "дах", "сантехн", "електр", "штукатурк", "маляр", "підлог", "демонтаж", 
    "благоустрій", "вікн", "двер", "опален", "водопровід", "каналізаці", 
    "вентиляці", "бетон", "асфальт", "монтаж", "реконструкці", "капітальний"
]

# Стоп-слова (исключают не-строительные закупки)
STOP_WORDS = [
    "ліки", "медичн", "фармацевт", "реактив", "препарат", "тести", "харчуван", 
    "продукт", "паливо", "бензин", "дизель", "канцтовар", "папір", "офіс"
]

# Разрешенные локации Днепровской агломерации
LOCATIONS = [
    "дніпро", "днепр", "самар", "новомосковськ", "підгородне", "подгородное", 
    "обухівка", "обуховка", "слобожанське", "слобожанское", "горянівське", 
    "сурсько-литовське", "дослідне"
]

def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, phone TEXT, spec TEXT, status TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS tenders (tender_id TEXT PRIMARY KEY, title TEXT, amount REAL)")
    conn.commit()
    conn.close()

def add_user_db(user_id, name, phone, spec, status="pending"):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO users (user_id, name, phone, spec, status) VALUES (?, ?, ?, ?, ?)", (user_id, name, phone, spec, status))
    conn.commit()
    conn.close()

def get_user_db(user_id):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT name, phone, spec, status FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def update_user_status(user_id, status):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))
    conn.commit()
    conn.close()

def get_approved_users():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE status = 'approved'")
    users = cursor.fetchall()
    conn.close()
    return [u[0] for u in users]

def is_tender_processed(tender_id):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT tender_id FROM tenders WHERE tender_id = ?", (tender_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def save_tender(tender_id, title, amount):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO tenders (tender_id, title, amount) VALUES (?, ?, ?)", (tender_id, title, amount))
    conn.commit()
    conn.close()

def extract_contacts_from_pdf(pdf_url):
    try:
        response = requests.get(pdf_url, timeout=15)
        if response.status_code == 200:
            pdf_file = io.BytesIO(response.content)
            reader = PdfReader(pdf_file)
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            
            import re
            # Ищем украинские номера телефонов
            phones = re.findall(r'(?:\+?38)?0\d{9}', text)
            unique_phones = list(dict.fromkeys(phones)) # Убираем дубли, сохраняя порядок
            
            formatted_phones = []
            for p in unique_phones:
                if not p.startswith("+38"):
                    if p.startswith("0"):
                        p = "+38" + p
                formatted_phones.append(p)
            
            if formatted_phones:
                return "📞 **Прямые контакты из документов:**\n" + "\n".join([f"• `{phone}`" for phone in formatted_phones[:3]])
    except Exception as e:
        logging.error(f"Ошибка чтения PDF: {e}")
    return "📞 Контакты и детали указаны в прикрепленной тендерной документации"

def fetch_dnipro_tenders():
    url = "https://public.api.openprocurement.org/api/2.5/tenders?descending=1"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.json().get("data", [])[:30]
    except Exception as e:
        logging.error(f"Ошибка Prozorro API: {e}")
    return []

async def process_single_tender(t_id):
    t_url = f"https://public.api.openprocurement.org/api/2.5/tenders/{t_id}"
    t_res = requests.get(t_url, timeout=10)
    if t_res.status_code != 200:
        return None
    
    t_data = t_res.json().get("data", {})
    title = t_data.get("title", "").lower()
    description = t_data.get("description", "").lower()
    
    # Проверка на стоп-слова
    full_text = f"{title} {description}"
    if any(stop_word in full_text for stop_word in STOP_WORDS):
        return None

    # Проверка локации
    address_str = ""
    items = t_data.get("items", [])
    if items:
        delivery_addr = items[0].get("deliveryAddress", {})
        address_str = f"{delivery_addr.get('locality', '')} {delivery_addr.get('streetAddress', '')}".lower()
    
    search_location_text = f"{title} {address_str}"
    is_dnipro_region = any(loc in search_location_text for loc in LOCATIONS)
    if not is_dnipro_region:
        return None

    # Проверка стройки
    is_construction = any(kw in full_text for kw in KEYWORDS)
    if not is_construction:
        return None
    
    # Достаем контактное лицо (Заказчик / Ответственный)
    enquiries_contact = "Заказчик Prozorro"
    enquiry_period = t_data.get("enquiries", {})
    procuring_entity = t_data.get("procuringEntity", {})
    entity_name = procuring_entity.get("name", "Организатор закупки")
    
    # Ищем PDF документы и вытаскиваем телефоны
    contact_info = "📞 Контакты указаны в документации тендера"
    documents = t_data.get("documents", [])
    for doc in documents:
        doc_url = doc.get("url")
        if doc_url and doc_url.lower().endswith(".pdf"):
            pdf_contact_result = extract_contacts_from_pdf(doc_url)
            if "Прямые контакты" in pdf_contact_result:
                contact_info = pdf_contact_result
                break

    card_text = (
        f"🏗 **НОВЫЙ СТРОИТЕЛЬНЫЙ ОБЪЕКТ (Днепр)**\n\n"
        f"📌 **Работы:** {t_data.get('title')}\n"
        f"🏢 **Заказчик:** {entity_name}\n"
        f"🔗 **Ссылка на тендер:** https://prozorro.gov.ua/tender/{t_data.get('tenderID')}\n\n"
        f"{contact_info}\n"
    )
    return card_text

async def prozorro_monitoring_task():
    while True:
        try:
            tenders = fetch_dnipro_tenders()
            for t_item in tenders:
                t_id = t_item.get("id")
                if is_tender_processed(t_id):
                    continue
                
                card_text = await process_single_tender(t_id)
                if card_text:
                    save_tender(t_id, "Tender", 0)
                    for u_id in get_approved_users():
                        try:
                            await bot.send_message(u_id, card_text, parse_mode="Markdown")
                        except Exception:
                            pass
        except Exception as e:
            logging.error(f"Ошибка цикла мониторинга: {e}")
            
        await asyncio.sleep(900)

class Registration(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_spec = State()

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user_db(user_id)
    if user:
        if user[3] == "approved":
            await message.answer("🎉 Вы в закрытом клубе мастеров Днепра! Ожидайте новые объекты.")
            return
        elif user[3] == "pending":
            await message.answer("⏳ Ваша заявка находится на рассмотрении у администратора.")
            return
    await message.answer("🏗 **БудДніпро | Закрытый клуб субподрядчиков**\n\nКак вас зовут? (ФИО или Имя)")
    await state.set_state(Registration.waiting_for_name)

@dp.message(Command("test_tender"))
async def cmd_test_tender(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔎 Ищу строительный объект и извлекаю контакты...")
    tenders = fetch_dnipro_tenders()
    found = False
    for t_item in tenders:
        sample_id = t_item.get("id")
        card_text = await process_single_tender(sample_id)
        if card_text:
            await message.answer(card_text, parse_mode="Markdown")
            found = True
            break
    if not found:
        await message.answer("Прямо сейчас строительных тендеров по Днепру с телефонами в PDF не найдено. Бот проверяет каждые 15 минут!")

@dp.message(Registration.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Укажите ваш контактный номер телефона:")
    await state.set_state(Registration.waiting_for_phone)

@dp.message(Registration.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await message.answer("Укажите вашу специализацию (например: Плиточник, Фасадчик, Универсал):")
    await state.set_state(Registration.waiting_for_spec)

@dp.message(Registration.waiting_for_spec)
async def process_spec(message: types.Message, state: FSMContext):
    data = await state.get_data()
    user_id = message.from_user.id
    add_user_db(user_id, data["name"], data["phone"], message.text, status="pending")
    await state.clear()
    await message.answer("✅ Ваша заявка отправлена администратору. Ожидайте одобрения доступа!")
    
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")
    ]])
    text = f"🚨 **ЗАЯВКА В ЗАКРЫТЫЙ КЛУБ!**\n\n👤 **Имя:** {data['name']}\n📞 **Телефон:** {data['phone']}\n🛠 **Специализация:** {message.text}\n🆔 **ID:** `{user_id}`"
    await bot.send_message(ADMIN_ID, text, reply_markup=admin_kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("approve_"))
async def approve_user(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    update_user_status(user_id, "approved")
    await bot.send_message(user_id, "🎉 Ваша заявка одобрена! Доступ к объектам открыт.")
    await callback.message.edit_text(callback.message.text + "\n\n**[СТАТУС: ✅ ОДОБРЕНО]**", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_user(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    update_user_status(user_id, "rejected")
    await bot.send_message(user_id, "❌ К сожалению, ваша заявка отклонена.")
    await callback.message.edit_text(callback.message.text + "\n\n**[СТАТУС: ✅ ОТКЛОНЕНО]**", parse_mode="Markdown")

async def main():
    init_db()
    logging.basicConfig(level=logging.INFO)
    print("🚀 Бот запущен с обновленным парсером контактов организатора и PDF!")
    asyncio.create_task(prozorro_monitoring_task())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())