import asyncio
import random
import os
import socket
import time
import requests
import xlsxwriter
from datetime import date, datetime, timezone, timedelta

from pyrogram import Client, filters, idle
from pyrogram.utils import get_event_loop
from pyrogram.errors import FloodWait, UserDeactivatedBan, AuthKeyDuplicated, AuthKeyUnregistered, Forbidden, UserAlreadyParticipant
from pyrogram.errors.exceptions.bad_request_400 import MsgIdInvalid, PeerIdInvalid, ReactionInvalid


from pyrogram.enums import ParseMode, ChatAction
from pyrogram.raw.functions.users import GetFullUser
from pyrogram.raw.types import InputUser
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
from pyrogram.errors import SessionPasswordNeeded

from pyrogram.raw.functions.contacts import Search

from handlers import add_handler, remove_handler, is_handler
from menus import pretexts_menu
from sqlite_functions import sql_select, sql_edit, core_commands
from config import API_ID, API_HASH, BOT_ID, BOT_LINK, BOT_TOKEN, ADMINS, AI_TOKEN, AI_AGENT_ID
try:
    from config import PROXY
except ImportError:
    PROXY = None

# важное

SENT_TODAY = 0
AI_API_BASE = 'https://agent.timeweb.cloud/api/v1/cloud-ai/agents'

conversation_history = ["Привет, как дела?", "Все отлично! Как я могу помочь вам?"]

admins = list(ADMINS)

MAIN_KEYBOARD = ReplyKeyboardMarkup([['Мои аккаунты 🙂'], ['Меню 🌴']], resize_keyboard=True)

# костыль для остановки парсинга, потом перепишу

working = False

# настройки бота

LIMIT = 2000000000
WORK_SINCE = 0
WORK_UNTIL = 0
MANAGERS_COUNT = 0
emojies = ('🤝', '👍', '🤔', '💪', '🤓')

# реакции по умолчанию для автолайков

DEFAULT_LIKE_EMOJIS = ('👍', '❤️', '🔥', '🥰', '👏')

# при необходимости создаём таблицы

for command in core_commands:
    sql_edit(command, ())

# загружаем или инициализируем список администраторов из DB

_db_admins = sql_select('SELECT user_id FROM bot_admins')
if _db_admins:
    admins = [int(r[0]) for r in _db_admins]
else:
    for _aid in ADMINS:
        sql_edit('INSERT OR IGNORE INTO bot_admins(user_id) VALUES(?)', (str(_aid),))

# получаем уже сохраненные сессии

managers = sql_select('SELECT token FROM managers')
workers = sql_select('SELECT session, proxy FROM workers')
worker_apps = []
manager_apps = []

if not managers:
    managers.append((BOT_TOKEN,))
    sql_edit('INSERT INTO managers(token) VALUES(?)', (BOT_TOKEN,))

apps = []
workers_list = {}

async def accounts(message):
    keyboard = []
    
    for user in workers_list.keys():
        try:
            user_info = await workers_list[user].get_me()
            keyboard.append([InlineKeyboardButton(f'{user_info.first_name}',
                                                  callback_data=f'user {user_info.phone_number}')])
        except UserDeactivatedBan:
            sql_edit(f"DELETE FROM workers WHERE session = '{user}'", ())
            continue
        except AuthKeyDuplicated:
            sql_edit(f"DELETE FROM workers WHERE session = '{user}'", ())
            continue
        except AuthKeyUnregistered:
            sql_edit(f"DELETE FROM workers WHERE session = '{user}'", ())
            continue
        except Exception as e:
            await message.reply(f'Ошибка при получении информации об аккаунте<pre>{e}</pre>')
            continue

    keyboard.append([InlineKeyboardButton('Добавить аккаунт 🤓', callback_data='+')])
    
    await message.edit(
        "<b>Список ваших аккаунтов</b>\n\nДля настройки аккаунта нажмите на его ник",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def menu(message):
    keyboard = []
    
    # for user in workers_list.keys():
    #     try:
    #         user_info = await workers_list[user].get_me()
    #         keyboard.append([InlineKeyboardButton(f'{user_info.first_name}',
    #                                               callback_data=f'user {user_info.phone_number}')])
    #     except UserDeactivatedBan:
    #         sql_edit(f'DELETE FROM workers WHERE session = {user}', ())
    #         continue
    #     except AuthKeyDuplicated:
    #         sql_edit(f'DELETE FROM workers WHERE session = {user}', ())
    #         continue
    #     except AuthKeyUnregistered:
    #         sql_edit(f'DELETE FROM workers WHERE session = {user}', ())
    #         continue
    #     except Exception as e:
    #         await message.reply(f'Ошибка при получении информации об аккаунте<pre>{e}</pre>')
    #         continue
    
    if workers_list:
        keyboard.append([InlineKeyboardButton('Спарсить юзеров в коментах 💬', callback_data='parse_comments')])
        keyboard.append([InlineKeyboardButton('Поиск каналов 🔎', callback_data='search')])
    
    keyboard.append([InlineKeyboardButton('Канал уведомлений 🥰', callback_data='service channel')])
    keyboard.append([InlineKeyboardButton('Токен AI ♻️', callback_data='token')])
    keyboard.append([InlineKeyboardButton('Agent ID AI 🖥️', callback_data='proxy')])
    keyboard.append([InlineKeyboardButton('Новый бот для управления 🤖', callback_data='bot')])
    keyboard.append([InlineKeyboardButton('Администраторы бота 👥', callback_data='manage_admins'),
                     InlineKeyboardButton('Общий прокси 🌐', callback_data='set_global_proxy')])
    keyboard.append([InlineKeyboardButton('Добавить аккаунт 🤓', callback_data='+')])
    
    await message.edit(
        "Список ваших аккаунтов:",
        reply_markup=InlineKeyboardMarkup(keyboard))

async def notify(text):
    service_channel = sql_select('SELECT channel FROM managers')
    if not service_channel or not service_channel[0][0]:
        return
    if not manager_apps:
        print('[notify] manager_apps пуст')
        return
    try:
        await manager_apps[0].send_message(int(service_channel[0][0]), text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f'[notify] ошибка отправки в канал: {e}')


def _timeweb_headers():
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {AI_TOKEN}',
    }

def _timeweb_url():
    return f'{AI_API_BASE}/{AI_AGENT_ID}/v1/chat/completions'

def generate_keywords(keyword):
    url = _timeweb_url()
    headers = _timeweb_headers()

    conversation_history.append(f'подбери 10 похожих ключевых слов к запросу "{keyword}" для поиска телеграм каналов')

    data = {
        'model': 'gpt-4',
        'messages': [
            {"role": "system", "content": "Ты полезный помощник."},
            {"role": "user", "content": conversation_history[-1]},
        ]
    }

    gptreq = requests.post(url, headers=headers, json=data)

    if 'error' in gptreq.json():
        return 'insufficient_quota'

    assistant_reply = gptreq.json()["choices"][0]['message']['content']

    if assistant_reply.startswith('"') and assistant_reply.endswith('"'):
        assistant_reply = assistant_reply[1:-1]

    conversation_history.append(assistant_reply)
    return assistant_reply
    
async def generate_response(prompt, role, app):
    try:
        url = _timeweb_url()
        headers = _timeweb_headers()

        conversation_history.append(prompt)

        data = {
            'model': 'gpt-4',
            'messages': [
                {"role": "system", "content": role},
                {"role": "user", "content": conversation_history[-1]},
            ]
        }

        gptreq = requests.post(url, headers=headers, json=data)

        if gptreq.status_code == 200:

            assistant_reply = gptreq.json()["choices"][0]['message']['content']

            if assistant_reply.startswith('"') and assistant_reply.endswith('"'):
                assistant_reply = assistant_reply[1:-1]

            conversation_history.append(assistant_reply)

            return assistant_reply

        else:
            for admin in admins:
                try:
                    await app.send_message(
                        admin, f'Ошибка при запросе к AI: [{gptreq.status_code}] {gptreq.json()["error"]["message"]}')
                except:
                    pass

    except Exception as e:
        print(e)


async def generate_dialogue(history, system_role, extra_prompt=''):
    """history: list of {'role': 'user'/'assistant', 'content': str}"""
    try:
        url = _timeweb_url()
        headers = _timeweb_headers()
        system_content = system_role
        if extra_prompt:
            system_content += f'\n\n{extra_prompt}'
        messages = [{"role": "system", "content": system_content}] + history
        gptreq = requests.post(url, headers=headers, json={'model': 'gpt-4', 'messages': messages})
        if gptreq.status_code == 200:
            reply = gptreq.json()["choices"][0]['message']['content']
            if reply.startswith('"') and reply.endswith('"'):
                reply = reply[1:-1]
            return reply
        else:
            print(f'[generate_dialogue] error {gptreq.status_code}: {gptreq.text}')
            return None
    except Exception as e:
        print(f'[generate_dialogue] exception: {e}')
        return None


def format_proxy(proxy):
    if len(proxy) == 5:
        return {"scheme": proxy[0],
                "hostname": proxy[1],
                "port": int(proxy[2]),
                "username": proxy[3],
                "password": proxy[4]}
    elif len(proxy) == 4:
        return {"scheme": proxy[0],
                "hostname": proxy[1],
                "port": int(proxy[2]),
                "secret": proxy[3]}
    elif len(proxy) == 3:
        return {"scheme": proxy[0],
                "hostname": proxy[1],
                "port": int(proxy[2])}
    else:
        return False

GLOBAL_PROXY = format_proxy(PROXY.split(':')) if PROXY else None

# переопределяем глобальный прокси из DB, если задан там
_db_proxy = sql_select('SELECT global_proxy FROM managers LIMIT 1')
if _db_proxy and _db_proxy[0][0]:
    GLOBAL_PROXY = format_proxy(_db_proxy[0][0].split(':'))


# при старте по питанию сеть поднимается позже бота — ждём её и повторяем подключение

NETWORK_WAIT = 180
CONNECT_ATTEMPTS = 3
CONNECT_PAUSE = 10

startup_failed = []


def wait_for_network(timeout=NETWORK_WAIT):
    """Ждём, пока станет доступен Telegram (или прокси, если он задан)."""
    host, port = '149.154.167.51', 443
    if GLOBAL_PROXY:
        host, port = GLOBAL_PROXY['hostname'], GLOBAL_PROXY['port']

    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.create_connection((host, port), timeout=5):
                return True
        except OSError as e:
            if time.monotonic() >= deadline:
                print(f'Сеть так и не появилась ({host}:{port}): {e}')
                return False
            print(f'Ждём сеть ({host}:{port}): {e}')
            time.sleep(5)


def connect_client(client, label):
    """Подключаем клиент с повторами — одного обрыва сети не хватит, чтобы бот остался мёртвым."""
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            client.start()
            client.get_me()
            client.stop()
            return True

        except (UserDeactivatedBan, AuthKeyDuplicated, AuthKeyUnregistered):
            raise

        except Exception as e:
            print(f'{label}: попытка {attempt}/{CONNECT_ATTEMPTS} не удалась — {e}')
            try:
                client.stop()
            except Exception:
                pass
            if attempt < CONNECT_ATTEMPTS:
                time.sleep(CONNECT_PAUSE)

    return False


def startup():
    for token in managers:

        global MANAGERS_COUNT

        try:
            client = Client(f"bot{MANAGERS_COUNT}", api_id=API_ID, api_hash=API_HASH, bot_token=token[0], proxy=GLOBAL_PROXY)

            if not connect_client(client, f'Управляющий бот bot{MANAGERS_COUNT}'):
                startup_failed.append(f'управляющий бот bot{MANAGERS_COUNT}')
                continue

            apps.append(client)
            manager_apps.append(client)
            setup_manager(client)

            MANAGERS_COUNT += 1

        except UserDeactivatedBan:
            continue
        except Exception as e:
            print(e)

    for session in workers:
        try:

            if session[1]:
                try:
                    proxy = session[1].split(':')
                    proxy = format_proxy(proxy)

                    client = Client(session[0], api_id=API_ID, api_hash=API_HASH, proxy=proxy)
                except ConnectionError:
                    client = Client(session[0], api_id=API_ID, api_hash=API_HASH, proxy=GLOBAL_PROXY)
                except:
                    client = Client(session[0], api_id=API_ID, api_hash=API_HASH, proxy=GLOBAL_PROXY)

            else:
                client = Client(session[0], api_id=API_ID, api_hash=API_HASH, proxy=GLOBAL_PROXY)

            try:
                connected = connect_client(client, f'Воркер {session[0]}')

            except UserDeactivatedBan:
                print(f'Worker {session[0]}: banned, removing')
                sql_edit(f"DELETE FROM workers WHERE session = '{session[0]}'", ())
                continue
            except AuthKeyDuplicated:
                print(f'Worker {session[0]}: auth key duplicated, removing')
                sql_edit(f"DELETE FROM workers WHERE session = '{session[0]}'", ())
                continue
            except AuthKeyUnregistered:
                print(f'Worker {session[0]}: auth key unregistered, removing')
                sql_edit(f"DELETE FROM workers WHERE session = '{session[0]}'", ())
                continue

            if not connected:
                print(f'Worker {session[0]}: не подключился, пропускаем')
                startup_failed.append(session[0])
                continue

            apps.append(client)
            worker_apps.append(client)
            workers_list[session[0]] = client
            setup_worker(client)
        except Exception as e:
            print(f'Startup error: {e}')


async def _show_autoreply_menu(edit_func, session):
    s = sql_select(f"SELECT auto_reply, reply_use_ai, reply_role, reply_prompt, reply_max_rounds, reply_delay, auto_reply_enabled FROM workers WHERE session = '{session}'")
    if not s:
        return
    auto_reply_text, reply_use_ai, reply_role, reply_prompt, reply_max_rounds, reply_delay, enabled = s[0]
    enabled = 1 if enabled is None else enabled
    reply_max_rounds = reply_max_rounds or 3
    reply_delay = reply_delay if reply_delay is not None else 1
    status = '✅ Включены' if enabled else '❌ Выключены'
    mode_label = '🤖 ИИ-диалог' if reply_use_ai else '📝 Статичный текст'
    text = (
        f'<b>Автоответы 💬</b>\n\n'
        f'Статус: <b>{status}</b>\n'
        f'Режим: <b>{mode_label}</b>\n\n'
        f'<b>Статичный текст:</b>\n<code>{auto_reply_text or "не задан"}</code>\n\n'
        f'<b>ИИ-диалог:</b>\n'
        f'Роль: <code>{reply_role or "не задана"}</code>\n'
        f'Промпт: <code>{reply_prompt or "не задан"}</code>\n'
        f'Раундов: <code>{reply_max_rounds}</code>\n\n'
        f'Задержка: <code>{reply_delay} сек</code>'
    )
    kb = [
        [InlineKeyboardButton('Выключить ❌' if enabled else 'Включить ✅',
                              callback_data=f'toggleautoreply {session}')],
        [InlineKeyboardButton(f'Режим: {"→ Статичный" if reply_use_ai else "→ ИИ-диалог"}',
                              callback_data=f'togglereplyai {session}')],
        [InlineKeyboardButton('Статичный текст ✏️', callback_data=f'set auto_reply {session}')],
        [InlineKeyboardButton('Роль для диалога 🤖', callback_data=f'set reply_role {session}')],
        [InlineKeyboardButton('Промпт для диалога 📝', callback_data=f'set reply_prompt {session}')],
        [InlineKeyboardButton(f'Раундов: {reply_max_rounds}', callback_data=f'set reply_max_rounds {session}'),
         InlineKeyboardButton(f'Задержка: {reply_delay} сек ⏱', callback_data=f'set reply_delay {session}')],
        [InlineKeyboardButton('Очистить историю 🗑', callback_data=f'clear_reply_history {session}')],
        [InlineKeyboardButton('Назад к аккаунту ◀️', callback_data=f'user {session}')],
    ]
    await edit_func(text, reply_markup=InlineKeyboardMarkup(kb))


def parse_emojis(raw_emojis):
    """Строку с реакциями ('👍,❤️ 🔥') превращаем в список."""
    if not raw_emojis:
        return list(DEFAULT_LIKE_EMOJIS)
    parts = [part.strip() for part in str(raw_emojis).replace(',', ' ').split()]
    parts = [part for part in parts if part]
    return parts or list(DEFAULT_LIKE_EMOJIS)


def fmt_number(value):
    """Убираем лишний .0 при выводе чисел в меню."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def likes_settings(session):
    """Настройки автолайков воркера: (enabled, emojis, delay, chance, big, notify)."""
    rows = sql_select(
        f"SELECT likes_enabled, likes_emojis, likes_delay, likes_chance, likes_big, likes_notify "
        f"FROM workers WHERE session = '{session}'")
    if not rows:
        return None
    enabled, raw_emojis, delay, chance, big, notify_flag = rows[0]

    try:
        delay = float(delay) if delay is not None else 0
    except (TypeError, ValueError):
        delay = 0
    if delay < 0:
        delay = 0

    try:
        chance = int(float(chance)) if chance is not None else 1
    except (TypeError, ValueError):
        chance = 1
    if chance < 1:
        chance = 1

    return (bool(enabled), parse_emojis(raw_emojis), delay, chance, bool(big), bool(notify_flag))


async def _show_autolikes_menu(edit_func, session):
    settings = likes_settings(session)
    if not settings:
        return
    enabled, emojis, delay, chance, big, notify_flag = settings

    chats_row = sql_select(f"SELECT COUNT(*) FROM like_chats WHERE session = '{session}'")
    chats_count = chats_row[0][0] if chats_row else 0

    text = (
        f'<b>Автолайки 👍</b>\n\n'
        f'Статус: <b>{"✅ Включены" if enabled else "❌ Выключены"}</b>\n'
        f'Чатов и каналов: <b>{chats_count}</b>\n'
        f'Задержка: <b>{fmt_number(delay)} сек</b>\n'
        f'Шанс: <b>1/{chance}</b>\n'
        f'Большая реакция: <b>{"✅" if big else "❌"}</b>\n'
        f'Уведомления: <b>{"✅" if notify_flag else "❌"}</b>\n\n'
        f'Реакции: {" ".join(emojis)}'
    )
    kb = [
        [InlineKeyboardButton('Выключить ❌' if enabled else 'Включить ✅',
                              callback_data=f'togglelikes {session}')],
        [InlineKeyboardButton('Реакции 😄', callback_data=f'set likes_emojis {session}')],
        [InlineKeyboardButton(f'Задержка: {fmt_number(delay)} сек ⌛', callback_data=f'set likes_delay {session}'),
         InlineKeyboardButton(f'Шанс: 1/{chance} 🎲', callback_data=f'set likes_chance {session}')],
        [InlineKeyboardButton(f'Большая реакция {"✅" if big else "❌"}',
                              callback_data=f'togglelikesbig {session}')],
        [InlineKeyboardButton(f'Уведомления {"✅" if notify_flag else "❌"}',
                              callback_data=f'togglelikesnotify {session}')],
        [InlineKeyboardButton(f'Чаты и каналы 🧿 ({chats_count})', callback_data=f'likechats {session}')],
        [InlineKeyboardButton('Назад к аккаунту ◀️', callback_data=f'user {session}')],
    ]
    await edit_func(text, reply_markup=InlineKeyboardMarkup(kb))


async def _show_like_chats_menu(edit_func, session):
    chats = sql_select(f"SELECT chat_id, chat_title FROM like_chats WHERE session = '{session}'")

    kb = []
    for chat in (chats or []):
        kb.append([InlineKeyboardButton(f'❌ {chat[1] or chat[0]}',
                                        callback_data=f'rmlikechat {chat[0]} {session}')])

    kb.append([InlineKeyboardButton('Добавить чат ➕', callback_data=f'addlikechat {session}')])
    kb.append([InlineKeyboardButton('Взять из нейрокомментинга 📥',
                                    callback_data=f'importlikechats {session}')])
    if chats:
        kb.append([InlineKeyboardButton('Очистить список 🗑', callback_data=f'clearlikechats {session}')])
    kb.append([InlineKeyboardButton('Назад к автолайкам ◀️', callback_data=f'autolikes {session}')])

    header = '<b>Чаты и каналы для автолайков 🧿</b>\n\n'
    header += ('Нажми на чат, чтобы убрать его из списка' if chats else 'Список пустой')

    await edit_func(header, reply_markup=InlineKeyboardMarkup(kb))


def setup_manager(app):
    
    manager_apps.append(app)
    
    @app.on_message(filters.command('clear_workers', '/'))
    async def callback(_, message):
        sql_edit('DELETE FROM workers', ())
        await message.reply('✅')
    
    @app.on_message(filters.command('start', '/'))
    async def callback(_, message):

        if message.from_user.id in admins:

            try:
                await app.set_bot_commands([
                    BotCommand("start", "Главное меню"),
                    BotCommand("cancel", "Отменить текущий ввод"),
                ])
            except Exception:
                pass

            await app.delete_messages(message.chat.id, message.id)
            await app.send_message(message.from_user.id, "🤓")
            
            await app.send_message(message.from_user.id, "Ваше меню:", reply_markup=ReplyKeyboardMarkup(
                [['Мои аккаунты 🙂'], ['Меню 🌴'],
                 # ['Заменить токен OpenAI ♻️'],
                 # ['Заменить прокси для ChatGPT 🖥️'],
                 # ['Спарсить комментарии 💬', 'Поиск каналов 🔎'],
                 ], resize_keyboard=True))
    
    @app.on_message(filters.command('Мои аккаунты 🙂', ''))
    async def callback(_, message):
        
        if message.from_user.id in admins:
            await app.delete_messages(message.chat.id, message.id)
            
            sent = await app.send_message(message.from_user.id, "Минутку...")
            
            await accounts(sent)
        
    @app.on_message(filters.command('Меню 🌴', ''))
    async def callback(_, message):
        
        if message.from_user.id in admins:
            await app.delete_messages(message.chat.id, message.id)
            
            sent = await app.send_message(message.from_user.id, "Минутку...")
            
            await menu(sent)
    
    @app.on_message(filters.command('Заменить токен OpenAI ♻️', ''))
    async def callback(_, message):
        
        await app.delete_messages(message.chat.id, message.id)
        
        sent = await app.send_message(message.from_user.id, "Минутку...")
        
        await accounts(sent)
    
    @app.on_message(filters.command('Заменить прокси для ChatGPT 🖥️', ''))
    async def callback(_, message):
        
        await app.delete_messages(message.chat.id, message.id)
        
        sent = await app.send_message(message.from_user.id, "Минутку...")
        
        await accounts(sent)
    
    @app.on_message(filters.command('Спарсить комментарии 💬', ''))
    async def callback(_, message):
        
        await app.delete_messages(message.chat.id, message.id)
        
        sent = await app.send_message(message.from_user.id, "Минутку...")
        
        await accounts(sent)
    
    @app.on_message(filters.command('Поиск каналов 🔎', ''))
    async def callback(_, msg):
        
        sent = await app.send_message(
            msg.from_user.id, "🔍 Введите запрос для поиска:",
            reply_markup=ReplyKeyboardMarkup([['Отмена ❌']], resize_keyboard=True))
        
        async def wait(message):
            await remove_handler(message.from_user.id)
            await app.delete_messages(sent.chat.id, sent.id)
            await app.delete_messages(message.chat.id, message.id)
            
            if message.text == 'Отмена ❌':
                sent2 = await app.send_message(message.from_user.id, "❌ Отменено",
                                               reply_markup=MAIN_KEYBOARD)
                await asyncio.sleep(2)
                await app.delete_messages(sent2.chat.id, sent2.id)
                return
            
            try:
                if workers_list:
                    
                    notification = await msg.reply(
                        'Ждём, пока AI подберёт ключевые слова... 😊')
                    
                    keywords = generate_keywords(message.text)
                    keywords = keywords.split('\n')
                    
                    keywords_to_remove = []
                    
                    for i in range(len(keywords)):
                        keyword = keywords[i]
                        if '. ' not in keyword:
                            keywords_to_remove.append(keyword)
                        else:
                            keywords[i] = keyword.split('. ')[1]
                    
                    for keyword in keywords_to_remove:
                        keywords.remove(keyword)
                    
                    keywords.append(message.text)
                    
                    for keyword in keywords:
                        
                        channels = await list(workers_list.values())[0].invoke(Search(q=keyword, limit=100))
                        if channels.chats:
                            
                            text = f'🌴 Каналы по запросу "{keyword}":\n'
                            
                            i = 1
                            for channel in channels.chats:
                                if channel.broadcast:
                                    if channel.username:
                                        text += f'\n<b>{i}.</b> {channel.title} (@{channel.username})'
                                    elif channel.usernames:
                                        text += f'\n<b>{i}.</b> {channel.title} (@{channel.usernames[0].username})'
                                    i += 1
                            
                            await notification.reply(text)
            
            except Exception as e:
                await msg.reply(f'😛 Произошла ошибка\n\n<pre>{e}</pre>')
        
        await add_handler(msg.from_user.id, wait)
    
    @app.on_message(filters.document)
    async def login(_, document):
        sent = await document.reply('🌴 Проверяем файл...')
        
        if document.document.file_name.endswith('.session'):
            
            async def progress(current, total):
                await sent.edit(f"🌴 Скачиваем файл... <code>{current * 100 / total:.1f}%</code>")
            
            directory = await app.download_media(
                document, file_name=f'{os.getcwd()}/{document.document.file_name}', progress=progress)
            
            await sent.edit(f"🌴 Файл скачан! {directory}")
            
            sent2 = await document.reply(
                "Введите SOCKS5 прокси в формате ниже:\n\n<code>scheme:ip:port:username:password</code>\n\n"
                "Пример: socks5:45.145.57.238:10253:ucXRvV:dcEscZ"
                "\n\n<i>/cancel для отмены</i>",
                reply_markup=ReplyKeyboardMarkup([['Без прокси'], ['Отмена ❌']], resize_keyboard=True))

            async def wait_for(proxy):

                await remove_handler(document.from_user.id)

                await app.delete_messages(sent2.chat.id, sent2.id)
                await app.delete_messages(proxy.chat.id, proxy.id)

                if proxy.text == 'Отмена ❌':
                    await app.send_message(document.from_user.id, '❌ Отменено', reply_markup=MAIN_KEYBOARD)
                    return

                try:

                    if proxy.text == 'Без прокси':
                        client = Client(document.document.file_name[:-8], api_id=API_ID, api_hash=API_HASH, proxy=GLOBAL_PROXY)
                    
                    else:
                        
                        proxy_to_login = proxy.text.split(':')
                        proxy_to_login = format_proxy(proxy_to_login)
                        if not proxy_to_login:
                            await app.send_message(document.from_user.id,
                                                   "Неправильный формат прокси. Пример правильного формата ниже:\n\n"
                                                   "<pre>socks5:45.145.57.238:10253:ucXRvV:dcEscZ</pre>")
                            return
                        
                        client = Client(document.document.file_name[:-8],
                                        api_id=API_ID, api_hash=API_HASH, proxy=proxy_to_login)
                    
                    await client.start()
                    account_info = await client.get_me()
                    await client.stop()
                    
                    os.rename(document.document.file_name, str(account_info.phone_number) + '.tmp')
                    os.rename(str(account_info.phone_number) + '.tmp', str(account_info.phone_number) + '.session')
                    
                    if account_info.is_bot:
                        await sent.edit(f"🤖")
                        await asyncio.sleep(2)
                        await sent.reply(f"😕 Ботов пока что так добавлять нельзя, ещё не написал код под это")
                    else:
                        client = Client(str(account_info.phone_number), api_id=API_ID, api_hash=API_HASH, proxy=GLOBAL_PROXY)
                        await client.start()
                        try:
                            await client.send_message((await app.get_me()).username, '👋')
                        except Exception as e:
                            await sent.reply(f'Аккаунт пидарас - {e}')
                        setup_worker(client)
                        await sent.edit(f"😎")
                        
                        workers_list[account_info.phone_number] = client
                        
                        if proxy.text != 'Без прокси':
                            sql_edit('INSERT INTO workers(session, proxy, prompt, auto_reply, delay, chance, be_first, what_to_write, mode) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                     (account_info.phone_number, proxy.text,
                                      'Вы - двадцатилетняя девушка',
                                      "Отвечу только когда перейдёшь по ссылке в описании)", 0, 1, 1,
                                      'Напиши интересный комментарий до 10 слов на тему этого поста:', 'text'))
                        else:
                            sql_edit('INSERT INTO workers(session, proxy, prompt, auto_reply, delay, chance, be_first, what_to_write, mode) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                     (account_info.phone_number, None,
                                      'Вы - двадцатилетняя девушка',
                                      "Отвечу только когда перейдёшь по ссылке в описании)", 0, 1, 1,
                                      'Напиши интересный комментарий до 10 слов на тему этого поста:', 'text'))
                        
                        try:
                            await document.reply(
                                f'<b>{account_info.mention}</b> @{account_info.username}\n\n'
                                f'⭐ Премиум: {account_info.is_premium}\n'
                                f'🚫 Ограничения: {account_info.is_restricted}',
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton('Профиль ⛓️', user_id=account_info.id)]]))
                        except PeerIdInvalid:
                            await document.reply(
                                f'<b>{account_info.mention}</b> @{account_info.username}\n\n'
                                f'⭐ Премиум: {account_info.is_premium}\n'
                                f'🚫 Ограничения: {account_info.is_restricted}',
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton(
                                        'Профиль ⛓️', url=f'tg://user?id={account_info.id}')]]))
                    
                
                except Exception as e:
                    await app.send_message(document.from_user.id, f'😛 Произошла ошибка\n\n<pre>{e}</pre>',
                                           reply_markup=MAIN_KEYBOARD)
            
            await add_handler(document.from_user.id, wait_for)
            
        else:
            await sent.edit(f"😕 Бот работает только с файлами формата <code>.session</code>!")
            
    @app.on_message()
    async def handle(_, message):
        if not await is_handler(message):
            if message.text.startswith('https://t.me/') or message.text.startswith('t.me/'):
                msg = await message.reply('Получаем посты...')
                
                if worker_apps:
                    #тут сделать выбор акка
                    posts = message.text.split('\n')
                    
                    global SENT_TODAY
                    
                    for post in posts:
                        
                        client = random.choice(worker_apps)
                        
                        client_data = await client.get_me()
                        settings = sql_select(f"SELECT * FROM workers WHERE session = '{client_data.phone_number}'")

                        service_channel = sql_select('SELECT channel FROM managers')
                        
                        if post.startswith('https://t.me/'):
                            post = post[13:]
                        elif post.startswith('t.me/'):
                            post = post[5:]
                        
                        post = post.split('/')
                        if post[0].isdigit():
                            post[0] = int(post[0])
                        
                        try:

                            chat = await client.get_chat(post[0])
                            post_message = await client.get_discussion_message(chat.id, int(post[1]))

                            if post_message.caption:
                                post = post_message.caption
                            else:
                                post = post_message.text

                            # await app.get_discussion_replies()
                            if settings[0][9] == 1:

                                pretexts = sql_select(
                                    f"SELECT text FROM pretexts WHERE session = '{client_data.phone_number}'")

                                sent_message = None
                                try:

                                    if pretexts:
                                        sent_message = await post_message.reply(random.choice(pretexts)[0])
                                    else:
                                        sent_message = await post_message.reply(random.choice(emojies))

                                except Forbidden:
                                    try:
                                        await client.join_chat(post_message.chat.id)
                                        if pretexts:
                                            sent_message = await post_message.reply(random.choice(pretexts)[0])
                                        else:
                                            sent_message = await post_message.reply(random.choice(emojies))
                                    except Exception as e:
                                        print(e)
                                        await notify(
                                            f'❌ <b>Ошибка при комментарии в {chat.title}</b>\n\n{e}')
                                        continue

                                except Exception as e:
                                    print(e)
                                    await notify(
                                        f'❌ <b>Ошибка при комментарии в {chat.title}</b>\n\n{e}')
                                    continue

                                return

                            if settings[0][6] == 1:
                                sent_message = None
                                try:

                                    pretexts = sql_select(f"SELECT text FROM pretexts WHERE session = '{client_data.phone_number}'")

                                    if pretexts:
                                        sent_message = await post_message.reply(random.choice(pretexts)[0])
                                    else:
                                        sent_message = await post_message.reply(random.choice(emojies))

                                except Forbidden:
                                    try:
                                        await client.join_chat(post_message.chat.id)
                                        test_message = await post_message.reply('👍')
                                        await test_message.delete()
                                    except Exception as e:
                                        print(e)
                                        await notify(
                                            f'❌ <b>Ошибка при комментарии в {chat.title}</b>\n\n{e}')
                                        continue
                                except Exception as e:
                                    print(e)
                                    await notify(
                                        f'❌ <b>Ошибка при комментарии в {chat.title}</b>\n\n{e}')
                                    continue
                                    
                            else:
                                sent_message = None
                        
                        except Exception as e:
                            print(e)
                            await notify(f'❌ <b>Ошибка при комментарии в {chat.title}</b>\n\n{e}')
                            return

                        prompt = settings[0][7] + f' "{post}"'
                        
                        reply = None
                        output = await generate_response(prompt, settings[0][2], app)
                        
                        try:
                            if sent_message:
                                reply = await sent_message.edit(output)
                                SENT_TODAY += 1
                                # for admin in admins:
                                #     for client in worker_apps:
                                #         try:
                                #             await client.send_message(admin, reply.link)
                                #         finally:
                                #             pass
                            else:
                                try:
                                    reply = await post_message.reply(output)
                                except Forbidden:
                                    try:
                                        await client.join_chat(post_message.chat.id)
                                        await post_message.reply(output)
                                    except Exception as e:
                                        await notify(
                                            f'❌ <b>Ошибка при комментарии в {post_message.chat.title}</b>\n\n{e}')
                                # for admin in admins:
                                #     for client in worker_apps:
                                #         try:
                                #             await client.send_message(admin, reply.link)
                                #         finally:
                                #             pass
                            if reply:
                                await notify(
                                    f'💬 <b>Новый комментарий в {post_message.chat.title}</b>\n\n'
                                    f'{reply.link}')

                            await msg.edit(msg.text + f'\n{reply.link}')

                        except Exception as e:
                            await notify(
                                f'❌ <b>Ошибка при комментарии в {post_message.chat.title}</b>\n\n{e}')
                        
                        
                else:
                    await msg.edit('Сначала добавьте в бота аккаунты!')
        
        
    
    @app.on_callback_query()
    async def callback(_, callback_query):

        if callback_query.from_user.id not in admins:
            return

        data = callback_query.data
        global workers_list

        if data.startswith('user'):
            try:
                await callback_query.message.edit('Получаем данные об аккаунте...')
                
                session = data.split()[1]
                settings = sql_select(f"SELECT * FROM workers WHERE session = '{session}'")

                client = workers_list[data.split(' ', 1)[1]]
                client_data = await client.get_me()
                keyboard = []

                channels_count_row = sql_select(f"SELECT COUNT(*) FROM channels WHERE session = '{session}'")
                channels_count = channels_count_row[0][0] if channels_count_row else 0

                auto_reply_enabled = settings[0][23] if len(settings[0]) > 23 and settings[0][23] is not None else 1
                broadcast_enabled = settings[0][20] if len(settings[0]) > 20 and settings[0][20] is not None else 0
                dm_enabled = settings[0][10] if len(settings[0]) > 10 and settings[0][10] is not None else 0

                likes_row = sql_select(f"SELECT likes_enabled FROM workers WHERE session = '{session}'")
                likes_enabled = likes_row[0][0] if likes_row and likes_row[0][0] is not None else 0
                like_chats_row = sql_select(f"SELECT COUNT(*) FROM like_chats WHERE session = '{session}'")
                like_chats_count = like_chats_row[0][0] if like_chats_row else 0

                reply_text = (
                    f'<b>{client_data.first_name}</b> @{client_data.username}:\n\n'
                    f'⭐ Премиум: {client_data.is_premium}\n'
                    f'🚫 Ограничения: {client_data.is_restricted}\n\n'
                    f'⚙️ Нейрокомментинг: {channels_count} каналов\n'
                    f'👍 Автолайки: {"✅" if likes_enabled else "❌"} ({like_chats_count} чатов)\n'
                    f'💬 Автоответы: {"✅" if auto_reply_enabled else "❌"}\n'
                    f'📢 Рассылка в чаты: {"✅" if broadcast_enabled else "❌"}\n'
                    f'📨 Аутрич в ЛС: {"✅" if dm_enabled else "❌"}')

                keyboard.append([InlineKeyboardButton('Профиль ⛓️', url=f'tg://user?id={client_data.id}'),
                                 InlineKeyboardButton('✏️', callback_data=f'change {client_data.phone_number}')])
                keyboard.append([InlineKeyboardButton('Изменить proxy 🌐', callback_data=f'set_worker_proxy {client_data.phone_number}'),
                                 InlineKeyboardButton('Удалить аккаунт ❌', callback_data=f'del {client_data.phone_number}')])
                keyboard.append([InlineKeyboardButton('Нейрокомментинг ⚙️', callback_data=f'neurocommenting {client_data.phone_number}')])
                keyboard.append([InlineKeyboardButton('Автолайки 👍', callback_data=f'autolikes {client_data.phone_number}')])
                keyboard.append([InlineKeyboardButton('Автоответы 💬', callback_data=f'autoreply_menu {client_data.phone_number}')])
                keyboard.append([InlineKeyboardButton('Аутрич в ЛС 📨', callback_data=f'outreach {client_data.phone_number}')])
                keyboard.append([InlineKeyboardButton('Инвайтинг 🦁', callback_data=f'inviting {client_data.phone_number}')])
                keyboard.append([InlineKeyboardButton('Рассылка по лс 🌪', callback_data=f'spam_dms {client_data.phone_number}')])
                keyboard.append([InlineKeyboardButton('Рассылка в чаты 📢', callback_data=f'broadcast {client_data.phone_number}')])
                keyboard.append([InlineKeyboardButton('Все Аккаунты ◀️', callback_data='accounts')])
                
                await callback_query.message.edit(reply_text, reply_markup=InlineKeyboardMarkup(keyboard))
                    

            except Exception as e:
                await callback_query.message.reply(f'😛 Произошла ошибка\n\n<pre>{e}</pre>')
                
        elif data.startswith('allchannels'):
            channels = sql_select(f"SELECT id FROM channels WHERE session = '{data.split()[1]}'")
            
            if not channels:
                await callback_query.answer('У этого аккаунта ещё нет подключенных аккаунтов 🙈',
                                            show_alert=True)
            else:
                
                await callback_query.message.edit('Минутку, ищем каналы...')
                
                client = workers_list[data.split()[1]]
                
                client_data = await client.get_me()
                
                text = (f'Аккаунт {client_data.mention} комментирует {len(channels)} каналов\n\n'
                        f'Чтобы удалить канал, нажмите на кнопку с его названием')
                
                kb = []
                
                for channel in channels:
                    try:
                        channel_object = await client.get_chat(channel[0])
                        
                        if channel_object:
                            kb.append([InlineKeyboardButton(
                                '❌' + channel_object.title, callback_data=f'removechannel {channel[0]} {data.split()[1]}')])
                    except Exception:
                        pass
                
                kb.append([InlineKeyboardButton('К аккаунту 👤', callback_data=f'user {data.split()[1]}')])
                kb.append([InlineKeyboardButton('Все аккаунты ◀️', callback_data='accounts')])
                
                await callback_query.message.edit(text, reply_markup=InlineKeyboardMarkup(kb))
            
        elif data.startswith('removechannel'):
            
            sql_edit(f"DELETE FROM channels WHERE id = {data.split()[1]} and session = '{data.split()[2]}'", ())
            client = workers_list[data.split()[2]]
            
            await callback_query.message.edit('Канал удалён', reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('Обратно к каналам 🧿', callback_data=f'allchannels {data.split()[1]}')],
                [InlineKeyboardButton('Все Аккаунты ◀️', callback_data='accounts')]]))
            
            await app.leave_chat(data.split()[1])
            
        elif data.startswith('switchbefirst'):
            
            current = sql_select(f"SELECT be_first FROM workers WHERE session = '{data.split()[1]}'")
            
            if current and current[0]:
                if current[0][0] == 0:
                    text = 'Теперь бот будет комментировать посты первым 🙂'
                    val = 1
                else:
                    text = 'Теперь бот НЕ будет комментировать посты первым 🙂'
                    val = 0
            
                sql_edit(f"UPDATE workers SET be_first = {val} WHERE session = '{data.split()[1]}'", ())
                await callback_query.message.edit(text, reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('К нейрокомментингу ⚙️', callback_data=f'neurocommenting {data.split()[1]}')],
                    [InlineKeyboardButton('Список аккаунтов ◀️', callback_data='accounts')]]))
            
        elif data.startswith('switchgpt'):

            current = sql_select(f"SELECT dont_use_gpt FROM workers WHERE session = '{data.split()[1]}'")

            if current and current[0]:
                if current[0][0] == 0:
                    text = 'Теперь бот будет комментировать посты шаблонами 🙂'
                    val = 1
                else:
                    text = 'Теперь бот НЕ будет комментировать посты шаблонами 🙂'
                    val = 0

                sql_edit(f"UPDATE workers SET dont_use_gpt = {val} WHERE session = '{data.split()[1]}'", ())
                await callback_query.message.edit(text, reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('К нейрокомментингу ⚙️', callback_data=f'neurocommenting {data.split()[1]}')],
                    [InlineKeyboardButton('Список аккаунтов ◀️', callback_data='accounts')]]))

        elif data.startswith('switchoutreach'):

            current = sql_select(f"SELECT dm_enabled FROM workers WHERE session = '{data.split()[1]}'")

            if current and current[0]:
                if current[0][0] == 0:
                    text = 'Аутрич в ЛС включён ✅'
                    val = 1
                else:
                    text = 'Аутрич в ЛС выключен ❌'
                    val = 0

                sql_edit(f"UPDATE workers SET dm_enabled = {val} WHERE session = '{data.split()[1]}'", ())
                await callback_query.message.edit(text, reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('К аутричу 📨', callback_data=f'outreach {data.split()[1]}')]]))

        elif data.startswith('accounts'):
            await accounts(callback_query.message)
            
        elif data.startswith('del '):
            
            client = workers_list[data.split(' ', 1)[1]]
            
            client_data = await client.get_me()
            
            await callback_query.message.edit(
                f'Вы уверены, что хотите удалить аккаунт {client_data.mention}?',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Да, удаляем', callback_data='confirm' + data)],
                    [InlineKeyboardButton('Назад ◀️', callback_data='accounts')]])
            )
        
        elif data.startswith('confirmdel'):
            
            client = workers_list[data.split(' ', 1)[1]]
            
            client_data = await client.get_me()
            await client.stop()
            workers_list.pop(data.split(' ', 1)[1])
            sql_edit(f'DELETE FROM workers WHERE session = \'{data.split(" ", 1)[1]}\'', ())
            await callback_query.message.edit(f'Аккаунт {client_data.mention} был удалён.')
            await asyncio.sleep(1)
            await accounts(callback_query.message)
        
        elif data.startswith('posts'):
            
            await callback_query.message.edit('🌴 Введите, сколько постов нам нужно получить с каждого канала:')
            
            async def wait(amount_message):
                
                amount = int(amount_message.text)
                
                if not amount:
                    await callback_query.message.edit('❌ Похоже, вы ввели не число')
                    await asyncio.sleep(2)
                    await accounts(callback_query.message)
                    return False
                
                client = workers_list[data.split()[1]]
                client_data = await client.get_me()
                
                all_channels = sql_select(f"SELECT id FROM channels WHERE session = '{client_data.phone_number}'")

                workbook = xlsxwriter.Workbook(f'посты{client_data.phone_number}.xlsx')
                
                worksheet = workbook.add_worksheet('Посты')
                
                index = 0
                for chat_id in all_channels:
                    
                    i = 0
                    
                    channel_info = await client.get_chat(chat_id[0])
                    worksheet.write(0, index, channel_info.title)
                    
                    async for message in client.get_chat_history(chat_id[0], limit=amount):
                        
                        global SENT_TODAY
                        
                        settings = sql_select(f"SELECT * FROM workers WHERE session = '{client_data.phone_number}'")

                        await asyncio.sleep(settings[0][4])
                        
                        if random.randint(1, settings[0][5]) == 1 and SENT_TODAY < LIMIT and (
                                message.text or message.caption):
                            
                            if message.caption:
                                post = message.caption
                            else:
                                post = message.text
                            
                            try:
                                message = await app.get_discussion_message(message.sender_chat.id, message.id)
                                # await app.get_discussion_replies()

                                if settings[0][9] == 1:

                                    pretexts = sql_select(
                                        f"SELECT text FROM pretexts WHERE session = '{client_data.phone_number}'")

                                    sent_message = None
                                    try:

                                        if pretexts:
                                            sent_message = await message.reply(random.choice(pretexts)[0])
                                        else:
                                            sent_message = await message.reply(random.choice(emojies))

                                    except Forbidden:
                                        try:
                                            await client.join_chat(message.chat.id)
                                            if pretexts:
                                                sent_message = await message.reply(random.choice(pretexts)[0])
                                            else:
                                                sent_message = await message.reply(random.choice(emojies))
                                        except Exception as e:
                                            print(e)
                                            # if service_channel:
                                            #     await manager_apps[0].send_message(
                                            #         service_channel[0][0],
                                            #         f'<b>Ошибка при комментарии в {chat.title}</b>\n\n'
                                            #         f'{e}')
                                            #     continue

                                    except Exception as e:
                                        print(e)
                                        # if service_channel:
                                        #     await manager_apps[0].send_message(
                                        #         service_channel[0][0],
                                        #         f'<b>Ошибка при комментарии в {chat.title}</b>\n\n'
                                        #         f'{e}')
                                        #     continue

                                    return

                                if settings[0][6] == 1:

                                    pretexts = sql_select(
                                        f"SELECT text FROM pretexts WHERE session = '{client_data.phone_number}'")

                                    if pretexts:
                                        sent_message = await message.reply(random.choice(pretexts)[0])
                                    else:
                                        sent_message = await message.reply(random.choice(emojies))

                                    print(sent_message.link)
                                else:
                                    sent_message = None
                            
                            except Exception as e:
                                print(1)
                                print(e)
                                return
                            
                            print(1)
                            prompt = settings[0][7] + f' "{post}"'

                            output = await generate_response(prompt, settings[0][2], app)

                            try:
                                if sent_message:
                                    reply = await sent_message.edit(output)
                                    SENT_TODAY += 1
                                    # for admin in admins:
                                    #     for client in worker_apps:
                                    #         try:
                                    #             await client.send_message(admin, reply.link)
                                    #         finally:
                                    #             pass
                                else:
                                    reply = await message.reply(output)
                                    # for admin in admins:
                                    #     for client in worker_apps:
                                    #         try:
                                    #             await client.send_message(admin, reply.link)
                                    #         finally:
                                    #             pass
                                
                                print(reply.link)
                            
                            except Exception as e:
                                print(2)
                                print(e)
                        
                        worksheet.write(1 + 2*i, index, message.text)
                        worksheet.write(2 + 2*i, index, message.link)
                        
                        i += 1
                    
                    index += 1
                
                workbook.close()
                await accounts(callback_query.message)
                await callback_query.message.reply_document(f'посты{client_data.phone_number}.xlsx')
            
            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('inviting'):

            client = workers_list[data.split()[1]]
            client_data = await client.get_me()

            await callback_query.message.edit('🌴 Пришлите ссылку на группу, в которую добавлять людей:')
            async def wait(channel_text):

                await remove_handler(channel_text.from_user.id)

                channel = channel_text.text
                if channel.startswith('https://t.me/+') or channel.startswith('https://t.me/-'):
                    pass
                elif channel.startswith('https://t.me/'):
                    channel = channel[13:]
                elif channel.startswith('t.me/'):
                    channel = channel[5:]
                elif channel.startswith('@'):
                    channel = channel[1:]
                elif channel.isdigit():
                    channel = int(channel)

                try:
                    group = await client.get_chat(channel)
                    res = await client.join_chat(channel)
                    if res:
                        await channel_text.reply(f'Успешно зашли в {group.title}')
                    else:
                        await channel_text.reply('Не получилось зайти в чат...')
                        return
                except UserAlreadyParticipant:
                    await channel_text.reply(f'Аккаунт уже в группе')
                except Exception as e:
                    await channel_text.reply(f'Не нашли чат. {e}')
                    return

                await callback_query.message.reply(
                    '🌴 Пришлите список людей для инвайта, каждый с новой строчки. Например:\n\n'
                    '@человек1\n@человек2')

                async def wait2(people_list):

                    await remove_handler(people_list.from_user.id)
                    try:
                        await app.delete_messages(people_list.from_user.id, people_list.id)
                    except Exception:
                        pass

                    await callback_query.message.reply(
                        'С каким промежутком добавлть людей? Для рандомного выбора напишите промежуток в таком формате: 1-5 (от 1й до 5ти сек)',
                    reply_markup=ReplyKeyboardMarkup([['Без задержки'], ['Отмена ❌']], resize_keyboard=True))

                    async def wait3(timeout):

                        await remove_handler(channel_text.from_user.id)

                        if timeout.text == 'Отмена ❌':
                            await app.send_message(timeout.from_user.id, '❌ Отменено', reply_markup=MAIN_KEYBOARD)
                            return

                        spl = timeout.text.split('-')

                        async def count_cooldown():

                            if timeout.text == 'Без задержки':
                                secs = 0
                            elif len(spl) > 1:
                                secs = random.randint(int(spl[0]), int(spl[1]))
                            else:
                                secs = int(timeout.text)

                            return secs

                        res_msg = await callback_query.message.reply('Запустили инвайтинг!',
                                                                     reply_markup=MAIN_KEYBOARD)

                        total = 0
                        successes = 0
                        failures = 0

                        users_to_add = []

                        for person in people_list.text.split('\n'):

                            total += 1

                            if person.startswith('https://t.me/'):
                                person = person[13:]
                            elif person.startswith('t.me/'):
                                person = person[5:]
                            elif person.startswith('@'):
                                person = person[1:]
                            elif person.isdigit():
                                person = int(person)

                            users_to_add.append(person)

                            try:
                                await client.add_chat_members(group.id, users_to_add)
                                successes += 1
                            except Exception as e:
                                await app.send_message(channel_text.from_user.id, f'Ошибка:\n\n<pre>{e}</pre>')
                                failures += 1

                            await asyncio.sleep(await count_cooldown())



                        await app.send_message(
                            people_list.from_user.id,
                            f'Рассылка с аккаунта {client_data.mention}:\n\n'
                            f'{total}/{len(people_list.text.split(chr(10)))}\n\n'
                            f'Отправлено {successes}\n'
                            f'Ошибок {failures}')

                        service_channel = sql_select('SELECT channel FROM managers')
                        if service_channel:
                            try:
                                await app.send_message(
                                    service_channel[0][0],
                                    f'📨 <b>Рассылка в ЛС завершена</b>\n\n'
                                    f'Аккаунт: {client_data.first_name}\n'
                                    f'Отправлено: {successes}\n'
                                    f'Ошибок: {failures}')
                            except Exception:
                                pass

                    await add_handler(callback_query.from_user.id, wait3)

                await add_handler(callback_query.from_user.id, wait2)

            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('spam_dms'):

            await callback_query.message.edit('🌴 Пришлите сообщение для рассылки:')

            async def wait(message):

                await remove_handler(message.from_user.id)

                await callback_query.message.reply('🌴 Пришлите список людей для рассылки, каждый с новой строчки. Например:\n\n'
                                                   '@человек1\n@человек2')


                async def wait2(people_list):
                    await remove_handler(people_list.from_user.id)
                    await app.delete_messages(people_list.from_user.id, people_list.id)

                    await callback_query.message.reply(
                        'С каким промежутком рассылать сообщения? Для рандомного выбора напишите промежуток в таком формате: 1-5 (от 1й до 5ти сек)',
                    reply_markup=ReplyKeyboardMarkup([['Без задержки'], ['Отмена ❌']], resize_keyboard=True))

                    async def wait3(timeout):

                        if timeout.text == 'Отмена ❌':
                            await app.send_message(timeout.from_user.id, '❌ Отменено', reply_markup=MAIN_KEYBOARD)
                            return

                        spl = timeout.text.split('-')

                        async def count_cooldown():

                            if timeout.text == 'Без задержки':
                                secs = 0
                            elif len(spl) > 1:
                                secs = random.randint(int(spl[0]), int(spl[1]))
                            else:
                                secs = int(timeout.text)

                            return secs

                        await remove_handler(people_list.from_user.id)
                        await app.delete_messages(people_list.from_user.id, timeout.id)

                        client = workers_list[data.split()[1]]
                        client_data = await client.get_me()


                        await client.send_message(BOT_LINK, random.choice(emojies))
                        await app.copy_message(client_data.id, message.chat.id, message.id)

                        msg = None
                        async for iMsg in client.get_chat_history(BOT_ID, limit=1, offset_id=-1):
                            msg = iMsg

                        total = 0
                        successes = 0
                        failures = 0

                        await callback_query.message.reply(
                            'Запустили рассылку!',
                            reply_markup=ReplyKeyboardMarkup([['Мои аккаунты 🙂'], ['Меню 🌴']],
                                                             resize_keyboard=True))

                        for person in people_list.text.split('\n'):

                            total += 1

                            if person.startswith('https://t.me/'):
                                person = person[13:]
                            elif person.startswith('t.me/'):
                                person = person[5:]
                            elif person.startswith('@'):
                                person = person[1:]
                            if person.isdigit():
                                person = int(person)

                            try:

                                await client.copy_message(person, BOT_ID, msg.id)
                                successes += 1
                            except Exception as e:
                                failures += 1
                                await message.reply(f'Ошибка при отправке сообщения @{person}:\n\n<pre>{e}</pre>')

                            await asyncio.sleep(await count_cooldown())

                        await app.send_message(
                            people_list.from_user.id,
                            f'Рассылка с аккаунта {client_data.mention}:\n\n'
                            f'{total}/{len(people_list.text.split(chr(10)))}\n\n'
                            f'Отправлено {successes}\n'
                            f'Ошибок {failures}')

                        service_channel = sql_select('SELECT channel FROM managers')
                        if service_channel:
                            try:
                                await app.send_message(
                                    service_channel[0][0],
                                    f'👥 <b>Инвайтинг завершён</b>\n\n'
                                    f'Аккаунт: {client_data.first_name}\n'
                                    f'Добавлено: {successes}\n'
                                    f'Ошибок: {failures}')
                            except Exception:
                                pass

                    await add_handler(callback_query.from_user.id, wait3)

                await add_handler(callback_query.from_user.id, wait2)

            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('newchannels'):
            
            if len(data.split()) < 3:
                await callback_query.message.edit(
                    'Выберите цель добавления каналов',
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton('Комментарии', callback_data=data + ' comments')],
                         [InlineKeyboardButton('Посты', callback_data=data + ' posts')]]))
            else:
            
                await callback_query.message.edit('🌴 Введите список каналов, каждый канал с новой строчки:')
                
                async def wait(message):

                    await remove_handler(message.from_user.id)
                    try:
                        await app.delete_messages(message.from_user.id, message.id)
                    except Exception:
                        pass

                    channels = message.text.split('\n')
                    
                    client = workers_list[data.split()[1]]
                    client_data = await client.get_me()
                    
                    success = 0
                    fail = 0
                    
                    await callback_query.message.edit(
                        f'<b>🌴 Вы прислали {len(channels)} каналов.</b> '
                        f'Начинаем вступать в них с аккаунта {client_data.first_name}...')
                    
                    for channel in channels:
                        
                        if channel.startswith('https://t.me/+') or channel.startswith('https://t.me/-'):
                            pass
                        elif channel.startswith('https://t.me/'):
                            channel = channel[13:]
                        elif channel.startswith('t.me/'):
                            channel = channel[5:]
                        elif channel.startswith('@'):
                            channel = channel[1:]
                        elif channel.isdigit():
                            channel = int(channel)
                        
                        try:
                            res = await client.join_chat(channel)
                            chat_id = res.id if hasattr(res, 'id') else res.chat.id
                            sql_edit(
                                f"INSERT INTO channels VALUES(?, '{data.split()[1]}', '{data.split()[2]}')", (chat_id,))
                            success += 1

                            try:
                                async for message in client.get_chat_history(chat_id, limit=1, offset_id=-1):
                                    dmsg = await client.get_discussion_message(chat_id, message.id)
                                    try:
                                        msg = await dmsg.reply('👍')
                                        await msg.delete()
                                    except Forbidden:
                                        try:
                                            await client.join_chat(dmsg.chat.id)
                                            msg = await dmsg.reply('👍')
                                            await msg.delete()
                                        except Exception as e:
                                            print(e)
                                    except Exception as e:
                                        print(e)
                            except Exception as e:
                                print(e)
                                    
                            
                            
                            text = f'<b>🌴 Вступаю в каналы...</b>\n\n✅ {success}'
                            if fail > 0:
                                text += f'\n❌ {fail}'
                                
                            await callback_query.message.edit(text)
                            
                        except Exception as e:
                            fail += 1
                            await callback_query.message.edit(f'<b>🌴 Вступаю в каналы...</b>\n\n✅ {success}\n❌ {fail}')
                            await callback_query.message.reply(
                                f'🤷‍♂️ Не получилось зайти в @{channel}<pre>{e}</pre>')
                            
                        
                        text = f'<b>🌴 Закончили вступать в каналы!</b>\n\n✅ {success}'
                        if fail > 0:
                            text += f'\n❌ {fail}'
                            
                        await callback_query.message.edit(text, reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('К аккаунту 👤', callback_data=f'user {data.split()[1]}')],
                            [InlineKeyboardButton('Все Аккаунты ◀️', callback_data='accounts')]]))
                    
                await add_handler(callback_query.from_user.id, wait)



        elif data.startswith('clearpretexts'):

            sql_edit(f"DELETE FROM pretexts WHERE session = '{data.split()[1]}'", ())
            await pretexts_menu(callback_query)

        elif data.startswith('delpretext'):

            sql_edit(f'DELETE FROM pretexts WHERE id = {data.split()[1]}', ())
            await pretexts_menu(callback_query)


        elif data.startswith('editpretexts'):

            await pretexts_menu(callback_query)


        elif data.startswith('addpretexts'):
            await callback_query.message.edit('🌴 Введите тексты, каждый текст с новой строчки:')

            async def wait(message):

                await remove_handler(message.from_user.id)
                try:
                    await app.delete_messages(message.from_user.id, message.id)
                except Exception:
                    pass

                pretexts = message.text.split('\n')

                for pretext in pretexts:
                    sql_edit(f"INSERT INTO pretexts(text, session) VALUES('{pretext}', '{data.split()[1]}')", ())

                await pretexts_menu(callback_query)

            await add_handler(callback_query.from_user.id, wait)


        elif data.startswith('outreach_groups'):

            session = data.split()[1]
            groups = sql_select(f"SELECT group_id FROM outreach_groups WHERE session = '{session}'")

            kb = []
            if groups:
                for g in groups:
                    try:
                        chat = await workers_list[session].get_chat(g[0])
                        label = chat.title
                    except Exception:
                        label = str(g[0])
                    kb.append([InlineKeyboardButton(f'❌ {label}', callback_data=f'del_outreach_group {g[0]} {session}')])

            kb.append([InlineKeyboardButton('Добавить группу ➕', callback_data=f'add_outreach_group {session}')])
            kb.append([InlineKeyboardButton('Назад ◀️', callback_data=f'outreach {session}')])

            await callback_query.message.edit(
                '<b>Группы для аутрича</b>\n\nНажми на группу чтобы удалить её из списка',
                reply_markup=InlineKeyboardMarkup(kb))

        elif data.startswith('del_outreach_group'):

            group_id = data.split()[1]
            session = data.split()[2]
            sql_edit(f'DELETE FROM outreach_groups WHERE group_id = {group_id} AND session = ?', (session,))
            # обновить data чтобы outreach_groups handler взял правильную сессию
            callback_query.data = f'outreach_groups {session}'
            data = callback_query.data

            groups = sql_select(f"SELECT group_id FROM outreach_groups WHERE session = '{session}'")
            kb = []
            if groups:
                for g in groups:
                    try:
                        chat = await workers_list[session].get_chat(g[0])
                        label = chat.title
                    except Exception:
                        label = str(g[0])
                    kb.append([InlineKeyboardButton(f'❌ {label}', callback_data=f'del_outreach_group {g[0]} {session}')])
            kb.append([InlineKeyboardButton('Добавить группу ➕', callback_data=f'add_outreach_group {session}')])
            kb.append([InlineKeyboardButton('Назад ◀️', callback_data=f'outreach {session}')])
            await callback_query.message.edit(
                '<b>Группы для аутрича</b>\n\nНажми на группу чтобы удалить её из списка',
                reply_markup=InlineKeyboardMarkup(kb))

        elif data.startswith('add_outreach_group'):

            session = data.split()[1]
            await callback_query.message.edit('🌴 Введите ссылку на группу или @username:')

            async def wait(message):

                await remove_handler(message.from_user.id)
                try:
                    await app.delete_messages(message.from_user.id, message.id)
                except Exception:
                    pass

                link = message.text.strip()
                if link.startswith('https://t.me/'):
                    link = link[13:]
                elif link.startswith('t.me/'):
                    link = link[5:]
                elif link.startswith('@'):
                    link = link[1:]

                try:
                    res = await workers_list[session].join_chat(link)
                    chat_id = res.id if hasattr(res, 'id') else res.chat.id
                    sql_edit('INSERT INTO outreach_groups(group_id, session) VALUES(?, ?)', (chat_id, session))
                    await callback_query.message.edit(
                        '✅ Группа добавлена',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('К списку групп', callback_data=f'outreach_groups {session}')]]))
                except Exception as e:
                    await callback_query.message.edit(
                        f'😛 Не удалось добавить группу\n\n<pre>{e}</pre>',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('Назад ◀️', callback_data=f'outreach {session}')]]))

            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('clear_outreach_triggers'):

            session = data.split()[1]
            sql_edit('DELETE FROM outreach_triggers WHERE session = ?', (session,))
            hint = 'Если список пустой — DM отправляется всем. Если слова заданы — только тем, чьё сообщение содержит хотя бы одно.'
            await callback_query.message.edit(
                f'<b>Триггерные слова 🎯</b>\n\n{hint}',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Добавить слово ➕', callback_data=f'add_outreach_trigger {session}')],
                    [InlineKeyboardButton('Назад ◀️', callback_data=f'outreach {session}')]]))

        elif data.startswith('del_outreach_trigger'):

            trigger_id = data.split()[1]
            session = data.split()[2]
            sql_edit('DELETE FROM outreach_triggers WHERE id = ? AND session = ?', (trigger_id, session))
            triggers = sql_select(f"SELECT id, word FROM outreach_triggers WHERE session = '{session}'")
            hint = 'Если список пустой — DM отправляется всем. Если слова заданы — только тем, чьё сообщение содержит хотя бы одно.'
            kb = []
            for t in triggers:
                kb.append([InlineKeyboardButton(f'❌ {t[1]}', callback_data=f'del_outreach_trigger {t[0]} {session}')])
            kb.append([InlineKeyboardButton('Добавить слово ➕', callback_data=f'add_outreach_trigger {session}')])
            if triggers:
                kb.append([InlineKeyboardButton('Очистить все 🗑', callback_data=f'clear_outreach_triggers {session}')])
            kb.append([InlineKeyboardButton('Назад ◀️', callback_data=f'outreach {session}')])
            await callback_query.message.edit(
                f'<b>Триггерные слова 🎯</b>\n\n{hint}',
                reply_markup=InlineKeyboardMarkup(kb))

        elif data.startswith('add_outreach_trigger'):

            session = data.split()[1]
            await callback_query.message.edit(
                '🎯 Введи ключевое слово или фразу.\n\n'
                'Если сообщение пользователя в группе содержит этот текст — DM будет отправлен.')

            async def wait(message):
                await remove_handler(message.from_user.id)
                try:
                    await app.delete_messages(message.from_user.id, message.id)
                except Exception:
                    pass
                word = message.text.strip()
                if word:
                    sql_edit('INSERT INTO outreach_triggers(word, session) VALUES(?, ?)', (word, session))
                    await callback_query.message.edit(
                        f'✅ Слово добавлено: <code>{word}</code>',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('К триггерным словам', callback_data=f'outreach_triggers {session}')]]))
                else:
                    await callback_query.message.edit(
                        'Пустое слово не добавлено',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('Назад ◀️', callback_data=f'outreach_triggers {session}')]]))

            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('outreach_triggers'):

            session = data.split()[1]
            triggers = sql_select(f"SELECT id, word FROM outreach_triggers WHERE session = '{session}'")
            hint = 'Если список пустой — DM отправляется всем. Если слова заданы — только тем, чьё сообщение содержит хотя бы одно.'
            kb = []
            for t in triggers:
                kb.append([InlineKeyboardButton(f'❌ {t[1]}', callback_data=f'del_outreach_trigger {t[0]} {session}')])
            kb.append([InlineKeyboardButton('Добавить слово ➕', callback_data=f'add_outreach_trigger {session}')])
            if triggers:
                kb.append([InlineKeyboardButton('Очистить все 🗑', callback_data=f'clear_outreach_triggers {session}')])
            kb.append([InlineKeyboardButton('Назад ◀️', callback_data=f'outreach {session}')])
            await callback_query.message.edit(
                f'<b>Триггерные слова 🎯</b>\n\n{hint}',
                reply_markup=InlineKeyboardMarkup(kb))

        elif data.startswith('outreach'):

            session = data.split()[1]
            settings = sql_select(f"SELECT dm_enabled, dm_text, dm_daily_limit, dm_delay FROM workers WHERE session = '{session}'")

            if not settings:
                return

            dm_enabled, dm_text, dm_daily_limit, dm_delay = settings[0]
            enabled_label = '✅ Включён' if dm_enabled else '❌ Выключен'
            today = str(date.today())
            sent_today = sql_select(
                f"SELECT COUNT(*) FROM messaged_users WHERE session = '{session}' AND messaged_date = '{today}'")
            sent_count = sent_today[0][0] if sent_today else 0
            triggers_count = sql_select(f"SELECT COUNT(*) FROM outreach_triggers WHERE session = '{session}'")
            n_triggers = triggers_count[0][0] if triggers_count else 0
            trigger_label = f'{n_triggers} сл.' if n_triggers else 'все'

            text = (
                f'<b>Аутрич в ЛС 📨</b>\n\n'
                f'Статус: {enabled_label}\n'
                f'Отправлено сегодня: {sent_count} / {dm_daily_limit}\n'
                f'Задержка между DM: {dm_delay} сек\n'
                f'Триггер: {trigger_label}\n\n'
                f'Текст сообщения:\n<code>{dm_text or "не задан"}</code>'
            )

            await callback_query.message.edit(text, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f'Вкл/Выкл', callback_data=f'switchoutreach {session}')],
                [InlineKeyboardButton('Текст сообщения ✏️', callback_data=f'set dm_text {session}')],
                [InlineKeyboardButton(f'Лимит в день: {dm_daily_limit}', callback_data=f'set dm_daily_limit {session}'),
                 InlineKeyboardButton(f'Задержка: {dm_delay} сек', callback_data=f'set dm_delay {session}')],
                [InlineKeyboardButton('Группы для мониторинга 🌐', callback_data=f'outreach_groups {session}')],
                [InlineKeyboardButton(f'Триггерные слова 🎯 ({trigger_label})', callback_data=f'outreach_triggers {session}')],
                [InlineKeyboardButton('Назад к аккаунту ◀️', callback_data=f'user {session}')]]))

        elif data.startswith('togglebroadcast '):

            session = data.split()[1]
            cur = sql_select(f"SELECT broadcast_enabled FROM workers WHERE session = '{session}'")
            new_val = 0 if (cur and cur[0][0]) else 1
            sql_edit("UPDATE workers SET broadcast_enabled = ? WHERE session = ?", (new_val, session))
            callback_query.data = f'broadcast {session}'
            data = callback_query.data

        if data.startswith('broadcast ') and len(data.split()) == 2:

            session = data.split()[1]
            s = sql_select(f"SELECT broadcast_enabled, broadcast_text, broadcast_interval FROM workers WHERE session = '{session}'")
            if not s:
                return
            enabled, text, interval = s[0]
            interval = interval or '600-1500'
            chats_count = sql_select(f"SELECT COUNT(*) FROM broadcast_chats WHERE session = '{session}'")[0][0]
            status = '✅ Включена' if enabled else '❌ Выключена'
            await callback_query.message.edit(
                f'<b>Рассылка в чаты 📢</b>\n\n'
                f'Статус: <b>{status}</b>\n'
                f'Интервал: <b>{interval} сек</b>\n'
                f'Чатов: <b>{chats_count}</b>\n'
                f'Активна: <b>10:00 – 18:00 МСК</b>\n\n'
                f'Текст:\n<code>{text or "не задан"}</code>',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Выключить ❌' if enabled else 'Включить ✅',
                                          callback_data=f'togglebroadcast {session}')],
                    [InlineKeyboardButton('Текст сообщения ✏️', callback_data=f'set broadcast_text {session}')],
                    [InlineKeyboardButton(f'Интервал: {interval} сек ⏱',
                                          callback_data=f'set broadcast_interval {session}')],
                    [InlineKeyboardButton('Список чатов 📋', callback_data=f'broadcast_chats {session}')],
                    [InlineKeyboardButton('Назад к аккаунту ◀️', callback_data=f'user {session}')],
                ]))

        elif data.startswith('del_broadcast_chat '):

            chat_id = data.split()[1]
            session = data.split()[2]
            sql_edit('DELETE FROM broadcast_chats WHERE chat_id = ? AND session = ?', (chat_id, session))
            callback_query.data = f'broadcast_chats {session}'
            data = callback_query.data

        if data.startswith('broadcast_chats '):

            session = data.split()[1]
            chats = sql_select(f"SELECT chat_id, chat_title FROM broadcast_chats WHERE session = '{session}'")
            kb = []
            for chat in (chats or []):
                title = chat[1] or chat[0]
                kb.append([InlineKeyboardButton(f'❌ {title}',
                                                callback_data=f'del_broadcast_chat {chat[0]} {session}')])
            kb.append([InlineKeyboardButton('Добавить чат ➕', callback_data=f'add_broadcast_chat {session}')])
            kb.append([InlineKeyboardButton('Назад ◀️', callback_data=f'broadcast {session}')])
            await callback_query.message.edit(
                '<b>Чаты для рассылки 📋</b>\n\nНажми на чат чтобы удалить его из списка',
                reply_markup=InlineKeyboardMarkup(kb))

        elif data.startswith('add_broadcast_chat '):

            session = data.split()[1]
            await callback_query.message.edit('📢 Введите ссылку на чат или @username:')

            async def wait(message):
                await remove_handler(message.from_user.id)
                try:
                    await app.delete_messages(message.from_user.id, message.id)
                except Exception:
                    pass

                link = message.text.strip()
                if link.startswith('https://t.me/'):
                    link = link[13:]
                elif link.startswith('t.me/'):
                    link = link[5:]
                elif link.startswith('@'):
                    link = link[1:]

                try:
                    chat = await workers_list[session].get_chat(link)
                    sql_edit(
                        'INSERT INTO broadcast_chats(chat_id, chat_title, session) VALUES(?, ?, ?)',
                        (str(chat.id), chat.title, session))
                    await callback_query.message.edit(
                        f'✅ Чат добавлен: <b>{chat.title}</b>',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('К списку чатов', callback_data=f'broadcast_chats {session}')]]))
                except Exception as e:
                    await callback_query.message.edit(
                        f'😛 Не удалось добавить чат\n\n<pre>{e}</pre>',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('Назад ◀️', callback_data=f'broadcast {session}')]]))

            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('change'):

            client = workers_list[data.split(" ", 2)[1]]
            
            
            client_data = await client.get_me()
            
            data_text = f'<b>Изменение профиля</b>\n\nТекущие данные:\n\n'\
                        f'Имя: <code>{client_data.first_name}</code>'
            
            if client_data.last_name:
                data_text += f'\nФамилия: <code>{client_data.last_name}</code>'
            else:
                data_text += f'\nФамилия не задана'
            
            if client_data.photo:
                data_text += f'\nФото: ✅'
            else:
                data_text += f'\nФото не задано'
            
            data_text += f'\n\n👇 Чтобы поменять или задать данные профиля, жмите кнопки ниже'
            
            try:
                await callback_query.message.edit(data_text, reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Фото', callback_data=f'update photo {client_data.phone_number}'),
                     InlineKeyboardButton('Имя', callback_data=f'update first_name {client_data.phone_number}')],
                    [InlineKeyboardButton('Описание', callback_data=f'update about {client_data.phone_number}'),
                     InlineKeyboardButton('Фамилия', callback_data=f'update last_name {client_data.phone_number}')],
                    [InlineKeyboardButton('Юзернейм', callback_data=f'update username {client_data.phone_number}')],
                    [InlineKeyboardButton('Профиль ⛓️', url=f'tg://user?id={client_data.id}')],
                    [InlineKeyboardButton('Назад ◀️', callback_data=f'user {client_data.phone_number}')]]))
            except PeerIdInvalid:
                await callback_query.message.edit(data_text, reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Фото', callback_data=f'update photo {client_data.phone_number}'),
                     InlineKeyboardButton('Имя', callback_data=f'update first_name {client_data.phone_number}')],
                    [InlineKeyboardButton('Описание', callback_data=f'update about {client_data.phone_number}'),
                     InlineKeyboardButton('Фамилия', callback_data=f'update last_name {client_data.phone_number}')],
                    [InlineKeyboardButton('Юзернейм', callback_data=f'update username {client_data.phone_number}')],
                    [InlineKeyboardButton('Профиль ⛓️', url=f'tg://user?id={client_data.id}')],
                    [InlineKeyboardButton('Назад ◀️', callback_data=f'user {client_data.phone_number}')]]))
                
        
        elif data == 'Main menu':
            await menu(callback_query.message)
        
        elif data.startswith('update'):
            
            args = data.split(" ", 2)
            client = workers_list[args[2]]
            
            async def end(message, client=client):
                await asyncio.sleep(2)
                
                client_data = await client.get_me()
                
                data_text = f'<b>Изменение профиля</b>\n\nТекущие данные:\n\n' \
                            f'Имя: <code>{client_data.first_name}</code>'
                
                if client_data.last_name:
                    data_text += f'\nФамилия: <code>{client_data.last_name}</code>'
                else:
                    data_text += f'\nФамилия не задана'
                
                if client_data.photo:
                    data_text += f'\nФото: ✅'
                else:
                    data_text += f'\nФото не задано'
                
                data_text += f'\n\n👇 Чтобы поменять или задать данные профиля, жмите кнопки ниже'
                
                try:
                    await callback_query.message.edit(data_text, reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('Фото', callback_data=f'update photo {client_data.phone_number}'),
                         InlineKeyboardButton('Имя', callback_data=f'update first_name {client_data.phone_number}')],
                        [InlineKeyboardButton('Описание', callback_data=f'update about {client_data.phone_number}'),
                         InlineKeyboardButton('Фамилия', callback_data=f'update last_name {client_data.phone_number}')],
                        [InlineKeyboardButton('Юзернейм', callback_data=f'update username {client_data.phone_number}')],
                        [InlineKeyboardButton('Профиль ⛓️', url=f'tg://user?id={client_data.id}')],
                        [InlineKeyboardButton('Назад ◀️', callback_data=f'user {client_data.phone_number}')]]))
                except PeerIdInvalid:
                    await callback_query.message.edit(data_text, reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('Фото', callback_data=f'update photo {client_data.phone_number}'),
                         InlineKeyboardButton('Имя', callback_data=f'update first_name {client_data.phone_number}')],
                        [InlineKeyboardButton('Описание', callback_data=f'update about {client_data.phone_number}'),
                         InlineKeyboardButton('Фамилия', callback_data=f'update last_name {client_data.phone_number}')],
                        [InlineKeyboardButton('Юзернейм', callback_data=f'update username {client_data.phone_number}')],
                        [InlineKeyboardButton('Профиль ⛓️', url=f'tg://user?id={client_data.id}')],
                        [InlineKeyboardButton('Назад ◀️', callback_data=f'user {client_data.phone_number}')]]))
                
            
            async def wait_for_photo(message, client=client):
                
                await remove_handler(message.from_user.id)
                await callback_query.message.edit('🌴 Ищем медиа...')
                
                async def progress(current, total):
                    await callback_query.message.edit(
                        f"🌴 Скачиваем аватарку... <code>{current * 100 / total:.1f}%</code>")
                
                if message.video:
                    
                    avatarka = await app.download_media(message, progress=progress, in_memory=True)
                    
                    await client.set_profile_photo(video=avatarka)
                    
                    await callback_query.message.edit(f"🌴 Фото профиля было успешно изменено!")
                
                elif message.photo:
                    
                    avatarka = await app.download_media(message, progress=progress,  in_memory=True)
                    
                    await client.set_profile_photo(photo=avatarka)
                    
                    await callback_query.message.edit(f"🌴 Фото профиля было успешно изменено!")
                
                else:
                    await callback_query.message.edit('❌ В качестве аватарки можно поставить только фото или видео')
                
                await app.delete_messages(message.from_user.id, message.id)
                await end(callback_query.message, client)
                
            async def wait_for_name(message, client=client):
                
                await remove_handler(message.from_user.id)
                
                if message.text:
                    try:
                        await client.update_profile(first_name=message.text)
                    except Exception as e:
                        await callback_query.message.edit(f'❌ Произошла ошибка<pre>{e}</pre>')
                else:
                    await callback_query.message.edit('❌ Имя должно быть текстом')
                
                await app.delete_messages(message.from_user.id, message.id)
                await end(callback_query.message, client)
                
            async def wait_for_surname(message, client=client):
                
                await remove_handler(message.from_user.id)
                
                if message.text:
                    try:
                        await client.update_profile(last_name=message.text)
                    except Exception as e:
                        await callback_query.message.edit(f'❌ Произошла ошибка<pre>{e}</pre>')
                else:
                    await callback_query.message.edit('❌ Фамилия должна быть текстом')
                
                await app.delete_messages(message.from_user.id, message.id)
                await end(callback_query.message, client)
                
            async def wait_for_about(message, client=client):
                
                await remove_handler(message.from_user.id)
                
                if message.text:
                    try:
                        await client.update_profile(bio=message.text)
                    except Exception as e:
                        await callback_query.message.edit(f'❌ Произошла ошибка<pre>{e}</pre>')
                else:
                    await callback_query.message.edit('❌ Описание должно быть текстом')
                
                await app.delete_messages(message.from_user.id, message.id)
                await end(callback_query.message, client)
                
            async def wait_for_username(message, client=client):
                
                await remove_handler(message.from_user.id)
                
                if message.text:
                    try:
                        await client.set_username(username=message.text)
                    except Exception as e:
                        await callback_query.message.edit(f'❌ Произошла ошибка<pre>{e}</pre>')
                else:
                    await callback_query.message.edit('❌ @юзернейм должен быть текстом')
                
                await app.delete_messages(message.from_user.id, message.id)
                await end(callback_query.message, client)
                
            functions = {
                'photo': ('новое фото', wait_for_photo),
                'first_name': ('новое имя', wait_for_name),
                'last_name': ('новую фамилию', wait_for_surname),
                'about': ('новое описание', wait_for_about),
                'username': ('новый @юзернейм', wait_for_username),
            }
            
            await callback_query.message.edit(f'✏️ Пришлите боту {functions[args[1]][0]}:')
            
            await add_handler(callback_query.from_user.id, functions[args[1]][1])
        
        elif data.startswith('neurocommenting ') and len(data.split()) == 2:

            session = data.split()[1]
            settings = sql_select(f"SELECT * FROM workers WHERE session = '{session}'")
            if not settings:
                return
            be_first_icon = '✅' if settings[0][6] == 1 else '❌'
            use_gpt_icon = '✅' if settings[0][9] == 1 else '❌'
            min_post_length = settings[0][19] if len(settings[0]) > 19 and settings[0][19] else 0
            text = (
                f'<b>Нейрокомментинг ⚙️</b>\n\n'
                f'Роль: <code>{settings[0][2] or "не задана"}</code>\n'
                f'Промпт: <code>{settings[0][7] or "не задан"}</code>\n\n'
                f'Задержка: <b>{settings[0][4]} сек</b>\n'
                f'Шанс: <b>1/{settings[0][5]}</b>\n'
                f'Первым: <b>{be_first_icon}</b>  Мин. символов: <b>{min_post_length or "нет"}</b>\n'
                f'Шаблоны: <b>{use_gpt_icon}</b>'
            )
            kb = [
                [InlineKeyboardButton('Промпт 🤖', callback_data=f'set what_to_write {session}')],
                [InlineKeyboardButton('Роль 🤖', callback_data=f'set prompt {session}')],
                [InlineKeyboardButton(f'Задержка: {settings[0][4]} сек ⌛', callback_data=f'set delay {session}'),
                 InlineKeyboardButton(f'Шанс: 1/{settings[0][5]} 🎲', callback_data=f'set chance {session}')],
                [InlineKeyboardButton(f'Первым {be_first_icon}', callback_data=f'switchbefirst {session}'),
                 InlineKeyboardButton(f'Мин. символов: {min_post_length or "нет"} 📏', callback_data=f'set min_post_length {session}')],
                [InlineKeyboardButton(f'Шаблоны {use_gpt_icon}', callback_data=f'switchgpt {session}')],
                [InlineKeyboardButton('Шаблоны коммента 📝', callback_data=f'editpretexts {session}')],
                [InlineKeyboardButton('Добавить каналы 💬', callback_data=f'newchannels {session} comments'),
                 InlineKeyboardButton('Список каналов 🧿', callback_data=f'allchannels {session}')],
                [InlineKeyboardButton('Последние посты 🌴', callback_data=f'posts {session}')],
                [InlineKeyboardButton('Назад к аккаунту ◀️', callback_data=f'user {session}')],
            ]
            await callback_query.message.edit(text, reply_markup=InlineKeyboardMarkup(kb))

        elif data.startswith('autolikes ') and len(data.split()) == 2:

            await _show_autolikes_menu(callback_query.message.edit, data.split()[1])

        elif data.startswith('togglelikesbig '):

            session = data.split()[1]
            current = sql_select(f"SELECT likes_big FROM workers WHERE session = '{session}'")
            new_val = 0 if (current and current[0][0]) else 1
            sql_edit('UPDATE workers SET likes_big = ? WHERE session = ?', (new_val, session))
            await _show_autolikes_menu(callback_query.message.edit, session)

        elif data.startswith('togglelikesnotify '):

            session = data.split()[1]
            current = sql_select(f"SELECT likes_notify FROM workers WHERE session = '{session}'")
            new_val = 0 if (current and current[0][0]) else 1
            sql_edit('UPDATE workers SET likes_notify = ? WHERE session = ?', (new_val, session))
            await _show_autolikes_menu(callback_query.message.edit, session)

        elif data.startswith('togglelikes '):

            session = data.split()[1]
            current = sql_select(f"SELECT likes_enabled FROM workers WHERE session = '{session}'")
            new_val = 0 if (current and current[0][0]) else 1
            sql_edit('UPDATE workers SET likes_enabled = ? WHERE session = ?', (new_val, session))
            await _show_autolikes_menu(callback_query.message.edit, session)

        elif data.startswith('likechats '):

            await _show_like_chats_menu(callback_query.message.edit, data.split()[1])

        elif data.startswith('rmlikechat '):

            chat_id = data.split()[1]
            session = data.split()[2]
            sql_edit('DELETE FROM like_chats WHERE chat_id = ? AND session = ?', (chat_id, session))
            await _show_like_chats_menu(callback_query.message.edit, session)

        elif data.startswith('clearlikechats '):

            session = data.split()[1]
            sql_edit('DELETE FROM like_chats WHERE session = ?', (session,))
            await _show_like_chats_menu(callback_query.message.edit, session)

        elif data.startswith('importlikechats '):

            session = data.split()[1]
            channels = sql_select(f"SELECT id FROM channels WHERE session = '{session}'")
            client = workers_list.get(session)

            imported = 0
            for channel in (channels or []):
                chat_id = str(channel[0])
                exists = sql_select(
                    f"SELECT chat_id FROM like_chats WHERE session = '{session}' AND chat_id = '{chat_id}'")
                if exists:
                    continue

                title = chat_id
                if client:
                    try:
                        chat = await client.get_chat(int(chat_id))
                        title = chat.title or chat_id
                    except Exception as e:
                        print(f'importlikechats: не получили чат {chat_id} \u2014 {e}')

                sql_edit('INSERT INTO like_chats(chat_id, chat_title, session) VALUES(?, ?, ?)',
                         (chat_id, title, session))
                imported += 1

            await callback_query.answer(f'Добавлено: {imported}')
            await _show_like_chats_menu(callback_query.message.edit, session)

        elif data.startswith('addlikechat '):

            session = data.split()[1]
            await callback_query.message.edit(
                '👍 Пришлите ссылку на канал или чат.\n\n'
                'Можно сразу несколько — каждый с новой строки.',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Отмена ✖️', callback_data=f'likechats {session}')]]))

            async def wait(message):

                await remove_handler(message.from_user.id)
                try:
                    await app.delete_messages(message.from_user.id, message.id)
                except Exception:
                    pass

                client = workers_list.get(session)
                if not client:
                    await callback_query.message.edit(
                        '😛 Аккаунт не найден',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('Назад ◀️', callback_data=f'autolikes {session}')]]))
                    return

                added = []
                failed = []

                for raw_link in (message.text or '').split('\n'):

                    link = raw_link.strip()
                    if not link:
                        continue

                    if link.startswith('https://t.me/+') or link.startswith('https://t.me/-'):
                        pass
                    elif link.startswith('https://t.me/'):
                        link = link[13:]
                    elif link.startswith('t.me/'):
                        link = link[5:]
                    elif link.startswith('@'):
                        link = link[1:]
                    elif link.lstrip('-').isdigit():
                        link = int(link)

                    try:
                        chat = None
                        try:
                            joined = await client.join_chat(link)
                            chat = joined if hasattr(joined, 'id') else getattr(joined, 'chat', None)
                        except UserAlreadyParticipant:
                            pass
                        except Exception as e:
                            print(f'addlikechat: не вступили в {link} \u2014 {e}')

                        if chat is None:
                            chat = await client.get_chat(link)

                        exists = sql_select(
                            f"SELECT chat_id FROM like_chats "
                            f"WHERE session = '{session}' AND chat_id = '{chat.id}'")
                        if not exists:
                            sql_edit('INSERT INTO like_chats(chat_id, chat_title, session) VALUES(?, ?, ?)',
                                     (str(chat.id), chat.title, session))

                        added.append(chat.title or str(chat.id))

                    except Exception as e:
                        failed.append(f'{raw_link.strip()} — {e}')

                text = '<b>Автолайки 👍</b>\n\n'
                if added:
                    text += '✅ Добавлены:\n' + '\n'.join(f'• {title}' for title in added) + '\n\n'
                if failed:
                    text += '❌ Не добавлены:\n' + '\n'.join(f'• {item}' for item in failed)
                if not added and not failed:
                    text += 'Ничего не добавлено'

                await callback_query.message.edit(
                    text,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('К списку чатов 🧿', callback_data=f'likechats {session}')],
                        [InlineKeyboardButton('К автолайкам ◀️', callback_data=f'autolikes {session}')]]))

            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('toggleautoreply '):

            session = data.split()[1]
            cur = sql_select(f"SELECT auto_reply_enabled FROM workers WHERE session = '{session}'")
            new_val = 0 if (cur and cur[0][0]) else 1
            sql_edit("UPDATE workers SET auto_reply_enabled = ? WHERE session = ?", (new_val, session))
            await _show_autoreply_menu(callback_query.message.edit, session)

        elif data.startswith('autoreply_menu'):

            session = data.split()[1]
            await _show_autoreply_menu(callback_query.message.edit, session)

        elif data.startswith('togglereplyai'):

            session = data.split()[1]
            current = sql_select(f"SELECT reply_use_ai FROM workers WHERE session = '{session}'")
            new_val = 0 if (current and current[0][0]) else 1
            sql_edit(f"UPDATE workers SET reply_use_ai = {new_val} WHERE session = '{session}'", ())
            await _show_autoreply_menu(callback_query.message.edit, session)

        elif data.startswith('clear_reply_history'):

            session = data.split()[1]
            sql_edit(f"DELETE FROM reply_history WHERE session = ?", (session,))
            await callback_query.message.edit(
                '🗑 История диалогов очищена.',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Назад ◀️', callback_data=f'autoreply_menu {session}')]]))

        elif data.startswith('confirmset auto_reply'):
            await callback_query.message.edit(
                '<b>Введите новый текст для автоответчика:</b>\n\n'
                'Можете написать что угодно. Например, предложение подписаться на ваш канал')

            async def wait(info):
                try:
                    await app.delete_messages(info.chat.id, info.id)
                except Exception:
                    pass

                try:
                    await remove_handler(callback_query.from_user.id)
                    sql_edit(f"UPDATE workers SET {data.split(' ', 2)[1]} = ? "
                             f"WHERE session = '{data.split(' ', 2)[2]}'", (info.text,))

                    session = data.split(' ', 2)[2]
                    await callback_query.message.edit(
                        '👍 Текст сохранён',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton('К автоответам ◀️', callback_data=f'autoreply_menu {session}')]]))

                except Exception as e:
                    await callback_query.message.edit(f'😛 Произошла ошибка\n\n<pre>{e}</pre>')
                    await remove_handler(callback_query.from_user.id)

            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('no auto_reply'):
            sql_edit(f"UPDATE workers SET {data.split(' ', 2)[1]} = ? "
                     f"WHERE session = '{data.split(' ', 2)[2]}'", ('-',))
            await callback_query.message.edit(
                'Статичный автоответ отключён.',
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton('К автоответам ◀️', callback_data='autoreply_menu ' + data.split()[2])]]))
            
        elif data.startswith('set '):
            
            if data.startswith('set prompt'):
                await callback_query.message.edit(
                    'Введите новую роль для ИИ:\n\n'
                    'Например:\n\n'
                    '• <code>Вы - студентка</code>\n'
                    '• <code>Вы - маркетолог</code>\n'
                    '• <code>Вы - Стив Джобс</code>\n')
            if data.startswith('set what_to_write'):
                await callback_query.message.edit(
                    'Введите новый промпт для ИИ:\n\n'
                    'Например:\n\n'
                    '• <code>Напиши интересный комментарий до 10 слов на тему этого поста:</code>\n'
                    '• <code>Напиши короткую историю на тему этого поста:</code>\n'
                    '• <code>Придумай шутку на тему этого поста:</code>\n')
            elif data.startswith('set auto_reply'):
                await callback_query.message.edit(
                    'Выберите действие:',
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton('Задать новый текст ✏️', callback_data='confirm' + data)],
                         [InlineKeyboardButton('Убрать автоответ', callback_data='no auto_reply ' + data.split()[2]),
                          InlineKeyboardButton('Назад ◀️', callback_data='autoreply_menu ' + data.split()[2])]]))
                return
            
            elif data.startswith('set reply_role'):
                await callback_query.message.edit(
                    'Введите роль для ИИ в личных сообщениях:\n\n'
                    'Например:\n'
                    '• <code>Вы — менеджер по продажам. Отвечайте вежливо и помогайте клиентам.</code>\n'
                    '• <code>Вы — эксперт по недвижимости.</code>')

            elif data.startswith('set reply_prompt'):
                await callback_query.message.edit(
                    'Введите промпт для ИИ-диалога в личных сообщениях:\n\n'
                    'Это дополнительные инструкции поверх роли. Например:\n'
                    '• <code>Отвечай коротко, задавай уточняющий вопрос в конце каждого сообщения.</code>\n'
                    '• <code>Мягко веди собеседника к записи на консультацию.</code>')

            elif data.startswith('set reply_max_rounds'):
                await callback_query.message.edit(
                    'Введите максимальное количество раундов диалога:\n\n'
                    '• <code>3</code> — ответит 3 раза, потом замолчит\n'
                    '• <code>0</code> — без ограничений')

            elif data.startswith('set reply_delay'):
                await callback_query.message.edit(
                    'Введите задержку перед автоответом в секундах:\n\n'
                    '• <code>0</code> — без задержки\n'
                    '• <code>5</code> — подождёт 5 секунд перед ответом')

            elif data.startswith('set min_post_length'):
                await callback_query.message.edit(
                    'Введите минимальное количество символов в посте для комментирования:\n\n'
                    '• <code>0</code> — комментировать любые посты\n'
                    '• <code>100</code> — пропускать посты короче 100 символов')

            elif data.startswith('set likes_emojis'):
                await callback_query.message.edit(
                    'Пришлите реакции, которые будет ставить аккаунт — через пробел или запятую:\n\n'
                    '• <code>👍</code> — всегда одна реакция\n'
                    '• <code>👍 ❤️ 🔥 🥰 👏</code> — бот выберет случайную из списка',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('Отмена ✖️', callback_data=f'autolikes {data.split()[2]}')]]))

            elif data.startswith('set likes_delay'):
                await callback_query.message.edit(
                    'Введите задержку перед лайком, в секундах:\n\n'
                    '• <code>0</code> — лайкать сразу\n'
                    '• <code>30</code> — подождать 30 секунд после выхода поста',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('Отмена ✖️', callback_data=f'autolikes {data.split()[2]}')]]))

            elif data.startswith('set likes_chance'):
                await callback_query.message.edit(
                    'Введите шанс, с которым будут ставиться лайки:\n\n'
                    'Если хотите 100%, введите 1 (1к<b>1</b>)\n'
                    'Если хотите 50%, введите 2 (1к<b>2</b>)\n'
                    'Если хотите 33%, введите 3 (1к<b>3</b>)',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('Отмена ✖️', callback_data=f'autolikes {data.split()[2]}')]]))

            elif data.startswith('set delay'):
                await callback_query.message.edit('Введите новое значение задержки, в секундах:')

            elif data.startswith('set dm_text'):
                await callback_query.message.edit('Введите текст сообщения, которое будет отправляться в ЛС:')

            elif data.startswith('set dm_daily_limit'):
                await callback_query.message.edit('Введите максимальное количество DM в день:')

            elif data.startswith('set dm_delay'):
                await callback_query.message.edit('Введите задержку между DM в секундах:')

            elif data.startswith('set broadcast_text'):
                await callback_query.message.edit(
                    'Введите текст сообщения для рассылки:',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('Отмена ✖️', callback_data=f'broadcast {data.split()[2]}')]]))

            elif data.startswith('set broadcast_interval'):
                await callback_query.message.edit(
                    'Введите интервал между сообщениями в секундах в формате <b>мин-макс</b>:\n\n'
                    '• <code>600-1500</code> — от 10 до 25 минут\n'
                    '• <code>3600-7800</code> — от 1 до 2.1 часа',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton('Отмена ✖️', callback_data=f'broadcast {data.split()[2]}')]]))

            elif data.startswith('set chance'):
                await callback_query.message.edit('Введите шанс, с которым будут оставляться комментарии\n\n'
                                                  'Если хотите 33%, введите 3 (1к<b>3</b>)\n'
                                                  'Если хотите 50%, введите 2 (1к<b>3</b>)\n'
                                                  'Если хотите 100%, введите 1 (1к<b>1</b>)')
            
            async def wait(info):
                try:
                    await app.delete_messages(info.chat.id, info.id)
                except Exception:
                    pass

                field = data.split(' ', 2)[1]
                session = data.split(' ', 2)[2]
                if field.startswith('broadcast_'):
                    back_cb = f'broadcast {session}'
                    back_label = 'Назад к рассылке ◀️'
                elif field in ('what_to_write', 'prompt', 'delay', 'chance', 'min_post_length'):
                    back_cb = f'neurocommenting {session}'
                    back_label = 'Назад к нейрокомментингу ◀️'
                elif field in ('reply_role', 'reply_prompt', 'reply_max_rounds', 'reply_delay'):
                    back_cb = f'autoreply_menu {session}'
                    back_label = 'Назад к автоответам ◀️'
                elif field.startswith('likes_'):
                    back_cb = f'autolikes {session}'
                    back_label = 'Назад к автолайкам ◀️'
                else:
                    back_cb = f'user {session}'
                    back_label = 'Назад к аккаунту ◀️'

                try:
                    await remove_handler(callback_query.from_user.id)

                    value = info.text

                    if field == 'likes_emojis':
                        value = ','.join(parse_emojis(info.text))

                    elif field in ('likes_delay', 'likes_chance'):
                        try:
                            value = float((info.text or '').replace(',', '.'))
                        except (TypeError, ValueError):
                            await callback_query.message.edit(
                                '😛 Нужно число. Попробуйте ещё раз.',
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton(back_label, callback_data=back_cb)]]))
                            return
                        if field == 'likes_chance':
                            value = max(1, int(value))
                        else:
                            value = max(0, value)

                    sql_edit(f"UPDATE workers SET {field} = ? WHERE session = '{session}'", (value,))

                    await callback_query.message.edit('👍 Данные обновлены',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(back_label, callback_data=back_cb)]]))

                except Exception as e:
                    await callback_query.message.edit(
                        f'😛 Произошла ошибка\n\n<pre>{e}</pre>',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(back_label, callback_data=back_cb)]]))
                    await remove_handler(callback_query.from_user.id)

            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('bot'):
            
            sent = await app.send_message(callback_query.from_user.id,
                                          "🤖 Введите токен бота, который будет "
                                          "использоваться для управления аккаунтами:",
                                          reply_markup=ReplyKeyboardMarkup([['Отмена ❌']], resize_keyboard=True))
            
            async def wait(token):

                await remove_handler(callback_query.from_user.id)
                try:
                    await app.delete_messages(token.chat.id, token.id)
                    await app.delete_messages(sent.chat.id, sent.id)
                except Exception:
                    pass

                if token.text == 'Отмена ❌':
                    
                    sent2 = await app.send_message(callback_query.from_user.id,
                                                  "❌ Отменено",
                                                  reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(2)
                    await app.delete_messages(sent2.chat.id, sent2.id)
                    return
            
                try:
                    global MANAGERS_COUNT
                    
                    client = Client(f"bot{MANAGERS_COUNT}", api_id=API_ID, api_hash=API_HASH, bot_token=token.text, proxy=GLOBAL_PROXY)
                    MANAGERS_COUNT += 1
                    
                    setup_manager(client)
                    await client.start()
                    
                    sql_edit('INSERT INTO managers(token) VALUES(?)', (token.text,))
                    sent2 = await app.send_message(callback_query.from_user.id, "👌 Бот подключен",
                                                   reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(2)
                    await app.delete_messages(sent2.chat.id, sent2.id)
                
                except Exception as e:
                    await sent.edit(f'😛 Произошла ошибка\n\n<pre>{e}</pre>')
            
            await add_handler(callback_query.from_user.id, wait)
        
        elif data.startswith('search'):
            
            sent = await app.send_message(callback_query.from_user.id,
                                          "🔍 Введите запрос для поиска:",
                                          reply_markup=ReplyKeyboardMarkup([['Отмена ❌']], resize_keyboard=True))
            
            async def wait(message):
                await remove_handler(callback_query.from_user.id)
                try:
                    await app.delete_messages(sent.chat.id, sent.id)
                    await app.delete_messages(message.chat.id, message.id)
                except Exception:
                    pass

                if message.text == 'Отмена ❌':
                    sent2 = await app.send_message(callback_query.from_user.id, "❌ Отменено",
                                                   reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(2)
                    try:
                        await app.delete_messages(sent2.chat.id, sent2.id)
                    except Exception:
                        pass
                    return

                try:
                    if workers_list:
                        
                        notification = await callback_query.message.reply(
                            'Ждём, пока AI подберёт ключевые слова... 😊')
                        

                        keywords = generate_keywords(message.text)
                        keywords = keywords.split('\n')
                        
                        keywords_to_remove = []
                        
                        for i in range(len(keywords)):
                            keyword = keywords[i]
                            if '. ' not in keyword:
                                keywords_to_remove.append(keyword)
                            else:
                                keywords[i] = keyword.split('. ')[1]
                        
                        for keyword in keywords_to_remove:
                            keywords.remove(keyword)
                        
                        keywords.append(message.text)
                        
                        for keyword in keywords:
                        
                            channels = await list(workers_list.values())[0].invoke(Search(q=keyword, limit=100))
                            if channels.chats:
                            
                                text = f'🌴 Каналы по запросу "{keyword}":\n'
                                
                                i = 1
                                for channel in channels.chats:
                                    if channel.broadcast:
                                        if channel.username:
                                            text += f'\n<b>{i}.</b> {channel.title} (@{channel.username})'
                                        elif channel.usernames:
                                            text += f'\n<b>{i}.</b> {channel.title} (@{channel.usernames[0].username})'
                                        i += 1
                                
                            
                                await notification.reply(text)
                    
                except Exception as e:
                    await callback_query.message.reply(f'😛 Произошла ошибка\n\n<pre>{e}</pre>')
            
            await add_handler(callback_query.from_user.id, wait)
        
        
        elif data.startswith('parse_comments'):
            
            sent = await app.send_message(
                callback_query.from_user.id, "🌴 Введите ссылку на канал:",
                reply_markup=ReplyKeyboardMarkup([['Отмена ❌']], resize_keyboard=True))
            
            async def wait(message, sent=sent):
                await remove_handler(callback_query.from_user.id)
                try:
                    await app.delete_messages(sent.chat.id, sent.id)
                    await app.delete_messages(message.chat.id, message.id)
                except Exception:
                    pass

                if message.text == 'Отмена ❌':
                    sent2 = await app.send_message(callback_query.from_user.id, "❌ Отменено",
                                                   reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(2)
                    try:
                        await app.delete_messages(sent2.chat.id, sent2.id)
                    except Exception:
                        pass
                    return
                
                try:
                    
                    if workers_list:
                    
                        if message.text.startswith('https://t.me/'):
                            chat_id = str(message.text[13:])
                        elif message.text.startswith('t.me/'):
                            chat_id = str(message.text[5:])
                        else:
                            chat_id = message.text
                        
                        client = random.choice(list(workers_list.values()))
                        
                        msgs = []
                        user_counts = {}  # {user_id: {'username': str|None, 'count': int}}

                        global working
                        invalids = 0

                        working = True

                        async for msg in client.get_chat_history(chat_id):
                            msgs.append(msg.id)

                        sent = await callback_query.message.reply(
                            f'Найдено {len(msgs)} постов, начинаю парсить комментарии\n\n'
                            f'<i>Завершить парсинг досрочно - /finish</i>')

                        async def finish(finishmessage):
                            if finishmessage.text == '/finish':
                                global working
                                working = False

                        await add_handler(callback_query.from_user.id, finish)


                        for message_id in msgs:
                            if working:

                                try:
                                    client = random.choice(list(workers_list.values()))
                                    async for message in client.get_discussion_replies(chat_id, message_id):

                                        if message.from_user:
                                            uid = message.from_user.id
                                            if uid not in user_counts:
                                                user_counts[uid] = {'username': None, 'count': 0}
                                            user_counts[uid]['count'] += 1
                                            if message.from_user.username:
                                                user_counts[uid]['username'] = message.from_user.username

                                except FloodWait as e:
                                    await asyncio.sleep(e.value)

                                except MsgIdInvalid:
                                    invalids += 1
                                    await sent.edit(f'Найдено {len(msgs)} постов, парсинг в процессе\n'
                                                    f'Бот не смог получить {invalids}💬\n\n'
                                                    f'<i>Завершить парсинг досрочно - /finish</i>')

                                except Exception as e:
                                    await callback_query.message.reply(
                                        f'🤖 Ошибка при парсинге\n\n Застряли на сообщении с id {message_id}\n\n'
                                        f'<pre>{e}</pre>')
                            else:
                                break

                        await remove_handler(callback_query.from_user.id)

                        sorted_users = sorted(user_counts.items(), key=lambda x: x[1]['count'], reverse=True)

                        id_results = open(f"IDs {chat_id}.txt", "w+")
                        id_results.truncate()
                        for uid, data in sorted_users:
                            id_results.write(f'{uid} — {data["count"]} комм.\n')
                        id_results.close()

                        username_results = open(f"Usernames {chat_id}.txt", "w+")
                        username_results.truncate()
                        for uid, data in sorted_users:
                            if data['username']:
                                username_results.write(f'@{data["username"]} — {data["count"]} комм.\n')
                        username_results.close()

                        await app.send_document(callback_query.from_user.id, f'IDs {chat_id}.txt')
                        await app.send_document(callback_query.from_user.id, f'Usernames {chat_id}.txt')
                
                except Exception as e:
                    await callback_query.message.reply(f'😛 {e}')
            
            await add_handler(callback_query.from_user.id, wait)
        
        elif data == 'service channel':
            
            sent = await app.send_message(callback_query.from_user.id,
                                          "<b>🤖 Чтобы добавить канал для уведомлений:</b>\n\n"
                                          "1) Дайте боту админку в канале\n"
                                          "2) Пришлите ссылку на канал 👇",
                                          reply_markup=ReplyKeyboardMarkup([['Отмена ❌']], resize_keyboard=True))
            
            async def wait(channel_message):
                
                await remove_handler(callback_query.from_user.id)
                
                if channel_message.text == 'Отмена ❌':
                    sent2 = await app.send_message(
                        callback_query.from_user.id, "❌ Отменено", reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(1.1)
                    await app.delete_messages(sent2.chat.id, sent2.id)
                    return
                
                channel_message = channel_message.text
                
                try:
                    
                    if channel_message.startswith('https://t.me/+') or channel_message.startswith('https://t.me/-'):
                        await app.send_message(callback_query.from_user.id, 'Бот не может заходить по пригласительным ссылкам')
                        return
                    elif channel_message.startswith('https://t.me/'):
                        channel_message = channel_message[13:]
                    elif channel_message.startswith('t.me/'):
                        channel_message = channel_message[5:]
                    elif channel_message.startswith('@'):
                        channel_message = channel_message[1:]
                    elif channel_message.isdigit():
                        channel_message = int(channel_message)
                        
                    service_channel = await app.get_chat(channel_message)

                    sql_edit('UPDATE managers SET channel = ?', (service_channel.id,))

                    # Тестовая отправка от менеджер-бота чтобы проверить доступ
                    test_ok = False
                    test_error = ''
                    if manager_apps:
                        try:
                            await manager_apps[0].send_message(
                                service_channel.id,
                                f'✅ Канал уведомлений настроен. Буду присылать сюда логи работы.')
                            test_ok = True
                        except Exception as e:
                            test_error = str(e)

                    if test_ok:
                        result_text = f"👌 Уведомления настроены в {service_channel.title}"
                    else:
                        result_text = (
                            f"⚠️ Канал сохранён, но бот не может туда писать.\n\n"
                            f"Убедитесь что бот добавлен администратором в {service_channel.title}\n\n"
                            f"Ошибка: <code>{test_error}</code>")

                    sent2 = await app.send_message(
                        callback_query.from_user.id,
                        result_text,
                        reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(3)
                    try:
                        await app.delete_messages(sent2.chat.id, sent2.id)
                    except Exception:
                        pass
                
                except Exception as e:
                    print(e)
                    await channel_message.reply(f'😛 Произошла ошибка\n\n<pre>{e}</pre>')
            
            await add_handler(callback_query.from_user.id, wait)
        
        elif data.startswith('token'):
            
            sent = await app.send_message(callback_query.from_user.id,
                                          "🤖 Введите Bearer-токен AI:",
                                          reply_markup=ReplyKeyboardMarkup([['Отмена ❌']], resize_keyboard=True))

            async def wait(token):

                await remove_handler(callback_query.from_user.id)
                await app.delete_messages(token.chat.id, token.id)
                await app.delete_messages(sent.chat.id, sent.id)

                if token.text == 'Отмена ❌':

                    sent2 = await app.send_message(callback_query.from_user.id,
                                                   "❌ Отменено",
                                                   reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(2)
                    await app.delete_messages(sent2.chat.id, sent2.id)
                    return

                try:

                    F = open('tw_token.txt', 'w', encoding='utf-8')
                    F.write(token.text.strip())
                    F.close()

                    sent2 = await app.send_message(callback_query.from_user.id, "👌 Токен изменён",
                                                   reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(2)
                    await app.delete_messages(sent2.chat.id, sent2.id)

                except Exception as e:
                    await sent.edit(f'😛 Произошла ошибка\n\n<pre>{e}</pre>')
            
            await add_handler(callback_query.from_user.id, wait)
            
        elif data.startswith('proxy'):
            
            sent = await app.send_message(
                callback_query.from_user.id,
                "🤖 Введите Agent Access ID:",
                reply_markup=ReplyKeyboardMarkup([['Отмена ❌']], resize_keyboard=True))

            async def wait(token):

                await remove_handler(callback_query.from_user.id)
                await app.delete_messages(token.chat.id, token.id)
                await app.delete_messages(sent.chat.id, sent.id)

                if token.text == 'Отмена ❌':

                    sent2 = await app.send_message(callback_query.from_user.id,
                                                   "❌ Отменено",
                                                   reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(2)
                    await app.delete_messages(sent2.chat.id, sent2.id)
                    return

                try:

                    f = open('tw_agent_id.txt', 'w', encoding='utf-8')
                    f.write(token.text.strip())
                    f.close()

                    sent2 = await app.send_message(callback_query.from_user.id, "👌 Agent ID изменён",
                                                   reply_markup=MAIN_KEYBOARD)
                    await asyncio.sleep(2)
                    await app.delete_messages(sent2.chat.id, sent2.id)

                except Exception as e:
                    await sent.edit(f'😛 Произошла ошибка\n\n<pre>{e}</pre>')
            
            await add_handler(callback_query.from_user.id, wait)
            
        elif data == 'manage_admins':

            db_rows = sql_select('SELECT user_id FROM bot_admins')
            kb = []
            for row in (db_rows or []):
                kb.append([InlineKeyboardButton(f'❌ {row[0]}', callback_data=f'del_admin {row[0]}')])
            kb.append([InlineKeyboardButton('Добавить ➕', callback_data='add_admin')])
            kb.append([InlineKeyboardButton('Назад ◀️', callback_data='Main menu')])
            await callback_query.message.edit(
                '<b>Администраторы бота 👥</b>\n\nНажми на ID чтобы удалить',
                reply_markup=InlineKeyboardMarkup(kb))

        elif data.startswith('del_admin '):

            user_id = data.split()[1]
            sql_edit('DELETE FROM bot_admins WHERE user_id = ?', (user_id,))
            if int(user_id) in admins:
                admins.remove(int(user_id))
            db_rows = sql_select('SELECT user_id FROM bot_admins')
            kb = []
            for row in (db_rows or []):
                kb.append([InlineKeyboardButton(f'❌ {row[0]}', callback_data=f'del_admin {row[0]}')])
            kb.append([InlineKeyboardButton('Добавить ➕', callback_data='add_admin')])
            kb.append([InlineKeyboardButton('Назад ◀️', callback_data='Main menu')])
            await callback_query.message.edit(
                '<b>Администраторы бота 👥</b>\n\nНажми на ID чтобы удалить',
                reply_markup=InlineKeyboardMarkup(kb))

        elif data == 'add_admin':

            await callback_query.message.edit('Введите Telegram ID нового администратора:',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Отмена ✖️', callback_data='manage_admins')]]))

            async def wait(message):
                await remove_handler(callback_query.from_user.id)
                try:
                    await app.delete_messages(message.chat.id, message.id)
                except Exception:
                    pass
                try:
                    new_id = int(message.text.strip())
                    sql_edit('INSERT OR IGNORE INTO bot_admins(user_id) VALUES(?)', (str(new_id),))
                    if new_id not in admins:
                        admins.append(new_id)
                    db_rows = sql_select('SELECT user_id FROM bot_admins')
                    kb = []
                    for row in (db_rows or []):
                        kb.append([InlineKeyboardButton(f'❌ {row[0]}', callback_data=f'del_admin {row[0]}')])
                    kb.append([InlineKeyboardButton('Добавить ➕', callback_data='add_admin')])
                    kb.append([InlineKeyboardButton('Назад ◀️', callback_data='Main menu')])
                    await callback_query.message.edit(
                        f'✅ Добавлен: <code>{new_id}</code>\n\n<b>Администраторы бота 👥</b>\n\nНажми на ID чтобы удалить',
                        reply_markup=InlineKeyboardMarkup(kb))
                except ValueError:
                    await callback_query.message.edit('❌ Неверный формат. Введите числовой Telegram ID.',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад ◀️', callback_data='manage_admins')]]))
                except Exception as e:
                    await callback_query.message.edit(f'😛 Ошибка\n\n<pre>{e}</pre>',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад ◀️', callback_data='manage_admins')]]))
                    await remove_handler(callback_query.from_user.id)

            await add_handler(callback_query.from_user.id, wait)

        elif data == 'set_global_proxy':

            global GLOBAL_PROXY
            db_row = sql_select('SELECT global_proxy FROM managers LIMIT 1')
            current_proxy = db_row[0][0] if db_row and db_row[0][0] else (PROXY or 'не задан')
            await callback_query.message.edit(
                f'<b>Общий прокси 🌐</b>\n\nТекущий: <code>{current_proxy}</code>\n\n'
                f'Введите новый прокси в формате:\n'
                f'<code>socks5:host:port</code>\n'
                f'<code>socks5:host:port:user:pass</code>\n\n'
                f'Или нажмите «Без прокси» чтобы убрать',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Без прокси', callback_data='clear_global_proxy')],
                    [InlineKeyboardButton('Отмена ✖️', callback_data='Main menu')]]))

            async def wait(message):
                global GLOBAL_PROXY
                await remove_handler(callback_query.from_user.id)
                try:
                    await app.delete_messages(message.chat.id, message.id)
                except Exception:
                    pass
                try:
                    proxy_str = message.text.strip()
                    parsed = format_proxy(proxy_str.split(':'))
                    if not parsed:
                        await callback_query.message.edit('❌ Неверный формат прокси.',
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад ◀️', callback_data='Main menu')]]))
                        return
                    GLOBAL_PROXY = parsed
                    sql_edit('UPDATE managers SET global_proxy = ?', (proxy_str,))
                    await callback_query.message.edit(
                        f'✅ Общий прокси обновлён: <code>{proxy_str}</code>',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад ◀️', callback_data='Main menu')]]))
                except Exception as e:
                    await callback_query.message.edit(f'😛 Ошибка\n\n<pre>{e}</pre>',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад ◀️', callback_data='Main menu')]]))
                    await remove_handler(callback_query.from_user.id)

            await add_handler(callback_query.from_user.id, wait)

        elif data == 'clear_global_proxy':

            global GLOBAL_PROXY
            GLOBAL_PROXY = None
            sql_edit('UPDATE managers SET global_proxy = ?', (None,))
            await callback_query.message.edit(
                '✅ Общий прокси убран.',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад ◀️', callback_data='Main menu')]]))

        elif data.startswith('set_worker_proxy '):

            session = data.split()[1]
            cur = sql_select(f"SELECT proxy FROM workers WHERE session = '{session}'")
            current_proxy = cur[0][0] if cur and cur[0][0] else 'не задан'
            await callback_query.message.edit(
                f'<b>Прокси аккаунта</b>\n\nТекущий: <code>{current_proxy}</code>\n\n'
                f'Введите новый прокси:\n'
                f'<code>socks5:host:port</code>\n'
                f'<code>socks5:host:port:user:pass</code>',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('Без прокси', callback_data=f'clear_worker_proxy {session}')],
                    [InlineKeyboardButton('Отмена ✖️', callback_data=f'user {session}')]]))

            async def wait(message):
                await remove_handler(callback_query.from_user.id)
                try:
                    await app.delete_messages(message.chat.id, message.id)
                except Exception:
                    pass
                try:
                    proxy_str = message.text.strip()
                    if not format_proxy(proxy_str.split(':')):
                        await callback_query.message.edit('❌ Неверный формат прокси.',
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Назад ◀️', callback_data=f'user {session}')]]))
                        return
                    sql_edit(f"UPDATE workers SET proxy = ? WHERE session = ?", (proxy_str, session))
                    await callback_query.message.edit(
                        f'✅ Прокси обновлён: <code>{proxy_str}</code>',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('К аккаунту ◀️', callback_data=f'user {session}')]]))
                except Exception as e:
                    await callback_query.message.edit(f'😛 Ошибка\n\n<pre>{e}</pre>',
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('К аккаунту ◀️', callback_data=f'user {session}')]]))
                    await remove_handler(callback_query.from_user.id)

            await add_handler(callback_query.from_user.id, wait)

        elif data.startswith('clear_worker_proxy '):

            session = data.split()[1]
            sql_edit("UPDATE workers SET proxy = ? WHERE session = ?", (None, session))
            await callback_query.message.edit(
                '✅ Прокси убран. Будет использоваться общий прокси.',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('К аккаунту ◀️', callback_data=f'user {session}')]]))

        elif data == '+':
            sent = await callback_query.message.reply("Введите номер аккаунта:\n\n<i>/cancel для отмены</i>")
            
            async def wait(number):
                
                if number.text.startswith('8'):
                    number.text = number.text.replace('8', '7', 1)
                
                await app.delete_messages(sent.chat.id, sent.id)
                await app.delete_messages(number.chat.id, number.id)
                sent2 = await callback_query.message.reply("Введите SOCKS5 прокси в формате ниже:\n\n"
                                                  "<code>scheme:ip:port:username:password</code>\n\n"
                                                  "Пример: socks5:45.145.57.238:10253:ucXRvV:dcEscZ",
                                                  reply_markup=ReplyKeyboardMarkup([['Без прокси']],
                                                                                   resize_keyboard=True))
                
                async def wait_for(proxy):
                    
                    await app.delete_messages(sent2.chat.id, sent2.id)
                    await app.delete_messages(proxy.chat.id, proxy.id)
                    
                    try:
                        
                        phone = number.text.replace(' ', '').replace('+', '')
                        
                        if proxy.text == 'Без прокси':
                            client = Client(phone, API_ID, API_HASH, proxy=GLOBAL_PROXY)

                        else:
                            
                            proxy_to_login = proxy.text.split(':')
                            proxy_to_login = format_proxy(proxy_to_login)
                            if not proxy_to_login:
                                await app.send_message(number.from_user.id,
                                                       "Неправильный формат прокси. Пример правильного формата ниже:\n\n"
                                                       "<pre>socks5:45.145.57.238:10253:ucXRvV:dcEscZ</pre>")
                                return
                            
                            client = Client(phone, API_ID, API_HASH, proxy=proxy_to_login)
                        
                        # подключаемся к серверам
                        await client.connect()
                        
                        # отправляем код для входа
                        s_code = await client.send_code(phone)
                        
                        await remove_handler(callback_query.from_user.id)
                        await app.send_message(
                            callback_query.from_user.id,
                            "👇 Введите код, который прислал Telegram",
                            reply_markup=ReplyKeyboardMarkup([['Отмена ❌']], resize_keyboard=True))
                        
                        async def wait_for_the(code):
                            await remove_handler(callback_query.from_user.id)
                            await app.delete_messages(code.chat.id, code.id)
                            
                            try:
                                try:
                                    await client.sign_in(phone, s_code.phone_code_hash, code.text)
                                
                                except SessionPasswordNeeded:
                                    
                                    await callback_query.message.reply("👇 Введите код двухфакторной ауентификации "
                                                                       "(Пароль от аккаунта)")
                                    
                                    async def wait_for_pass(word):
                                        await remove_handler(callback_query.from_user.id)
                                        await app.delete_messages(code.chat.id, code.id)
                                        
                                        try:
                                            await client.check_password(word.text)
                                            await client.disconnect()
                                            await client.start()
                                            setup_worker(client)
                                            
                                            if proxy.text != 'Без прокси':
                                                sql_edit('INSERT INTO workers(session, proxy, prompt, auto_reply, delay, chance, be_first, what_to_write, mode) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                                         (phone, proxy.text,
                                                          'Вы - двадцатилетняя девушка',
                                                          "Отвечу только когда перейдёшь по ссылке в описании)", 0, 1, 1,
                                      'Напиши интересный комментарий до 10 слов на тему этого поста:', 'text'))
                                            else:
                                                sql_edit('INSERT INTO workers(session, proxy, prompt, auto_reply, delay, chance, be_first, what_to_write, mode) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                                         (phone, None,
                                                          'Вы - двадцатилетняя девушка',
                                                          "Отвечу только когда перейдёшь по ссылке в описании)", 0, 1, 1,
                                      'Напиши интересный комментарий до 10 слов на тему этого поста:', 'text'))
                                            workers_list[phone] = client
                                            
                                            sent3 = await callback_query.message.reply(
                                                "👌 Аккаунт подключен",
                                                reply_markup=ReplyKeyboardMarkup([['Мои аккаунты 🙂'], ['Меню 🌴']],
                                                                                 resize_keyboard=True))
                                            accounts_message = await callback_query.message.reply('...')
                                            await asyncio.sleep(1)
                                            await accounts(accounts_message)
                                        
                                        
                                        except Exception as e2:
                                            await app.send_message(number.from_user.id,
                                                                   f'😛 Произошла ошибка\n\n<pre>{e2}</pre>')
                                            await remove_handler(callback_query.from_user.id)
                                    
                                    await add_handler(number.from_user.id, wait_for_pass)
                                    return
                                
                                await client.disconnect()
                                await client.start()
                                setup_worker(client)
                                worker_apps.append(client)
                                
                                if proxy.text != 'Без прокси':
                                    sql_edit('INSERT INTO workers(session, proxy, prompt, auto_reply, delay, chance, be_first, what_to_write, mode) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                             (phone, proxy.text,
                                              'Вы - двадцатилетняя девушка',
                                              "Отвечу только когда перейдёшь по ссылке в описании)", 0, 1, 1,
                                      'Напиши интересный комментарий до 10 слов на тему этого поста:', 'text'))
                                else:
                                    sql_edit('INSERT INTO workers(session, proxy, prompt, auto_reply, delay, chance, be_first, what_to_write, mode) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                             (phone, None,
                                              'Вы - двадцатилетняя девушка',
                                              "Отвечу только когда перейдёшь по ссылке в описании)", 0, 1, 1,
                                      'Напиши интересный комментарий до 10 слов на тему этого поста:', 'text'))
                                workers_list[phone] = client
                                
                                sent3 = await callback_query.message.reply(
                                    "👌 Аккаунт подключен",
                                    reply_markup=ReplyKeyboardMarkup([['Мои аккаунты 🙂'], ['Меню 🌴']],
                                                                     resize_keyboard=True))
                                accounts_message = await callback_query.message.reply('...')
                                await asyncio.sleep(1)
                                await accounts(accounts_message)
                                await sent3.delete()
                            
                            
                            except Exception as e2:
                                await app.send_message(number.from_user.id, f'😛 Произошла ошибка\n\n<pre>{e2}</pre>')
                        
                        await remove_handler(callback_query.from_user.id)
                        await add_handler(callback_query.from_user.id, wait_for_the)
                    
                    except Exception as e:
                        await app.send_message(number.from_user.id, f'😛 Произошла ошибка\n\n<pre>{e}</pre>',
                                               reply_markup=ReplyKeyboardMarkup([['Мои аккаунты 🙂'], ['Меню 🌴']], resize_keyboard=True))
                    
                
                await remove_handler(callback_query.from_user.id)
                await add_handler(callback_query.from_user.id, wait_for)
            
            
            await add_handler(callback_query.from_user.id, wait)


MSK = timezone(timedelta(hours=3))


async def broadcast_loop(app):
    client_data = await app.get_me()
    phone = client_data.phone_number
    while True:
        try:
            now = datetime.now(MSK)
            if not (10 <= now.hour < 18):
                next_start = now.replace(hour=10, minute=0, second=0, microsecond=0)
                if now.hour >= 18:
                    next_start += timedelta(days=1)
                sleep_sec = (next_start - now).total_seconds()
                await asyncio.sleep(sleep_sec)
                continue

            settings = sql_select(f"SELECT * FROM workers WHERE session = '{phone}'")
            if not settings or len(settings[0]) <= 20 or not settings[0][20]:
                await asyncio.sleep(60)
                continue

            broadcast_text = settings[0][21]
            interval_raw = settings[0][22] or '600-1500'

            if not broadcast_text:
                await asyncio.sleep(60)
                continue

            try:
                iv_parts = str(interval_raw).split('-')
                interval_min = int(iv_parts[0])
                interval_max = int(iv_parts[1])
            except Exception:
                interval_min, interval_max = 600, 1500

            chats = sql_select(f"SELECT chat_id, chat_title FROM broadcast_chats WHERE session = '{phone}'")
            if not chats:
                await asyncio.sleep(60)
                continue

            for chat in chats:
                now = datetime.now(MSK)
                if now.hour >= 18:
                    break
                s = sql_select(f"SELECT * FROM workers WHERE session = '{phone}'")
                if not s or not s[0][20]:
                    break
                try:
                    await app.send_message(int(chat[0]), s[0][21])
                    chat_title = chat[1] or chat[0]
                    await notify(
                        f'📢 <b>Рассылка отправлена</b>\n\n'
                        f'Аккаунт: {client_data.first_name}\n'
                        f'Чат: {chat_title}\n'
                        f'Текст: {s[0][21][:300]}')
                except Exception as e:
                    print(f'broadcast send error: {e}')
                await asyncio.sleep(random.randint(interval_min, interval_max))
        except Exception as e:
            print(f'broadcast_loop error: {e}')
            await asyncio.sleep(60)


def setup_worker(app):

    _broadcast_started = False
    _client_data_cache = None

    async def get_me_cached():
        nonlocal _client_data_cache
        if _client_data_cache is None:
            _client_data_cache = await app.get_me()
        return _client_data_cache

    @app.on_raw_update(group=99)
    async def _start_broadcast(client, update, users, chats):
        nonlocal _broadcast_started
        if not _broadcast_started:
            _broadcast_started = True
            asyncio.create_task(broadcast_loop(client))

    async def pick_allowed_emoji(chat_id, preferred):
        """Если наши реакции в чате запрещены — берём разрешённую чатом."""
        try:
            chat = await app.get_chat(chat_id)
        except Exception as e:
            print(f'autolike: не получили чат {chat_id} — {e}')
            return None

        available = getattr(chat, 'available_reactions', None)
        if available is None or getattr(available, 'all_are_enabled', False):
            return None

        allowed = [r.emoji for r in (available.reactions or []) if getattr(r, 'emoji', None)]
        if not allowed:
            return None

        common = [emoji for emoji in preferred if emoji in allowed]
        return random.choice(common or allowed)

    async def put_reaction(message):
        """Ставим реакцию на новый пост канала или сообщение в чате."""
        try:
            if getattr(message, 'empty', False) or getattr(message, 'service', None) or message.outgoing:
                return

            client_data = await get_me_cached()
            session = client_data.phone_number

            if message.from_user and message.from_user.id == client_data.id:
                return

            in_list = sql_select(
                f"SELECT chat_id FROM like_chats "
                f"WHERE session = '{session}' AND chat_id = '{message.chat.id}'")
            if not in_list:
                return

            settings = likes_settings(session)
            if not settings:
                return

            enabled, emojis, delay, chance, big, notify_flag = settings
            if not enabled:
                return

            if random.randint(1, chance) != 1:
                return

            if delay > 0:
                await asyncio.sleep(delay)

            emoji = random.choice(emojis)

            try:
                await message.react(emoji, big=big)
            except ReactionInvalid:
                emoji = await pick_allowed_emoji(message.chat.id, emojis)
                if not emoji:
                    print(f'autolike: реакции недоступны в {message.chat.id}')
                    return
                await message.react(emoji, big=big)
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                await message.react(emoji, big=big)

            if notify_flag:
                await notify(
                    f'👍 <b>Автолайк</b>\n\n'
                    f'Аккаунт: {client_data.first_name}\n'
                    f'Чат: {message.chat.title}\n'
                    f'Реакция: {emoji}\n\n'
                    f'{message.link}')

        except Exception as e:
            print(f'autolike error: {e}')

    @app.on_message(filters.channel, group=1)
    async def autolike_channel(_, message):
        asyncio.create_task(put_reaction(message))

    @app.on_message(filters.group, group=1)
    async def autolike_group(_, message):
        asyncio.create_task(put_reaction(message))

    @app.on_message(filters.channel, group=2)
    async def comment(_, message):

        global SENT_TODAY
        channel_message = message
        client_data = await get_me_cached()
        
        channels = sql_select(f"SELECT purpose, session FROM channels WHERE session = '{client_data.phone_number}' "
                              f'and id = {message.chat.id}')

        if channels:


            # if channels[0][0] == 'comments':
            if True:

                settings = sql_select(f"SELECT * FROM workers WHERE session = '{client_data.phone_number}'")
                
                await asyncio.sleep(settings[0][4])
                
                post = message.caption or message.text or ''
                min_post_length = settings[0][19] if len(settings[0]) > 19 and settings[0][19] else 0

                if random.randint(1, settings[0][5]) == 1 and SENT_TODAY < LIMIT and post and len(post) >= min_post_length:

                    service_channel = sql_select('SELECT channel FROM managers')
                    
                    try:
                        message = await app.get_discussion_message(message.sender_chat.id, message.id)
                        # await app.get_discussion_replies()

                        if settings[0][9] == 1:

                            pretexts = sql_select(
                                f"SELECT text FROM pretexts WHERE session = '{client_data.phone_number}'")

                            sent_message = None
                            try:

                                if pretexts:
                                    sent_message = await message.reply(random.choice(pretexts)[0])
                                else:
                                    sent_message = await message.reply(random.choice(emojies))

                            except Forbidden:
                                try:
                                    await client.join_chat(message.chat.id)
                                    if pretexts:
                                        sent_message = await message.reply(random.choice(pretexts)[0])
                                    else:
                                        sent_message = await message.reply(random.choice(emojies))
                                except Exception as e:
                                    print(e)
                                    # if service_channel:
                                    #     await manager_apps[0].send_message(
                                    #         service_channel[0][0],
                                    #         f'<b>Ошибка при комментарии в {chat.title}</b>\n\n'
                                    #         f'{e}')
                                    #     continue

                            except Exception as e:
                                print(e)
                                # if service_channel:
                                #     await manager_apps[0].send_message(
                                #         service_channel[0][0],
                                #         f'<b>Ошибка при комментарии в {chat.title}</b>\n\n'
                                #         f'{e}')
                                #     continue

                            if sent_message:
                                await notify(
                                    f'💬 <b>Шаблонный комментарий</b>\n\n'
                                    f'Аккаунт: {client_data.first_name}\n'
                                    f'Чат: {channel_message.chat.title}\n\n'
                                    f'{sent_message.link}')
                            return

                        if settings[0][6] == 1:
                            pretexts = sql_select(
                                f"SELECT text FROM pretexts WHERE session = '{client_data.phone_number}'")

                            if pretexts:
                                sent_message = await message.reply(random.choice(pretexts)[0])
                            else:
                                sent_message = await message.reply(random.choice(emojies))
                        else:
                            sent_message = None

                    except Exception as e:
                        await notify(
                            f'❌ <b>Ошибка комментария</b>\n\n'
                            f'Аккаунт: {client_data.first_name}\n'
                            f'Чат: {channel_message.chat.title}\n\n'
                            f'{e}')
                        return

                    prompt = settings[0][7] + f' "{post}"'

                    output = await generate_response(prompt, settings[0][2], app)

                    try:
                        if sent_message:
                            reply = await sent_message.edit(output)
                            SENT_TODAY += 1
                        else:
                            reply = await message.reply(output)
                        await notify(
                            f'💬 <b>Новый комментарий</b>\n\n'
                            f'Аккаунт: {client_data.first_name}\n'
                            f'Чат: {channel_message.chat.title}\n\n'
                            f'{reply.link}')

                    except Exception as e:
                        await notify(
                            f'❌ <b>Ошибка комментария</b>\n\n'
                            f'Аккаунт: {client_data.first_name}\n'
                            f'Чат: {channel_message.chat.title}\n\n'
                            f'{e}')
    
    @app.on_message(filters.group, group=3)
    async def comment_group(_, message):

        global SENT_TODAY
        if not (message.text or message.caption):
            return

        client_data = await get_me_cached()
        channels = sql_select(f"SELECT purpose, session FROM channels WHERE session = '{client_data.phone_number}' "
                              f'and id = {message.chat.id}')

        if not channels:
            return

        settings = sql_select(f"SELECT * FROM workers WHERE session = '{client_data.phone_number}'")

        await asyncio.sleep(settings[0][4])

        post = message.caption or message.text or ''
        min_post_length = settings[0][19] if len(settings[0]) > 19 and settings[0][19] else 0

        if random.randint(1, settings[0][5]) != 1 or SENT_TODAY >= LIMIT or len(post) < min_post_length:
            return

        service_channel = sql_select('SELECT channel FROM managers')

        try:
            if settings[0][9] == 1:
                pretexts = sql_select(f"SELECT text FROM pretexts WHERE session = '{client_data.phone_number}'")
                if pretexts:
                    sent_message = await message.reply(random.choice(pretexts)[0])
                else:
                    sent_message = await message.reply(random.choice(emojies))
                if sent_message:
                    await notify(
                        f'💬 <b>Шаблонный комментарий</b>\n\n'
                        f'Аккаунт: {client_data.first_name}\n'
                        f'Чат: {message.chat.title}\n\n'
                        f'{sent_message.link}')
                return

            if settings[0][6] == 1:
                pretexts = sql_select(f"SELECT text FROM pretexts WHERE session = '{client_data.phone_number}'")
                if pretexts:
                    sent_message = await message.reply(random.choice(pretexts)[0])
                else:
                    sent_message = await message.reply(random.choice(emojies))
            else:
                sent_message = None

            prompt = settings[0][7] + f' "{post}"'
            output = await generate_response(prompt, settings[0][2], app)

            if sent_message:
                reply = await sent_message.edit(output)
            else:
                reply = await message.reply(output)

            SENT_TODAY += 1

            await notify(
                f'💬 <b>Новый комментарий</b>\n\n'
                f'Аккаунт: {client_data.first_name}\n'
                f'Чат: {message.chat.title}\n\n'
                f'{reply.link}')

        except Exception as e:
            await notify(
                f'❌ <b>Ошибка комментария</b>\n\n'
                f'Аккаунт: {client_data.first_name}\n'
                f'Чат: {message.chat.title}\n\n'
                f'{e}')

    @app.on_message(filters.group, group=4)
    async def dm_outreach(_, message):

        if not message.from_user or message.from_user.is_bot:
            return

        client_data = await get_me_cached()

        if message.from_user.id == client_data.id:
            return

        in_outreach = sql_select(
            f"SELECT group_id FROM outreach_groups "
            f"WHERE session = '{client_data.phone_number}' AND group_id = {message.chat.id}")
        if not in_outreach:
            return

        settings = sql_select(f"SELECT * FROM workers WHERE session = '{client_data.phone_number}'")
        if not settings or not settings[0][10]:
            return

        dm_text = settings[0][11]
        dm_daily_limit = settings[0][12]
        dm_delay = settings[0][13]

        if not dm_text:
            return

        today = str(date.today())
        sent_today = sql_select(
            f"SELECT COUNT(*) FROM messaged_users "
            f"WHERE session = '{client_data.phone_number}' AND messaged_date = '{today}'")
        if sent_today and sent_today[0][0] >= dm_daily_limit:
            return

        user_id = str(message.from_user.id)

        already_messaged = sql_select(
            f"SELECT user_id FROM messaged_users "
            f"WHERE session = '{client_data.phone_number}' AND user_id = '{user_id}'")
        if already_messaged:
            return

        triggers = sql_select(f"SELECT word FROM outreach_triggers WHERE session = '{client_data.phone_number}'")
        if triggers:
            msg_text = (message.text or '').lower()
            if not any(t[0].lower() in msg_text for t in triggers):
                return

        await asyncio.sleep(dm_delay)

        service_channel = sql_select('SELECT channel FROM managers')
        try:
            await app.send_message(int(user_id), dm_text)
            sql_edit(
                'INSERT INTO messaged_users(user_id, session, messaged_date) VALUES(?, ?, ?)',
                (user_id, client_data.phone_number, today))
            sql_edit(
                'INSERT INTO reply_history(user_id, session, role, content) VALUES(?, ?, ?, ?)',
                (user_id, client_data.phone_number, 'assistant', dm_text))
            sender_name = message.from_user.username or message.from_user.first_name
            await notify(
                f'📨 <b>Аутрич DM отправлен</b>\n\n'
                f'Аккаунт: {client_data.first_name}\n'
                f'Группа: {message.chat.title}\n'
                f'Получатель: @{sender_name}')
        except Exception as e:
            print(f'dm_outreach error: {e}')
            await notify(
                f'❌ <b>Ошибка аутрич DM</b>\n\n'
                f'Аккаунт: {client_data.first_name}\n'
                f'Группа: {message.chat.title}\n\n'
                f'{e}')

    @app.on_message(filters.private)
    async def comment(_, message):

        if not message.from_user:
            return
        if message.outgoing:
            return
        if message.from_user.id == BOT_ID:
            return
        if message.from_user.is_bot:
            return

        await app.send_chat_action(message.chat.id, ChatAction.TYPING)
        client_data = await get_me_cached()
        settings = sql_select(f"SELECT * FROM workers WHERE session = '{client_data.phone_number}'")
        if not settings:
            await app.send_chat_action(message.chat.id, ChatAction.CANCEL)
            return

        auto_reply_enabled = settings[0][23] if len(settings[0]) > 23 and settings[0][23] is not None else 1
        if not auto_reply_enabled:
            await app.send_chat_action(message.chat.id, ChatAction.CANCEL)
            return

        reply_use_ai = settings[0][14] if len(settings[0]) > 14 else 0
        reply_role   = settings[0][15] if len(settings[0]) > 15 else None
        reply_prompt = settings[0][16] if len(settings[0]) > 16 else None
        reply_max_rounds = settings[0][17] if len(settings[0]) > 17 and settings[0][17] else 3
        reply_delay  = settings[0][18] if len(settings[0]) > 18 and settings[0][18] is not None else 1

        sender = f'@{message.from_user.username}' if message.from_user.username else message.from_user.first_name

        if reply_use_ai and reply_role:
            user_id = str(message.from_user.id)
            history_rows = sql_select(
                f"SELECT role, content FROM reply_history "
                f"WHERE user_id = '{user_id}' AND session = '{client_data.phone_number}' "
                f"ORDER BY id ASC")
            history = [{'role': r[0], 'content': r[1]} for r in history_rows] if history_rows else []

            if reply_max_rounds > 0:
                assistant_count = sum(1 for h in history if h['role'] == 'assistant')
                if assistant_count >= reply_max_rounds:
                    await app.send_chat_action(message.chat.id, ChatAction.CANCEL)
                    return

            user_text = message.text or ''
            sql_edit(
                'INSERT INTO reply_history(user_id, session, role, content) VALUES(?, ?, ?, ?)',
                (user_id, client_data.phone_number, 'user', user_text))
            history.append({'role': 'user', 'content': user_text})

            await asyncio.sleep(reply_delay)
            response = await generate_dialogue(history, reply_role, reply_prompt or '')
            if response:
                await message.reply(response)
                sql_edit(
                    'INSERT INTO reply_history(user_id, session, role, content) VALUES(?, ?, ?, ?)',
                    (user_id, client_data.phone_number, 'assistant', response))
                round_num = sum(1 for h in history if h['role'] == 'assistant') + 1
                await notify(
                    f'💬 <b>ИИ-диалог (раунд {round_num}/{reply_max_rounds if reply_max_rounds else "∞"})</b>\n\n'
                    f'Аккаунт: {client_data.first_name}\n'
                    f'Написал: {sender}\n'
                    f'Сообщение: {user_text[:200]}\n\n'
                    f'Ответ: {response[:300]}')

        elif settings[0][3] and settings[0][3] != '-':
            await asyncio.sleep(reply_delay)
            await message.reply(settings[0][3])
            await notify(
                f'💬 <b>Автоответ отправлен</b>\n\n'
                f'Аккаунт: {client_data.first_name}\n'
                f'Написал: {sender}\n'
                f'Сообщение: {(message.text or "")[:200]}\n\n'
                f'Ответ: {settings[0][3][:300]}')

        else:
            await app.send_chat_action(message.chat.id, ChatAction.CANCEL)

async def announce_ready():
    """Пишем админам и в сервисный канал, что бот подключился к Telegram и готов к работе."""
    if not manager_apps:
        return

    manager = manager_apps[0]
    workers_info = []

    for worker in worker_apps:
        try:
            me = await worker.get_me()
        except Exception as e:
            print(f'[ready] не получили данные воркера: {e}')
            continue

        session = me.phone_number

        channels_row = sql_select(f"SELECT COUNT(*) FROM channels WHERE session = '{session}'")
        channels_count = channels_row[0][0] if channels_row else 0

        like_chats_row = sql_select(f"SELECT COUNT(*) FROM like_chats WHERE session = '{session}'")
        like_chats_count = like_chats_row[0][0] if like_chats_row else 0

        flags = sql_select(
            f"SELECT likes_enabled, auto_reply_enabled, broadcast_enabled, dm_enabled "
            f"FROM workers WHERE session = '{session}'")
        likes_on, reply_on, broadcast_on, dm_on = flags[0] if flags else (0, 1, 0, 0)

        workers_info.append(
            f'\u2022 <b>{me.first_name}</b> (+{session})\n'
            f'   \u2699\ufe0f каналов: {channels_count} \u00b7 '
            f'\U0001f44d автолайки: {"\u2705" if likes_on else "\u274c"} ({like_chats_count}) \u00b7 '
            f'\U0001f4ac автоответы: {"\u2705" if reply_on else "\u274c"} \u00b7 '
            f'\U0001f4e2 рассылка: {"\u2705" if broadcast_on else "\u274c"} \u00b7 '
            f'\U0001f4e8 аутрич: {"\u2705" if dm_on else "\u274c"}')

    lines = [
        '\u2705 <b>Бот подключился к Telegram и готов к работе</b>',
        '',
        f'Управляющих ботов: <b>{MANAGERS_COUNT}</b>',
        f'Рабочих аккаунтов: <b>{len(workers_info)}</b>',
    ]

    if workers_info:
        lines.append('')
        lines.extend(workers_info)

    if startup_failed:
        lines.append('')
        lines.append('\u26a0\ufe0f Не подключились: ' + ', '.join(str(item) for item in startup_failed))

    text = '\n'.join(lines)

    for admin in admins:
        try:
            await manager.send_message(admin, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f'[ready] не смогли написать админу {admin}: {e}')

    await notify(text)


async def run_apps():
    """То же, что pyrogram.compose, плюс уведомление о готовности после реального подключения."""
    await asyncio.gather(*[app.start() for app in apps])

    try:
        await announce_ready()
    except Exception as e:
        print(f'[ready] не смогли отправить уведомление: {e}')

    await idle()

    await asyncio.gather(*[app.stop() for app in apps])


wait_for_network()
startup()

if not apps:
    print('Ни один клиент не подключился — выходим, systemd перезапустит')
    raise SystemExit(1)

get_event_loop().run_until_complete(run_apps())
