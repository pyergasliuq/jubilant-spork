#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║           🤖  Telegram Бот-Модератор  v2                         ║
║                                                                  ║
║  Установка:  pip install aiogram                                 ║
║  Запуск:     python bot.py                                       ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import logging
import random
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
import os
# ══════════════════════════════════════════════════════════════════
#  КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════════════════════════

TOKEN       = os.getenv("API_TOKEN")
MAIN_ADMINS = {2080411409, 2096132893}
LOG_CHAT    = -1002799479493
DB_FILE     = "moderator.db"

# ══════════════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════════════

def _db(row: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    if row:
        conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _db(False) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS warnings (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL,
            chat_id  INTEGER NOT NULL,
            reason   TEXT,
            admin_id INTEGER,
            ts       DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bans (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL,
            chat_id  INTEGER NOT NULL,
            reason   TEXT,
            admin_id INTEGER,
            active   INTEGER DEFAULT 1,
            ts       DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS roles (
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            role    TEXT    NOT NULL,
            by_id   INTEGER,
            PRIMARY KEY (user_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS reports (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id   INTEGER,
            target_id INTEGER,
            chat_id   INTEGER,
            msg       TEXT,
            reason    TEXT,
            ts        DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER,
            key     TEXT,
            val     TEXT,
            PRIMARY KEY (chat_id, key)
        );

        CREATE TABLE IF NOT EXISTS captcha (
            user_id INTEGER,
            chat_id INTEGER,
            answer  INTEGER,
            msg_id  INTEGER,
            PRIMARY KEY (user_id, chat_id)
        );
        """)


# ══════════════════════════════════════════════════════════════════
#  НАСТРОЙКИ ЧАТА
# ══════════════════════════════════════════════════════════════════

DEFAULTS: dict[str, str] = {
    "antispam":          "1",
    "antispam_count":    "3",
    "antispam_action":   "warn",    # warn | mute | delete
    "antiflood":         "1",
    "antiflood_limit":   "5",
    "antiflood_seconds": "5",
    "antiflood_action":  "mute",    # mute | warn | delete
    "antimat":           "1",
    "antimat_action":    "warn",    # warn | mute | delete
    "captcha":           "0",
    "max_warns":         "3",
    "warn_action":       "mute",    # mute | kick | ban
    "welcome_text":      "",
    "nav_buttons":       "[]",
}

TOGGLE_KEYS = {"antispam", "antiflood", "antimat", "captcha"}


def cfg(chat_id: int, key: str) -> str:
    conn = _db()
    r = conn.execute(
        "SELECT val FROM settings WHERE chat_id=? AND key=?", (chat_id, key)
    ).fetchone()
    conn.close()
    return r["val"] if r else DEFAULTS.get(key, "")


def set_cfg(chat_id: int, key: str, val: str) -> None:
    with _db(False) as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (chat_id, key, val))


def toggle_cfg(chat_id: int, key: str) -> str:
    new = "0" if cfg(chat_id, key) == "1" else "1"
    set_cfg(chat_id, key, new)
    return new


# ══════════════════════════════════════════════════════════════════
#  РОЛИ
# ══════════════════════════════════════════════════════════════════

LEVELS   = {"owner": 3, "admin": 2, "moderator": 1, "user": 0}
ROLE_RU  = {"owner": "Владелец", "admin": "Администратор",
             "moderator": "Модератор", "user": "Пользователь"}


def get_role(uid: int, cid: int) -> str:
    if uid in MAIN_ADMINS:
        return "owner"
    conn = _db()
    r = conn.execute(
        "SELECT role FROM roles WHERE user_id=? AND chat_id=?", (uid, cid)
    ).fetchone()
    conn.close()
    return r["role"] if r else "user"


def role_level(uid: int, cid: int) -> int:
    return LEVELS.get(get_role(uid, cid), 0)


def set_role_db(uid: int, cid: int, role: str, by: int) -> None:
    with _db(False) as c:
        if role == "user":
            c.execute("DELETE FROM roles WHERE user_id=? AND chat_id=?", (uid, cid))
        else:
            c.execute("INSERT OR REPLACE INTO roles VALUES (?,?,?,?)", (uid, cid, role, by))


def can_act_on(actor: int, target: int, cid: int) -> bool:
    if target in MAIN_ADMINS:
        return False
    return role_level(actor, cid) > role_level(target, cid)


def has_role(uid: int, cid: int, min_role: str = "moderator") -> bool:
    return uid in MAIN_ADMINS or role_level(uid, cid) >= LEVELS.get(min_role, 1)


# ══════════════════════════════════════════════════════════════════
#  ПРЕДУПРЕЖДЕНИЯ
# ══════════════════════════════════════════════════════════════════

def add_warn(uid: int, cid: int, reason: str, by: int) -> int:
    with _db(False) as c:
        c.execute(
            "INSERT INTO warnings (user_id,chat_id,reason,admin_id) VALUES (?,?,?,?)",
            (uid, cid, reason, by),
        )
    conn = _db(False)
    n = conn.execute(
        "SELECT COUNT(*) FROM warnings WHERE user_id=? AND chat_id=?", (uid, cid)
    ).fetchone()[0]
    conn.close()
    return n


def pop_warn(uid: int, cid: int) -> bool:
    conn = _db()
    r = conn.execute(
        "SELECT id FROM warnings WHERE user_id=? AND chat_id=? ORDER BY id DESC LIMIT 1",
        (uid, cid),
    ).fetchone()
    conn.close()
    if r:
        with _db(False) as c:
            c.execute("DELETE FROM warnings WHERE id=?", (r["id"],))
        return True
    return False


def get_warns(uid: int, cid: int):
    conn = _db()
    rows = conn.execute(
        "SELECT reason, ts FROM warnings WHERE user_id=? AND chat_id=? ORDER BY id DESC",
        (uid, cid),
    ).fetchall()
    conn.close()
    return rows


def clear_warns(uid: int, cid: int) -> None:
    with _db(False) as c:
        c.execute("DELETE FROM warnings WHERE user_id=? AND chat_id=?", (uid, cid))


def mass_amnesty(cid: int) -> int:
    """Снять все предупреждения всех пользователей в чате. Возвращает кол-во."""
    conn = _db(False)
    n = conn.execute(
        "SELECT COUNT(*) FROM warnings WHERE chat_id=?", (cid,)
    ).fetchone()[0]
    conn.execute("DELETE FROM warnings WHERE chat_id=?", (cid,))
    conn.commit()
    conn.close()
    return n


# ══════════════════════════════════════════════════════════════════
#  БАНЫ
# ══════════════════════════════════════════════════════════════════

def add_ban(uid: int, cid: int, reason: str, by: int) -> None:
    with _db(False) as c:
        c.execute(
            "INSERT INTO bans (user_id,chat_id,reason,admin_id) VALUES (?,?,?,?)",
            (uid, cid, reason, by),
        )


def remove_ban(uid: int, cid: int) -> None:
    with _db(False) as c:
        c.execute(
            "UPDATE bans SET active=0 WHERE user_id=? AND chat_id=? AND active=1",
            (uid, cid),
        )


def get_bans(cid: int, limit: int = 15):
    conn = _db()
    rows = conn.execute(
        "SELECT user_id, reason, ts FROM bans "
        "WHERE chat_id=? AND active=1 ORDER BY id DESC LIMIT ?",
        (cid, limit),
    ).fetchall()
    conn.close()
    return rows


# ══════════════════════════════════════════════════════════════════
#  АНТИМАТ
# ══════════════════════════════════════════════════════════════════

_MAT_RE = re.compile(
    r'б[ля]+д[ьт]?'
    r'|[её]б[аеёиуы]?[нт]?'
    r'|[хx][уy][йjeёиея]'
    r'|п[иi][зz]д'
    r'|шлю[хx]'
    r'|мудак|мудил'
    r'|залуп'
    r'|долбо[её]б'
    r'|[сs][уy][кk][аaiи]',
    re.IGNORECASE,
)


def has_mat(text: str) -> bool:
    return bool(_MAT_RE.search(text))


# ══════════════════════════════════════════════════════════════════
#  УТИЛИТЫ
# ══════════════════════════════════════════════════════════════════

def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{name}</a>'


def parse_dur(s: str) -> Optional[int]:
    m = re.match(r"^(\d+)([smhd])$", (s or "").strip().lower())
    if not m:
        return None
    return int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def fmt_dur(s: str) -> str:
    m = re.match(r"^(\d+)([smhd])$", (s or "").strip().lower())
    if not m:
        return s
    labels = {"s": "сек", "m": "мин", "h": "ч", "d": "дн"}
    return f"{m[1]} {labels[m[2]]}"


def on_icon(v: str) -> str:
    return "✅" if v == "1" else "❌"


def _fmt_welcome(text: str, user, chat_title: str) -> str:
    uname = f"@{user.username}" if getattr(user, "username", None) else user.full_name
    return (
        text
        .replace("{имя}",    user.full_name)
        .replace("{юзер}",   uname)
        .replace("{чат}",    chat_title)
        .replace("{mention}", mention(user.id, user.full_name))
    )


async def _log(text: str) -> None:
    try:
        await bot.send_message(LOG_CHAT, text)
    except Exception as e:
        logging.error("Ошибка лога: %s", e)


async def _get_target(msg: Message):
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user
    return None


async def _apply_warn_limit(cid: int, uid: int, uname: str) -> str:
    action = cfg(cid, "warn_action")
    clear_warns(uid, cid)
    if action == "kick":
        await bot.ban_chat_member(cid, uid)
        await bot.unban_chat_member(cid, uid)
        return f"🚪 {mention(uid, uname)} выгнан за превышение лимита предупреждений."
    if action == "ban":
        await bot.ban_chat_member(cid, uid)
        add_ban(uid, cid, "Превышение лимита предупреждений", 0)
        return f"🔨 {mention(uid, uname)} заблокирован за превышение лимита предупреждений."
    # mute по умолчанию
    await bot.restrict_chat_member(
        cid, uid,
        ChatPermissions(can_send_messages=False),
        until_date=datetime.now() + timedelta(hours=1),
    )
    return f"🔇 {mention(uid, uname)} замучен на 1 ч за превышение лимита предупреждений."


_UNMUTE_PERMS = ChatPermissions(
    can_send_messages=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_send_polls=True,
)

_NO_RIGHTS = "❌ Недостаточно прав."
_NO_REPLY  = "❌ Ответьте на сообщение пользователя."
_NO_ACT    = "❌ Нельзя применить действие к этому пользователю."

# ══════════════════════════════════════════════════════════════════
#  БОТ
# ══════════════════════════════════════════════════════════════════

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()
rtr = Router()
dp.include_router(rtr)

# ══════════════════════════════════════════════════════════════════
#  РУССКИЕ ПСЕВДОНИМЫ
# ══════════════════════════════════════════════════════════════════

RU_CMD: dict[str, str] = {
    # предупреждения
    "варн":               "warn",
    "предупредить":       "warn",
    "снятьварн":          "unwarn",
    "снятьпред":          "unwarn",
    "варны":              "warnings",
    "предупреждения":     "warnings",
    "очиститьварны":      "clearwarns",
    "амнистия":           "amnesty",
    # мут
    "мут":                "mute",
    "заглушить":          "mute",
    "размут":             "unmute",
    "разглушить":         "unmute",
    # кик/бан
    "кик":                "kick",
    "выгнать":            "kick",
    "бан":                "ban",
    "заблок":             "ban",
    "разбан":             "unban",
    "разблок":            "unban",
    "баны":               "banlist",
    "списокбанов":        "banlist",
    # роли
    "роль":               "setrole",
    "назначитьроль":      "setrole",
    "снятьроль":          "removerole",
    "мояроль":            "myrole",
    "ктоэто":             "whois",
    # жалоба
    "жалоба":             "report",
    # настройки
    "настройки":          "settings",
    "установить":         "set",
    "вкл":                "toggle_on",
    "выкл":               "toggle_off",
    "переключить":        "toggle",
    # приветствие
    "приветствие":        "setwelcome",
    "удалитьприветствие": "delwelcome",
    # навигация
    "меню":               "nav",
    "навигация":          "nav",
    "установитьнав":      "setnav",
    # прочее
    "ид":                 "id",
    "рассылка":           "broadcast",
}


def _parse_ru_cmd(text: str) -> Optional[tuple[str, str]]:
    t     = text.strip()
    lower = t.lower()
    # Сортируем по убыванию длины, чтобы «снятьварн» не перебивался «снять»
    for ru in sorted(RU_CMD, key=len, reverse=True):
        if lower == ru:
            return RU_CMD[ru], ""
        if lower.startswith(ru + " "):
            return RU_CMD[ru], t[len(ru):].strip()
    return None


# ══════════════════════════════════════════════════════════════════
#  ЯДРО КОМАНД (вызывается и из /slash и из русских псевдонимов)
# ══════════════════════════════════════════════════════════════════

async def _do_warn(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    if not can_act_on(m.from_user.id, target.id, m.chat.id):
        return await m.reply(_NO_ACT)
    reason = args or "Без причины"
    n      = add_warn(target.id, m.chat.id, reason, m.from_user.id)
    maxw   = int(cfg(m.chat.id, "max_warns"))
    await m.reply(
        f"⚠️ {mention(target.id, target.full_name)} — "
        f"предупреждение {n}/{maxw}\n📝 {reason}"
    )
    await _log(
        f"⚠️ <b>Варн</b> | {m.chat.title}\n"
        f"👤 {mention(target.id, target.full_name)}\n"
        f"🛡 {mention(m.from_user.id, m.from_user.full_name)}\n"
        f"📝 {reason} ({n}/{maxw})"
    )
    if n >= maxw:
        try:
            msg = await _apply_warn_limit(m.chat.id, target.id, target.full_name)
            await m.answer(msg)
            await _log(
                f"🔴 <b>Лимит варнов</b> | {m.chat.title}\n"
                f"👤 {mention(target.id, target.full_name)}"
            )
        except TelegramBadRequest as e:
            await m.reply(f"❌ Не удалось применить действие: {e}")


async def _do_unwarn(m: Message) -> None:
    if not has_role(m.from_user.id, m.chat.id):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    if pop_warn(target.id, m.chat.id):
        n = len(get_warns(target.id, m.chat.id))
        await m.reply(f"✅ Предупреждение снято. Осталось: {n}")
        await _log(
            f"✅ <b>Снятие варна</b> | {m.chat.title}\n"
            f"👤 {mention(target.id, target.full_name)}\n"
            f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
        )
    else:
        await m.reply("ℹ️ У пользователя нет предупреждений.")


async def _do_warnings(m: Message) -> None:
    target = (await _get_target(m)) or m.from_user
    warns  = get_warns(target.id, m.chat.id)
    maxw   = cfg(m.chat.id, "max_warns")
    if not warns:
        return await m.reply(
            f"✅ У {mention(target.id, target.full_name)} нет предупреждений."
        )
    lines = [f"⚠️ <b>{mention(target.id, target.full_name)}: {len(warns)}/{maxw}</b>"]
    for i, w in enumerate(warns, 1):
        lines.append(f"{i}. {w['reason']} <i>({str(w['ts'])[:10]})</i>")
    await m.reply("\n".join(lines))


async def _do_clearwarns(m: Message) -> None:
    if not has_role(m.from_user.id, m.chat.id):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    clear_warns(target.id, m.chat.id)
    await m.reply(
        f"✅ Все предупреждения {mention(target.id, target.full_name)} сброшены."
    )
    await _log(
        f"🗑 <b>Сброс варнов</b> | {m.chat.title}\n"
        f"👤 {mention(target.id, target.full_name)}\n"
        f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
    )


async def _do_amnesty(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id, "admin"):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if target:
        # Амнистия одного пользователя
        clear_warns(target.id, m.chat.id)
        await m.reply(
            f"🕊 Амнистия: {mention(target.id, target.full_name)} — "
            f"все предупреждения сняты."
        )
        await _log(
            f"🕊 <b>Амнистия</b> | {m.chat.title}\n"
            f"👤 {mention(target.id, target.full_name)}\n"
            f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
        )
    else:
        # Массовая амнистия
        n = mass_amnesty(m.chat.id)
        await m.reply(
            f"🕊 <b>Массовая амнистия!</b>\n"
            f"Снято предупреждений: <b>{n}</b> у всех пользователей чата."
        )
        await _log(
            f"🕊 <b>Массовая амнистия</b> | {m.chat.title}\n"
            f"Снято варнов: {n}\n"
            f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
        )


async def _do_mute(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    if not can_act_on(m.from_user.id, target.id, m.chat.id):
        return await m.reply(_NO_ACT)
    parts    = args.split(maxsplit=1)
    secs     = parse_dur(parts[0]) if parts else None
    if secs:
        time_lbl = fmt_dur(parts[0])
        reason   = parts[1] if len(parts) > 1 else "Без причины"
        until    = datetime.now() + timedelta(seconds=secs)
    else:
        time_lbl = "навсегда"
        reason   = args or "Без причины"
        until    = None
    try:
        await bot.restrict_chat_member(
            m.chat.id, target.id,
            ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await m.reply(
            f"🔇 {mention(target.id, target.full_name)} замучен ({time_lbl})\n"
            f"📝 {reason}"
        )
        await _log(
            f"🔇 <b>Мут</b> | {m.chat.title}\n"
            f"👤 {mention(target.id, target.full_name)}\n"
            f"🛡 {mention(m.from_user.id, m.from_user.full_name)}\n"
            f"⏱ {time_lbl} | 📝 {reason}"
        )
    except TelegramBadRequest as e:
        await m.reply(f"❌ {e}")


async def _do_unmute(m: Message) -> None:
    if not has_role(m.from_user.id, m.chat.id):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    try:
        await bot.restrict_chat_member(m.chat.id, target.id, _UNMUTE_PERMS)
        await m.reply(f"🔊 {mention(target.id, target.full_name)} размучен.")
        await _log(
            f"🔊 <b>Размут</b> | {m.chat.title}\n"
            f"👤 {mention(target.id, target.full_name)}\n"
            f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
        )
    except TelegramBadRequest as e:
        await m.reply(f"❌ {e}")


async def _do_kick(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    if not can_act_on(m.from_user.id, target.id, m.chat.id):
        return await m.reply(_NO_ACT)
    reason = args or "Без причины"
    try:
        await bot.ban_chat_member(m.chat.id, target.id)
        await bot.unban_chat_member(m.chat.id, target.id)
        await m.reply(
            f"🚪 {mention(target.id, target.full_name)} выгнан\n📝 {reason}"
        )
        await _log(
            f"🚪 <b>Кик</b> | {m.chat.title}\n"
            f"👤 {mention(target.id, target.full_name)}\n"
            f"🛡 {mention(m.from_user.id, m.from_user.full_name)}\n"
            f"📝 {reason}"
        )
    except TelegramBadRequest as e:
        await m.reply(f"❌ {e}")


async def _do_ban(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    if not can_act_on(m.from_user.id, target.id, m.chat.id):
        return await m.reply(_NO_ACT)
    reason = args or "Без причины"
    try:
        await bot.ban_chat_member(m.chat.id, target.id)
        add_ban(target.id, m.chat.id, reason, m.from_user.id)
        await m.reply(
            f"🔨 {mention(target.id, target.full_name)} заблокирован\n📝 {reason}"
        )
        await _log(
            f"🔨 <b>Бан</b> | {m.chat.title}\n"
            f"👤 {mention(target.id, target.full_name)}\n"
            f"🛡 {mention(m.from_user.id, m.from_user.full_name)}\n"
            f"📝 {reason}"
        )
    except TelegramBadRequest as e:
        await m.reply(f"❌ {e}")


async def _do_unban(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id):
        return await m.reply(_NO_RIGHTS)
    uid  = None
    name = "пользователь"
    target = await _get_target(m)
    if target:
        uid, name = target.id, target.full_name
    elif args:
        try:
            uid  = int(args.strip())
            name = f"ID {uid}"
        except ValueError:
            return await m.reply(
                "❌ Ответьте на сообщение или укажите числовой ID:\n/unban 123456789"
            )
    else:
        return await m.reply(
            "❌ Ответьте на сообщение или укажите ID:\n/unban 123456789"
        )
    try:
        await bot.unban_chat_member(m.chat.id, uid, only_if_banned=True)
        remove_ban(uid, m.chat.id)
        await m.reply(f"✅ {mention(uid, name)} разбанен.")
        await _log(
            f"✅ <b>Разбан</b> | {m.chat.title}\n"
            f"👤 {mention(uid, name)}\n"
            f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
        )
    except TelegramBadRequest as e:
        await m.reply(f"❌ {e}")


async def _do_banlist(m: Message) -> None:
    if not has_role(m.from_user.id, m.chat.id):
        return await m.reply(_NO_RIGHTS)
    bans = get_bans(m.chat.id)
    if not bans:
        return await m.reply("✅ Список банов пуст.")
    lines = [f"🔨 <b>Заблокированные ({len(bans)}):</b>"]
    for b in bans:
        lines.append(
            f"• <code>{b['user_id']}</code> — {b['reason']} "
            f"<i>({str(b['ts'])[:10]})</i>"
        )
    await m.reply("\n".join(lines))


async def _do_setrole(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id, "admin"):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    ru_roles = {"владелец": "owner", "администратор": "admin", "модератор": "moderator"}
    new_role = ru_roles.get(args.strip().lower(), args.strip().lower())
    if new_role not in ("owner", "admin", "moderator"):
        return await m.reply(
            "❌ Доступные роли:\n"
            "owner / владелец\n"
            "admin / администратор\n"
            "moderator / модератор"
        )
    if not can_act_on(m.from_user.id, target.id, m.chat.id):
        return await m.reply(_NO_ACT)
    if (LEVELS.get(new_role, 0) >= role_level(m.from_user.id, m.chat.id)
            and m.from_user.id not in MAIN_ADMINS):
        return await m.reply("❌ Нельзя назначить роль выше или равную своей.")
    set_role_db(target.id, m.chat.id, new_role, m.from_user.id)
    ru = ROLE_RU.get(new_role, new_role)
    await m.reply(f"✅ {mention(target.id, target.full_name)} → <b>{ru}</b>")
    await _log(
        f"🎭 <b>Роль</b> | {m.chat.title}\n"
        f"👤 {mention(target.id, target.full_name)} → {ru}\n"
        f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
    )


async def _do_removerole(m: Message) -> None:
    if not has_role(m.from_user.id, m.chat.id, "admin"):
        return await m.reply(_NO_RIGHTS)
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    if not can_act_on(m.from_user.id, target.id, m.chat.id):
        return await m.reply(_NO_ACT)
    set_role_db(target.id, m.chat.id, "user", m.from_user.id)
    await m.reply(f"✅ Роль {mention(target.id, target.full_name)} снята.")
    await _log(
        f"🎭 <b>Снятие роли</b> | {m.chat.title}\n"
        f"👤 {mention(target.id, target.full_name)}\n"
        f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
    )


async def _do_report(m: Message, args: str) -> None:
    if not m.reply_to_message:
        return await m.reply("❌ Ответьте на сообщение, чтобы пожаловаться.")
    target   = m.reply_to_message.from_user
    reason   = args or "Без причины"
    msg_text = (m.reply_to_message.text or "[медиа]")[:500]
    with _db(False) as c:
        c.execute(
            "INSERT INTO reports (from_id,target_id,chat_id,msg,reason) VALUES (?,?,?,?,?)",
            (m.from_user.id, target.id, m.chat.id, msg_text, reason),
        )
    try:
        await m.delete()
    except Exception:
        pass
    await m.answer("✅ Жалоба принята и отправлена модераторам.")
    await _log(
        f"🚨 <b>Жалоба</b> | {m.chat.title}\n"
        f"👤 {mention(m.from_user.id, m.from_user.full_name)} "
        f"→ {mention(target.id, target.full_name)}\n"
        f"📝 {reason}\n💬 {msg_text[:200]}"
    )


async def _do_settings(m: Message) -> None:
    if not has_role(m.from_user.id, m.chat.id, "admin"):
        return await m.reply(_NO_RIGHTS)
    cid = m.chat.id
    await m.reply(
        f"⚙️ <b>Настройки: {m.chat.title}</b>\n\n"
        f"{on_icon(cfg(cid,'antispam'))} <b>antispam</b> — "
        f"порог: {cfg(cid,'antispam_count')} | действие: {cfg(cid,'antispam_action')}\n"
        f"{on_icon(cfg(cid,'antiflood'))} <b>antiflood</b> — "
        f"лимит: {cfg(cid,'antiflood_limit')} за {cfg(cid,'antiflood_seconds')} с | "
        f"действие: {cfg(cid,'antiflood_action')}\n"
        f"{on_icon(cfg(cid,'antimat'))} <b>antimat</b> — "
        f"действие: {cfg(cid,'antimat_action')}\n"
        f"{on_icon(cfg(cid,'captcha'))} <b>captcha</b>\n"
        f"⚠️ <b>max_warns</b>: {cfg(cid,'max_warns')} | "
        f"действие при лимите: {cfg(cid,'warn_action')}\n\n"
        f"<i>Переключить: /toggle antispam  или  вкл antispam</i>\n"
        f"<i>Изменить значение: /set ключ значение</i>"
    )


async def _do_toggle(m: Message, args: str, force: Optional[bool] = None) -> None:
    if not has_role(m.from_user.id, m.chat.id, "admin"):
        return await m.reply(_NO_RIGHTS)
    key = args.strip().lower()
    if key not in TOGGLE_KEYS:
        return await m.reply(
            f"❌ Можно переключать: {', '.join(sorted(TOGGLE_KEYS))}"
        )
    if force is True:
        set_cfg(m.chat.id, key, "1")
        new = "1"
    elif force is False:
        set_cfg(m.chat.id, key, "0")
        new = "0"
    else:
        new = toggle_cfg(m.chat.id, key)
    state = "включён ✅" if new == "1" else "выключен ❌"
    await m.reply(f"🔧 <b>{key}</b> {state}")
    await _log(
        f"⚙️ <b>Переключение</b> | {m.chat.title}\n"
        f"{key} → {state}\n"
        f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
    )


async def _do_set(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id, "admin"):
        return await m.reply(_NO_RIGHTS)
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply("❌ Использование: /set параметр значение")
    key, val = parts
    if key not in DEFAULTS:
        return await m.reply("❌ Неизвестный параметр. Смотрите /settings")
    set_cfg(m.chat.id, key, val)
    await m.reply(f"✅ <b>{key}</b> = <code>{val}</code>")
    await _log(
        f"⚙️ <b>Настройка</b> | {m.chat.title}\n"
        f"{key} → {val}\n"
        f"🛡 {mention(m.from_user.id, m.from_user.full_name)}"
    )


async def _do_setwelcome(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id, "admin"):
        return await m.reply(_NO_RIGHTS)
    if not args:
        return await m.reply(
            "❌ Укажите текст приветствия.\n\n"
            "<b>Переменные:</b>\n"
            "{имя} — полное имя\n"
            "{юзер} — @username\n"
            "{чат} — название чата\n"
            "{mention} — упоминание-ссылка\n\n"
            "Пример: /setwelcome Добро пожаловать, {имя}! 👋"
        )
    set_cfg(m.chat.id, "welcome_text", args)
    preview = _fmt_welcome(args, m.from_user, m.chat.title or "")
    await m.reply(
        f"✅ Приветствие установлено!\n\n"
        f"<b>Предпросмотр:</b>\n{preview}"
    )


async def _do_delwelcome(m: Message) -> None:
    if not has_role(m.from_user.id, m.chat.id, "admin"):
        return await m.reply(_NO_RIGHTS)
    set_cfg(m.chat.id, "welcome_text", "")
    await m.reply("✅ Приветствие удалено.")


async def _do_nav(m: Message) -> None:
    raw = cfg(m.chat.id, "nav_buttons")
    try:
        buttons = json.loads(raw)
    except Exception:
        buttons = []
    if not buttons:
        return await m.reply(
            "ℹ️ Навигация не настроена.\n\n"
            "Добавьте кнопки командой /setnav:\n"
            "/setnav Правила|https://t.me/...\n"
            "Поддержка|https://t.me/...\n\n"
            "Каждая строка — одна кнопка."
        )
    kb_rows = []
    for btn in buttons:
        try:
            kb_rows.append([InlineKeyboardButton(text=btn["text"], url=btn["url"])])
        except Exception:
            pass
    if not kb_rows:
        return await m.reply("❌ Ошибка в данных навигации. Переустановите /setnav")
    await m.reply(
        f"🧭 <b>Навигация | {m.chat.title}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


async def _do_setnav(m: Message, args: str) -> None:
    if not has_role(m.from_user.id, m.chat.id, "admin"):
        return await m.reply(_NO_RIGHTS)
    if not args:
        return await m.reply(
            "❌ Использование:\n"
            "/setnav Правила|https://t.me/...\n"
            "Поддержка|https://t.me/...\n\n"
            "Каждая строка — одна кнопка: <b>Текст|ссылка</b>"
        )
    lines   = [l.strip() for l in args.split("\n") if l.strip()]
    buttons = []
    errors  = []
    for line in lines:
        if "|" not in line:
            errors.append(f"⚠️ Нет разделителя «|»: {line}")
            continue
        text, url = line.split("|", 1)
        text, url = text.strip(), url.strip()
        if not text or not url:
            errors.append(f"⚠️ Пустое поле: {line}")
            continue
        buttons.append({"text": text, "url": url})
    if not buttons:
        return await m.reply("❌ Ни одной корректной кнопки.\n" + "\n".join(errors))
    set_cfg(m.chat.id, "nav_buttons", json.dumps(buttons, ensure_ascii=False))
    reply = f"✅ Навигация установлена: {len(buttons)} кнопок."
    if errors:
        reply += "\n\n" + "\n".join(errors)
    await m.reply(reply)


async def _do_id(m: Message) -> None:
    if m.reply_to_message and m.reply_to_message.from_user:
        u = m.reply_to_message.from_user
        return await m.reply(f"🆔 {mention(u.id, u.full_name)}: <code>{u.id}</code>")
    await m.reply(
        f"🆔 Ваш ID: <code>{m.from_user.id}</code>\n"
        f"💬 Чат ID: <code>{m.chat.id}</code>"
    )


async def _do_broadcast(m: Message, args: str) -> None:
    if m.from_user.id not in MAIN_ADMINS:
        return await m.reply(_NO_RIGHTS)
    if not args:
        return await m.reply("❌ Укажите текст рассылки.")
    await bot.send_message(m.chat.id, f"📢 <b>Объявление</b>\n\n{args}")
    await m.reply("✅ Рассылка отправлена.")
    await _log(
        f"📢 <b>Рассылка</b> | {m.chat.title}\n"
        f"🛡 {mention(m.from_user.id, m.from_user.full_name)}\n"
        f"📝 {args[:300]}"
    )


# ══════════════════════════════════════════════════════════════════
#  ДИСПЕТЧЕР РУССКИХ КОМАНД
# ══════════════════════════════════════════════════════════════════

async def _dispatch_ru_cmd(m: Message, text: str) -> bool:
    parsed = _parse_ru_cmd(text)
    if not parsed:
        return False
    cmd, args = parsed
    dispatch: dict[str, any] = {
        "warn":       lambda: _do_warn(m, args),
        "unwarn":     lambda: _do_unwarn(m),
        "warnings":   lambda: _do_warnings(m),
        "clearwarns": lambda: _do_clearwarns(m),
        "amnesty":    lambda: _do_amnesty(m, args),
        "mute":       lambda: _do_mute(m, args),
        "unmute":     lambda: _do_unmute(m),
        "kick":       lambda: _do_kick(m, args),
        "ban":        lambda: _do_ban(m, args),
        "unban":      lambda: _do_unban(m, args),
        "banlist":    lambda: _do_banlist(m),
        "setrole":    lambda: _do_setrole(m, args),
        "removerole": lambda: _do_removerole(m),
        "myrole":     lambda: m.reply(
            f"🎭 Ваша роль: <b>{ROLE_RU.get(get_role(m.from_user.id, m.chat.id))}</b>"
        ),
        "whois":      lambda: _whois_impl(m),
        "report":     lambda: _do_report(m, args),
        "settings":   lambda: _do_settings(m),
        "set":        lambda: _do_set(m, args),
        "toggle":     lambda: _do_toggle(m, args),
        "toggle_on":  lambda: _do_toggle(m, args, force=True),
        "toggle_off": lambda: _do_toggle(m, args, force=False),
        "setwelcome": lambda: _do_setwelcome(m, args),
        "delwelcome": lambda: _do_delwelcome(m),
        "nav":        lambda: _do_nav(m),
        "setnav":     lambda: _do_setnav(m, args),
        "id":         lambda: _do_id(m),
        "broadcast":  lambda: _do_broadcast(m, args),
    }
    fn = dispatch.get(cmd)
    if fn:
        await fn()
        return True
    return False


async def _whois_impl(m: Message) -> None:
    target = await _get_target(m)
    if not target:
        return await m.reply(_NO_REPLY)
    r = get_role(target.id, m.chat.id)
    await m.reply(
        f"🎭 {mention(target.id, target.full_name)}: <b>{ROLE_RU.get(r, r)}</b>"
    )


# ══════════════════════════════════════════════════════════════════
#  SLASH КОМАНДЫ
# ══════════════════════════════════════════════════════════════════

@rtr.message(Command("start", "help"))
async def cmd_help(m: Message) -> None:
    await m.reply(
        "🤖 <b>Бот-Модератор v2</b>\n\n"
        "<b>🔨 Модерация</b> (ответом на сообщение):\n"
        "/warn — предупреждение | <i>варн</i>\n"
        "/unwarn — снять варн | <i>снятьварн</i>\n"
        "/warnings — список варнов | <i>варны</i>\n"
        "/clearwarns — сбросить варны | <i>очиститьварны</i>\n"
        "/amnesty — амнистия | <i>амнистия</i>\n"
        "/mute [1h|30m|1d] — мут | <i>мут</i>\n"
        "/unmute — размут | <i>размут</i>\n"
        "/kick — выгнать | <i>кик</i>\n"
        "/ban — заблокировать | <i>бан</i>\n"
        "/unban [reply|ID] — разбан | <i>разбан</i>\n"
        "/banlist — список банов | <i>баны</i>\n\n"
        "<b>🎭 Роли</b>:\n"
        "/setrole owner|admin|moderator | <i>роль</i>\n"
        "/removerole — снять роль | <i>снятьроль</i>\n"
        "/myrole — моя роль | <i>мояроль</i>\n"
        "/whois — роль пользователя | <i>ктоэто</i>\n\n"
        "<b>⚙️ Настройки</b>:\n"
        "/settings — настройки | <i>настройки</i>\n"
        "/toggle antispam|antiflood|antimat|captcha | <i>вкл/выкл</i>\n"
        "/set ключ значение | <i>установить</i>\n\n"
        "<b>👋 Приветствие и навигация</b>:\n"
        "/setwelcome текст | <i>приветствие</i>\n"
        "/delwelcome — удалить | <i>удалитьприветствие</i>\n"
        "/setnav Кнопка|url — меню | <i>установитьнав</i>\n"
        "/nav — показать меню | <i>меню</i>\n\n"
        "<b>📢 Прочее</b>:\n"
        "/report — жалоба (ответом) | <i>жалоба</i>\n"
        "/id — узнать ID | <i>ид</i>\n"
        "/broadcast текст | <i>рассылка</i>"
    )


@rtr.message(Command("warn"))
async def cmd_warn(m: Message, command: CommandObject) -> None:
    await _do_warn(m, command.args or "")

@rtr.message(Command("unwarn"))
async def cmd_unwarn(m: Message) -> None:
    await _do_unwarn(m)

@rtr.message(Command("warnings"))
async def cmd_warnings(m: Message) -> None:
    await _do_warnings(m)

@rtr.message(Command("clearwarns"))
async def cmd_clearwarns(m: Message) -> None:
    await _do_clearwarns(m)

@rtr.message(Command("amnesty"))
async def cmd_amnesty(m: Message, command: CommandObject) -> None:
    await _do_amnesty(m, command.args or "")

@rtr.message(Command("mute"))
async def cmd_mute(m: Message, command: CommandObject) -> None:
    await _do_mute(m, command.args or "")

@rtr.message(Command("unmute"))
async def cmd_unmute(m: Message) -> None:
    await _do_unmute(m)

@rtr.message(Command("kick"))
async def cmd_kick(m: Message, command: CommandObject) -> None:
    await _do_kick(m, command.args or "")

@rtr.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject) -> None:
    await _do_ban(m, command.args or "")

@rtr.message(Command("unban"))
async def cmd_unban(m: Message, command: CommandObject) -> None:
    await _do_unban(m, command.args or "")

@rtr.message(Command("banlist"))
async def cmd_banlist(m: Message) -> None:
    await _do_banlist(m)

@rtr.message(Command("setrole"))
async def cmd_setrole(m: Message, command: CommandObject) -> None:
    await _do_setrole(m, command.args or "")

@rtr.message(Command("removerole"))
async def cmd_removerole(m: Message) -> None:
    await _do_removerole(m)

@rtr.message(Command("myrole"))
async def cmd_myrole(m: Message) -> None:
    r = get_role(m.from_user.id, m.chat.id)
    await m.reply(f"🎭 Ваша роль: <b>{ROLE_RU.get(r, r)}</b>")

@rtr.message(Command("whois"))
async def cmd_whois(m: Message) -> None:
    await _whois_impl(m)

@rtr.message(Command("settings"))
async def cmd_settings(m: Message) -> None:
    await _do_settings(m)

@rtr.message(Command("set"))
async def cmd_set(m: Message, command: CommandObject) -> None:
    await _do_set(m, command.args or "")

@rtr.message(Command("toggle"))
async def cmd_toggle(m: Message, command: CommandObject) -> None:
    await _do_toggle(m, command.args or "")

@rtr.message(Command("setwelcome"))
async def cmd_setwelcome(m: Message, command: CommandObject) -> None:
    await _do_setwelcome(m, command.args or "")

@rtr.message(Command("delwelcome"))
async def cmd_delwelcome(m: Message) -> None:
    await _do_delwelcome(m)

@rtr.message(Command("nav"))
async def cmd_nav(m: Message) -> None:
    await _do_nav(m)

@rtr.message(Command("setnav"))
async def cmd_setnav(m: Message, command: CommandObject) -> None:
    await _do_setnav(m, command.args or "")

@rtr.message(Command("report"))
async def cmd_report(m: Message, command: CommandObject) -> None:
    await _do_report(m, command.args or "")

@rtr.message(Command("id"))
async def cmd_id(m: Message) -> None:
    await _do_id(m)

@rtr.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject) -> None:
    await _do_broadcast(m, command.args or "")


# ══════════════════════════════════════════════════════════════════
#  КАПЧА
# ══════════════════════════════════════════════════════════════════

async def _send_captcha(uid: int, cid: int) -> None:
    a, b  = random.randint(1, 12), random.randint(1, 12)
    ans   = a + b
    wrongs = list(
        {ans + random.randint(1, 5), abs(ans - random.randint(1, 4)) + 1, ans + 7} - {ans}
    )[:2]
    opts = [ans] + wrongs
    random.shuffle(opts)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=str(o),
            callback_data=f"cap:{uid}:{'ok' if o == ans else 'no'}",
        )
        for o in opts
    ]])
    try:
        await bot.restrict_chat_member(cid, uid, ChatPermissions(can_send_messages=False))
        msg = await bot.send_message(
            cid,
            f"👋 Добро пожаловать!\nРешите пример: <b>{a} + {b} = ?</b>",
            reply_markup=kb,
        )
        with _db(False) as c:
            c.execute("INSERT OR REPLACE INTO captcha VALUES (?,?,?,?)",
                      (uid, cid, ans, msg.message_id))
    except Exception as e:
        logging.error("Ошибка капчи: %s", e)


@rtr.callback_query(F.data.startswith("cap:"))
async def cb_captcha(call: CallbackQuery) -> None:
    _, uid_s, result = call.data.split(":")
    uid = int(uid_s)
    if call.from_user.id != uid:
        return await call.answer("Это не ваша капча!", show_alert=True)
    cid = call.message.chat.id
    if result == "ok":
        try:
            await bot.restrict_chat_member(cid, uid, _UNMUTE_PERMS)
        except Exception:
            pass
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer("✅ Верно! Добро пожаловать!", show_alert=True)
        with _db(False) as c:
            c.execute("DELETE FROM captcha WHERE user_id=? AND chat_id=?", (uid, cid))
        await _log(
            f"✅ <b>Капча пройдена</b> | {call.message.chat.title}\n"
            f"👤 {mention(uid, call.from_user.full_name)}"
        )
        wt = cfg(cid, "welcome_text")
        if wt:
            await bot.send_message(
                cid,
                _fmt_welcome(wt, call.from_user, call.message.chat.title or ""),
            )
    else:
        await call.answer("❌ Неверно! Попробуйте ещё раз.", show_alert=True)
        await _log(
            f"❌ <b>Капча провалена</b> | {call.message.chat.title}\n"
            f"👤 {mention(uid, call.from_user.full_name)}"
        )


@rtr.chat_member()
async def on_chat_member(event: ChatMemberUpdated) -> None:
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    if new == ChatMemberStatus.MEMBER and old in {
        ChatMemberStatus.LEFT, ChatMemberStatus.KICKED
    }:
        uid = event.new_chat_member.user.id
        cid = event.chat.id
        if cfg(cid, "captcha") == "1":
            await _send_captcha(uid, cid)
        else:
            wt = cfg(cid, "welcome_text")
            if wt:
                try:
                    await bot.send_message(
                        cid,
                        _fmt_welcome(wt, event.new_chat_member.user, event.chat.title or ""),
                    )
                except Exception as e:
                    logging.error("Ошибка приветствия: %s", e)


# ══════════════════════════════════════════════════════════════════
#  СТРАЖИ: РУССКИЕ КОМАНДЫ + АНТИМАТ + АНТИФЛУД + АНТИСПАМ
# ══════════════════════════════════════════════════════════════════

_flood: dict = defaultdict(list)
_spam:  dict = defaultdict(list)


@rtr.message(F.chat.type.in_({"group", "supergroup"}))
async def guard(m: Message) -> None:
    if not m.from_user or m.from_user.is_bot:
        return
    uid, cid = m.from_user.id, m.chat.id
    text     = m.text or m.caption or ""
    now      = time.monotonic()
    key      = (uid, cid)

    # ── 0. Русские команды ───────────────────────────────────
    if text:
        handled = await _dispatch_ru_cmd(m, text)
        if handled:
            return

    # ── Модераторов стражами не трогаем ─────────────────────
    if has_role(uid, cid):
        return

    # ── 1. АНТИМАТ ───────────────────────────────────────────
    if cfg(cid, "antimat") == "1" and text and has_mat(text):
        try:
            await m.delete()
        except Exception:
            pass
        await _log(
            f"🤬 <b>Антимат</b> | {m.chat.title}\n"
            f"👤 {mention(uid, m.from_user.full_name)}\n"
            f"💬 {text[:150]}"
        )
        action = cfg(cid, "antimat_action")
        if action == "mute":
            try:
                await bot.restrict_chat_member(
                    cid, uid,
                    ChatPermissions(can_send_messages=False),
                    until_date=datetime.now() + timedelta(minutes=10),
                )
                await m.answer(
                    f"🔇 {mention(uid, m.from_user.full_name)}, "
                    f"мут 10 мин за нецензурную лексику."
                )
            except Exception:
                pass
        elif action != "delete":  # warn
            n    = add_warn(uid, cid, "Нецензурная лексика", 0)
            maxw = int(cfg(cid, "max_warns"))
            await m.answer(
                f"⚠️ {mention(uid, m.from_user.full_name)}, "
                f"не используйте мат! Предупреждение {n}/{maxw}"
            )
            if n >= maxw:
                try:
                    msg = await _apply_warn_limit(cid, uid, m.from_user.full_name)
                    await m.answer(msg)
                except Exception:
                    pass
        return

    # ── 2. АНТИФЛУД ─────────────────────────────────────────
    if cfg(cid, "antiflood") == "1":
        sec   = int(cfg(cid, "antiflood_seconds"))
        limit = int(cfg(cid, "antiflood_limit"))
        _flood[key] = [t for t in _flood[key] if now - t < sec]
        _flood[key].append(now)
        if len(_flood[key]) > limit:
            _flood[key].clear()
            await _log(
                f"🌊 <b>Антифлуд</b> | {m.chat.title}\n"
                f"👤 {mention(uid, m.from_user.full_name)}"
            )
            action = cfg(cid, "antiflood_action")
            if action == "delete":
                try:
                    await m.delete()
                except Exception:
                    pass
            elif action == "warn":
                n    = add_warn(uid, cid, "Флуд", 0)
                maxw = int(cfg(cid, "max_warns"))
                await m.answer(
                    f"⚠️ {mention(uid, m.from_user.full_name)}, "
                    f"предупреждение {n}/{maxw} за флуд."
                )
                if n >= maxw:
                    try:
                        msg = await _apply_warn_limit(cid, uid, m.from_user.full_name)
                        await m.answer(msg)
                    except Exception:
                        pass
            else:  # mute
                try:
                    await bot.restrict_chat_member(
                        cid, uid,
                        ChatPermissions(can_send_messages=False),
                        until_date=datetime.now() + timedelta(minutes=5),
                    )
                    await m.answer(
                        f"🌊 {mention(uid, m.from_user.full_name)}, мут 5 мин за флуд."
                    )
                except Exception:
                    pass
            return

    # ── 3. АНТИСПАМ ─────────────────────────────────────────
    if cfg(cid, "antispam") == "1" and text:
        thresh = int(cfg(cid, "antispam_count"))
        _spam[key].append(text)
        if len(_spam[key]) > thresh + 1:
            _spam[key] = _spam[key][-(thresh + 1):]
        if len(_spam[key]) >= thresh and len(set(_spam[key][-thresh:])) == 1:
            _spam[key].clear()
            await _log(
                f"🔁 <b>Антиспам</b> | {m.chat.title}\n"
                f"👤 {mention(uid, m.from_user.full_name)}\n"
                f"💬 {text[:150]}"
            )
            try:
                await m.delete()
            except Exception:
                pass
            action = cfg(cid, "antispam_action")
            if action == "mute":
                try:
                    await bot.restrict_chat_member(
                        cid, uid,
                        ChatPermissions(can_send_messages=False),
                        until_date=datetime.now() + timedelta(minutes=10),
                    )
                    await m.answer(
                        f"🔁 {mention(uid, m.from_user.full_name)}, мут 10 мин за спам."
                    )
                except Exception:
                    pass
            elif action == "warn":
                n    = add_warn(uid, cid, "Спам", 0)
                maxw = int(cfg(cid, "max_warns"))
                await m.answer(
                    f"⚠️ {mention(uid, m.from_user.full_name)}, "
                    f"предупреждение {n}/{maxw} за спам."
                )
                if n >= maxw:
                    try:
                        msg = await _apply_warn_limit(cid, uid, m.from_user.full_name)
                        await m.answer(msg)
                    except Exception:
                        pass
            # "delete" — сообщение уже удалено выше


# ══════════════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════════════

async def main() -> None:
    init_db()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.info("🤖 Бот-Модератор v2 запущен")
    await dp.start_polling(
        bot,
        allowed_updates=["message", "callback_query", "chat_member"],
    )


if __name__ == "__main__":
    asyncio.run(main())
