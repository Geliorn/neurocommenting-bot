#!/bin/bash
# Открывает страницу монитора, подняв службу, если она не запущена.
# Путь и порт подставляются при установке ярлыка.

PORT="${COMBOT_MONITOR_PORT:-8787}"
URL="http://127.0.0.1:${PORT}"

systemctl --user start combot-monitor.service 2>/dev/null \
  || systemctl start combot-monitor.service 2>/dev/null

# ждём, пока страница поднимется, но не дольше шести секунд
for _ in $(seq 1 20); do
    if curl -s -o /dev/null --max-time 1 "$URL"; then
        break
    fi
    sleep 0.3
done

exec xdg-open "$URL"
