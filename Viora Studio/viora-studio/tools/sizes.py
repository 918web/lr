#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Честные размеры файлов в таблицах документации.

Цифры в таблицах тиров писались руками и врали почти втрое.
Слабая модель по этим цифрам выбирает тир, то есть врущая таблица
сразу ломает бюджет контекста. Теперь их считает скрипт.

С версии 5.6.0 скрипт стережёт ещё и штуки: сколько файлов в reference,
сколько эталонов в evals/cases.json и сколько питон-скриптов в tools.
Раньше README обещал 19 файлов reference и 14 скриптов при реальных 25 и 20:
агент читал эти цифры и делал вывод, что половина скилла ему приснилась.

    python3 tools/sizes.py            печатает правду
    python3 tools/sizes.py --write    вписывает правду в таблицы и в счётчики
    python3 tools/sizes.py --check    валит сборку, если цифры разошлись с файлами
"""

import json
import os
import re
import shutil
import sys
import tempfile

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
SKILL = os.path.dirname(HERE)
ROOT = os.path.dirname(SKILL)

DOCS = (
    (os.path.join("viora-studio", "ROUTER.md"), SKILL),
    ("AGENTS.md", ROOT),
    (os.path.join("viora-studio", "SKILL.md"), SKILL),
)

SHOW_FILES = (
    "QUICKCARD.txt",
    "SKILL.md",
    "ROUTER.md",
    "PROMPT-CORE.txt",
    "PROMPT-FULL.txt",
)

# Счётчики штук. Ключ, регекс, три русские формы слова, якорь в строке.
# Якорь нужен там, где слово слишком общее: "19 файлов" бывает о чём угодно.
# Длинная форма идёт первой, иначе регекс матчит "файл" внутри "файлов"
# и оставляет хвост: получается "25 файловов". Лукахед держит то же самое.
COUNT_RULES = (
    ("reference", re.compile(r"(\d+) (файлов|файла|файл)(?!\w)"),
     ("файл", "файла", "файлов"), "reference/"),
    ("evals", re.compile(r"(\d+) (эталонов|эталона|эталон)(?!\w)"),
     ("эталон", "эталона", "эталонов"), ""),
    ("tools", re.compile(r"(\d+) (питон-скриптов|питон-скрипта|питон-скрипт)(?!\w)"),
     ("питон-скрипт", "питон-скрипта", "питон-скриптов"), ""),
)

COUNT_DOCS = (
    ("README.md", SKILL),
    ("AGENTS.md", SKILL),
    (os.path.join("viora-studio", "SKILL.md"), SKILL),
)

CELL_KB = re.compile(r"^\d+ КБ$")
CELL_TWO_KB = re.compile(r"^\d+ / \d+ КБ$")
CELL_RANGE_KB = re.compile(r"^плюс \d+-\d+ КБ$")
CELL_CHARS = re.compile(r"^\d+$")
IN_TICKS = re.compile(r"`([^`]+)`")


def size_bytes(path):
    return os.path.getsize(path)


def kb(total):
    return max(1, int(round(total / 1024.0)))


def chars(path):
    with open(path, encoding="utf-8") as handle:
        return len(handle.read())


def round100(value):
    return int(round(value / 100.0)) * 100


def row_paths(row, base):
    found = []
    for item in IN_TICKS.findall(row):
        item = item.strip()
        if not item.endswith((".md", ".txt")):
            continue
        full = os.path.join(base, item)
        if os.path.exists(full) and full not in found:
            found.append(full)
    return found


def reference_range(base):
    folder = os.path.join(base, "reference")
    if not os.path.isdir(folder):
        return None
    sizes = [size_bytes(os.path.join(folder, name))
             for name in sorted(os.listdir(folder)) if name.endswith(".md")]
    if not sizes:
        return None
    return kb(min(sizes)), kb(max(sizes))


def new_cell(cell, paths, base):
    if CELL_RANGE_KB.match(cell):
        pair = reference_range(base)
        if not pair:
            return None
        return "плюс %d-%d КБ" % pair
    if CELL_TWO_KB.match(cell):
        if len(paths) != 2:
            return None
        return "%d / %d КБ" % (kb(size_bytes(paths[0])), kb(size_bytes(paths[1])))
    if CELL_KB.match(cell):
        if not paths:
            return None
        return "%d КБ" % kb(sum(size_bytes(item) for item in paths))
    if CELL_CHARS.match(cell):
        if not paths:
            return None
        return str(round100(sum(chars(item) for item in paths)))
    return None


def plural(value, forms):
    """Один файл, два файла, пять файлов. Без этого скрипт пишет по-роботски."""
    tail = abs(value) % 100
    if 11 <= tail <= 14:
        return forms[2]
    tail %= 10
    if tail == 1:
        return forms[0]
    if 2 <= tail <= 4:
        return forms[1]
    return forms[2]


def counts(base=None):
    """Сколько всего в скилле на самом деле. Считаем по диску, а не по памяти."""
    base = base or SKILL
    out = {}
    folder = os.path.join(base, "reference")
    if os.path.isdir(folder):
        out["reference"] = len([n for n in os.listdir(folder) if n.endswith(".md")])
    folder = os.path.join(base, "tools")
    if os.path.isdir(folder):
        out["tools"] = len([n for n in os.listdir(folder) if n.endswith(".py")])
    cases = os.path.join(base, "evals", "cases.json")
    if os.path.exists(cases):
        try:
            with open(cases, encoding="utf-8") as handle:
                data = json.load(handle)
        except ValueError:
            data = None
        rows = data.get("cases") if isinstance(data, dict) else data
        if isinstance(rows, list):
            out["evals"] = len(rows)
    return out


def count_changes(doc, base, root=None):
    """Строки документации, где штуки разошлись с тем, что лежит в папках."""
    path = os.path.join(root or ROOT, doc)
    if not os.path.exists(path):
        return path, [], []
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    real = counts(base)
    changes = []
    for index, line in enumerate(lines):
        fresh = line
        for key, pattern, forms, anchor in COUNT_RULES:
            if key not in real or (anchor and anchor not in fresh):
                continue
            value = real[key]
            fresh = pattern.sub("%d %s" % (value, plural(value, forms)), fresh)
        if fresh == line:
            continue
        changes.append({
            "line": index + 1,
            "old": line.strip(),
            "new": fresh.strip(),
            "row": fresh,
            "index": index,
        })
    return path, lines, changes


def is_separator(line):
    return set(line) <= set("|-: ")


def scan(doc, base):
    path = os.path.join(ROOT, doc)
    if not os.path.exists(path):
        return path, [], []
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    changes = []
    for index, line in enumerate(lines):
        if not line.startswith("|") or is_separator(line):
            continue
        cells = line.split("|")
        if len(cells) < 4:
            continue
        tail = cells[-2].strip()
        fresh = new_cell(tail, row_paths(line, base), base)
        if fresh is None or fresh == tail:
            continue
        cells[-2] = " %s " % fresh
        changes.append({
            "line": index + 1,
            "old": tail,
            "new": fresh,
            "row": "|".join(cells),
            "index": index,
        })
    return path, lines, changes


def show():
    print("| Файл | Байт | КБ | Знаков |")
    print("|---|---|---|---|")
    for name in SHOW_FILES:
        path = os.path.join(SKILL, name)
        if not os.path.exists(path):
            continue
        print("| %s | %d | %d | %d |"
              % (name, size_bytes(path), kb(size_bytes(path)), chars(path)))
    pair = reference_range(SKILL)
    if pair:
        print("")
        print("Один файл reference весит от %d до %d КБ." % pair)
    real = counts(SKILL)
    if real:
        print("")
        print("Штуки по диску: reference %d, эталонов %d, питон-скриптов %d."
              % (real.get("reference", 0), real.get("evals", 0), real.get("tools", 0)))
    print("Считаем и байты, и знаки: кириллица ест два байта на знак.")
    return 0


def run(write):
    total = 0
    for doc, base in DOCS:
        path, lines, changes = scan(doc, base)
        if not changes:
            continue
        total += len(changes)
        for item in changes:
            print("%s:%d  %s -> %s" % (doc, item["line"], item["old"], item["new"]))
            lines[item["index"]] = item["row"]
        if write:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
    for doc, base in COUNT_DOCS:
        path, lines, changes = count_changes(doc, base)
        if not changes:
            continue
        total += len(changes)
        for item in changes:
            print("%s:%d  %s -> %s" % (doc, item["line"], item["old"], item["new"]))
            lines[item["index"]] = item["row"]
        if write:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines))
    if write:
        print("Поправлено строк: %d" % total)
        return 0
    if total:
        print("Цифры в документации разошлись с файлами: %d строк." % total)
        print("Лечится одной командой: python3 tools/sizes.py --write")
        return 1
    print("Цифры в документации совпадают с файлами.")
    return 0


def selftest():
    checks = []
    checks.append(("килобайты округляются", kb(9454) == 9 and kb(17492) == 17))
    checks.append(("маленький файл не ноль", kb(10) == 1))
    checks.append(("сотни округляются", round100(6749) == 6700 and round100(6751) == 6800))

    quick = os.path.join(SKILL, "QUICKCARD.txt")
    skill = os.path.join(SKILL, "SKILL.md")
    checks.append(("ячейка в КБ считается",
                   new_cell("3 КБ", [quick], SKILL) == "%d КБ" % kb(size_bytes(quick))))
    checks.append(("две цифры в одной ячейке",
                   new_cell("13 / 90 КБ", [quick, skill], SKILL)
                   == "%d / %d КБ" % (kb(size_bytes(quick)), kb(size_bytes(skill)))))
    checks.append(("коридор reference считается",
                   (new_cell("плюс 3-7 КБ", [], SKILL) or "").startswith("плюс ")))
    checks.append(("знаки считаются",
                   new_cell("0", [quick], SKILL) == str(round100(chars(quick)))))
    checks.append(("текстовая ячейка не трогается",
                   new_cell("по одному файлу", [quick], SKILL) is None))
    checks.append(("цифра без файла не трогается",
                   new_cell("6000", [], SKILL) is None))
    checks.append(("чужие кавычки не путают",
                   row_paths("| бери `--strict` и `SKILL.md` | 1 |", SKILL) == [skill]))
    checks.append(("разделитель таблицы пропускается",
                   is_separator("|---|---|") and not is_separator("| a | 1 |")))
    checks.append(("сумма двух файлов в одной ячейке",
                   new_cell("1 КБ", [quick, skill], SKILL)
                   == "%d КБ" % kb(size_bytes(quick) + size_bytes(skill))))
    _, _, changes = scan(os.path.join("viora-studio", "ROUTER.md"), SKILL)
    checks.append(("роутер читается без ошибок", isinstance(changes, list)))

    forms = ("файл", "файла", "файлов")
    checks.append(("один файл", plural(1, forms) == "файл"))
    checks.append(("два файла", plural(2, forms) == "файла" and plural(24, forms) == "файла"))
    checks.append(("пять файлов", plural(5, forms) == "файлов" and plural(25, forms) == "файлов"))
    checks.append(("одиннадцать файлов",
                   plural(11, forms) == "файлов" and plural(21, forms) == "файл"))

    real = counts(SKILL)
    checks.append(("штуки считаются по диску",
                   real.get("reference", 0) > 0 and real.get("tools", 0) > 0
                   and real.get("evals", 0) > 0))

    tmp = tempfile.mkdtemp(prefix="viora-sizes-")
    try:
        fake = os.path.join(tmp, "viora-studio")
        os.makedirs(os.path.join(fake, "reference"))
        os.makedirs(os.path.join(fake, "tools"))
        os.makedirs(os.path.join(fake, "evals"))
        for name in ("a.md", "b.md", "c.md"):
            open(os.path.join(fake, "reference", name), "w", encoding="utf-8").write("x")
        for name in ("one.py", "two.py"):
            open(os.path.join(fake, "tools", name), "w", encoding="utf-8").write("x")
        with open(os.path.join(fake, "evals", "cases.json"), "w", encoding="utf-8") as handle:
            json.dump({"cases": [1, 2, 3, 4]}, handle)
        doc = os.path.join(tmp, "README.md")
        with open(doc, "w", encoding="utf-8") as handle:
            handle.write("    reference/    тир 2: 19 файлов, одна тема на файл\n"
                         "    evals/        cases.json на 24 эталона и две фикстуры\n"
                         "    tools/        14 питон-скриптов плюс check_all.sh\n"
                         "| `SKILL.md` | 24 КБ |\n"
                         "В папке автора лежит 19 файлов с черновиками\n")
        _, _, fresh = count_changes("README.md", fake, root=tmp)
        got = {item["line"]: item["new"] for item in fresh}
        checks.append(("счётчик reference чинится",
                       got.get(1, "").startswith("reference/    тир 2: 3 файла,")))
        checks.append(("счётчик эталонов чинится", "4 эталона" in got.get(2, "")))
        checks.append(("счётчик скриптов чинится", "2 питон-скрипта" in got.get(3, "")))
        checks.append(("форма слова не двоится",
                       "файловов" not in " ".join(got.values())
                       and "эталонова" not in " ".join(got.values())))
        checks.append(("размер в КБ счётчики не трогают", 4 not in got))
        checks.append(("чужие файлы без якоря не трогаются", 5 not in got))
        _, _, again = count_changes("README.md", fake, root=tmp)
        checks.append(("без --write файл не меняется", len(again) == len(fresh)))
        checks.append(("нет документа: пустой список",
                       count_changes("NETU.md", fake, root=tmp)[2] == []))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = 0
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
        if not ok:
            failed += 1
    print("Итого: %d проверок размеров, провалилось %d" % (len(checks), failed))
    return 1 if failed else 0


def main(argv):
    if not argv or argv[0] in ("--show", "show"):
        return show()
    flag = argv[0]
    if flag == "--write":
        return run(True)
    if flag == "--check":
        return run(False)
    if flag == "--selftest":
        return selftest()
    if flag in ("--version", "version"):
        print("sizes.py %s" % VERSION)
        return 0
    print("Не знаю флаг: %s" % flag)
    print("Есть: --show, --write, --check, --selftest, --version")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
