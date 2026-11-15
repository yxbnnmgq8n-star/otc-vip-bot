import os
import re
import uuid
import sqlite3
import logging
from html import escape

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

# BotHost configuration:
# Set BOT_TOKEN and ADMIN_ID in the hosting panel's Environment Variables.
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "6308550657"))
SUPPORT_USERNAME = "otcmanagerr"
DB_PATH = os.getenv("DB_PATH", "otc_vip.sqlite3")
PHOTO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "otc.jpg")

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Add a NEW token in BotHost -> Environment Variables."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("otc_vip")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
states = {}

class DealState:
    SET_AMOUNT = "set_amount"
    SET_DESCRIPTION = "set_description"

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deals (
                deal_id TEXT PRIMARY KEY,
                seller_id INTEGER NOT NULL,
                buyer_id INTEGER,
                amount TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'created',
                seller_confirmed INTEGER NOT NULL DEFAULT 0,
                buyer_confirmed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

def register_user(user):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users(user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
        """, (user.id, user.username, user.first_name))

def username(user_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, first_name FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()
    if not row:
        return f"ID {user_id}"
    if row["username"]:
        return "@" + escape(row["username"])
    return escape(row["first_name"] or f"ID {user_id}")

def main_menu():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📄 Создать сделку", callback_data="create_deal"))
    kb.add(InlineKeyboardButton("📋 Мои сделки", callback_data="my_deals"))
    kb.add(InlineKeyboardButton("📞 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}"))
    return kb

def back_menu():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔙 Вернуться в меню", callback_data="main_menu"))
    return kb

def send_menu(chat_id, message_id=None):
    text = (
        "<b>Добро пожаловать в ELF OTC</b>\n\n"
        "Бот помогает создавать записи о договорённостях и отслеживать подтверждения "
        "участников.\n\n"
        "⚠️ Бот не принимает и не хранит деньги, не проверяет платежи и не является "
        "гарантом сделки. Не отправляйте деньги или подарки администратору/посредникам "
        "по просьбе бота. Самостоятельно проверяйте контрагента и условия."
    )
    if message_id:
        try:
            bot.edit_message_caption(
                caption=text, chat_id=chat_id, message_id=message_id,
                reply_markup=main_menu(), parse_mode="HTML"
            )
            return
        except Exception:
            try:
                bot.edit_message_text(
                    text, chat_id, message_id,
                    reply_markup=main_menu(), parse_mode="HTML"
                )
                return
            except Exception:
                pass
    if os.path.isfile(PHOTO_PATH) and os.path.getsize(PHOTO_PATH) > 0:
        try:
            with open(PHOTO_PATH, "rb") as photo:
                bot.send_photo(chat_id, photo, caption=text,
                               reply_markup=main_menu(), parse_mode="HTML")
            return
        except Exception:
            logger.exception("Could not send menu image; sending text instead")
    bot.send_message(chat_id, text, reply_markup=main_menu(), parse_mode="HTML")

def deal_link(deal_id):
    me = bot.get_me()
    return f"https://t.me/{me.username}?start={deal_id}"

@bot.message_handler(commands=["start"])
def start(message):
    register_user(message.from_user)
    args = message.text.split(maxsplit=1)
    if len(args) == 2:
        deal_id = args[1].strip()
        with get_db() as conn:
            deal = conn.execute(
                "SELECT * FROM deals WHERE deal_id=?", (deal_id,)
            ).fetchone()
            if (deal and deal["seller_id"] != message.from_user.id
                    and deal["buyer_id"] is None and deal["status"] == "created"):
                conn.execute(
                    "UPDATE deals SET buyer_id=?, status='joined' "
                    "WHERE deal_id=? AND buyer_id IS NULL",
                    (message.from_user.id, deal_id)
                )
                seller_id = deal["seller_id"]
            else:
                deal = None
                seller_id = None
        if deal:
            bot.send_message(
                seller_id,
                f"👤 Пользователь {username(message.from_user.id)} присоединился "
                f"к записи сделки <code>{escape(deal_id)}</code>."
            )
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton(
                "✅ Подтвердить завершение", callback_data=f"confirm_{deal_id}"
            ))
            kb.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
            bot.send_message(
                message.chat.id,
                "<b>Вы присоединились к записи сделки.</b>\n\n"
                f"Описание: {escape(deal['description'])}\n"
                f"Условия/сумма: {escape(deal['amount'])}\n\n"
                "Это только запись договорённости. Бот не подтверждает оплату "
                "или передачу товара.",
                reply_markup=kb
            )
            return
    send_menu(message.chat.id)

@bot.message_handler(commands=["admin"])
def admin(message):
    register_user(message.from_user)
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "❌ У вас нет прав администратора.")
        return
    with get_db() as conn:
        users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        deals_count = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM deals WHERE status IN ('created','joined')"
        ).fetchone()[0]
    bot.send_message(
        message.chat.id,
        "<b>Админ-панель</b>\n\n"
        f"Пользователей: {users_count}\n"
        f"Всего записей: {deals_count}\n"
        f"Активных записей: {active_count}"
    )

@bot.message_handler(commands=["cancel"])
def cancel(message):
    states.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "Ввод отменён.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    register_user(call.from_user)
    uid = call.from_user.id
    try:
        if call.data == "main_menu":
            send_menu(call.message.chat.id, call.message.message_id)

        elif call.data == "create_deal":
            states[uid] = {"step": DealState.SET_AMOUNT}
            bot.send_message(
                call.message.chat.id,
                "Введите сумму/условия одной строкой, например: <code>100 USDT</code>.\n"
                "Это информационное поле, бот не проводит оплату. Для отмены: /cancel"
            )

        elif call.data == "my_deals":
            with get_db() as conn:
                rows = conn.execute(
                    "SELECT deal_id, amount, description, status FROM deals "
                    "WHERE seller_id=? OR buyer_id=? ORDER BY created_at DESC LIMIT 10",
                    (uid, uid)
                ).fetchall()
            if not rows:
                bot.send_message(call.message.chat.id, "У вас пока нет сделок.", reply_markup=back_menu())
            else:
                lines = ["<b>Ваши последние записи:</b>"]
                for row in rows:
                    lines.append(
                        f"\nID: <code>{escape(row['deal_id'])}</code>\n"
                        f"Условия: {escape(row['amount'])}\n"
                        f"Описание: {escape(row['description'])}\n"
                        f"Статус: {escape(row['status'])}"
                    )
                bot.send_message(call.message.chat.id, "\n".join(lines), reply_markup=back_menu())

        elif call.data.startswith("confirm_"):
            deal_id = call.data[len("confirm_"):]
            with get_db() as conn:
                deal = conn.execute(
                    "SELECT * FROM deals WHERE deal_id=?", (deal_id,)
                ).fetchone()
                if not deal or uid not in (deal["seller_id"], deal["buyer_id"]):
                    bot.answer_callback_query(call.id, "Нет доступа к этой записи.", show_alert=True)
                    return
                if uid == deal["seller_id"]:
                    conn.execute("UPDATE deals SET seller_confirmed=1 WHERE deal_id=?", (deal_id,))
                else:
                    conn.execute("UPDATE deals SET buyer_confirmed=1 WHERE deal_id=?", (deal_id,))
                updated = conn.execute(
                    "SELECT * FROM deals WHERE deal_id=?", (deal_id,)
                ).fetchone()
                completed = bool(updated["seller_confirmed"] and updated["buyer_confirmed"])
                if completed:
                    conn.execute("UPDATE deals SET status='completed' WHERE deal_id=?", (deal_id,))
            bot.answer_callback_query(call.id, "Подтверждение сохранено.")
            if completed:
                result = (
                    f"Запись сделки <code>{escape(deal_id)}</code> закрыта: "
                    "обе стороны отметили её завершённой. Это не подтверждение платежа."
                )
                bot.send_message(deal["seller_id"], result)
                if deal["buyer_id"]:
                    bot.send_message(deal["buyer_id"], result)
            else:
                bot.send_message(
                    call.message.chat.id,
                    "Ваша отметка сохранена. Ожидается подтверждение второй стороны. "
                    "Это не подтверждает оплату."
                )
        else:
            bot.answer_callback_query(call.id)
    except Exception:
        logger.exception("Callback error")
        try:
            bot.answer_callback_query(call.id, "Произошла ошибка.", show_alert=True)
        except Exception:
            pass

@bot.message_handler(content_types=["text"])
def handle_text(message):
    uid = message.from_user.id
    register_user(message.from_user)
    state = states.get(uid)
    if not state:
        return

    if state["step"] == DealState.SET_AMOUNT:
        value = message.text.strip()
        if not value or len(value) > 100:
            bot.reply_to(message, "Введите условия длиной до 100 символов или /cancel.")
            return
        state["amount"] = value
        state["step"] = DealState.SET_DESCRIPTION
        bot.reply_to(message, "Теперь кратко опишите предмет договорённости (до 700 символов).")
        return

    if state["step"] == DealState.SET_DESCRIPTION:
        description = message.text.strip()
        if not description or len(description) > 700:
            bot.reply_to(message, "Описание должно быть длиной от 1 до 700 символов.")
            return
        deal_id = str(uuid.uuid4())
        with get_db() as conn:
            conn.execute(
                "INSERT INTO deals (deal_id,seller_id,amount,description,status) "
                "VALUES (?,?,?,?, 'created')",
                (deal_id, uid, state["amount"], description)
            )
        states.pop(uid, None)
        link = deal_link(deal_id)
        bot.send_message(
            message.chat.id,
            "<b>✅ Запись создана.</b>\n\n"
            f"ID: <code>{escape(deal_id)}</code>\n"
            f"Условия/сумма: {escape(state['amount'])}\n"
            f"Описание: {escape(description)}\n\n"
            f"Ссылка для второго участника:\n{link}\n\n"
            "Помните: бот только фиксирует договорённость и не гарантирует исполнение.",
            reply_markup=back_menu(),
            disable_web_page_preview=True
        )

if __name__ == "__main__":
    init_db()
    logger.info("Starting bot. Admin ID: %s", ADMIN_ID)
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
