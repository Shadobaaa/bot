#!/usr/bin/env python3
import sqlite3
from datetime import datetime, time
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest

# ===== НАСТРОЙКИ (ИЗМЕНИ ПОД СЕБЯ) =====
BOT_TOKEN = '
CHAT_ID = # ID твоей группы

# Фиксированные платежи
RENT = 29000      # аренда
INTERNET = 750    # интернет

# Тарифы
COLD_PRICE = 33.14      # холодная вода за куб
HOT_PRICE = 242.55      # горячая вода за куб
WATER_DISPOSAL = 27.02  # водоотведение за куб
POWER_PRICE = 5.9      # электричество за кВт

# ===== БАЗА ДАННЫХ =====
DB_PATH = Path(__file__).parent / "rent.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY,
            cold REAL,
            hot REAL,
            power REAL,
            date TEXT,
            month TEXT
        )
    ''')

    # Миграция для существующих БД: добавляем колонку month и заполняем из date.
    cursor.execute("PRAGMA table_info(readings)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'month' not in columns:
        cursor.execute("ALTER TABLE readings ADD COLUMN month TEXT")
        cursor.execute("UPDATE readings SET month = strftime('%Y-%m', date) WHERE month IS NULL")

    # Одна запись показаний на месяц — повторный ввод перезаписывает.
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_readings_month ON readings (month)"
    )

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY,
            month TEXT,
            amount REAL,
            paid BOOLEAN
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY,
            month TEXT,
            reminder_type TEXT,
            sent BOOLEAN
        )
    ''')

    conn.commit()
    conn.close()

def get_last():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT cold, hot, power FROM readings ORDER BY id DESC LIMIT 1")
    result = cursor.fetchone()
    conn.close()
    return result if result else (None, None, None)

def save_readings(cold, hot, power):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now_iso = datetime.now().isoformat()
    # За счёт UNIQUE-индекса по month повторный ввод за тот же месяц
    # перезаписывает предыдущую запись вместо создания дубля.
    cursor.execute(
        "INSERT OR REPLACE INTO readings (cold, hot, power, date, month) "
        "VALUES (?, ?, ?, ?, strftime('%Y-%m', ?))",
        (cold, hot, power, now_iso, now_iso)
    )
    conn.commit()
    conn.close()

def mark_paid(month, amount):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO payments (month, amount, paid) VALUES (?, ?, ?)",
        (month, amount, True)
    )
    conn.commit()
    conn.close()

def is_paid(month):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT paid FROM payments WHERE month = ?", (month,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else False

def get_payment_history(limit=5):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT month, amount FROM payments ORDER BY month DESC LIMIT ?", (limit,))
    result = cursor.fetchall()
    conn.close()
    return result

def has_readings_for_month(month):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT date FROM readings WHERE strftime('%Y-%m', date) = ?", (month,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def get_last_readings_for_month(month):
    """Последние показания (cold, hot, power) за конкретный месяц или None."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT cold, hot, power FROM readings WHERE strftime('%Y-%m', date) = ? ORDER BY id DESC LIMIT 1",
        (month,)
    )
    result = cursor.fetchone()
    conn.close()
    return result

def get_prev_month_str(month):
    """Предыдущий месяц в формате YYYY-MM."""
    dt = datetime.strptime(month, "%Y-%m")
    if dt.month == 1:
        return f"{dt.year - 1}-12"
    return f"{dt.year}-{dt.month - 1:02d}"

def was_reminder_sent(month, reminder_type):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT sent FROM reminders WHERE month = ? AND reminder_type = ?", (month, reminder_type))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else False

def mark_reminder_sent(month, reminder_type):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO reminders (month, reminder_type, sent) VALUES (?, ?, ?)",
        (month, reminder_type, True)
    )
    conn.commit()
    conn.close()

# ===== КЛАВИАТУРЫ =====
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 Ввести показания", callback_data="readings")],
        [InlineKeyboardButton("💰 Оплатил", callback_data="pay")],
        [InlineKeyboardButton("📜 История", callback_data="history")],
        [InlineKeyboardButton("ℹ️ Статус", callback_data="status")],
        [InlineKeyboardButton("📞 Тарифы", callback_data="tariffs")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard():
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]]
    return InlineKeyboardMarkup(keyboard)

def get_pay_confirm_keyboard():
    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить оплату", callback_data="pay_confirm")],
        [InlineKeyboardButton("↩️ Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def safe_edit_message(query, text, reply_markup=None, parse_mode='Markdown'):
    try:
        await query.edit_message_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise e

# ===== ОБРАБОТЧИКИ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏠 *Бот учёта квартплаты*\n\n"
        "Что хотите сделать?",
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "readings":
        context.user_data.clear()
        context.user_data['step'] = 'cold'
        await safe_edit_message(
            query,
            "📸 *Шаг 1/3*\n\n"
            "Отправьте *фото счётчика холодной воды*\n"
            "После фото напишите показания цифрами\n\n"
            "Например: 123.4",
            reply_markup=get_cancel_keyboard()
        )

    elif data == "pay":
        await handle_pay(query, context)

    elif data == "pay_confirm":
        await pay_confirm(query, context)

    elif data == "history":
        await show_history(query)
    
    elif data == "status":
        await show_status(query)
    
    elif data == "tariffs":
        await show_tariffs(query)
    
    elif data == "cancel":
        context.user_data.clear()
        await safe_edit_message(
            query,
            "❌ Ввод отменён\n\n"
            "🏠 Главное меню",
            reply_markup=get_main_keyboard()
        )
    
    elif data == "back":
        context.user_data.clear()
        await safe_edit_message(
            query,
            "🏠 Главное меню",
            reply_markup=get_main_keyboard()
        )

async def handle_pay(query, context: ContextTypes.DEFAULT_TYPE):
    current_month = datetime.now().strftime("%Y-%m")

    if is_paid(current_month):
        await safe_edit_message(
            query,
            f"✅ Месяц {current_month} уже оплачен",
            reply_markup=get_main_keyboard()
        )
        return

    last = get_last_readings_for_month(current_month)

    if last is None:
        await safe_edit_message(
            query,
            "❌ Нет показаний за этот месяц\n"
            "Сначала введите показания через меню",
            reply_markup=get_main_keyboard()
        )
        return

    last_cold, last_hot, last_power = last

    prev_month = get_prev_month_str(current_month)
    prev = get_last_readings_for_month(prev_month)
    fixed = RENT + INTERNET

    if prev:
        cold_used = max(0, last_cold - prev[0])
        hot_used = max(0, last_hot - prev[1])
        power_used = max(0, last_power - prev[2])

        cold_cost = cold_used * COLD_PRICE
        hot_cost = hot_used * HOT_PRICE
        disposal_cost = (cold_used + hot_used) * WATER_DISPOSAL
        power_cost = power_used * POWER_PRICE

        total = fixed + cold_cost + hot_cost + disposal_cost + power_cost

        breakdown = (
            f"💧 Холодная: {cold_used:.1f} куб × {COLD_PRICE} = {cold_cost:.2f}\n"
            f"💧 Горячая: {hot_used:.1f} куб × {HOT_PRICE} = {hot_cost:.2f}\n"
            f"🚰 Водоотведение: {cold_used + hot_used:.1f} куб × {WATER_DISPOSAL} = {disposal_cost:.2f}\n"
            f"⚡ Электричество: {power_used:.1f} кВт × {POWER_PRICE} = {power_cost:.2f}"
        )
    else:
        total = fixed
        breakdown = (
            f"📊 Нет показаний за {prev_month}, коммунальные будут рассчитаны со следующего месяца"
        )

    # Сохраняем превью в user_data, чтобы pay_confirm зафиксировал именно эту сумму.
    context.user_data['pay_preview'] = {
        'month': current_month,
        'total': total,
    }

    await safe_edit_message(
        query,
        f"💳 *К оплате за {current_month}:*\n\n"
        f"💰 *Итого: {total:.2f} руб.*\n"
        f"🏠 Аренда+интернет: {fixed:.2f} руб.\n"
        f"{breakdown}\n\n"
        f"Подтвердите оплату после перевода денег:",
        parse_mode='Markdown',
        reply_markup=get_pay_confirm_keyboard()
    )

async def pay_confirm(query, context: ContextTypes.DEFAULT_TYPE):
    preview = context.user_data.get('pay_preview')
    if not preview:
        await safe_edit_message(
            query,
            "❌ Превью оплаты устарело, откройте заново через 'Оплатил'",
            reply_markup=get_main_keyboard()
        )
        return

    month = preview['month']
    total = preview['total']
    context.user_data.pop('pay_preview', None)

    mark_paid(month, total)

    await safe_edit_message(
        query,
        f"✅ *Оплата за {month} принята!*\n\n"
        f"💰 Сумма: {total:.2f} руб.",
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

async def show_history(query):
    history = get_payment_history()
    
    if not history:
        await safe_edit_message(
            query,
            "📜 История платежей пуста",
            reply_markup=get_main_keyboard()
        )
        return
    
    text = "*📜 История платежей:*\n\n"
    for month, amount in history:
        text += f"📅 {month}: {amount:.2f} руб.\n"
    
    await safe_edit_message(
        query,
        text,
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

async def show_status(query):
    current_month = datetime.now().strftime("%Y-%m")
    paid = is_paid(current_month)
    
    last_cold, last_hot, last_power = get_last()
    
    text = f"📅 *Месяц:* {current_month}\n"
    text += f"💰 *Статус:* {'✅ Оплачен' if paid else '❌ Не оплачен'}\n\n"
    
    if last_cold:
        text += f"📊 *Последние показания:*\n"
        text += f"💧 Холодная: {last_cold} куб\n"
        text += f"💧 Горячая: {last_hot} куб\n"
        text += f"⚡ Свет: {last_power} кВт\n"
    else:
        text += "📊 Показания ещё не вводились"
    
    await safe_edit_message(
        query,
        text,
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

async def show_tariffs(query):
    text = (
        "*💰 Текущие тарифы:*\n\n"
        f"🏠 Аренда: {RENT} руб.\n"
        f"🌐 Интернет: {INTERNET} руб.\n"
        f"💧 Холодная вода: {COLD_PRICE} руб/куб\n"
        f"💧 Горячая вода: {HOT_PRICE} руб/куб\n"
        f"🚰 Водоотведение: {WATER_DISPOSAL} руб/куб\n"
        f"⚡ Электричество: {POWER_PRICE} руб/кВт"
    )
    await safe_edit_message(
        query,
        text,
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('step')
    
    if not step:
        await update.message.reply_text(
            "❓ Используйте кнопку 'Ввести показания' в меню",
            reply_markup=get_main_keyboard()
        )
        return
    
    if step == 'cold':
        context.user_data['step'] = 'cold_value'
        await update.message.reply_text(
            "✅ Фото получено\n\n"
            "📝 Введите *показания холодной воды* (например: 123.4):",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )
    
    elif step == 'hot':
        context.user_data['step'] = 'hot_value'
        await update.message.reply_text(
            "✅ Фото получено\n\n"
            "📝 Введите *показания горячей воды* (например: 56.7):",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )
    
    elif step == 'power':
        context.user_data['step'] = 'power_value'
        await update.message.reply_text(
            "✅ Фото получено\n\n"
            "📝 Введите *показания электричества* (например: 150):",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('step')
    
    if not step:
        await update.message.reply_text(
            "❓ Используйте кнопки меню",
            reply_markup=get_main_keyboard()
        )
        return
    
    try:
        value = float(update.message.text.replace(',', '.'))
    except:
        await update.message.reply_text(
            "❌ Введите число (например: 123.4)",
            reply_markup=get_cancel_keyboard()
        )
        return
    
    if step == 'cold_value':
        last = get_last()
        if last and last[0] is not None and value < last[0]:
            await update.message.reply_text(
                f"❌ Показания ({value}) меньше предыдущих ({last[0]})\n"
                f"Введите значение не меньше {last[0]} или нажмите 'Отмена'",
                reply_markup=get_cancel_keyboard()
            )
            return
        context.user_data['cold'] = value
        context.user_data['step'] = 'hot'
        await update.message.reply_text(
            f"💧 Холодная вода: {value} куб\n\n"
            "✅ Принято!\n\n"
            "📸 *Шаг 2/3*\nОтправьте *фото счётчика горячей воды*",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )

    elif step == 'hot_value':
        last = get_last()
        if last and last[1] is not None and value < last[1]:
            await update.message.reply_text(
                f"❌ Показания ({value}) меньше предыдущих ({last[1]})\n"
                f"Введите значение не меньше {last[1]} или нажмите 'Отмена'",
                reply_markup=get_cancel_keyboard()
            )
            return
        context.user_data['hot'] = value
        context.user_data['step'] = 'power'
        await update.message.reply_text(
            f"💧 Горячая вода: {value} куб\n\n"
            "✅ Принято!\n\n"
            "📸 *Шаг 3/3*\nОтправьте *фото электросчётчика*",
            parse_mode='Markdown',
            reply_markup=get_cancel_keyboard()
        )

    elif step == 'power_value':
        last = get_last()
        if last and last[2] is not None and value < last[2]:
            await update.message.reply_text(
                f"❌ Показания ({value}) меньше предыдущих ({last[2]})\n"
                f"Введите значение не меньше {last[2]} или нажмите 'Отмена'",
                reply_markup=get_cancel_keyboard()
            )
            return
        power = value
        cold = context.user_data.get('cold')
        hot = context.user_data.get('hot')

        save_readings(cold, hot, power)
        context.user_data.clear()

        current_month = datetime.now().strftime("%Y-%m")
        prev_month = get_prev_month_str(current_month)
        prev = get_last_readings_for_month(prev_month)
        fixed = RENT + INTERNET

        if prev:
            cold_used = max(0, cold - prev[0])
            hot_used = max(0, hot - prev[1])
            power_used = max(0, power - prev[2])

            cold_cost = cold_used * COLD_PRICE
            hot_cost = hot_used * HOT_PRICE
            disposal_cost = (cold_used + hot_used) * WATER_DISPOSAL
            power_cost = power_used * POWER_PRICE

            total = fixed + cold_cost + hot_cost + disposal_cost + power_cost

            await update.message.reply_text(
                f"✅ *Показания сохранены!*\n\n"
                f"💰 *Сумма к оплате: {total:.2f} руб.*\n\n"
                f"🏠 Аренда+интернет: {fixed:.2f} руб.\n"
                f"💧 Холодная: {cold_used:.1f} куб × {COLD_PRICE} = {cold_cost:.2f}\n"
                f"💧 Горячая: {hot_used:.1f} куб × {HOT_PRICE} = {hot_cost:.2f}\n"
                f"🚰 Водоотведение: {cold_used + hot_used:.1f} куб × {WATER_DISPOSAL} = {disposal_cost:.2f}\n"
                f"⚡ Электричество: {power_used:.1f} кВт × {POWER_PRICE} = {power_cost:.2f}\n\n"
                f"Нажмите кнопку 'Оплатил' после перевода денег",
                parse_mode='Markdown',
                reply_markup=get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                f"✅ *Показания сохранены!*\n\n"
                f"🏠 Аренда+интернет: {fixed:.2f} руб.\n\n"
                f"📊 Нет показаний за {prev_month}, расход воды и света будет рассчитан со следующего месяца\n\n"
                f"Нажмите кнопку 'Оплатил' после перевода денег",
                parse_mode='Markdown',
                reply_markup=get_main_keyboard()
            )

# ===== НАПОМИНАНИЯ =====
async def send_morning_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Утреннее напоминание в 10:00"""
    now = datetime.now()
    current_month = now.strftime("%Y-%m")
    day = now.day

    # Только 18-го числа
    if day == 18:
        if not was_reminder_sent(current_month, 'morning'):
            has_readings = has_readings_for_month(current_month)

            if not has_readings:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text="🔔 *НАПОМИНАНИЕ* 🔔\n\n"
                         "Сегодня 18 число!\n\n"
                         "📸 Отправьте показания счётчиков через кнопку 'Ввести показания'\n\n"
                         "После расчёта суммы нажмите кнопку 'Оплатил'",
                    parse_mode='Markdown',
                    reply_markup=get_main_keyboard()
                )
                mark_reminder_sent(current_month, 'morning')

async def send_evening_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Вечернее напоминание в 18:00"""
    now = datetime.now()
    current_month = now.strftime("%Y-%m")
    day = now.day

    # Только 18-го числа
    if day == 18:
        if not was_reminder_sent(current_month, 'evening'):
            has_readings = has_readings_for_month(current_month)

            if not has_readings:
                await context.bot.send_message(
                    chat_id=CHAT_ID,
                    text="🔔 *ПОВТОРНОЕ НАПОМИНАНИЕ* 🔔\n\n"
                         "Сегодня 18 число, уже вечер!\n\n"
                         "📸 Не забудьте отправить показания счётчиков через кнопку 'Ввести показания'\n\n"
                         "💰 После расчёта суммы нажмите 'Оплатил'",
                    parse_mode='Markdown',
                    reply_markup=get_main_keyboard()
                )
                mark_reminder_sent(current_month, 'evening')

# ===== ЗАПУСК =====
def main():
    init_db()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    
    # Кнопки
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Фото и текст
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    
    # Напоминания (только 7-го числа)
    job_queue = app.job_queue
    if job_queue:
        # Утром в 10:00
        job_queue.run_daily(
            send_morning_reminder,
            time=time(hour=10, minute=0)
        )
        # Вечером в 18:00
        job_queue.run_daily(
            send_evening_reminder,
            time=time(hour=18, minute=0)
        )
        print("⏰ Напоминания настроены:")
        print("   - 18-го числа в 10:00 и 18:00 (о показаниях)")
    
    print("🤖 Бот запущен!")
    print(f"📁 БД: {DB_PATH}")
    print("✅ Готов к работе!")
    
    app.run_polling()

if __name__ == '__main__':
    main()
