from sqlite_functions import sql_select
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


async def pretexts_menu(callback_query):

    data = callback_query.data
    session = data.split()[1]
    pretexts = sql_select(f"SELECT text, id FROM pretexts WHERE session = '{session}'")

    kb = []

    if pretexts:
        texts_display = '\n'.join(f'<code>{p[0]}</code>' for p in pretexts)
        header = f'<b>Шаблонные ответы и тексты для первого коммента</b>\n\n{texts_display}'

        for pretext in pretexts:
            kb.append([InlineKeyboardButton(pretext[0], callback_data=f'delpretext {pretext[1]}')])

        kb.append([InlineKeyboardButton('Добавить тексты ➕', callback_data=f'addpretexts {session}'),
                   InlineKeyboardButton('Очистить тексты 🗑', callback_data=f'clearpretexts {session}')])

    else:
        header = '<b>Шаблонные ответы и тексты для первого коммента</b>\n\nСписок пустой'
        kb.append([InlineKeyboardButton('Добавить тексты ➕', callback_data=f'addpretexts {session}')])

    await callback_query.message.edit(header, reply_markup=InlineKeyboardMarkup(kb))
