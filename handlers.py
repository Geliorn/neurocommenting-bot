handlers = {}

async def remove_handler(userid):
    if userid in handlers:
        handlers.pop(userid)

async def add_handler(userid, function):
    handlers[userid] = {'function': function}

async def is_handler(message):
    if message.from_user.id in handlers:
        if message.text == '/cancel':
            await remove_handler(message.from_user.id)
            await message.reply('<i>Отменено.</i>')
            return True
        await handlers[message.from_user.id]['function'](message)
        return True
    else:
        return False
