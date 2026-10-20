#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Локальный монитор бота: статус процесса, связь, фоновые процессы, статистика.

Работает отдельным процессом и ничего не ждёт от Telegram — поэтому показывает
и зависший, и мёртвый бот.

    python monitor.py              снимок в терминале
    python monitor.py --watch      живое обновление
    python monitor.py --web        страница на 127.0.0.1:8787
    python monitor.py --watchdog   одна проверка, для systemd timer
"""

import argparse
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, 'state.json')
DB_FILE = os.path.join(BASE_DIR, 'database.db')
LOG_FILE = os.path.join(BASE_DIR, 'monitor.log')
WATCHDOG_FILE = os.path.join(BASE_DIR, 'watchdog.json')

SERVICE = 'com'
DEFAULT_PORT = 8787

PROBE_TTL = 30          # сколько секунд живёт кеш активных проб
STALE_FACTOR = 3        # heartbeat устарел, если старше interval * STALE_FACTOR
TCP_TIMEOUT = 3

# адреса для проб: интернет в обход Telegram и несколько DC
NET_TARGETS = (('1.1.1.1', 443), ('8.8.8.8', 53))
TG_TARGETS = (('149.154.167.51', 443), ('149.154.175.50', 443), ('91.108.56.100', 443))
TG_HOST = 'api.telegram.org'

KIND_LABELS = {
    'comment': 'комментарии',
    'like': 'лайки',
    'reply': 'автоответы',
    'dm': 'аутрич DM',
    'broadcast': 'рассылка',
}


# ---------------------------------------------------------------- состояние бота

def read_state():
    """Читаем state.json, который бот перезаписывает раз в 30 секунд."""
    try:
        with open(STATE_FILE, encoding='utf-8') as state_file:
            return json.load(state_file), None
    except FileNotFoundError:
        return None, 'бот ни разу не записал state.json'
    except Exception as e:
        return None, f'state.json не прочитан: {e}'


def process_uptime(pid):
    """Сколько секунд живёт процесс — без разбора локализованных дат systemd."""
    if not pid:
        return None
    try:
        out = subprocess.run(['ps', '-o', 'etimes=', '-p', str(pid)],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) if out else None
    except Exception:
        return None


def service_state():
    """Что systemd думает о сервисе. Чтение прав не требует."""
    info = {'active': 'unknown', 'sub': '', 'pid': 0, 'restarts': 0, 'result': '', 'uptime': None,
            'error': None}
    props = ('ActiveState', 'SubState', 'MainPID', 'NRestarts', 'Result')
    try:
        done = subprocess.run(['systemctl', 'show', SERVICE] + [f'-p{p}' for p in props],
                              capture_output=True, text=True, timeout=5)
        values = dict(line.split('=', 1) for line in done.stdout.splitlines() if '=' in line)
    except Exception as e:
        info['error'] = f'systemctl недоступен: {e}'
        return info

    info['active'] = values.get('ActiveState', 'unknown')
    info['sub'] = values.get('SubState', '')
    info['result'] = values.get('Result', '')
    try:
        info['pid'] = int(values.get('MainPID', '0') or 0)
        info['restarts'] = int(values.get('NRestarts', '0') or 0)
    except ValueError:
        pass
    info['uptime'] = process_uptime(info['pid'])
    return info


# ---------------------------------------------------------------------- пробы сети

def tcp_check(host, port, timeout=TCP_TIMEOUT):
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, int((time.monotonic() - started) * 1000), None
    except OSError as e:
        return False, None, str(e)


def check_any(targets):
    """Доступен ли хоть один адрес — так проверяем интернет в целом."""
    details = []
    for host, port in targets:
        ok, ms, err = tcp_check(host, port)
        if ok:
            return {'ok': True, 'detail': f'{host}:{port} ответил за {ms} мс'}
        details.append(f'{host}:{port} — {err}')
    return {'ok': False, 'detail': '; '.join(details)}


def check_telegram(targets):
    """Сколько дата-центров Telegram отвечает напрямую."""
    results = []
    for host, port in targets:
        ok, ms, err = tcp_check(host, port)
        results.append({'target': f'{host}:{port}', 'ok': ok, 'ms': ms, 'error': err})
    alive = [r for r in results if r['ok']]
    if alive:
        detail = f'{len(alive)} из {len(results)} DC отвечают ({alive[0]["ms"]} мс)'
    else:
        detail = results[0]['error'] if results else 'нет адресов для проверки'
    return {'ok': bool(alive), 'detail': detail, 'targets': results}


def check_dns(host=TG_HOST):
    try:
        addr = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)[0][4][0]
        return {'ok': True, 'detail': f'{host} → {addr}'}
    except OSError as e:
        return {'ok': False, 'detail': f'{host} не разрешается: {e}'}


def find_proxy():
    """Прокси берём оттуда же, откуда бот: сначала база, потом config.py."""
    raw = None
    try:
        con = sqlite3.connect(f'file:{DB_FILE}?mode=ro', uri=True, timeout=2)
        row = con.execute('SELECT global_proxy FROM managers LIMIT 1').fetchone()
        con.close()
        if row and row[0]:
            raw = row[0]
    except Exception:
        pass

    if not raw and BASE_DIR not in sys.path:
        sys.path.insert(0, BASE_DIR)
    if not raw:
        try:
            import config
            raw = getattr(config, 'PROXY', None)
        except Exception:
            raw = None

    if not raw:
        return None

    parts = str(raw).split(':')
    proxy = {'raw': raw, 'scheme': parts[0]}
    if len(parts) >= 3:
        proxy['hostname'] = parts[1]
        try:
            proxy['port'] = int(parts[2])
        except ValueError:
            return None
    else:
        return None
    if len(parts) == 5:
        proxy['username'], proxy['password'] = parts[3], parts[4]
    elif len(parts) == 4:
        proxy['secret'] = parts[3]
    return proxy


def check_proxy(proxy, target=TG_TARGETS[0]):
    """SOCKS5 проверяем рукопожатием до DC, остальные схемы — просто TCP."""
    if not proxy:
        return {'ok': None, 'detail': 'не задан'}

    if proxy['scheme'] not in ('socks5', 'socks4'):
        ok, ms, err = tcp_check(proxy['hostname'], proxy['port'])
        detail = f'{proxy["scheme"]} {proxy["hostname"]}:{proxy["port"]} — '
        detail += f'доступен ({ms} мс), но Telegram через него не проверить' if ok else str(err)
        return {'ok': ok, 'detail': detail}

    host, port = target
    try:
        sock = socket.create_connection((proxy['hostname'], proxy['port']), timeout=TCP_TIMEOUT + 1)
    except OSError as e:
        return {'ok': False, 'detail': f'прокси недоступен: {e}'}

    try:
        sock.settimeout(TCP_TIMEOUT + 1)
        sock.sendall(b'\x05\x02\x00\x02' if proxy.get('username') else b'\x05\x01\x00')
        greeting = sock.recv(2)
        if len(greeting) < 2 or greeting[0] != 5:
            return {'ok': False, 'detail': 'ответ не похож на SOCKS5'}
        if greeting[1] == 0xFF:
            return {'ok': False, 'detail': 'прокси отклонил методы авторизации'}
        if greeting[1] == 2:
            user = proxy.get('username', '').encode()
            password = proxy.get('password', '').encode()
            sock.sendall(b'\x01' + bytes([len(user)]) + user + bytes([len(password)]) + password)
            auth = sock.recv(2)
            if len(auth) < 2 or auth[1] != 0:
                return {'ok': False, 'detail': 'прокси не принял логин или пароль'}
        sock.sendall(b'\x05\x01\x00\x01' + socket.inet_aton(host) + port.to_bytes(2, 'big'))
        reply = sock.recv(10)
        if len(reply) < 2 or reply[1] != 0:
            code = reply[1] if len(reply) > 1 else '?'
            return {'ok': False, 'detail': f'прокси не открыл соединение до DC (код {code})'}
        return {'ok': True, 'detail': f'через прокси {proxy["hostname"]}:{proxy["port"]} Telegram доступен'}
    except OSError as e:
        return {'ok': False, 'detail': f'ошибка через прокси: {e}'}
    finally:
        sock.close()


def route_info(ip=TG_TARGETS[0][0]):
    """Через какой интерфейс сейчас идёт трафик до Telegram — факт, а не условие."""
    try:
        done = subprocess.run(['ip', 'route', 'get', ip], capture_output=True, text=True, timeout=4)
        out = (done.stdout + done.stderr).strip()
    except Exception as e:
        return {'ok': False, 'detail': f'маршрут не определён: {e}'}

    dev = re.search(r'\bdev\s+(\S+)', out)
    src = re.search(r'\bsrc\s+(\S+)', out)
    if not dev:
        return {'ok': False, 'detail': out.splitlines()[0] if out else 'маршрута нет'}

    detail = f'через {dev.group(1)}'
    if src:
        detail += f' ({src.group(1)})'
    return {'ok': True, 'dev': dev.group(1), 'src': src.group(1) if src else None, 'detail': detail}


def interfaces():
    """Поднятые интерфейсы с адресами — чтобы видеть переезды между сетями."""
    try:
        out = subprocess.run(['ip', '-brief', 'addr'], capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return []

    found = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] in ('UP', 'UNKNOWN') and parts[0] != 'lo':
            found.append(f'{parts[0]} {parts[2]}')
    return found


def run_probes():
    """Все пробы параллельно — при мёртвой сети укладываемся в таймаут одной."""
    proxy = find_proxy()
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {
            'internet': pool.submit(check_any, NET_TARGETS),
            'dns': pool.submit(check_dns),
            'telegram': pool.submit(check_telegram, TG_TARGETS),
            'proxy': pool.submit(check_proxy, proxy),
            'route': pool.submit(route_info),
            'interfaces': pool.submit(interfaces),
        }
        probes = {}
        for name, job in jobs.items():
            try:
                probes[name] = job.result(timeout=15)
            except Exception as e:
                probes[name] = {'ok': False, 'detail': f'проба не выполнена: {e}'}

    probes['ts'] = time.time()
    probes['proxy_configured'] = bool(proxy)
    return probes


# ------------------------------------------------------------------- статистика

def read_events():
    """Агрегаты журнала событий: сегодня, неделя, ошибки, разбивка по дням."""
    stats = {'today': {}, 'week': {}, 'errors_today': 0, 'last_errors': [], 'days': [], 'error': None}
    try:
        con = sqlite3.connect(f'file:{DB_FILE}?mode=ro', uri=True, timeout=3)
    except Exception as e:
        stats['error'] = f'база не открыта: {e}'
        return stats

    try:
        start_today = datetime.combine(date.today(), datetime.min.time()).timestamp()
        week_ago = time.time() - 7 * 86400

        for kind, count in con.execute(
                'SELECT kind, COUNT(*) FROM events WHERE ts >= ? AND ok = 1 GROUP BY kind',
                (start_today,)):
            stats['today'][kind] = count

        for kind, count in con.execute(
                'SELECT kind, COUNT(*) FROM events WHERE ts >= ? AND ok = 1 GROUP BY kind',
                (week_ago,)):
            stats['week'][kind] = count

        row = con.execute('SELECT COUNT(*) FROM events WHERE ts >= ? AND ok = 0',
                          (start_today,)).fetchone()
        stats['errors_today'] = row[0] if row else 0

        for ts, kind, title, detail in con.execute(
                'SELECT ts, kind, chat_title, detail FROM events WHERE ok = 0 ORDER BY id DESC LIMIT 5'):
            stats['last_errors'].append({'ts': ts, 'kind': kind, 'chat': title, 'detail': detail})

        by_day = {}
        for day, kind, count in con.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS d, kind, COUNT(*) "
                'FROM events WHERE ts >= ? AND ok = 1 GROUP BY d, kind', (week_ago,)):
            by_day.setdefault(day, {})[kind] = count

        for shift in range(6, -1, -1):
            day = str(date.today() - timedelta(days=shift))
            stats['days'].append({'day': day, 'kinds': by_day.get(day, {})})
    except Exception as e:
        stats['error'] = f'журнал не прочитан: {e}'
    finally:
        con.close()

    return stats


def read_logs(lines=40):
    """Хвост журнала сервиса, чтобы не лезть в терминал."""
    try:
        done = subprocess.run(['journalctl', '-u', SERVICE, '-n', str(lines), '--no-pager',
                               '-o', 'short-iso'], capture_output=True, text=True, timeout=8)
        return (done.stdout or done.stderr).strip().splitlines()
    except Exception as e:
        return [f'журнал недоступен: {e}']


# --------------------------------------------------------------------- диагноз

def diagnose(state, state_error, service, probes):
    """Сводим три независимых факта в один понятный вердикт."""
    net_ok = bool(probes['internet'].get('ok'))
    tg_direct = bool(probes['telegram'].get('ok'))
    proxy_ok = probes['proxy'].get('ok') is True
    tg_ok = tg_direct or proxy_ok

    age = None
    stale = True
    if state:
        age = time.time() - state.get('ts', 0)
        stale = age > max(state.get('interval', 30) * STALE_FACTOR, 60)

    clients = (state.get('managers', []) + state.get('workers', [])) if state else []
    offline = [c for c in clients if not c.get('connected')]

    if service['active'] != 'active':
        return {'level': 'down', 'headline': 'Бот не запущен',
                'hint': f'systemd: {service["active"]} / {service["sub"]}. Нажми «Запустить».'}

    if stale:
        if not net_ok:
            return {'level': 'down', 'headline': 'Нет интернета — бот без связи',
                    'hint': f'{probes["internet"]["detail"]}. Проблема не в боте.'}
        if not tg_ok:
            return {'level': 'down', 'headline': 'Telegram недоступен',
                    'hint': 'Сеть есть, до DC не доходим: включи туннель или задай прокси.'}
        reason = state_error or (f'heartbeat обновлялся {int(age // 60)} мин назад' if age else 'нет данных')
        return {'level': 'down', 'headline': 'Бот завис — связь есть, состояние не обновляется',
                'hint': f'{reason}. Нужен перезапуск.'}

    if offline:
        names = ', '.join(c.get('session') or c.get('name') for c in offline)
        if not tg_ok:
            return {'level': 'down', 'headline': f'Клиенты отключены: {names}',
                    'hint': 'Telegram недоступен — сначала поднимай туннель или прокси.'}
        return {'level': 'warn', 'headline': f'Клиент не подключён: {names}',
                'hint': 'Связь есть, значит поможет перезапуск.'}

    slow = [c for c in clients
            if c.get('last_rpc_ok') and time.time() - c['last_rpc_ok'] > max(state.get('interval', 30) * 3, 120)]
    if slow:
        names = ', '.join(c.get('session') or c.get('name') for c in slow)
        return {'level': 'warn', 'headline': f'Telegram давно не отвечал на запросы: {names}',
                'hint': 'Соединение держится, но RPC не проходят — присмотрись.'}

    if not tg_direct and proxy_ok:
        return {'level': 'ok', 'headline': 'Всё работает — через прокси',
                'hint': probes['proxy']['detail']}

    return {'level': 'ok', 'headline': 'Всё работает', 'hint': probes['route'].get('detail', '')}


_probe_cache = {'ts': 0, 'data': None}


def collect(force_probe=False, with_logs=False):
    """Единый снимок для всех режимов вывода."""
    now = time.time()
    if force_probe or not _probe_cache['data'] or now - _probe_cache['ts'] > PROBE_TTL:
        _probe_cache['data'] = run_probes()
        _probe_cache['ts'] = now

    state, state_error = read_state()
    service = service_state()
    probes = _probe_cache['data']

    data = {
        'ts': now,
        'state': state,
        'state_error': state_error,
        'state_age': (now - state['ts']) if state else None,
        'service': service,
        'probes': probes,
        'events': read_events(),
        'diagnosis': diagnose(state, state_error, service, probes),
        'accounts': accounts_from_db(state),
    }
    if with_logs:
        data['logs'] = read_logs()
    return data


def accounts_from_db(state):
    """Живое состояние клиентов из state.json дополняем настройками из базы."""
    rows = {}
    try:
        con = sqlite3.connect(f'file:{DB_FILE}?mode=ro', uri=True, timeout=3)
        for session, likes, reply, broadcast, dm in con.execute(
                'SELECT session, likes_enabled, auto_reply_enabled, broadcast_enabled, dm_enabled FROM workers'):
            channels = con.execute('SELECT COUNT(*) FROM channels WHERE session = ?', (session,)).fetchone()
            like_chats = con.execute('SELECT COUNT(*) FROM like_chats WHERE session = ?', (session,)).fetchone()
            rows[str(session)] = {
                'session': str(session),
                'flags': {'likes': bool(likes), 'reply': reply is None or bool(reply),
                          'broadcast': bool(broadcast), 'dm': bool(dm)},
                'channels': channels[0] if channels else 0,
                'like_chats': like_chats[0] if like_chats else 0,
            }
        con.close()
    except Exception:
        return list(rows.values())

    for worker in ((state or {}).get('workers') or []):
        key = str(worker.get('session') or worker.get('name'))
        row = rows.setdefault(key, {'session': key, 'flags': {}, 'channels': 0, 'like_chats': 0})
        row.update({'title': worker.get('title'), 'connected': worker.get('connected'),
                    'dc': worker.get('dc'), 'proxy': worker.get('proxy'),
                    'last_rpc_ok': worker.get('last_rpc_ok'), 'error': worker.get('error')})

    return list(rows.values())


# ----------------------------------------------------------------- вывод в терминал

COLORS = {'ok': '\033[32m', 'warn': '\033[33m', 'down': '\033[31m',
          'dim': '\033[90m', 'bold': '\033[1m', 'off': '\033[0m'}

MARKS = {'ok': '\U0001f7e2', 'warn': '\U0001f7e1', 'down': '\U0001f534'}


def paint(text, kind, enabled=True):
    if not enabled or kind not in COLORS:
        return text
    return f'{COLORS[kind]}{text}{COLORS["off"]}'


def human_age(seconds):
    if seconds is None:
        return 'неизвестно'
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds} сек'
    if seconds < 3600:
        return f'{seconds // 60} мин'
    hours, minutes = divmod(seconds // 60, 60)
    return f'{hours} ч {minutes} мин' if minutes else f'{hours} ч'


def sparkline(values):
    blocks = '▁▂▃▄▅▆▇█'
    peak = max(values) if values else 0
    if not peak:
        return blocks[0] * len(values)
    return ''.join(blocks[min(len(blocks) - 1, int(value / peak * (len(blocks) - 1)))] for value in values)


def flag_mark(value):
    return '✅' if value else '❌'


def probe_mark(value):
    if value is None:
        return '—'
    return '✅' if value else '❌'


def render_text(data, color=True):
    diag = data['diagnosis']
    state = data['state'] or {}
    service = data['service']
    probes = data['probes']
    events = data['events']
    lines = []

    lines.append(f'{MARKS[diag["level"]]} {paint(diag["headline"], diag["level"], color)}')
    if diag.get('hint'):
        lines.append(f'   {paint(diag["hint"], "dim", color)}')
    lines.append('')

    service_line = f'{service["active"]} ({service["sub"]})'
    if service['pid']:
        service_line += f', pid {service["pid"]}, uptime {human_age(service["uptime"])}'
    if service['restarts']:
        service_line += f', рестартов {service["restarts"]}'
    lines.append(f'Процесс     {service_line}')

    if data['state_age'] is None:
        lines.append(f'Heartbeat   {paint(data["state_error"] or "нет данных", "down", color)}')
    else:
        fresh = data['state_age'] <= max(state.get('interval', 30) * STALE_FACTOR, 60)
        lines.append(f'Heartbeat   {paint(human_age(data["state_age"]) + " назад", "ok" if fresh else "down", color)}'
                     f' (интервал {state.get("interval", "?")} с)')

    clients = state.get('managers', []) + state.get('workers', [])
    if clients:
        parts = []
        for client in clients:
            mark = '✅' if client.get('connected') else '❌'
            label = client.get('session') or client.get('name')
            dc = f' DC{client["dc"]}' if client.get('dc') else ''
            parts.append(f'{mark} {label}{dc}')
        lines.append('Клиенты     ' + ' · '.join(parts))

    for client in clients:
        if client.get('error'):
            lines.append(f'            {paint(client.get("name") + ": " + client["error"], "warn", color)}')

    if state.get('startup_failed'):
        lines.append('            ' + paint('не подключились при старте: ' +
                                            ', '.join(state['startup_failed']), 'warn', color))

    lines.append('')
    lines.append('Связь:')
    lines.append(f'  интернет        {probe_mark(probes["internet"].get("ok"))} {probes["internet"]["detail"]}')
    lines.append(f'  DNS             {probe_mark(probes["dns"].get("ok"))} {probes["dns"]["detail"]}')
    lines.append(f'  Telegram        {probe_mark(probes["telegram"].get("ok"))} {probes["telegram"]["detail"]}')
    lines.append(f'  прокси          {probe_mark(probes["proxy"].get("ok"))} {probes["proxy"]["detail"]}')
    lines.append(f'  маршрут         {probes["route"]["detail"]}')
    if probes.get('interfaces'):
        lines.append('  интерфейсы      ' + ' · '.join(probes['interfaces']))

    if data['accounts']:
        lines.append('')
        lines.append('Аккаунты:')
        for account in data['accounts']:
            title = account.get('title') or 'без имени'
            mark = MARKS['ok'] if account.get('connected') else MARKS['down']
            dc = f' DC{account["dc"]}' if account.get('dc') else ''
            lines.append(f'  {mark} {title} (+{account["session"]}){dc}')
            flags = account.get('flags', {})
            lines.append(f'      ⚙️ каналов {account.get("channels", 0)} · '
                         f'\U0001f44d лайки {flag_mark(flags.get("likes"))} ({account.get("like_chats", 0)}) · '
                         f'\U0001f4ac автоответы {flag_mark(flags.get("reply"))} · '
                         f'\U0001f4e2 рассылка {flag_mark(flags.get("broadcast"))} · '
                         f'\U0001f4e8 аутрич {flag_mark(flags.get("dm"))}')

    processes = state.get('processes') or {}
    lines.append('')
    lines.append('Процессы:')
    lines.append('  рассылка        ' + (', '.join(processes.get('broadcast') or []) or 'не запущена'))
    lines.append('  парсинг         ' + ('идёт' if processes.get('parsing') else 'не идёт'))
    lines.append(f'  задач           {processes.get("tasks", "?")}')

    lines.append('')
    lines.append('Статистика:')
    if events.get('error'):
        lines.append(f'  {paint(events["error"], "warn", color)}')
    today = ' · '.join(f'{KIND_LABELS.get(k, k)} {v}' for k, v in sorted(events['today'].items())) or 'пусто'
    lines.append(f'  сегодня         {today}')
    for kind in sorted(events['week']):
        series = [day['kinds'].get(kind, 0) for day in events['days']]
        lines.append(f'  7 дней {KIND_LABELS.get(kind, kind):<10} {sparkline(series)}  {events["week"][kind]}')
    if events['errors_today'] or events['last_errors']:
        last = events['last_errors'][0] if events['last_errors'] else None
        tail = ''
        if last:
            when = datetime.fromtimestamp(last['ts']).strftime('%H:%M')
            tail = f', последняя {when} {last["kind"]} / {last.get("chat") or "-"} — {last.get("detail") or ""}'
        lines.append('  ошибки          ' + paint(f'{events["errors_today"]} за сутки{tail}', 'warn', color))

    if data.get('logs'):
        lines.append('')
        lines.append('Журнал (последние строки):')
        for line in data['logs'][-10:]:
            lines.append(f'  {paint(line, "dim", color)}')

    return '\n'.join(lines)


# ------------------------------------------------------------------- управление

def systemctl(action):
    """Пробуем без прав, затем через sudo -n, если правило добавлено."""
    for command in (['systemctl', '--no-ask-password', action, SERVICE],
                    ['sudo', '-n', 'systemctl', action, SERVICE]):
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=25)
        except Exception as e:
            return False, f'{action}: не выполнено ({e})'
        if done.returncode == 0:
            return True, f'{action}: выполнено'
        message = (done.stderr or done.stdout or '').strip().splitlines()
        last_error = message[0] if message else 'без сообщения'

    hint = ''
    if 'interactive authentication' in last_error or 'password is required' in last_error:
        hint = ' — нужно правило sudoers (см. README)'
    return False, f'{action}: {last_error}{hint}'


def do_restart():
    """Перезапуск без всяких прав: SIGTERM процессу, systemd поднимет его сам."""
    service = service_state()
    if service['active'] == 'active' and service['pid']:
        try:
            os.kill(service['pid'], signal.SIGTERM)
            return True, f'процесс {service["pid"]} остановлен, systemd поднимет его заново'
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    return systemctl('restart')


# ---------------------------------------------------------------------- вотчдог

def log_line(text):
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'{stamp} {text}'
    print(line)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as log:
            log.write(line + '\n')
    except Exception:
        pass


def notify_desktop(title, text):
    """notify-send, если есть сеанс рабочего стола. На голом VDS просто пропускаем."""
    binary = None
    for path in ('/usr/bin/notify-send', '/bin/notify-send'):
        if os.path.exists(path):
            binary = path
            break
    if not binary:
        return False

    env = dict(os.environ)
    if 'DBUS_SESSION_BUS_ADDRESS' not in env:
        bus = f'/run/user/{os.getuid()}/bus'
        if not os.path.exists(bus):
            return False
        env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={bus}'
        env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')

    try:
        subprocess.run([binary, '-a', 'ComBot', '-u', 'critical', title, text],
                       env=env, capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def load_history():
    try:
        with open(WATCHDOG_FILE, encoding='utf-8') as history_file:
            return json.load(history_file)
    except Exception:
        return {'last_restart': 0, 'streak': 0, 'last_level': 'ok', 'last_notify': 0, 'down_since': None}


def save_history(history):
    try:
        with open(WATCHDOG_FILE, 'w', encoding='utf-8') as history_file:
            json.dump(history, history_file, ensure_ascii=False)
    except Exception as e:
        log_line(f'вотчдог: не сохранили историю — {e}')


def watchdog(restart_enabled=True, cooldown=600, max_streak=3):
    data = collect(force_probe=True)
    diag = data['diagnosis']
    probes = data['probes']
    history = load_history()
    now = time.time()

    connectivity = bool(probes['internet'].get('ok')) and (
        bool(probes['telegram'].get('ok')) or probes['proxy'].get('ok') is True)

    if diag['level'] == 'ok':
        if history.get('last_level') != 'ok':
            downtime = human_age(now - history['down_since']) if history.get('down_since') else 'неизвестно'
            log_line(f'вотчдог: восстановилось, простой {downtime}')
            notify_desktop('ComBot снова работает', f'Простой: {downtime}')
        history.update({'last_level': 'ok', 'streak': 0, 'down_since': None})
        save_history(history)
        return data

    if history.get('down_since') is None:
        history['down_since'] = now

    if not connectivity:
        if now - history.get('last_notify', 0) > 900:
            log_line(f'вотчдог: {diag["headline"]} — {diag["hint"]} (не вмешиваемся: связи нет)')
            notify_desktop('ComBot: нет связи', diag['headline'])
            history['last_notify'] = now
        history['last_level'] = diag['level']
        save_history(history)
        return data

    action = 'start' if data['service']['active'] != 'active' else 'restart'

    if not restart_enabled:
        log_line(f'вотчдог: {diag["headline"]} (автодействия отключены)')
        notify_desktop('ComBot: проблема', diag['headline'])
        history.update({'last_level': diag['level'], 'last_notify': now})
        save_history(history)
        return data

    if now - history.get('last_restart', 0) < cooldown:
        log_line(f'вотчдог: {diag["headline"]}, но ждём кулдаун')
        history['last_level'] = diag['level']
        save_history(history)
        return data

    if history.get('streak', 0) >= max_streak:
        if now - history.get('last_notify', 0) > 900:
            log_line(f'вотчдог: {history["streak"]} перезапуска подряд не помогли, только уведомляю')
            notify_desktop('ComBot не поднимается', f'{diag["headline"]}. Нужны руки.')
            history['last_notify'] = now
        history['last_level'] = diag['level']
        save_history(history)
        return data

    ok, message = do_restart() if action == 'restart' else systemctl('start')
    history.update({'last_restart': now, 'streak': history.get('streak', 0) + 1,
                    'last_level': diag['level'], 'last_notify': now})
    save_history(history)

    log_line(f'вотчдог: {diag["headline"]} → {action}: {message}')
    notify_desktop('ComBot перезапущен' if ok else 'ComBot: перезапуск не удался',
                   f'{diag["headline"]}\n{message}')
    return data


# ------------------------------------------------------------------ веб-страница

PAGE = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ComBot — монитор</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --text:#1c1e21; --dim:#6b7280; --line:#e5e7eb;
        --ok:#15803d; --warn:#b45309; --down:#b91c1c; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181d; --card:#1f2228; --text:#e7e9ee; --dim:#9aa1ad; --line:#2d3139;
          --ok:#4ade80; --warn:#fbbf24; --down:#f87171; }
}
* { box-sizing:border-box; }
body { margin:0; padding:16px; background:var(--bg); color:var(--text);
       font:14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
h1 { font-size:18px; margin:0 0 12px; }
.banner { padding:14px 16px; border-radius:10px; color:#fff; margin-bottom:14px; }
.banner.ok { background:#15803d; } .banner.warn { background:#b45309; } .banner.down { background:#b91c1c; }
.banner b { font-size:16px; } .banner div { opacity:.9; margin-top:4px; }
.grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(290px, 1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
.card h2 { font-size:12px; text-transform:uppercase; letter-spacing:.04em;
           color:var(--dim); margin:0 0 8px; }
.row { display:flex; justify-content:space-between; gap:10px; padding:3px 0; }
.row span:last-child { text-align:right; }
.dim { color:var(--dim); }
.ok { color:var(--ok); } .warn { color:var(--warn); } .down { color:var(--down); }
.bars { display:flex; align-items:flex-end; gap:3px; height:36px; margin:6px 0; }
.bars i { flex:1; background:var(--ok); border-radius:2px 2px 0 0; min-height:2px; opacity:.85; }
button { font:inherit; padding:7px 12px; border-radius:7px; border:1px solid var(--line);
         background:var(--card); color:var(--text); cursor:pointer; }
button:hover { border-color:var(--dim); }
button.danger { color:var(--down); }
#actions { display:flex; flex-wrap:wrap; gap:8px; margin:14px 0 6px; }
#result { min-height:20px; margin-bottom:10px; }
pre { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px;
      overflow:auto; max-height:320px; font-size:12px; margin:0; }
table { width:100%; border-collapse:collapse; }
td { padding:4px 0; vertical-align:top; }
td.f { color:var(--dim); font-size:13px; }
</style></head><body>
<h1>ComBot — монитор <span class="dim" id="clock"></span></h1>
<div class="banner down" id="banner"><b>Загрузка…</b></div>
<div id="actions">
  <button onclick="act('restart', 'Перезапустить бота?')">Перезапустить</button>
  <button onclick="act('start')">Запустить</button>
  <button class="danger" onclick="act('stop', 'Остановить бота?')">Остановить</button>
  <button onclick="act('probe')">Проверить связь сейчас</button>
  <button onclick="loadLogs()">Журнал</button>
</div>
<div id="result" class="dim"></div>
<div class="grid" id="cards"></div>
<h2 class="dim" style="margin:16px 0 6px">Журнал сервиса</h2>
<pre id="logs">нажми «Журнал»</pre>
<script>
const TOKEN = "__TOKEN__";
const mark = v => v === null || v === undefined ? '<span class="dim">—</span>'
  : (v ? '<span class="ok">✅</span>' : '<span class="down">❌</span>');
const age = s => s === null || s === undefined ? 'неизвестно'
  : s < 60 ? Math.round(s) + ' сек' : s < 3600 ? Math.round(s / 60) + ' мин'
  : Math.floor(s / 3600) + ' ч ' + Math.round((s % 3600) / 60) + ' мин';
const esc = t => String(t === null || t === undefined ? '' : t)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const row = (k, v) => '<div class="row"><span class="dim">' + k + '</span><span>' + v + '</span></div>';
const LABELS = {comment:'комментарии', like:'лайки', reply:'автоответы', dm:'аутрич DM', broadcast:'рассылка'};

async function render() {
  let d;
  try { d = await (await fetch('/api/state')).json(); }
  catch (e) {
    document.getElementById('banner').className = 'banner down';
    document.getElementById('banner').innerHTML = '<b>Монитор не отвечает</b><div>' + esc(e) + '</div>';
    return;
  }
  const g = d.diagnosis, st = d.state || {}, sv = d.service, pr = d.probes, ev = d.events;
  const banner = document.getElementById('banner');
  banner.className = 'banner ' + g.level;
  banner.innerHTML = '<b>' + esc(g.headline) + '</b><div>' + esc(g.hint || '') + '</div>';
  document.getElementById('clock').textContent = new Date(d.ts * 1000).toLocaleTimeString('ru-RU');

  const cards = [];
  cards.push('<div class="card"><h2>Процесс</h2>'
    + row('systemd', esc(sv.active + ' / ' + sv.sub))
    + row('pid', esc(sv.pid || '—'))
    + row('uptime', age(sv.uptime))
    + row('рестартов', esc(sv.restarts))
    + row('heartbeat', d.state_age === null ? '<span class="down">' + esc(d.state_error) + '</span>'
        : age(d.state_age) + ' назад') + '</div>');

  let net = '<div class="card"><h2>Связь</h2>'
    + row('интернет', mark(pr.internet.ok) + ' ' + esc(pr.internet.detail))
    + row('DNS', mark(pr.dns.ok) + ' ' + esc(pr.dns.detail))
    + row('Telegram', mark(pr.telegram.ok) + ' ' + esc(pr.telegram.detail))
    + row('прокси', mark(pr.proxy.ok) + ' ' + esc(pr.proxy.detail))
    + row('маршрут', esc(pr.route.detail));
  if (pr.interfaces && pr.interfaces.length) net += row('интерфейсы', esc(pr.interfaces.join(' · ')));
  cards.push(net + '</div>');

  const p = st.processes || {};
  cards.push('<div class="card"><h2>Процессы</h2>'
    + row('рассылка', esc((p.broadcast || []).join(', ') || 'не запущена'))
    + row('парсинг', p.parsing ? 'идёт' : 'не идёт')
    + row('задач', esc(p.tasks === undefined ? '—' : p.tasks))
    + row('клиентов', esc((st.managers || []).length + (st.workers || []).length)) + '</div>');

  let acc = '<div class="card"><h2>Аккаунты</h2><table>';
  (d.accounts || []).forEach(a => {
    const f = a.flags || {};
    acc += '<tr><td>' + (a.connected ? '🟢' : '🔴') + ' <b>' + esc(a.title || 'без имени') + '</b>'
      + ' <span class="dim">+' + esc(a.session) + (a.dc ? ' · DC' + a.dc : '') + '</span>'
      + (a.error ? '<br><span class="warn">' + esc(a.error) + '</span>' : '')
      + '<div class="f">каналов ' + esc(a.channels) + ' · лайки ' + mark(f.likes) + ' (' + esc(a.like_chats) + ')'
      + ' · автоответы ' + mark(f.reply) + ' · рассылка ' + mark(f.broadcast)
      + ' · аутрич ' + mark(f.dm) + '</div></td></tr>';
  });
  cards.push(acc + '</table></div>');

  let stats = '<div class="card"><h2>Статистика</h2>';
  const today = Object.keys(ev.today || {});
  stats += row('сегодня', today.length
    ? today.map(k => esc(LABELS[k] || k) + ' ' + ev.today[k]).join(' · ') : 'пусто');
  Object.keys(ev.week || {}).forEach(k => {
    const series = (ev.days || []).map(x => (x.kinds || {})[k] || 0);
    const peak = Math.max.apply(null, series.concat([1]));
    stats += '<div class="row"><span class="dim">7 дней: ' + esc(LABELS[k] || k)
      + '</span><span>' + ev.week[k] + '</span></div><div class="bars">'
      + series.map(v => '<i style="height:' + Math.round(v / peak * 100) + '%" title="' + v + '"></i>').join('')
      + '</div>';
  });
  stats += row('ошибок за сутки', ev.errors_today
    ? '<span class="warn">' + ev.errors_today + '</span>' : '0');
  (ev.last_errors || []).slice(0, 3).forEach(e => {
    stats += '<div class="f">' + new Date(e.ts * 1000).toLocaleTimeString('ru-RU') + ' · '
      + esc(e.kind) + ' · ' + esc(e.chat || '—') + ' — ' + esc(e.detail || '') + '</div>';
  });
  cards.push(stats + '</div>');

  document.getElementById('cards').innerHTML = cards.join('');
}

async function act(what, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  const out = document.getElementById('result');
  out.textContent = 'выполняю…';
  try {
    const r = await fetch('/api/' + what, {method: 'POST', headers: {'X-CSRF-Token': TOKEN}});
    const j = await r.json();
    out.innerHTML = (j.ok ? '<span class="ok">✅ </span>' : '<span class="down">❌ </span>') + esc(j.message);
  } catch (e) { out.innerHTML = '<span class="down">❌ ' + esc(e) + '</span>'; }
  setTimeout(render, 1500);
}

async function loadLogs() {
  const box = document.getElementById('logs');
  box.textContent = 'читаю…';
  try {
    const j = await (await fetch('/api/logs')).json();
    box.textContent = (j.lines || []).join('\\n');
    box.scrollTop = box.scrollHeight;
  } catch (e) { box.textContent = String(e); }
}

render();
setInterval(render, 3000);
</script></body></html>"""


def serve(port):
    """Страница только на 127.0.0.1; чтение открыто, действия — по токену."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from secrets import token_urlsafe

    token = token_urlsafe(24)
    allowed_origins = (f'http://127.0.0.1:{port}', f'http://localhost:{port}')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'combot-monitor'

        def log_message(self, *args):
            pass

        def _send(self, body, content_type='application/json; charset=utf-8', code=200):
            payload = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Frame-Options', 'DENY')
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj, ensure_ascii=False), code=code)

        def _authorized(self):
            # чужая страница в браузере не поставит кастомный заголовок без CORS-preflight
            if self.headers.get('X-CSRF-Token') != token:
                return False
            origin = self.headers.get('Origin')
            return not origin or origin in allowed_origins

        def do_GET(self):
            if self.path in ('/', '/index.html'):
                return self._send(PAGE.replace('__TOKEN__', token), 'text/html; charset=utf-8')
            if self.path == '/api/state':
                return self._json(collect())
            if self.path == '/api/logs':
                return self._json({'lines': read_logs(80)})
            self._json({'ok': False, 'message': 'нет такого адреса'}, code=404)

        def do_POST(self):
            if not self._authorized():
                return self._json({'ok': False, 'message': 'запрос без токена страницы'}, code=403)

            if self.path == '/api/probe':
                data = collect(force_probe=True)
                return self._json({'ok': True, 'message': 'пробы обновлены: ' + data['diagnosis']['headline']})
            if self.path == '/api/restart':
                ok, message = do_restart()
                log_line(f'страница: перезапуск — {message}')
                return self._json({'ok': ok, 'message': message})
            if self.path in ('/api/start', '/api/stop'):
                ok, message = systemctl(self.path.rsplit('/', 1)[1])
                log_line(f'страница: {message}')
                return self._json({'ok': ok, 'message': message})
            self._json({'ok': False, 'message': 'нет такого действия'}, code=404)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    httpd = Server(('127.0.0.1', port), Handler)
    print(f'Монитор: http://127.0.0.1:{port}  (Ctrl+C чтобы остановить)')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nостановлен')
    finally:
        httpd.server_close()


def main():
    parser = argparse.ArgumentParser(description='Локальный монитор ComBot')
    parser.add_argument('--watch', action='store_true', help='живое обновление в терминале')
    parser.add_argument('--web', action='store_true', help=f'страница на 127.0.0.1:{DEFAULT_PORT}')
    parser.add_argument('--watchdog', action='store_true', help='одна проверка для systemd timer')
    parser.add_argument('--json', action='store_true', help='снимок в JSON')
    parser.add_argument('--logs', action='store_true', help='добавить хвост журнала в снимок')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--interval', type=float, default=3, help='период обновления для --watch')
    parser.add_argument('--no-restart', action='store_true', help='вотчдог только уведомляет')
    parser.add_argument('--no-color', action='store_true')
    args = parser.parse_args()

    if args.web:
        return serve(args.port)

    if args.watchdog:
        data = watchdog(restart_enabled=not args.no_restart)
        return 0 if data['diagnosis']['level'] == 'ok' else 1

    if args.watch:
        color = sys.stdout.isatty() and not args.no_color
        try:
            while True:
                snapshot = collect(with_logs=args.logs)
                sys.stdout.write('\033[H\033[2J' + render_text(snapshot, color) + '\n')
                sys.stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0

    data = collect(force_probe=True, with_logs=args.logs)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(render_text(data, sys.stdout.isatty() and not args.no_color))
    return 0 if data['diagnosis']['level'] == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
