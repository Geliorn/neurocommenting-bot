import sqlite3


with sqlite3.connect("database.db") as connection:
    cursor = connection.cursor()

    core_commands = [
        "PRAGMA encoding='UTF-8'",
        'ALTER TABLE channels ADD COLUMN purpose TEXT DEFAULT "comments"',
        'CREATE TABLE IF NOT EXISTS managers(token TEXT)',
        'CREATE TABLE IF NOT EXISTS stickers(pack_id TEXT NOT NULL, sticker TEXT NOT NULL, emoji TEXT NOT NULL, session TEXT)',
        'CREATE TABLE IF NOT EXISTS channels(id INT, session TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS workers'
        '(session TEXT NOT NULL, proxy TEXT, prompt TEXT, auto_reply TEXT, '
        'delay FLOAT DEFAULT 0, chance INT DEFAULT 1, be_first INT DEFAULT 1, what_to_write TEXT, mode TEXT DEFAULT "text")',
        'ALTER TABLE stickers ADD session TEXT DEFAULT 79858490415',
        'ALTER TABLE managers ADD COLUMN channel text',
        'ALTER TABLE workers ADD COLUMN dont_use_gpt INT DEFAULT 0',
        
        'CREATE TABLE IF NOT EXISTS pretexts'
        '(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, session TEXT NOT NULL)',

        'UPDATE channels SET purpose = "comments"',

        'CREATE TABLE IF NOT EXISTS outreach_groups(group_id INT, session TEXT NOT NULL)',
        'CREATE TABLE IF NOT EXISTS messaged_users(user_id TEXT NOT NULL, session TEXT NOT NULL, messaged_date TEXT NOT NULL)',
        'ALTER TABLE workers ADD COLUMN dm_enabled INT DEFAULT 0',
        'ALTER TABLE workers ADD COLUMN dm_text TEXT',
        'ALTER TABLE workers ADD COLUMN dm_daily_limit INT DEFAULT 20',
        'ALTER TABLE workers ADD COLUMN dm_delay FLOAT DEFAULT 60',

        'CREATE TABLE IF NOT EXISTS outreach_triggers(id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT NOT NULL, session TEXT NOT NULL)',

        'ALTER TABLE workers ADD COLUMN reply_use_ai INT DEFAULT 0',
        'ALTER TABLE workers ADD COLUMN reply_role TEXT',
        'ALTER TABLE workers ADD COLUMN reply_prompt TEXT',
        'ALTER TABLE workers ADD COLUMN reply_max_rounds INT DEFAULT 3',
        'ALTER TABLE workers ADD COLUMN reply_delay FLOAT DEFAULT 1',
        'ALTER TABLE workers ADD COLUMN min_post_length INT DEFAULT 0',
        'CREATE TABLE IF NOT EXISTS reply_history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, session TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL)',

        'CREATE TABLE IF NOT EXISTS broadcast_chats(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, chat_title TEXT, session TEXT NOT NULL)',
        'ALTER TABLE workers ADD COLUMN broadcast_enabled INT DEFAULT 0',
        'ALTER TABLE workers ADD COLUMN broadcast_text TEXT',
        'ALTER TABLE workers ADD COLUMN broadcast_interval TEXT DEFAULT "600-1500"',
        'ALTER TABLE workers ADD COLUMN auto_reply_enabled INT DEFAULT 1',
        'CREATE TABLE IF NOT EXISTS bot_admins(user_id TEXT NOT NULL UNIQUE)',
        'ALTER TABLE managers ADD COLUMN global_proxy TEXT',

        'CREATE TABLE IF NOT EXISTS like_chats'
        '(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, chat_title TEXT, session TEXT NOT NULL)',
        'ALTER TABLE workers ADD COLUMN likes_enabled INT DEFAULT 0',
        "ALTER TABLE workers ADD COLUMN likes_emojis TEXT DEFAULT '\U0001F44D,\u2764\ufe0f,\U0001F525,\U0001F970,\U0001F44F'",
        'ALTER TABLE workers ADD COLUMN likes_delay FLOAT DEFAULT 5',
        'ALTER TABLE workers ADD COLUMN likes_chance INT DEFAULT 1',
        'ALTER TABLE workers ADD COLUMN likes_big INT DEFAULT 0',
        'ALTER TABLE workers ADD COLUMN likes_notify INT DEFAULT 0',

        # журнал событий для статистики и монитора
        'CREATE TABLE IF NOT EXISTS events'
        '(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, session TEXT, kind TEXT NOT NULL, '
        'chat_id TEXT, chat_title TEXT, ok INT DEFAULT 1, detail TEXT)',
        'CREATE INDEX IF NOT EXISTS events_ts ON events(ts)',
        'CREATE INDEX IF NOT EXISTS events_kind_ts ON events(kind, ts)',

        # WAL — чтобы монитор читал базу, не мешая боту писать
        'PRAGMA journal_mode=WAL',
    ]

    def sql_edit(command, args):
        try:
            s_c = connection.cursor()
            s_c.execute(command, args)
            connection.commit()
        except Exception as exc:
            connection.rollback()
            print(f"{command}\nrollback: {exc}")


    def sql_select(command):
        try:
            s_c = connection.cursor()
            s_c.execute(command)
            return s_c.fetchall()
        except Exception as exc:
            print(f"{command}\nsql_select() error: {exc}")
