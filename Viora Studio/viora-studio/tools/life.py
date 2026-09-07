#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
life.py 1.0.0

Повседневные ответы про машину, погоду и утро для VIORA STUDIO.
Только стандартная библиотека Python 3.8+. Ключей нет, установки нет.

Зачем этот файл. Автор пишет в Избранное "виора пк" или "виора погода" и ждёт
одну короткую строку, а не рассказ. Считать место на диске, переводить коды
погоды в русские слова и складывать дайджест на утро моделью дорого и ненадёжно:
она путает гигабайты с гибибайтами и выдумывает осадки. Поэтому цифры считает
код, а модель только пересылает готовый текст.

Быстрый вход:
    python3 tools/life.py pc
    python3 tools/life.py weather
    python3 tools/life.py weather Казань
    python3 tools/life.py morning
    python3 tools/life.py pc --json
    python3 tools/life.py --selftest

Город и часовой пояс по умолчанию берутся из memory/profile.md: заполни его
один раз командой python3 tools/memory.py profile set город=Астрахань.
Погода приходит с open-meteo.com, ключ не нужен. Ответ лежит в кэше 30 минут
в рабочей папке моста, чтобы десять вопросов подряд не били по сети.
Сторонние пакеты не нужны: psutil берём, только если он уже стоит в системе,
иначе честно пишем "нет данных".

Коды возврата: 0 порядок, 1 данных нет или сети нет, 2 ошибка вызова.
"""

import argparse
import datetime
import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)

UA = "VIORA STUDIO life.py"
TIMEOUT = 10
TRIES = 2
CACHE_TTL = 30 * 60
MORNING_LIMIT = 1500
NO_DATA = "нет данных"

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_FIELDS = ("temperature_2m_max", "temperature_2m_min",
                "precipitation_probability_max", "weathercode")

# Служебные файловые системы: они не диски автора, и место на них ничего не говорит.
SKIP_FS = ("proc", "sysfs", "devtmpfs", "tmpfs", "devpts", "cgroup", "cgroup2",
           "squashfs", "overlay", "autofs", "debugfs", "tracefs", "securityfs",
           "pstore", "fusectl", "configfs", "bpf", "hugetlbfs", "mqueue",
           "binfmt_misc", "efivarfs", "ramfs", "nsfs", "rpc_pipefs", "selinuxfs")

MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница",
            "суббота", "воскресенье")

# Коды WMO из open-meteo. Автору нужны слова, а не число 61.
WEATHER_CODES = {
    0: "ясно", 1: "почти ясно", 2: "облачно с прояснениями", 3: "пасмурно",
    45: "туман", 48: "туман с изморозью",
    51: "слабая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "сильная ледяная морось",
    61: "слабый дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "слабый снег", 73: "снег", 75: "сильный снег", 77: "снежная крупа",
    80: "ливень местами", 81: "ливень", 82: "сильный ливень",
    85: "снегопад местами", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "гроза с сильным градом",
}

_UNVERIFIED = ssl.create_default_context()
_UNVERIFIED.check_hostname = False
_UNVERIFIED.verify_mode = ssl.CERT_NONE


# ------------------------------------------------------------------ соседи

_NEIGHBOURS = {}


def neighbour(name):
    """Соседний скрипт из tools как модуль. Нет файла, значит None.

    Импортируем по пути, а не через sys.path: скилл лежит в папке с пробелом,
    и в трёх папках обнаружения теперь заглушки. Путь надёжнее.
    """
    if name in _NEIGHBOURS:
        return _NEIGHBOURS[name]
    path = os.path.join(HERE, name + ".py")
    module = None
    if os.path.exists(path):
        try:
            spec = importlib.util.spec_from_file_location("viora_life_" + name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001
            module = None
    _NEIGHBOURS[name] = module
    return module


def profile_value(key, root=None):
    """Поле профиля автора или пустая строка. Профиль имеет право быть пустым."""
    module = neighbour("memory")
    if module is None or not hasattr(module, "profile_get"):
        return ""
    try:
        return module.profile_get(root or SKILL, key) or ""
    except Exception:  # noqa: BLE001
        return ""


def home():
    """Рабочая папка моста: кэш погоды живёт рядом с ящиком заявок."""
    module = neighbour("tg")
    if module is not None and hasattr(module, "home"):
        try:
            return module.home()
        except Exception:  # noqa: BLE001
            pass
    env = os.environ.get("VIORA_TG_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"),
                                                            ".local", "state")
    return os.path.join(base, "viora-studio")


# -------------------------------------------------------------------- кэш

def cache_file(key):
    safe = re.sub(r"[^a-z0-9._-]+", "-", key.lower()).strip("-") or "cache"
    return os.path.join(home(), "life-cache", safe + ".json")


def cache_read(key, ttl=CACHE_TTL, now=None):
    """Свежий ответ из кэша или None. Просрочку выбрасываем молча."""
    path = cache_file(key)
    try:
        age = (now if now is not None else time.time()) - os.path.getmtime(path)
        if age > ttl:
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def cache_write(key, data):
    path = cache_file(key)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        return True
    except (OSError, TypeError, ValueError):
        return False


# ------------------------------------------------------------------- сеть

def fetch(url, timeout=TIMEOUT):
    """Одна дорога в интернет: только GET и только текст ответа."""
    headers = {"User-Agent": UA, "Accept": "application/json",
               "Accept-Language": "ru,en;q=0.9", "Accept-Encoding": "identity"}
    last = None
    for attempt in range(TRIES):
        for ctx in (None, _UNVERIFIED):
            request = urllib.request.Request(url, headers=headers)
            try:
                if ctx is None:
                    resp = urllib.request.urlopen(request, timeout=timeout)
                else:
                    resp = urllib.request.urlopen(request, timeout=timeout, context=ctx)
                raw = resp.read()
                enc = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(enc, "replace")
            except Exception as exc:  # noqa: BLE001
                last = exc
        if attempt + 1 < TRIES:
            time.sleep(0.4 * (attempt + 1))
    raise last if last else IOError("нет ответа")


# Точка подмены для тестов: селфтест гоняет весь конвейер без сети.
FETCH = fetch


def get_json(url, timeout=TIMEOUT):
    return json.loads(FETCH(url, timeout=timeout))


# ------------------------------------------------------------------ цифры

def human_bytes(size):
    """Байты в человеческие гигабайты. Тысяча, а не 1024: так пишут на коробке."""
    try:
        size = float(size)
    except (TypeError, ValueError):
        return NO_DATA
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1000 or unit == "ТБ":
            if unit in ("Б", "КБ", "МБ"):
                return "%d %s" % (round(size), unit)
            return "%.1f %s" % (size, unit)
        size /= 1000.0
    return NO_DATA


def human_uptime(seconds):
    """Секунды работы в слова: дни, часы, минуты."""
    if seconds is None:
        return NO_DATA
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return NO_DATA
    if seconds < 60:
        return "меньше минуты"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append("%d %s" % (days, plural(days, "день", "дня", "дней")))
    if hours:
        parts.append("%d %s" % (hours, plural(hours, "час", "часа", "часов")))
    if minutes and not days:
        parts.append("%d %s" % (minutes, plural(minutes, "минута", "минуты", "минут")))
    return " ".join(parts) or "меньше минуты"


def plural(count, one, few, many):
    count = abs(int(count)) % 100
    if 11 <= count <= 19:
        return many
    tail = count % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def psutil_module():
    """psutil, только если он уже стоит в системе. Ставить его мы не просим."""
    try:
        import psutil  # noqa: F401
    except Exception:  # noqa: BLE001
        return None
    return psutil


# --------------------------------------------------------------------- пк

def parse_mounts(text):
    """Из /proc/mounts оставить настоящие разделы автора, без служебных."""
    rows = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mount, kind = parts[0], parts[1].replace("\\040", " "), parts[2]
        if kind in SKIP_FS:
            continue
        if not device.startswith("/dev/") and mount != "/":
            continue
        if mount in rows:
            continue
        rows.append(mount)
    return rows


def mount_points(read=None, isdir=None):
    """Точки, по которым считаем место. На Windows это буквы диска."""
    if os.name == "nt" and read is None:
        letters = ["%s:\\" % chr(code) for code in range(ord("A"), ord("Z") + 1)]
        found = [path for path in letters if os.path.exists(path)]
        return found or ["C:\\"]
    text = read() if read is not None else read_text("/proc/mounts")
    rows = parse_mounts(text)
    # Отдельные файлы тоже бывают подмонтированы: /etc/hosts это не диск автора.
    check = isdir if isdir is not None else (os.path.isdir if read is None else None)
    if check is not None:
        rows = [mount for mount in rows if check(mount)]
    if not rows:
        rows = ["/"]
    home_dir = os.path.expanduser("~")
    if read is None and os.path.isdir(home_dir) and home_dir not in rows:
        try:
            if os.stat(home_dir).st_dev != os.stat("/").st_dev:
                rows.append(home_dir)
        except OSError:
            pass
    return rows


def disks(read=None, usage=None):
    """Место по всем разделам: сколько всего, занято и свободно."""
    measure = usage or shutil.disk_usage
    rows = []
    seen = set()
    for mount in mount_points(read=read):
        try:
            total, used, free = measure(mount)
        except (OSError, ValueError):
            continue
        if not total:
            continue
        key = (total, free)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"mount": mount, "total": int(total), "used": int(used),
                     "free": int(free), "percent": round(used * 100.0 / total, 1)})
    return rows


def mem_from_meminfo(text):
    """Память из /proc/meminfo: всего и доступно, в байтах."""
    found = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        digits = re.findall(r"\d+", value)
        if not digits:
            continue
        found[name.strip()] = int(digits[0]) * 1024
    total = found.get("MemTotal")
    free = found.get("MemAvailable")
    if free is None:
        free = found.get("MemFree")
    if not total or free is None:
        return None
    return {"total": total, "free": free,
            "percent": round((total - free) * 100.0 / total, 1)}


def mem_windows():
    """Память на Windows через ctypes: сторонних пакетов не надо."""
    try:
        import ctypes

        class Status(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = Status()
        status.dwLength = ctypes.sizeof(Status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        total = int(status.ullTotalPhys)
        free = int(status.ullAvailPhys)
        if not total:
            return None
        return {"total": total, "free": free,
                "percent": round((total - free) * 100.0 / total, 1)}
    except Exception:  # noqa: BLE001
        return None


def mem_info(read=None):
    """Память машины или None, если система молчит."""
    if read is not None:
        return mem_from_meminfo(read())
    module = psutil_module()
    if module is not None:
        try:
            data = module.virtual_memory()
            return {"total": int(data.total), "free": int(data.available),
                    "percent": round(float(data.percent), 1)}
        except Exception:  # noqa: BLE001
            pass
    if os.name == "nt":
        return mem_windows()
    return mem_from_meminfo(read_text("/proc/meminfo"))


def battery_from_sysfs(capacity, status):
    """Батарея из /sys/class/power_supply: проценты и что она делает."""
    digits = re.findall(r"\d+", capacity or "")
    if not digits:
        return None
    words = {"Charging": "заряжается", "Discharging": "от батареи",
             "Full": "заряжена", "Not charging": "не заряжается",
             "Unknown": "состояние неизвестно"}
    state = (status or "").strip()
    return {"percent": int(digits[0]), "state": words.get(state, state or NO_DATA)}


def battery_windows():
    try:
        import ctypes

        class Power(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte), ("Reserved1", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong),
                        ("BatteryFullLifeTime", ctypes.c_ulong)]

        power = Power()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)):
            return None
        percent = int(power.BatteryLifePercent)
        if percent > 100:
            return None
        state = "от сети" if int(power.ACLineStatus) == 1 else "от батареи"
        return {"percent": percent, "state": state}
    except Exception:  # noqa: BLE001
        return None


def battery_info(root=None):
    """Заряд батареи или None: у настольной машины её просто нет."""
    if root is not None:
        for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
            if not name.upper().startswith("BAT"):
                continue
            folder = os.path.join(root, name)
            data = battery_from_sysfs(read_text(os.path.join(folder, "capacity")),
                                      read_text(os.path.join(folder, "status")))
            if data:
                return data
        return None
    module = psutil_module()
    if module is not None and hasattr(module, "sensors_battery"):
        try:
            data = module.sensors_battery()
            if data is not None:
                state = "от сети" if getattr(data, "power_plugged", False) else "от батареи"
                return {"percent": int(round(data.percent)), "state": state}
        except Exception:  # noqa: BLE001
            pass
    if os.name == "nt":
        return battery_windows()
    return battery_info(root="/sys/class/power_supply")


def uptime_from_proc(text):
    digits = re.findall(r"[\d.]+", text or "")
    if not digits:
        return None
    try:
        return float(digits[0])
    except ValueError:
        return None


def uptime_seconds(read=None):
    """Сколько машина не выключалась, в секундах, или None."""
    if read is not None:
        return uptime_from_proc(read())
    if os.name == "nt":
        try:
            import ctypes

            return float(ctypes.windll.kernel32.GetTickCount64()) / 1000.0
        except Exception:  # noqa: BLE001
            return None
    value = uptime_from_proc(read_text("/proc/uptime"))
    if value is not None:
        return value
    module = psutil_module()
    if module is not None and hasattr(module, "boot_time"):
        try:
            return max(0.0, time.time() - float(module.boot_time()))
        except Exception:  # noqa: BLE001
            return None
    return None


def local_ip(connector=None):
    """Локальный адрес машины. Пакетов не шлём: connect по UDP только выбирает маршрут."""
    make = connector or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    try:
        sock = make()
        try:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:  # noqa: BLE001
        return ""


def pc_report():
    """Всё про машину одной пачкой: диски, память, батарея, время работы, адрес."""
    return {
        "host": platform.node() or socket.gethostname() or NO_DATA,
        "system": " ".join(part for part in (platform.system(), platform.release()) if part)
                  or os.name,
        "uptime": uptime_seconds(),
        "disks": disks(),
        "memory": mem_info(),
        "battery": battery_info(),
        "ip": local_ip(),
    }


def pc_text(data):
    """Короткий текст для Телеграма: строка на факт, без рассказа."""
    lines = ["ПК %s, %s" % (data.get("host") or NO_DATA, data.get("system") or NO_DATA)]
    lines.append("Работает: %s" % human_uptime(data.get("uptime")))
    rows = data.get("disks") or []
    if rows:
        for row in rows:
            lines.append("Диск %s: свободно %s из %s, занято %s%%"
                         % (row["mount"], human_bytes(row["free"]),
                            human_bytes(row["total"]), row["percent"]))
    else:
        lines.append("Диски: %s" % NO_DATA)
    memory = data.get("memory")
    if memory:
        lines.append("Память: свободно %s из %s, занято %s%%"
                     % (human_bytes(memory["free"]), human_bytes(memory["total"]),
                        memory["percent"]))
    else:
        lines.append("Память: %s" % NO_DATA)
    battery = data.get("battery")
    if battery:
        lines.append("Батарея: %s%%, %s" % (battery["percent"], battery["state"]))
    else:
        lines.append("Батарея: %s" % NO_DATA)
    lines.append("Адрес в сети: %s" % (data.get("ip") or NO_DATA))
    return "\n".join(lines)


# ------------------------------------------------------------------ погода

def geo_url(city):
    query = urllib.parse.urlencode({"name": city, "count": 1,
                                    "language": "ru", "format": "json"})
    return "%s?%s" % (GEO_URL, query)


def forecast_url(lat, lon, tz, days=3):
    query = urllib.parse.urlencode([
        ("latitude", "%.4f" % float(lat)),
        ("longitude", "%.4f" % float(lon)),
        ("daily", ",".join(DAILY_FIELDS)),
        ("current_weather", "true"),
        ("timezone", tz or "auto"),
        ("forecast_days", str(max(1, min(int(days), 7)))),
    ])
    return "%s?%s" % (FORECAST_URL, query)


def weather_word(code):
    """Код WMO в русские слова. Незнакомый код честно называем числом."""
    try:
        number = int(code)
    except (TypeError, ValueError):
        return NO_DATA
    return WEATHER_CODES.get(number, "код погоды %d" % number)


def pick(values, index):
    try:
        return values[index]
    except (TypeError, IndexError, KeyError):
        return None


def temp_text(value):
    if value is None:
        return NO_DATA
    try:
        return "%d" % int(round(float(value)))
    except (TypeError, ValueError):
        return NO_DATA


def rain_text(value):
    if value is None:
        return NO_DATA
    try:
        return "%d%%" % int(round(float(value)))
    except (TypeError, ValueError):
        return NO_DATA


def geocode(city):
    """Город в координаты. Не нашлся или сети нет, значит None."""
    city = (city or "").strip()
    if not city:
        return None
    try:
        data = get_json(geo_url(city))
    except Exception:  # noqa: BLE001
        return None
    rows = (data or {}).get("results") or []
    if not rows:
        return None
    row = rows[0]
    try:
        return {"city": row.get("name") or city,
                "lat": float(row["latitude"]), "lon": float(row["longitude"]),
                "country": row.get("country") or "", "tz": row.get("timezone") or ""}
    except (KeyError, TypeError, ValueError):
        return None


def parse_forecast(payload, days=2):
    """Ответ open-meteo в список дней с русскими словами."""
    daily = (payload or {}).get("daily") or {}
    dates = daily.get("time") or []
    rows = []
    for index, date in enumerate(dates[:max(1, int(days))]):
        code = pick(daily.get("weathercode"), index)
        rows.append({"date": date,
                     "max": pick(daily.get("temperature_2m_max"), index),
                     "min": pick(daily.get("temperature_2m_min"), index),
                     "rain": pick(daily.get("precipitation_probability_max"), index),
                     "code": code, "word": weather_word(code)})
    current = None
    now = (payload or {}).get("current_weather") or {}
    if now:
        current = {"temp": now.get("temperature"), "code": now.get("weathercode"),
                   "word": weather_word(now.get("weathercode"))}
    return {"days": rows, "now": current}


def weather_report(city="", tz="", days=2, use_cache=True):
    """Погода на сегодня и завтра. Город и пояс без аргументов берём из профиля."""
    city = (city or "").strip() or profile_value("city")
    if not city:
        return {"ok": False, "why": "город не задан, запиши его: "
                                   "python3 tools/memory.py profile set город=Астрахань"}
    tz = (tz or "").strip() or profile_value("tz")
    key = "weather-%s-%s-%d" % (city, tz or "auto", days)
    if use_cache:
        cached = cache_read(key)
        if cached:
            cached["cached"] = True
            return cached
    place = geocode(city)
    if place is None:
        return {"ok": False, "why": "город не нашёлся или сети нет: %s" % city}
    zone = tz or place.get("tz") or "auto"
    try:
        payload = get_json(forecast_url(place["lat"], place["lon"], zone, days=days + 1))
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": "прогноз не пришёл, сети нет"}
    parsed = parse_forecast(payload, days=days)
    if not parsed["days"]:
        return {"ok": False, "why": "прогноз пришёл пустым"}
    data = {"ok": True, "city": place["city"], "country": place.get("country", ""),
            "tz": (payload or {}).get("timezone") or zone,
            "days": parsed["days"], "now": parsed["now"], "cached": False}
    if use_cache:
        cache_write(key, data)
    return data


def day_name(date_text_value):
    """Имя дня недели из даты вида 2026-09-06."""
    try:
        day = datetime.date(*[int(part) for part in str(date_text_value).split("-")[:3]])
    except (TypeError, ValueError):
        return str(date_text_value or NO_DATA)
    return WEEKDAYS[day.weekday()]


def weather_text(data):
    """Две или три строки про погоду, без лишних слов."""
    if not data.get("ok"):
        return "Погода: %s" % (data.get("why") or NO_DATA)
    lines = ["Погода: %s" % data.get("city", NO_DATA)]
    now = data.get("now") or None
    if now and now.get("temp") is not None:
        lines.append("Сейчас: %s, %s" % (now.get("word", NO_DATA), temp_text(now.get("temp"))))
    names = ("Сегодня", "Завтра", "Послезавтра")
    for index, row in enumerate(data.get("days") or []):
        label = names[index] if index < len(names) else day_name(row.get("date"))
        lines.append("%s: %s, от %s до %s, дождь %s"
                     % (label, row.get("word", NO_DATA), temp_text(row.get("min")),
                        temp_text(row.get("max")), rain_text(row.get("rain"))))
    if data.get("cached"):
        lines.append("Цифры из кэша, моложе получаса.")
    return "\n".join(lines)


# -------------------------------------------------------------------- утро

def date_text(now):
    return "%s, %d %s" % (WEEKDAYS[now.weekday()], now.day, MONTHS[now.month - 1])


def reminders_today(now=None, provider=None):
    """Напоминания на сегодня из tools/schedule.py. Нет файла, значит пусто."""
    if provider is not None:
        try:
            return [str(row) for row in provider(now)]
        except Exception:  # noqa: BLE001
            return []
    module = neighbour("schedule")
    if module is None or not hasattr(module, "today_lines"):
        return []
    try:
        return [str(row) for row in module.today_lines(now=now)]
    except Exception:  # noqa: BLE001
        return []


def clip(text, limit=MORNING_LIMIT):
    """Обрезка по границе строки: в Телеграме лучше короче, чем обрубок слова."""
    if len(text) <= limit:
        return text
    tail = "дальше не влезло"
    kept, total = [], 0
    for line in text.splitlines():
        if total + len(line) + 1 > limit - len(tail) - 1:
            break
        kept.append(line)
        total += len(line) + 1
    kept.append(tail)
    return "\n".join(kept)


def morning_report(now=None, use_cache=True, provider=None):
    """Дайджест на утро: дата, погода, место, батарея, напоминания."""
    now = now or datetime.datetime.now()
    return {"date": date_text(now), "name": profile_value("name"),
            "weather": weather_report(use_cache=use_cache),
            "disks": disks(), "battery": battery_info(),
            "reminders": reminders_today(now, provider=provider)}


def morning_text(data, limit=MORNING_LIMIT):
    """Одно сообщение автору до %d знаков."""
    name = data.get("name") or ""
    hello = "Доброе утро, %s." % name if name else "Доброе утро."
    lines = ["%s %s" % (hello, data.get("date", ""))]
    lines.append(weather_text(data.get("weather") or {}))
    rows = data.get("disks") or []
    if rows:
        worst = min(rows, key=lambda row: row["free"])
        lines.append("Место: на %s свободно %s, занято %s%%"
                     % (worst["mount"], human_bytes(worst["free"]), worst["percent"]))
    else:
        lines.append("Место: %s" % NO_DATA)
    battery = data.get("battery")
    if battery:
        lines.append("Батарея: %s%%, %s" % (battery["percent"], battery["state"]))
    tasks = data.get("reminders") or []
    if tasks:
        lines.append("Напоминания на сегодня:")
        for row in tasks:
            lines.append("- %s" % row)
    else:
        lines.append("Напоминаний на сегодня нет.")
    return clip("\n".join(lines), limit)


# ------------------------------------------------------------------ тесты

FAKE_GEO = json.dumps({"results": [{"name": "Астрахань", "latitude": 46.3497,
                                   "longitude": 48.0408, "country": "Россия",
                                   "timezone": "Europe/Astrakhan"}]},
                      ensure_ascii=False)
FAKE_FORECAST = json.dumps({
    "timezone": "Europe/Astrakhan",
    "current_weather": {"temperature": 18.4, "weathercode": 3},
    "daily": {"time": ["2026-09-06", "2026-09-07", "2026-09-08"],
              "temperature_2m_max": [24.1, 26.0, 27.4],
              "temperature_2m_min": [12.3, 14.0, 15.1],
              "precipitation_probability_max": [60, 5, 0],
              "weathercode": [61, 0, 95]}}, ensure_ascii=False)
FAKE_MEMINFO = ("MemTotal:       16000000 kB\nMemFree:          200000 kB\n"
                "MemAvailable:    8000000 kB\nBuffers:          100000 kB\n")
FAKE_MOUNTS = ("proc /proc proc rw 0 0\n"
               "/dev/nvme0n1p2 / ext4 rw 0 0\n"
               "tmpfs /dev/shm tmpfs rw 0 0\n"
               "/dev/nvme0n1p3 /home ext4 rw 0 0\n"
               "//server/share /mnt/net cifs rw 0 0\n")


def fake_fetch(url, timeout=TIMEOUT):
    """Сеть для тестов: два адреса известны, остальное отказ."""
    if "geocoding-api" in url:
        return FAKE_GEO
    if "/v1/forecast" in url:
        return FAKE_FORECAST
    raise IOError("в тестах сети нет: %s" % url)


class FakeSocket(object):
    """Подмена сокета: никуда не ходит, просто зовётся адресом."""

    def __init__(self, ip="192.168.1.42"):
        self.ip = ip
        self.closed = False

    def connect(self, where):
        self.where = where

    def getsockname(self):
        return (self.ip, 51515)

    def close(self):
        self.closed = True


def selftest():
    """Всё проверяется без сети и без чужих пакетов."""
    global FETCH
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    saved_fetch = FETCH
    saved_home = os.environ.get("VIORA_TG_HOME")
    saved_neighbours = dict(_NEIGHBOURS)
    box = tempfile.mkdtemp(prefix="viora-life-")
    os.environ["VIORA_TG_HOME"] = box
    _NEIGHBOURS["tg"] = None
    _NEIGHBOURS["memory"] = None
    _NEIGHBOURS["schedule"] = None
    texts = []
    try:
        FETCH = fake_fetch

        # Разделы и место
        mounts = parse_mounts(FAKE_MOUNTS)
        ok("служебные разделы выкинуты", "/proc" not in mounts and "/dev/shm" not in mounts)
        ok("корень и домашний раздел на месте", mounts == ["/", "/home"])
        ok("сетевая папка не считается диском", "/mnt/net" not in mounts)
        picked = mount_points(read=lambda: FAKE_MOUNTS + "/dev/root /etc/hosts ext4 rw 0 0\n",
                              isdir=lambda path: path != "/etc/hosts")
        ok("подмонтированный файл не считается диском",
           "/etc/hosts" not in picked and "/" in picked)
        ok("пустой список разделов падает на корень",
           mount_points(read=lambda: "", isdir=lambda path: True) == ["/"])
        rows = disks(read=lambda: FAKE_MOUNTS,
                     usage=lambda mount: (500 * 10 ** 9, 400 * 10 ** 9, 100 * 10 ** 9)
                     if mount == "/" else (1000 * 10 ** 9, 100 * 10 ** 9, 900 * 10 ** 9))
        ok("диски собрались", len(rows) == 2)
        ok("процент занятого считается", rows[0]["percent"] == 80.0)
        ok("одинаковые разделы не двоятся",
           len(disks(read=lambda: FAKE_MOUNTS,
                     usage=lambda mount: (10, 4, 6))) == 1)
        ok("байты в гигабайты", human_bytes(12300000000) == "12.3 ГБ")
        ok("мегабайты без дробей", human_bytes(5000000) == "5 МБ")
        ok("мусор вместо числа не ломает", human_bytes("как-то так") == NO_DATA)

        # Память, батарея, время работы
        memory = mem_from_meminfo(FAKE_MEMINFO)
        ok("память разобралась", memory and memory["total"] == 16000000 * 1024)
        ok("свободно берётся из MemAvailable", memory["free"] == 8000000 * 1024)
        ok("процент памяти считается", memory["percent"] == 50.0)
        ok("пустой meminfo даёт нет данных", mem_info(read=lambda: "") is None)
        battery = battery_from_sysfs("87\n", "Discharging\n")
        ok("батарея разобралась", battery == {"percent": 87, "state": "от батареи"})
        ok("зарядка по-русски",
           battery_from_sysfs("100", "Full")["state"] == "заряжена")
        ok("без батареи честный None", battery_from_sysfs("", "") is None)
        ok("батарея без папки не падает",
           battery_info(root=os.path.join(box, "нету")) is None)
        ok("время работы разобралось", uptime_from_proc("93784.12 12345.6") == 93784.12)
        ok("время работы словами", human_uptime(93784) == "1 день 2 часа")
        ok("минуты словами", human_uptime(300) == "5 минут")
        ok("короткий уптайм честный", human_uptime(10) == "меньше минуты")
        ok("нет уптайма, нет данных", human_uptime(None) == NO_DATA)
        ok("адрес берётся из сокета", local_ip(connector=FakeSocket) == "192.168.1.42")

        def broken_socket():
            raise OSError("сети нет")

        ok("без сети адрес пуст", local_ip(connector=broken_socket) == "")

        # Текст про пк
        text = pc_text({"host": "luna-pc", "system": "Windows 11", "uptime": 93784,
                        "disks": rows, "memory": memory,
                        "battery": {"percent": 87, "state": "от батареи"},
                        "ip": "192.168.1.42"})
        texts.append(text)
        ok("в тексте пк есть диск", "Диск /" in text)
        ok("в тексте пк есть память и батарея",
           "Память:" in text and "Батарея: 87%" in text)
        ok("текст пк короткий", len(text) <= 700)
        empty = pc_text({})
        texts.append(empty)
        ok("пустой пк пишет нет данных", empty.count(NO_DATA) >= 3)

        # Погода
        ok("код 0 это ясно", weather_word(0) == "ясно")
        ok("код 61 это дождь", weather_word(61) == "слабый дождь")
        ok("код 95 это гроза", weather_word(95) == "гроза")
        ok("чужой код называем числом", weather_word(777) == "код погоды 777")
        ok("пустой код это нет данных", weather_word(None) == NO_DATA)
        ok("в адресе геокодинга есть город и язык",
           "language=ru" in geo_url("Астрахань") and "name=" in geo_url("Астрахань"))
        url = forecast_url(46.3497, 48.0408, "Europe/Astrakhan", days=3)
        ok("в адресе прогноза все четыре поля",
           all(field in url for field in DAILY_FIELDS))
        ok("пояс уехал в адрес", "Europe%2FAstrakhan" in url)
        place = geocode("Астрахань")
        ok("город нашёлся", place and place["city"] == "Астрахань")
        ok("координаты числами", isinstance(place["lat"], float))
        ok("без города геокодинг молчит", geocode("") is None)
        parsed = parse_forecast(json.loads(FAKE_FORECAST), days=2)
        ok("два дня в прогнозе", len(parsed["days"]) == 2)
        ok("слово погоды на месте", parsed["days"][0]["word"] == "слабый дождь")
        ok("вероятность дождя на месте", parsed["days"][0]["rain"] == 60)
        ok("текущая погода разобралась", parsed["now"]["word"] == "пасмурно")
        ok("пустой ответ даёт пустые дни", parse_forecast({}, days=2)["days"] == [])

        data = weather_report(city="Астрахань", tz="Europe/Astrakhan", use_cache=False)
        ok("погода собралась", data.get("ok") and len(data["days"]) == 2)
        text = weather_text(data)
        texts.append(text)
        ok("в тексте погоды есть город", text.startswith("Погода: Астрахань"))
        ok("в тексте погоды есть сегодня и завтра",
           "Сегодня:" in text and "Завтра:" in text)
        ok("температура округлилась", "от 12 до 24" in text)
        ok("без города просим профиль",
           "profile set" in weather_report(city="", use_cache=False).get("why", ""))

        cached_once = weather_report(city="Астрахань", tz="Europe/Astrakhan", use_cache=True)
        ok("первый ответ свежий", cached_once.get("ok") and not cached_once.get("cached"))
        FETCH = lambda *a, **k: (_ for _ in ()).throw(IOError("сети нет"))  # noqa: E731
        again = weather_report(city="Астрахань", tz="Europe/Astrakhan", use_cache=True)
        ok("второй ответ из кэша", again.get("ok") and again.get("cached"))
        ok("кэш лежит в рабочей папке моста", cache_file("weather-x").startswith(box))
        ok("просроченный кэш не берётся",
           cache_read("weather-Астрахань-Europe/Astrakhan-2", ttl=0) is None)
        ok("без сети и без кэша честный отказ",
           weather_report(city="Казань", use_cache=False).get("ok") is False)
        FETCH = fake_fetch

        # Утро
        ok("дата по-русски",
           date_text(datetime.datetime(2026, 9, 6, 8, 0)) == "воскресенье, 6 сентября")
        ok("без расписания напоминаний нет", reminders_today() == [])
        ok("поставщик напоминаний работает",
           reminders_today(provider=lambda now: ["08:00 выпить таблетку"]) ==
           ["08:00 выпить таблетку"])
        ok("кривой поставщик не роняет утро",
           reminders_today(provider=lambda now: 1 / 0) == [])
        digest = {"date": "воскресенье, 6 сентября", "name": "Луна",
                  "weather": data, "disks": rows,
                  "battery": {"percent": 87, "state": "от батареи"},
                  "reminders": ["08:00 morning", "09:30 выпить таблетку"]}
        text = morning_text(digest)
        texts.append(text)
        ok("утро здоровается именем", text.startswith("Доброе утро, Луна."))
        ok("в утре есть погода, место и напоминания",
           "Погода:" in text and "Место:" in text and "08:00 morning" in text)
        ok("утро влезает в одно сообщение", len(text) <= MORNING_LIMIT)
        ok("без имени утро тоже вежливое",
           morning_text(dict(digest, name="")).startswith("Доброе утро. "))
        ok("пустое расписание говорит прямо",
           "Напоминаний на сегодня нет." in morning_text(dict(digest, reminders=[])))
        long_text = morning_text(dict(digest, reminders=["%02d:00 дело номер %d" % (i % 24, i)
                                                        for i in range(200)]))
        texts.append(long_text)
        ok("длинное утро обрезано", len(long_text) <= MORNING_LIMIT)
        ok("обрезка честно признаётся", long_text.endswith("дальше не влезло"))
        ok("короткий текст не режется", clip("две\nстроки", 100) == "две\nстроки")

        # Формат вывода
        ok("всё уезжает в json", isinstance(json.dumps(digest, ensure_ascii=False), str))
        joined = "\n".join(texts)
        ok("длинных тире нет ни в одном тексте",
           not any(ch in joined for ch in ("\u2014", "\u2013", "\u2015", "\u2012", "\u2212")))
    finally:
        FETCH = saved_fetch
        _NEIGHBOURS.clear()
        _NEIGHBOURS.update(saved_neighbours)
        if saved_home is None:
            os.environ.pop("VIORA_TG_HOME", None)
        else:
            os.environ["VIORA_TG_HOME"] = saved_home
        shutil.rmtree(box, ignore_errors=True)

    failed = 0
    for name, good in checks:
        print(("PASS " if good else "FAIL ") + name)
        if not good:
            failed += 1
    print("Итого: %d проверок жизни, провалилось %d" % (len(checks), failed))
    return 1 if failed else 0


# --------------------------------------------------------------------- вход

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="life.py",
        description="Пк, погода и утренний дайджест одной строкой для Телеграма")
    parser.add_argument("command", nargs="?", choices=("pc", "weather", "morning"),
                        help="pc, weather или morning")
    parser.add_argument("city", nargs="*", help="город для weather, по умолчанию из профиля")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--no-cache", action="store_true", dest="no_cache",
                        help="спросить погоду заново, мимо кэша")
    parser.add_argument("--days", type=int, default=2, help="сколько дней прогноза")
    parser.add_argument("--selftest", action="store_true", help="тесты без сети")
    parser.add_argument("--version", action="version", version="life.py %s" % VERSION)
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.command:
        parser.print_help()
        return 2

    use_cache = not args.no_cache
    if args.command == "pc":
        data = pc_report()
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.as_json else pc_text(data))
        return 0 if (data["disks"] or data["memory"]) else 1
    if args.command == "weather":
        data = weather_report(city=" ".join(args.city), days=max(1, args.days),
                              use_cache=use_cache)
        print(json.dumps(data, ensure_ascii=False, indent=2)
              if args.as_json else weather_text(data))
        return 0 if data.get("ok") else 1
    data = morning_report(use_cache=use_cache)
    print(json.dumps(data, ensure_ascii=False, indent=2)
          if args.as_json else morning_text(data))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("")
        sys.exit(1)
