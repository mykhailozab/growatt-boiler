import os
import re
import sys
import traceback
 
import tinytuya
from growattServer import GrowattApi
 
# ───────────────────────── Настройки ─────────────────────────
GROWATT_USER = os.getenv("GROWATT_USER")
GROWATT_PASS = os.getenv("GROWATT_PASS")
 
TUYA_CLIENT_ID = os.getenv("TUYA_CLIENT_ID")
TUYA_SECRET = os.getenv("TUYA_SECRET")
TUYA_DEVICE_ID = os.getenv("TUYA_DEVICE_ID")
# Именно `or`, а не значение по умолчанию в getenv: если секрета нет,
# GitHub передаёт пустую строку, и getenv("X", "eu") вернул бы "".
TUYA_REGION = os.getenv("TUYA_REGION") or "eu"
 
GRID_MIN_VOLTAGE = 150.0   # ниже этого значения считаем, что городской сети нет
TUYA_SWITCH_CODE = "switch_1"  # код выключателя у большинства розеток Tuya
VOLTAGE_KEY = ""           # если автопоиск не нашёл напряжение — впишите сюда имя поля из лога
DEBUG = os.getenv("DEBUG") == "1"
 
# Имена поля «напряжение сети на ВХОДЕ» у разных типов инверторов Growatt (в нижнем регистре).
# Специально нет outPutVolt / vEps и т.п.: это напряжение на ВЫХОДЕ инвертора,
# оно есть всегда, даже когда городской сети нет.
GRID_KEYS = ["vgrid", "gridvoltage", "vac1", "vac", "vact", "vacr", "uac1"]
 
# Типы устройств в Growatt называются по-разному — приводим к четырём группам.
TYPE_ALIASES = {"spf": "storage", "sph": "mix", "min": "tlx", "mod": "tlx", "inv": "inverter"}
 
 
def log(*args):
    print(*args, flush=True)
 
 
# ───────────────────────── Вспомогательное ─────────────────────────
def flatten(obj, out=None):
    """Разворачивает вложенные dict/list в один плоский словарь {имя_поля: значение}."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)):
                flatten(value, out)
            else:
                out.setdefault(str(key), value)
    elif isinstance(obj, list):
        for item in obj[:50]:
            flatten(item, out)
    return out
 
 
def to_float(value):
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
 
 
def find_grid_voltage(data):
    """Ищет напряжение сети в плоском словаре. Возвращает (имя_поля, значение) или (None, None)."""
    lower = {k.lower(): k for k in data}
 
    if VOLTAGE_KEY:  # ключ задан вручную — ищем только его
        key = lower.get(VOLTAGE_KEY.lower())
        value = to_float(data[key]) if key else None
        return (key, value) if value is not None else (None, None)
 
    for name in GRID_KEYS:
        key = lower.get(name)
        if key is not None:
            value = to_float(data[key])
            if value is not None:
                return key, value
 
    for low, key in lower.items():  # запасной вариант: что-то вроде gridVolt / voltageGrid
        if re.search(r"grid.*volt|volt.*grid", low):
            value = to_float(data[key])
            if value is not None:
                return key, value
    return None, None
 
 
def normalize_devices(devices):
    """device_list() в разных версиях библиотеки отдаёт список или словарь — приводим к списку."""
    if isinstance(devices, dict):
        for key in ("deviceList", "inverters", "datas", "obj", "data"):
            value = devices.get(key)
            if isinstance(value, dict):
                value = value.get("datas") or value.get("deviceList")
            if isinstance(value, list):
                devices = value
                break
        else:
            return []
    if not isinstance(devices, list):
        return []
    return [d for d in devices if isinstance(d, dict)]
 
 
def dump_numbers(name, flat):
    """Печатает только числовые поля (строки — имена, серийники — не печатаем)."""
    nums = ", ".join(f"{k}={v}" for k, v in flat.items() if to_float(v) is not None)
    log(f"  Числовые поля {name}: {nums}")
 
 
# ───────────────────────── Growatt ─────────────────────────
def get_grid_voltage():
    """Возвращает напряжение сети (В) или None, если получить не удалось."""
    api = GrowattApi()
    api.server_url = "https://server.growatt.com/"
 
    # Имитируем браузер для обхода защиты Cloudflare
    api.session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://server.growatt.com/",
    })
 
    log("Growatt: логин...")
    login = api.login(GROWATT_USER, GROWATT_PASS)
    if not isinstance(login, dict) or login.get("success") is False:
        msg = login.get("msg") if isinstance(login, dict) else login
        log(f"Growatt: логин не удался (msg={msg})")
        return None
 
    plants = login.get("data")
    if not plants or not isinstance(plants, list):
        log("Growatt: у аккаунта не найдено ни одной станции")
        return None
    plant_id = plants[0].get("plantId")
 
    devices = normalize_devices(api.device_list(plant_id))
    if not devices:
        log("Growatt: в станции нет устройств")
        return None
    dev = devices[0]
    sn = dev.get("deviceSn") or dev.get("sn") or dev.get("serialNum")
    dtype = str(dev.get("deviceType") or dev.get("deviceTypeName") or "").lower()
    if not sn:
        log(f"Growatt: не удалось взять серийный номер, поля устройства: {list(dev.keys())}")
        return None
    log(f"Growatt: устройств {len(devices)}, тип первого: '{dtype}', "
        f"последнее обновление данных: {dev.get('lastUpdateTime')}")
 
    # Какие методы библиотеки вызывать для какого типа.
    # mix_detail из старого кода тут сознательно нет: он только для MIX и отдаёт
    # графики выработки за период, а не текущее напряжение.
    groups = {
        "storage": [("storage_params", lambda: api.storage_params(sn)),
                    ("storage_detail", lambda: api.storage_detail(sn))],
        "mix": [("mix_system_status", lambda: api.mix_system_status(sn, plant_id)),
                ("mix_info", lambda: api.mix_info(sn, plant_id))],
        "tlx": [("tlx_system_status", lambda: api.tlx_system_status(plant_id, sn)),
                ("tlx_detail", lambda: api.tlx_detail(sn))],
        "inverter": [("inverter_detail", lambda: api.inverter_detail(sn))],
    }
    main_group = TYPE_ALIASES.get(dtype, dtype)
    order = ([main_group] if main_group in groups else []) + [g for g in groups if g != main_group]
 
    seen = []  # (метод, поля) — на случай, если напряжение не найдём
    for group in order:
        for name, call in groups[group]:
            try:
                raw = call()
            except Exception as e:  # метод может не подходить к этому типу устройства
                log(f"  {name}: {type(e).__name__}: {e}")
                continue
            flat = flatten(raw) if raw else {}
            if not flat:
                log(f"  {name}: пустой ответ")
                continue
            key, volts = find_grid_voltage(flat)
            if key:
                log(f"  {name}: поле '{key}' = {volts} В")
                if DEBUG:
                    dump_numbers(name, flat)
                return volts
            log(f"  {name}: поля с напряжением сети нет")
            seen.append((name, flat))
 
    log("Growatt: напряжение сети не найдено ни в одном ответе. Все числовые поля:")
    for name, flat in seen:
        dump_numbers(name, flat)
    return None
 
 
# ───────────────────────── Tuya ─────────────────────────
def set_socket(turn_on: bool) -> bool:
    cloud = tinytuya.Cloud(
        apiRegion=TUYA_REGION,
        apiKey=TUYA_CLIENT_ID,
        apiSecret=TUYA_SECRET,
        apiDeviceID=TUYA_DEVICE_ID,
    )
    commands = {"commands": [{"code": TUYA_SWITCH_CODE, "value": turn_on}]}
    result = cloud.sendcommand(TUYA_DEVICE_ID, commands)
 
    state = "ВКЛ" if turn_on else "ВЫКЛ"
    if isinstance(result, dict) and result.get("success"):
        log(f"Tuya: команда принята, розетка → {state}")
        return True
 
    log(f"Tuya: команда НЕ прошла. Ответ: {result}")
    try:  # диагностика: какие коды у устройства на самом деле
        log(f"Tuya: статус устройства: {cloud.getstatus(TUYA_DEVICE_ID)}")
    except Exception as e:
        log(f"Tuya: и статус получить не удалось: {e}")
    return False
 
 
# ───────────────────────── Основная логика ─────────────────────────
def main():
    required = {
        "GROWATT_USER": GROWATT_USER, "GROWATT_PASS": GROWATT_PASS,
        "TUYA_CLIENT_ID": TUYA_CLIENT_ID, "TUYA_SECRET": TUYA_SECRET,
        "TUYA_DEVICE_ID": TUYA_DEVICE_ID,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        log("Не заданы секреты в GitHub: " + ", ".join(missing))
        sys.exit(1)
 
    try:
        volts = get_grid_voltage()
    except Exception:
        traceback.print_exc()
        volts = None
 
    if volts is None:
        # Данных нет — розетку НЕ трогаем (не гадаем), но запуск помечаем как неудачный.
        log("Не удалось получить напряжение сети. Розетку не трогаю.")
        sys.exit(1)
 
    grid_ok = volts > GRID_MIN_VOLTAGE
    log(f"Напряжение сети: {volts} В → сеть {'есть' if grid_ok else 'НЕТ'}")
    if not grid_ok:
        log("Работаем от инвертора → выключаем розетку (бойлер).")
    ok = set_socket(grid_ok)
    sys.exit(0 if ok else 1)
 
 
if __name__ == "__main__":
    main()
