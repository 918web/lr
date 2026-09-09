#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autostart.py 1.0.0

Автозапуск сторожа Телеграма после каждого включения компьютера.

Один раз: python3 tools/autostart.py install. Дальше сторож поднимается сам
при входе в систему, без окна, с логом в рабочей папке моста. Убрать:
python3 tools/autostart.py remove. Посмотреть: python3 tools/autostart.py status.

Что ставится:
    Windows   задача планировщика schtasks /SC ONLOGON, запуск pythonw.exe
              с tools/tg_watch.py, окна нет, вывод в watch.log
    macOS     LaunchAgent ~/Library/LaunchAgents/studio.viora.watch.plist
              с KeepAlive: упал или комп проснулся, launchd поднимет снова
    Linux     systemd --user unit ~/.config/systemd/user/viora-watch.service
              с Restart=on-failure и enable

Сам сторож не даст запустить второй экземпляр: lock-файл с pid и сердцебиением.
Поэтому автозапуск и ручной start-watch не мешают друг другу.

Коды возврата: 0 порядок, 1 не получилось, 2 неизвестная система или ошибка вызова.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
ROOT = os.path.dirname(SKILL)

TASK_NAME = "VIORA STUDIO watch"
PLIST_LABEL = "studio.viora.watch"
UNIT_NAME = "viora-watch.service"

# Тесты подменяют эти точки, чтобы не трогать настоящий планировщик.
RUNNER = subprocess.run
SYSTEM = None


def system_name():
    if SYSTEM:
        return SYSTEM
    name = platform.system().lower()
    if name.startswith("win"):
        return "windows"
    if name == "darwin":
        return "macos"
    if name == "linux":
        return "linux"
    return name or "unknown"


def home_dir():
    return os.path.expanduser("~")


def state_home():
    """Рабочая папка моста: лог автозапуска лежит рядом с ящиком."""
    env = os.environ.get("VIORA_TG_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(home_dir(), ".local", "state")
    return os.path.join(base, "viora-studio")


def python_for_service():
    """Каким питоном запускать. На Windows pythonw, чтобы не было окна."""
    exe = sys.executable or "python3"
    if system_name() == "windows":
        folder, name = os.path.split(exe)
        quiet = os.path.join(folder, "pythonw.exe")
        if os.path.exists(quiet):
            return quiet
    return exe


def watch_script():
    return os.path.join(HERE, "tg_watch.py")


def secrets_dir():
    return os.path.join(ROOT, "secrets")


def run(argv, **kwargs):
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    try:
        done = RUNNER(argv, **kwargs)
    except OSError as err:
        return 127, "", str(err)
    return done.returncode, (done.stdout or ""), (done.stderr or "")


# ------------------------------------------------------------------ Windows

def win_command():
    """Строка команды для schtasks: пути в кавычках, лог через cmd."""
    log = os.path.join(state_home(), "autostart.log")
    return ('cmd /c ""%s" "%s" --quiet >> "%s" 2>&1"'
            % (python_for_service(), watch_script(), log))


def win_install():
    os.makedirs(state_home(), exist_ok=True)
    argv = ["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
            "/TN", TASK_NAME, "/TR", win_command()]
    code, out, err = run(argv)
    if code != 0:
        return False, (err or out).strip() or "schtasks вернул код %d" % code
    return True, "задача планировщика создана: %s" % TASK_NAME


def win_remove():
    code, out, err = run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME])
    if code != 0:
        text = (err or out).strip()
        if "не удается найти" in text.lower() or "cannot find" in text.lower():
            return True, "задачи и не было"
        return False, text or "schtasks вернул код %d" % code
    return True, "задача удалена: %s" % TASK_NAME


def win_status():
    code, out, err = run(["schtasks", "/Query", "/TN", TASK_NAME])
    if code != 0:
        return False, "задача не стоит"
    return True, "задача планировщика %s" % TASK_NAME


# ------------------------------------------------------------------ macOS

def plist_path():
    return os.path.join(home_dir(), "Library", "LaunchAgents", PLIST_LABEL + ".plist")


def plist_text():
    log = os.path.join(state_home(), "autostart.log")
    rows = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0">',
        "<dict>",
        "  <key>Label</key><string>%s</string>" % PLIST_LABEL,
        "  <key>ProgramArguments</key>",
        "  <array>",
        "    <string>%s</string>" % python_for_service(),
        "    <string>%s</string>" % watch_script(),
        "    <string>--quiet</string>",
        "  </array>",
        "  <key>WorkingDirectory</key><string>%s</string>" % SKILL,
        "  <key>EnvironmentVariables</key>",
        "  <dict>",
        "    <key>VIORA_SECRETS</key><string>%s</string>" % secrets_dir(),
        "    <key>PYTHONIOENCODING</key><string>utf-8</string>",
        "    <key>PYTHONUTF8</key><string>1</string>",
        "    <key>PATH</key><string>%s</string>" % os.environ.get(
            "PATH", "/usr/local/bin:/usr/bin:/bin"),
        "  </dict>",
        "  <key>RunAtLoad</key><true/>",
        "  <key>KeepAlive</key><true/>",
        "  <key>ThrottleInterval</key><integer>15</integer>",
        "  <key>StandardOutPath</key><string>%s</string>" % log,
        "  <key>StandardErrorPath</key><string>%s</string>" % log,
        "</dict>",
        "</plist>",
        "",
    ]
    return "\n".join(rows)


def mac_install():
    path = plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(state_home(), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(plist_text())
    run(["launchctl", "unload", path])
    code, out, err = run(["launchctl", "load", "-w", path])
    if code != 0:
        return False, (err or out).strip() or "launchctl вернул код %d" % code
    return True, "LaunchAgent записан и загружен: %s" % path


def mac_remove():
    path = plist_path()
    if not os.path.exists(path):
        return True, "агента и не было"
    run(["launchctl", "unload", "-w", path])
    try:
        os.unlink(path)
    except OSError as err:
        return False, "не удалил plist: %s" % err
    return True, "LaunchAgent снят: %s" % path


def mac_status():
    path = plist_path()
    if not os.path.exists(path):
        return False, "plist не стоит"
    code, out, _err = run(["launchctl", "list"])
    loaded = code == 0 and PLIST_LABEL in out
    return True, "LaunchAgent %s, %s" % (PLIST_LABEL, "загружен" if loaded else "записан, но не загружен")


# ------------------------------------------------------------------ Linux

def unit_path():
    return os.path.join(home_dir(), ".config", "systemd", "user", UNIT_NAME)


def unit_text():
    rows = [
        "[Unit]",
        "Description=VIORA STUDIO: сторож Избранного в Телеграме",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=%s" % SKILL,
        "Environment=VIORA_SECRETS=%s" % secrets_dir(),
        "Environment=PYTHONIOENCODING=utf-8",
        "Environment=PYTHONUTF8=1",
        "ExecStart=%s %s --quiet" % (python_for_service(), watch_script()),
        "Restart=on-failure",
        "RestartSec=15",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(rows)


def linux_install():
    if not shutil.which("systemctl"):
        return False, "нет systemctl: запускай sh start-watch.sh руками или через cron @reboot"
    path = unit_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(state_home(), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(unit_text())
    run(["systemctl", "--user", "daemon-reload"])
    code, out, err = run(["systemctl", "--user", "enable", "--now", UNIT_NAME])
    if code != 0:
        return False, (err or out).strip() or "systemctl вернул код %d" % code
    return True, "unit записан и включён: %s" % path


def linux_remove():
    path = unit_path()
    if not os.path.exists(path):
        return True, "unit и не было"
    run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
    try:
        os.unlink(path)
    except OSError as err:
        return False, "не удалил unit: %s" % err
    run(["systemctl", "--user", "daemon-reload"])
    return True, "unit снят: %s" % path


def linux_status():
    path = unit_path()
    if not os.path.exists(path):
        return False, "unit не стоит"
    code, out, _err = run(["systemctl", "--user", "is-enabled", UNIT_NAME])
    state = (out or "").strip() or "неизвестно"
    return True, "systemd unit %s, %s" % (UNIT_NAME, state)


# ------------------------------------------------------------------ команды

HANDLERS = {
    "windows": (win_install, win_remove, win_status),
    "macos": (mac_install, mac_remove, mac_status),
    "linux": (linux_install, linux_remove, linux_status),
}


def do(action):
    name = system_name()
    if name not in HANDLERS:
        return False, "система %s: автозапуск ставится руками, смотри TELEGRAM-SETUP.md" % name
    install, remove, status = HANDLERS[name]
    if action == "install":
        if not os.path.exists(watch_script()):
            return False, "нет tools/tg_watch.py рядом, ставить нечего"
        return install()
    if action == "remove":
        return remove()
    return status()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":
        return selftest()
    ap = argparse.ArgumentParser(description="Автозапуск сторожа Телеграма при входе в систему")
    ap.add_argument("action", nargs="?", choices=("install", "remove", "status", "selftest"),
                    default="status")
    ap.add_argument("--short", action="store_true", help="одна строка для doctor")
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--version", action="version", version="autostart.py %s" % VERSION)
    args = ap.parse_args(argv)
    if args.action == "selftest":
        return selftest()
    ok, text = do(args.action)
    if args.as_json:
        print(json.dumps({"system": system_name(), "action": args.action, "ok": ok,
                          "detail": text}, ensure_ascii=False))
        return 0 if ok else 1
    if args.action == "status":
        if args.short:
            print(("установлен: %s" % text) if ok else ("нет: %s" % text))
            return 0
        print("Автозапуск сторожа, %s" % system_name())
        print("  %s %s" % ("установлен," if ok else "не установлен,", text))
        if not ok:
            print("  поставить: python3 tools/autostart.py install")
        return 0
    print(text)
    if ok and args.action == "install":
        print("Сторож поднимется сам при следующем входе в систему. Проверка: python3 tools/tg.py doctor")
    return 0 if ok else 1


# ------------------------------------------------------------------ тесты

class FakeDone(object):
    def __init__(self, code=0, out="", err=""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


def selftest():
    global RUNNER, SYSTEM
    import tempfile
    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        head = argv[0]
        if head == "schtasks" and "/Query" in argv:
            return FakeDone(0, "VIORA STUDIO watch  Ready")
        if head == "launchctl" and "list" in argv:
            return FakeDone(0, "123\t0\t%s" % PLIST_LABEL)
        if head == "systemctl" and "is-enabled" in argv:
            return FakeDone(0, "enabled\n")
        return FakeDone(0, "", "")

    keep = (RUNNER, SYSTEM, os.environ.get("HOME"), os.environ.get("VIORA_TG_HOME"))
    tmp = tempfile.mkdtemp(prefix="viora-autostart-")
    try:
        RUNNER = fake_run
        os.environ["HOME"] = tmp
        os.environ["VIORA_TG_HOME"] = os.path.join(tmp, "state")

        SYSTEM = "windows"
        cmd = win_command()
        ok("windows: команда с tg_watch и логом", "tg_watch.py" in cmd and "autostart.log" in cmd)
        good, text = do("install")
        ok("windows: install зовёт schtasks ONLOGON",
           good and calls[-1][0] == "schtasks" and "ONLOGON" in calls[-1] and TASK_NAME in calls[-1])
        good, text = do("status")
        ok("windows: status видит задачу", good and TASK_NAME in text)
        good, text = do("remove")
        ok("windows: remove зовёт /Delete", good and "/Delete" in calls[-1])

        SYSTEM = "macos"
        good, text = do("install")
        ok("macos: plist записан", good and os.path.exists(plist_path()))
        body = open(plist_path(), encoding="utf-8").read()
        ok("macos: KeepAlive и RunAtLoad", "<key>KeepAlive</key><true/>" in body and "RunAtLoad" in body)
        ok("macos: секреты через окружение", "VIORA_SECRETS" in body and "secrets" in body)
        ok("macos: launchctl load", calls[-1][:2] == ["launchctl", "load"])
        good, text = do("status")
        ok("macos: status видит агента", good and "загружен" in text)
        good, text = do("remove")
        ok("macos: plist снят", good and not os.path.exists(plist_path()))

        SYSTEM = "linux"
        keep_which = shutil.which
        shutil.which = lambda name: "/usr/bin/systemctl"
        try:
            good, text = do("install")
            ok("linux: unit записан", good and os.path.exists(unit_path()))
            body = open(unit_path(), encoding="utf-8").read()
            ok("linux: Restart=on-failure", "Restart=on-failure" in body)
            ok("linux: ExecStart на tg_watch", "tg_watch.py --quiet" in body)
            ok("linux: enable --now", "enable" in calls[-1] and "--now" in calls[-1])
            good, text = do("status")
            ok("linux: status видит unit", good and "enabled" in text)
            good, text = do("remove")
            ok("linux: unit снят", good and not os.path.exists(unit_path()))
            good, text = do("status")
            ok("linux: без unit статус честный", not good)
        finally:
            shutil.which = keep_which
        shutil.which = lambda name: None
        try:
            good, text = do("install")
            ok("linux без systemctl: честный отказ", not good and "systemctl" in text)
        finally:
            shutil.which = keep_which

        SYSTEM = "haiku"
        good, text = do("install")
        ok("чужая система: отказ с подсказкой", not good and "руками" in text)
    finally:
        RUNNER, SYSTEM = keep[0], keep[1]
        if keep[2] is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = keep[2]
        if keep[3] is None:
            os.environ.pop("VIORA_TG_HOME", None)
        else:
            os.environ["VIORA_TG_HOME"] = keep[3]
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [name for name, good in checks if not good]
    for name, good in checks:
        print("%s %s" % ("PASS" if good else "FAIL", name))
    print("Итого: %d проверок автозапуска, провалилось %d" % (len(checks), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
