"""ELF OTC Telegram bot.

Configure TELEGRAM_BOT_TOKEN and BOT_ADMIN_IDS before starting the bot.
Runtime state is saved to state.json beside this file.
"""

import html
import json
import math
import os
import re
import threading
import uuid
from pathlib import Path

import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto


BASE_DIR = Path(__file__).resolve().parent
PHOTO_PATH = BASE_DIR / "otc.jpg"
STATE_PATH = BASE_DIR / "state.json"
SUPPORT_USERNAME = "otcmanagerr"
GIFT_RECIPIENT = "@otcmanagerr"


def load_local_env():
    """Load optional key=value settings from a private .env file."""
    env_path = BASE_DIR / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


load_local_env()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError(
        "Не задан TELEGRAM_BOT_TOKEN. Установите переменную окружения с токеном бота."
    )

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=False)
state_lock = threading.RLock()

users = {}
deals = {}
admins = set()


def _configured_admins():
    values = os.getenv("BOT_ADMIN_IDS", "")
    result = set()
    for value in values.split(","):
        value = value.strip()
        if value:
            try:
                admin_id = int(value)
            except ValueError as exc:
                raise RuntimeError("BOT_ADMIN_IDS должен содержать числовые Telegram ID через запятую") from exc
            if admin_id > 0:
                result.add(admin_id)
    return result


admins.update(_configured_admins())
if not admins:
    raise RuntimeError("Не задан BOT_ADMIN_IDS. Укажите хотя бы один Telegram ID администратора.")


def save_state():
    """Atomically persist users, deals, and administrator IDs."""
    with state_lock:
        payload = {
            "users": users,
            "deals": deals,
            "admins": sorted(admins),
        }
        temporary_path = STATE_PATH.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        temporary_path.replace(STATE_PATH)


def load_state():
    if not STATE_PATH.exists():
        return
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("корневое значение должно быть объектом")
        loaded_users = payload.get("users", {})
        loaded_deals = payload.get("deals", {})
        loaded_admins = payload.get("admins", [])
        if not isinstance(loaded_users, dict) or not isinstance(loaded_deals, dict):
            raise ValueError("повреждён раздел пользователей или сделок")
        users.update({int(user_id): value for user_id, value in loaded_users.items()})
        deals.update(loaded_deals)
        admins.update(int(admin_id) for admin_id in loaded_admins)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось загрузить файл состояния {STATE_PATH}: {exc}") from exc


load_state()


class DealState:
    SET_AMOUNT = 1
    SET_DESCRIPTION = 2


def init_user(user_id, username=None):
    """Create a local profile without making a fragile Bot API getChat call."""
    with state_lock:
        user = users.get(user_id)
        changed = False
        if user is None:
            user = {
                "username": username,
                "ton_wallet": "-",
                "card_details": "-",
                "lang": "ru",
                "success_deals": 0,
                "balance": {"TON": 0.0, "RUB": 0.0, "STARS": 0.0},
                "referral_id": str(user_id),
                "referred_by": None,
                "deal_state": None,
                "current_deal": None,
                "awaiting_input": None,
                "awaiting_admin_id": False,
                "awaiting_fake_deals": False,
                "awaiting_fake_balance": False,
            }
            users[user_id] = user
            changed = True
        elif username and user.get("username") != username:
            user["username"] = username
            changed = True
        if changed:
            save_state()
        return user


def user_label(user_id):
    user = users.get(user_id, {})
    username = user.get("username")
    return f"@{html.escape(username)}" if username else f"ID {user_id}"


def main_menu(user_id):
    lang = users[user_id].get("lang", "ru")
    keyboard = InlineKeyboardMarkup()
    if lang == "en":
        keyboard.add(InlineKeyboardButton("🪙 Payment details", callback_data="wallet_menu"))
        keyboard.add(InlineKeyboardButton("📄 Create a deal", callback_data="create_deal"))
        keyboard.add(InlineKeyboardButton("🧷 Referral link", callback_data="referral"))
        keyboard.add(InlineKeyboardButton("🌐 Language", callback_data="change_lang"))
        keyboard.add(InlineKeyboardButton("📞 Support", url=f"https://t.me/{SUPPORT_USERNAME}"))
    else:
        keyboard.add(InlineKeyboardButton("🪙 Добавить/изменить кошелёк", callback_data="wallet_menu"))
        keyboard.add(InlineKeyboardButton("📄 Создать сделку", callback_data="create_deal"))
        keyboard.add(InlineKeyboardButton("🧷 Реферальная ссылка", callback_data="referral"))
        keyboard.add(InlineKeyboardButton("🌐 Сменить язык", callback_data="change_lang"))
        keyboard.add(InlineKeyboardButton("📞 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}"))
    return keyboard


def welcome_text(user_id):
    if users[user_id].get("lang") == "en":
        return (
            "<b>Welcome to ELF OTC</b>\n\n"
            "Create a deal and share its link with the other participant.\n\n"
            "Choose an option below:"
        )
    return (
        "<b>Добро пожаловать в ELF OTC</b>\n\n"
        "Создайте сделку и отправьте ссылку второму участнику.\n\n"
        "Выберите нужный раздел ниже:"
    )


def send_photo_message(chat_id, message_id, text, reply_markup=None):
    """Send or replace a photo message, with a text-only fallback."""
    has_photo = PHOTO_PATH.is_file() and PHOTO_PATH.stat().st_size > 0
    if has_photo:
        try:
            with PHOTO_PATH.open("rb") as photo:
                if message_id:
                    bot.edit_message_media(
                        chat_id=chat_id,
                        message_id=message_id,
                        media=InputMediaPhoto(photo, caption=text, parse_mode="HTML"),
                        reply_markup=reply_markup,
                    )
                else:
                    bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=text,
                        reply_markup=reply_markup,
                    )
            return
        except Exception as exc:
            print(f"Не удалось отправить фото, отправляю текст: {exc}")

    if message_id:
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=reply_markup)


def cancel_draft(user):
    if isinstance(user.get("current_deal"), dict):
        user["current_deal"] = None
        user["deal_state"] = None
    user["awaiting_input"] = None


def parse_start_payload(message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        return
    payload = parts[1].strip()
    user_id = message.from_user.id
    try:
        deal_id = str(uuid.UUID(payload))
    except (ValueError, AttributeError):
        deal_id = None

    if deal_id:
        with state_lock:
            deal = deals.get(deal_id)
            if not deal:
                return
            if deal["seller_id"] == user_id:
                send_photo_message(
                    message.chat.id, None,
                    "❌ Нельзя присоединиться к собственной сделке.",
                    main_menu(user_id),
                )
                return
            if deal.get("status") not in ("created", "joined"):
                send_photo_message(
                    message.chat.id, None,
                    "❌ Эта сделка уже закрыта или оплачена.",
                    main_menu(user_id),
                )
                return
            if deal.get("buyer_id") not in (None, user_id):
                send_photo_message(
                    message.chat.id, None,
                    "❌ К сделке уже присоединился другой покупатель.",
                    main_menu(user_id),
                )
                return
            deal["buyer_id"] = user_id
            deal["status"] = "joined"
            users[user_id]["current_deal"] = deal_id
            save_state()

        seller_id = deal["seller_id"]
        seller_text = (
            f"<b>👤 Покупатель {user_label(user_id)} присоединился к сделке #{deal_id}</b>\n"
            f"• Успешные сделки: {users[user_id]['success_deals']}\n\n"
            "Проверьте, что это ожидаемый вами пользователь."
        )
        seller_keyboard = InlineKeyboardMarkup()
        seller_keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(seller_id, None, seller_text, seller_keyboard)

        currency = deal["currency"]
        if currency == "TON":
            payment_details = f"🏦 Адрес для оплаты (TON): {html.escape(users[seller_id]['ton_wallet'])}"
        elif currency == "RUB":
            payment_details = f"💳 Реквизиты для оплаты: {html.escape(users[seller_id]['card_details'])}"
        else:
            payment_details = "🌟 Оплата Telegram Stars не подключена. Свяжитесь с поддержкой для оформления оплаты."
        buyer_text = (
            f"<b>💳 Сделка #{deal_id}</b>\n\n"
            "👤 Вы покупатель.\n"
            f"📌 Продавец: {user_label(seller_id)}\n"
            f"• Успешные сделки: {users[seller_id]['success_deals']}\n\n"
            f"• Предложение: {html.escape(deal['description'])}\n\n"
            f"{payment_details}\n"
            f"\n💰 Сумма: {deal['amount']:g} {currency}\n"
            f"📝 Мемо платежа: <code>{deal_id}</code>\n\n"
            "Внешние переводы автоматически не проверяются: после оплаты нажмите «Я оплатил переводом» "
            "и дождитесь ручной проверки администратора."
        )
        buyer_keyboard = InlineKeyboardMarkup()
        buyer_keyboard.add(InlineKeyboardButton("💸 Оплатить с баланса", callback_data="pay_from_balance"))
        if currency in {"TON", "RUB"}:
            buyer_keyboard.add(
                InlineKeyboardButton("✅ Я оплатил переводом", callback_data=f"external_paid_{deal_id}")
            )
        buyer_keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(user_id, None, buyer_text, buyer_keyboard)
        return

    try:
        referrer_id = int(payload)
    except ValueError:
        return
    if referrer_id != user_id and referrer_id in users and not users[user_id].get("referred_by"):
        users[user_id]["referred_by"] = referrer_id
        save_state()


@bot.message_handler(commands=["start"])
def handle_start(message):
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    parse_start_payload(message)
    send_photo_message(message.chat.id, None, welcome_text(user_id), main_menu(user_id))


@bot.message_handler(commands=["admin"])
def handle_admin(message):
    user_id = message.from_user.id
    init_user(user_id, message.from_user.username)
    if user_id not in admins:
        bot.reply_to(message, "❌ У вас нет прав администратора")
        return
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("✏️ Выдать админку", callback_data="add_admin"))
    keyboard.add(InlineKeyboardButton("📊 Статистика бота", callback_data="stats"))
    keyboard.add(InlineKeyboardButton("💼 Изменить счётчик сделок", callback_data="fake_deals"))
    keyboard.add(InlineKeyboardButton("💸 Изменить тестовый баланс", callback_data="fake_balance"))
    keyboard.add(InlineKeyboardButton("❌ Закрыть панель", callback_data="close_admin"))
    bot.send_message(message.chat.id, "<b>Админ-панель</b>", reply_markup=keyboard)


@bot.message_handler(commands=["balance"])
def handle_balance(message):
    user_id = message.from_user.id
    user = init_user(user_id, message.from_user.username)
    text = (
        f"👤 {user_label(user_id)}, ваш баланс:\n\n"
        f"💎 TON: {user['balance']['TON']:g}\n"
        f"💳 RUB: {user['balance']['RUB']:g}\n"
        f"⭐️ STARS: {user['balance']['STARS']:g}\n\n"
        f"💼 Успешных сделок: {user['success_deals']}"
    )
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
    bot.send_message(message.chat.id, text, reply_markup=keyboard)


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    username = call.from_user.username
    user = init_user(user_id, username)
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data or ""
    admin_actions = {"add_admin", "stats", "fake_deals", "fake_balance", "close_admin"}
    is_external_review = data.startswith(("external_approve_", "external_reject_"))

    if (data in admin_actions or is_external_review) and user_id not in admins:
        bot.answer_callback_query(call.id, "❌ Нет прав администратора", show_alert=True)
        return
    bot.answer_callback_query(call.id)

    if data == "main_menu":
        cancel_draft(user)
        save_state()
        send_photo_message(chat_id, message_id, welcome_text(user_id), main_menu(user_id))

    elif data == "wallet_menu":
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("💼 TON-кошелёк", callback_data="set_ton"))
        keyboard.add(InlineKeyboardButton("💳 Реквизиты карты", callback_data="set_card"))
        keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(chat_id, message_id, "<b>Выберите способ оплаты:</b>", keyboard)

    elif data in ("set_ton", "set_card"):
        user["awaiting_input"] = "ton" if data == "set_ton" else "card"
        save_state()
        if data == "set_ton":
            text = (
                f"<b>Ваш TON-кошелёк:</b> {html.escape(user['ton_wallet'])}\n\n"
                "Отправьте адрес TON-кошелька (EQ…, UQ…, kQ…, 0Q… или raw-адрес)."
            )
        else:
            text = (
                f"<b>Ваши реквизиты:</b> {html.escape(user['card_details'])}\n\n"
                "Отправьте реквизиты, например: Банк — номер карты."
            )
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(chat_id, message_id, text, keyboard)

    elif data == "create_deal":
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("💎 TON", callback_data="method_TON"))
        keyboard.add(InlineKeyboardButton("💳 Карта (РФ)", callback_data="method_RUB"))
        keyboard.add(InlineKeyboardButton("⭐ Stars", callback_data="method_STARS"))
        keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(chat_id, message_id, "<b>Выберите валюту сделки:</b>", keyboard)

    elif data.startswith("method_"):
        currency = data.removeprefix("method_")
        if currency not in {"TON", "RUB", "STARS"}:
            return
        if currency == "STARS":
            keyboard = InlineKeyboardMarkup()
            keyboard.add(InlineKeyboardButton("📞 Поддержка", url=f"https://t.me/{SUPPORT_USERNAME}"))
            keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
            send_photo_message(
                chat_id, message_id,
                "❌ Создание сделок с Telegram Stars отключено: платёжная интеграция не настроена.",
                keyboard,
            )
            return
        if currency == "TON" and user["ton_wallet"] == "-":
            send_photo_message(chat_id, message_id, "Сначала добавьте TON-кошелёк в меню реквизитов.", None)
            return
        if currency == "RUB" and user["card_details"] == "-":
            send_photo_message(chat_id, message_id, "Сначала добавьте реквизиты карты в меню реквизитов.", None)
            return
        user["deal_state"] = DealState.SET_AMOUNT
        user["current_deal"] = {"currency": currency, "seller_id": user_id}
        save_state()
        text = "<b>Создание сделки</b>\n\nВведите положительную сумму, например <code>100.5</code>."
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔙 Отмена", callback_data="main_menu"))
        send_photo_message(chat_id, message_id, text, keyboard)

    elif data == "referral":
        link = f"https://t.me/{bot.get_me().username}?start={user['referral_id']}"
        count = sum(1 for item in users.values() if item.get("referred_by") == user_id)
        text = (
            f"<b>🧷 Ваша ссылка:</b>\n{html.escape(link)}\n\n"
            f"Приглашено пользователей: {count}."
        )
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(chat_id, message_id, text, keyboard)

    elif data == "change_lang":
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🇺🇸 English", callback_data="lang_en"))
        keyboard.add(InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"))
        keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(chat_id, message_id, "<b>🌐 Choose your language / Выберите язык:</b>", keyboard)

    elif data in ("lang_en", "lang_ru"):
        user["lang"] = "en" if data == "lang_en" else "ru"
        save_state()
        send_photo_message(chat_id, message_id, welcome_text(user_id), main_menu(user_id))

    elif data == "add_admin":
        user["awaiting_admin_id"] = True
        save_state()
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("❌ Закрыть", callback_data="close_admin"))
        bot.edit_message_text("<b>Введите Telegram ID нового администратора:</b>", chat_id, message_id, reply_markup=keyboard)

    elif data == "stats":
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("❌ Закрыть", callback_data="close_admin"))
        text = (
            f"<b>📊 Статистика бота</b>\n\nПользователей: {len(users)}\n"
            f"Администраторов: {len(admins)}\nСделок: {len(deals)}"
        )
        bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard)

    elif data in ("fake_deals", "fake_balance"):
        user["awaiting_fake_deals"] = data == "fake_deals"
        user["awaiting_fake_balance"] = data == "fake_balance"
        save_state()
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("❌ Закрыть", callback_data="close_admin"))
        if data == "fake_deals":
            text = "Введите ID и положительное количество сделок. Пример: <code>12345678 2</code>"
        else:
            text = "Введите ID, положительную сумму и валюту. Пример: <code>12345678 15 TON</code>"
        bot.edit_message_text(text, chat_id, message_id, reply_markup=keyboard)

    elif data == "close_admin":
        user["awaiting_admin_id"] = False
        user["awaiting_fake_deals"] = False
        user["awaiting_fake_balance"] = False
        save_state()
        bot.delete_message(chat_id, message_id)

    elif data.startswith("external_paid_"):
        deal_id = data.removeprefix("external_paid_")
        with state_lock:
            deal = deals.get(deal_id)
            if (
                not deal
                or deal.get("buyer_id") != user_id
                or deal.get("status") != "joined"
                or deal.get("currency") not in {"TON", "RUB"}
            ):
                bot.send_message(chat_id, "❌ Нельзя отправить эту сделку на проверку оплаты.")
                return
            deal["status"] = "payment_review"
            save_state()

        admin_text = (
            "<b>🔎 Проверьте внешний перевод</b>\n\n"
            f"Сделка: <code>{deal_id}</code>\n"
            f"Покупатель: {user_label(user_id)}\n"
            f"Продавец: {user_label(deal['seller_id'])}\n"
            f"Сумма: {deal['amount']:g} {deal['currency']}\n"
            f"Описание: {html.escape(deal['description'])}\n\n"
            "Сверьте перевод вручную перед подтверждением."
        )
        review_keyboard = InlineKeyboardMarkup()
        review_keyboard.add(
            InlineKeyboardButton("✅ Перевод получен", callback_data=f"external_approve_{deal_id}")
        )
        review_keyboard.add(
            InlineKeyboardButton("❌ Не получен", callback_data=f"external_reject_{deal_id}")
        )
        notified = False
        for admin_id in tuple(admins):
            try:
                bot.send_message(admin_id, admin_text, reply_markup=review_keyboard)
                notified = True
            except Exception as exc:
                print(f"Не удалось уведомить администратора {admin_id}: {exc}")
        if not notified:
            with state_lock:
                if deals.get(deal_id, {}).get("status") == "payment_review":
                    deals[deal_id]["status"] = "joined"
                    save_state()
            bot.send_message(
                chat_id,
                "❌ Не удалось уведомить администраторов. Попросите их открыть бота через /start и повторите.",
            )
            return
        bot.send_message(chat_id, "⏳ Перевод отправлен администраторам на ручную проверку.")

    elif data.startswith(("external_approve_", "external_reject_")):
        approved = data.startswith("external_approve_")
        deal_id = data.removeprefix("external_approve_" if approved else "external_reject_")
        with state_lock:
            deal = deals.get(deal_id)
            if not deal or deal.get("status") != "payment_review":
                bot.send_message(chat_id, "❌ Запрос оплаты уже обработан или сделка не найдена.")
                return
            deal["status"] = "paid" if approved else "joined"
            save_state()
        if approved:
            bot.send_message(
                deal["buyer_id"],
                f"✅ Администратор подтвердил внешний перевод по сделке <code>{deal_id}</code>.",
            )
            seller_keyboard = InlineKeyboardMarkup()
            seller_keyboard.add(
                InlineKeyboardButton(f"🎁 Подарок отправлен {GIFT_RECIPIENT}", callback_data=f"confirm_send_{deal_id}")
            )
            bot.send_message(
                deal["seller_id"],
                f"✅ Перевод по сделке <code>{deal_id}</code> проверен администратором.\n"
                f"Описание: {html.escape(deal['description'])}\n"
                f"🎁 Отправьте подарок пользователю {GIFT_RECIPIENT}.",
                reply_markup=seller_keyboard,
            )
            bot.send_message(chat_id, f"✅ Оплата по сделке {deal_id} подтверждена.")
        else:
            bot.send_message(
                deal["buyer_id"],
                f"❌ Перевод по сделке <code>{deal_id}</code> пока не подтверждён. "
                "Свяжитесь с поддержкой, если перевод уже выполнен.",
            )
            bot.send_message(chat_id, f"❌ Оплата по сделке {deal_id} не подтверждена.")

    elif data == "pay_from_balance":
        deal_id = user.get("current_deal")
        if not isinstance(deal_id, str):
            bot.send_message(chat_id, "❌ У вас нет активной сделки.")
            return
        with state_lock:
            deal = deals.get(deal_id)
            if not deal or deal.get("buyer_id") != user_id:
                bot.send_message(chat_id, "❌ Сделка не найдена или вы не её покупатель.")
                return
            if deal.get("status") != "joined":
                bot.send_message(chat_id, "❌ Эта сделка уже оплачена или закрыта.")
                return
            currency = deal["currency"]
            if user["balance"].get(currency, 0) < deal["amount"]:
                bot.send_message(chat_id, "❌ Недостаточно средств на балансе.")
                return
            user["balance"][currency] -= deal["amount"]
            users[deal["seller_id"]]["balance"][currency] += deal["amount"]
            deal["status"] = "paid"
            save_state()

        text = (
            f"<b>✅ Оплата с баланса учтена для сделки #{deal_id}.</b>\n\n"
            "Дождитесь подтверждения передачи товара."
        )
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(chat_id, message_id, text, keyboard)
        seller_text = (
            f"<b>✅ Оплата с баланса учтена для сделки #{deal_id}</b>\n\n"
            f"📜 Описание: {html.escape(deal['description'])}\n"
            f"🎁 Отправьте подарок пользователю {GIFT_RECIPIENT}."
        )
        seller_keyboard = InlineKeyboardMarkup()
        seller_keyboard.add(
            InlineKeyboardButton(f"🎁 Подарок отправлен {GIFT_RECIPIENT}", callback_data=f"confirm_send_{deal_id}")
        )
        send_photo_message(deal["seller_id"], None, seller_text, seller_keyboard)

    elif data.startswith("confirm_send_"):
        deal_id = data.removeprefix("confirm_send_")
        with state_lock:
            deal = deals.get(deal_id)
            if not deal or deal.get("seller_id") != user_id or deal.get("status") != "paid":
                bot.send_message(chat_id, "❌ Нельзя подтвердить отправку этой сделки.")
                return
            deal["status"] = "sent"
            save_state()
        send_photo_message(
            chat_id, message_id,
            f"✅ Вы отметили отправку подарка {GIFT_RECIPIENT}. Ожидается подтверждение покупателя.",
            None,
        )
        buyer_keyboard = InlineKeyboardMarkup()
        buyer_keyboard.add(InlineKeyboardButton("✅ Подтвердить передачу", callback_data=f"confirm_recv_{deal_id}"))
        send_photo_message(
            deal["buyer_id"], None,
            f"<b>🎁 Продавец {user_label(deal['seller_id'])} отметил передачу подарка "
            f"пользователю {GIFT_RECIPIENT} по сделке #{deal_id}.</b>",
            buyer_keyboard,
        )

    elif data.startswith("confirm_recv_"):
        deal_id = data.removeprefix("confirm_recv_")
        with state_lock:
            deal = deals.get(deal_id)
            if not deal or deal.get("buyer_id") != user_id or deal.get("status") != "sent":
                bot.send_message(chat_id, "❌ Нельзя подтвердить получение этой сделки.")
                return
            deal["status"] = "completed"
            users[deal["seller_id"]]["success_deals"] += 1
            users[deal["buyer_id"]]["success_deals"] += 1
            save_state()
        text = f"<b>✅ Сделка #{deal_id} завершена.</b>\n\nСпасибо за использование сервиса."
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(deal["seller_id"], None, text, keyboard)
        send_photo_message(deal["buyer_id"], None, text, keyboard)


def _handle_admin_text(message, user):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if user_id not in admins:
        return False

    if user.get("awaiting_admin_id"):
        try:
            new_admin_id = int(text)
            if new_admin_id <= 0:
                raise ValueError
        except ValueError:
            bot.reply_to(message, "❌ Введите положительный Telegram ID.")
            return True
        admins.add(new_admin_id)
        user["awaiting_admin_id"] = False
        save_state()
        bot.reply_to(message, f"✅ Пользователь {new_admin_id} добавлен в администраторы.")
        return True

    if user.get("awaiting_fake_deals"):
        parts = text.split()
        try:
            if len(parts) != 2:
                raise ValueError
            target_id, count = map(int, parts)
            if target_id <= 0 or count <= 0:
                raise ValueError
        except ValueError:
            bot.reply_to(message, "❌ Формат: ID и положительное количество, например <code>12345678 2</code>")
            return True
        target = init_user(target_id)
        target["success_deals"] += count
        user["awaiting_fake_deals"] = False
        save_state()
        bot.reply_to(message, f"✅ Счётчик сделок для пользователя {target_id} изменён на +{count}.")
        return True

    if user.get("awaiting_fake_balance"):
        parts = text.split()
        try:
            if len(parts) != 3:
                raise ValueError
            target_id = int(parts[0])
            amount = float(parts[1])
            currency = parts[2].upper()
            if target_id <= 0 or not math.isfinite(amount) or amount <= 0:
                raise ValueError
            if currency not in {"TON", "RUB", "STARS"}:
                raise ValueError
        except ValueError:
            bot.reply_to(message, "❌ Формат: ID, положительная сумма и TON/RUB/STARS.")
            return True
        target = init_user(target_id)
        target["balance"][currency] += amount
        user["awaiting_fake_balance"] = False
        save_state()
        bot.reply_to(message, f"✅ Баланс пользователя {target_id} изменён на +{amount:g} {currency}.")
        return True

    return False


@bot.message_handler(content_types=["text"])
def handle_text(message):
    user_id = message.from_user.id
    user = init_user(user_id, message.from_user.username)
    text = (message.text or "").strip()

    # Admin input is handled before generic wallet/card text to avoid swallowing it.
    if _handle_admin_text(message, user):
        return

    if user.get("awaiting_input") == "ton":
        friendly = re.fullmatch(r"(?:EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}", text)
        raw = re.fullmatch(r"-?\d+:[0-9a-fA-F]{64}", text)
        if not (friendly or raw):
            bot.reply_to(message, "❌ Похоже, это неверный TON-адрес. Проверьте адрес и отправьте ещё раз.")
            return
        user["ton_wallet"] = text
        user["awaiting_input"] = None
        save_state()
        bot.reply_to(message, f"✅ TON-кошелёк сохранён: <code>{html.escape(text)}</code>")
        return

    if user.get("awaiting_input") == "card":
        if len(text) < 5 or len(text) > 200:
            bot.reply_to(message, "❌ Реквизиты должны содержать от 5 до 200 символов.")
            return
        user["card_details"] = text
        user["awaiting_input"] = None
        save_state()
        bot.reply_to(message, f"✅ Реквизиты сохранены: {html.escape(text)}")
        return

    if user.get("deal_state") == DealState.SET_AMOUNT:
        try:
            amount = float(text.replace(",", "."))
            if not math.isfinite(amount) or amount <= 0:
                raise ValueError
        except ValueError:
            bot.reply_to(message, "❌ Введите положительную сумму, например <code>100.5</code>.")
            return
        user["current_deal"]["amount"] = amount
        user["deal_state"] = DealState.SET_DESCRIPTION
        save_state()
        bot.reply_to(message, "📝 Укажите описание предложения (до 500 символов).")
        return

    if user.get("deal_state") == DealState.SET_DESCRIPTION:
        if not text or len(text) > 500:
            bot.reply_to(message, "❌ Описание должно содержать от 1 до 500 символов.")
            return
        draft = user["current_deal"]
        deal_id = str(uuid.uuid4())
        deals[deal_id] = {
            "seller_id": user_id,
            "amount": draft["amount"],
            "currency": draft["currency"],
            "description": text,
            "buyer_id": None,
            "status": "created",
        }
        user["deal_state"] = None
        user["current_deal"] = None
        save_state()
        link = f"https://t.me/{bot.get_me().username}?start={deal_id}"
        deal_text = (
            f"<b>✅ Сделка создана</b>\n\n"
            f"💰 Сумма: {draft['amount']:g} {draft['currency']}\n"
            f"📜 Описание: {html.escape(text)}\n"
            f"🔗 Ссылка покупателю: {html.escape(link)}"
        )
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("🔙 В меню", callback_data="main_menu"))
        send_photo_message(message.chat.id, None, deal_text, keyboard)
        return

    bot.send_message(message.chat.id, "Используйте /start, чтобы открыть меню бота.")


if __name__ == "__main__":
    print("Бот запущен.")
    bot.infinity_polling(skip_pending=True)
