#!/usr/bin/env python3
"""Перенос почты с чужого IMAP-сервера (Яндекс, Mail.ru, Gmail...) на свой сервер.

Запуск:  python3 migrate.py user@example.ru [--days N] [--dry-run]   (по умолчанию переносятся ВСЕ письма)
Настройки — в файле config.env (путь можно задать переменной MAIL_ENV_FILE), образец config.example.env.
Пароль старого ящика: MAIL_<ЛОГИН>_PASS, пароль нового ящика: MAIL_NEW_<ЛОГИН>_PASS
(<ЛОГИН> — часть адреса до @ заглавными, дефис заменён на _).
Имена папок сохраняются (разделитель уровней приводится к разделителю нового сервера).
Повторный запуск безопасен: письма, которые уже есть в папке (по Message-ID или, если его нет, по дате+отправителю+теме), не копируются второй раз.
Старый сервер только читается, ничего в нём не меняется. При обрыве соединения скрипт переподключается.
"""
import argparse
import os
import base64
import datetime as dt
import imaplib
import re
import socket
import ssl
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.environ.get("MAIL_ENV_FILE", os.path.join(HERE, "..", "config.env"))


def parse_env_line(line):
    """Строка config.env -> (ключ, значение) или None. Понимает пробелы вокруг «=», кавычки и хвостовой комментарий « # ...»."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    k, v = line.split("=", 1)
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1]
    else:
        v = re.split(r"\s+#", v, 1)[0].strip()
    return k.strip(), v


def _cfg(name, default):
    """Значение из окружения или config.env (читается один раз при запуске)."""
    if name in os.environ:
        return os.environ[name]
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            kv = parse_env_line(line)
            if kv and kv[0] == name:
                return kv[1]
    except OSError:
        pass
    return default


SRC_HOST, SRC_PORT = _cfg("SRC_HOST", "imap.yandex.ru"), int(_cfg("SRC_PORT", "993"))
DST_HOST, DST_PORT = _cfg("DST_HOST", "localhost"), int(_cfg("DST_PORT", "993"))
SKIP_FLAGS = {b"\\Noselect", b"\\NonExistent"}
SKIP_FOLDERS = {"Spam", "Trash", "Junk", "Корзина", "Спам", "Удалённые", "Deleted Items"}   # спам и корзина по умолчанию НЕ переносятся (ключ --include-junk)
class NetDown(RuntimeError):
    """Сервер недоступен (долгий обрыв связи): это сбой запуска, а не вина конкретного письма."""


NET_ERRORS = (imaplib.IMAP4.abort, ssl.SSLError, ConnectionError, EOFError, socket.timeout, OSError)


def load_env():
    env = {}
    for line in open(ENV_FILE, encoding="utf-8"):
        kv = parse_env_line(line)
        if kv:
            env[kv[0]] = kv[1]
    return env


def pick_password(env, names):
    for n in names:
        if env.get(n):
            return env[n], n
    sys.exit("Не найден пароль в config.env, искали: " + ", ".join(names))


class Conn:
    """IMAP-соединение, которое само переподключается при обрыве."""

    def __init__(self, host, port, user, pw):
        self.host, self.port, self.user, self.pw = host, port, user, pw
        self.m = None
        self.folder = None
        self.connect()

    def connect(self):
        last = None
        for attempt in range(5):
            try:
                m = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context(), timeout=120)
                m.login(self.user, self.pw)
                self.m = m
                if self.folder:
                    m.select(q(self.folder), readonly=True)
                return
            except imaplib.IMAP4.error as e:
                if b"AUTHENTICATIONFAILED" in str(e).encode() or "AUTHENTICATIONFAILED" in str(e):
                    raise
                last = e
            except NET_ERRORS as e:
                last = e
            time.sleep(5 * (attempt + 1))
        raise NetDown(f"не удалось подключиться к {self.host}: {last}")

    def call(self, name, *args, **kw):
        for attempt in range(4):
            try:
                return getattr(self.m, name)(*args, **kw)
            except NET_ERRORS:
                time.sleep(3 * (attempt + 1))
                self.connect()
        raise NetDown(f"{name}: обрыв соединения не удалось пережить")

    def select(self, folder, readonly=True):
        self.folder = folder if readonly else None
        return self.call("select", q(folder), readonly=readonly)


LIST_RE = re.compile(rb'\((?P<flags>.*?)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')


def list_folders(c):
    typ, data = c.call("list")
    out = []
    delim = None
    for raw in data:
        if not raw:
            continue
        mt = LIST_RE.match(raw)
        if not mt:
            continue
        flags = set(mt.group("flags").split())
        name = mt.group("name").decode()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"')
        d = mt.group("delim").decode().strip('"')
        delim = delim or (d if d != "NIL" else None)
        if flags & SKIP_FLAGS:
            continue
        out.append((name, flags))
    return out, delim


def q(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


MID_RE = re.compile(rb"(?im)^message-id:[ \t]*(<[^>\r\n]+>)")


import hashlib

HDR_FIELDS = "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)])"


def msg_key(hdr):
    """Ключ письма для защиты от дублей: Message-ID, а если его нет, хэш из даты, отправителя и темы."""
    hdr = re.split(rb"\r?\n\r?\n", hdr, 1)[0]   # только заголовки: строки «Subject:» в теле пересылок не в счёт
    mm = MID_RE.search(hdr)
    if mm:
        return mm.group(1)
    parts = []
    for name in (b"date", b"from", b"subject"):
        m2 = re.search(rb"(?im)^" + name + rb":[ \t]*(.*)$", hdr)
        parts.append(m2.group(1).strip() if m2 else b"")
    if not any(parts):
        return None
    return b"nokey:" + hashlib.sha1(b"|".join(parts)).hexdigest().encode()


def is_junk(folder, flags, include=False):
    """Спам и корзина (по флагам сервера или по имени папки, имена в modified UTF-7 декодируются)."""
    return (not include) and (imap_utf7_decode(folder) in SKIP_FOLDERS or b"\\Junk" in flags or b"\\Trash" in flags)


def dst_message_ids(c, folder):
    typ, _ = c.select(folder, readonly=True)
    if typ != "OK":
        return set()
    typ, data = c.call("search", None, "ALL")
    ids = data[0].split()
    seen = set()
    for i in range(0, len(ids), 500):
        chunk = b",".join(ids[i:i + 500]).decode()
        typ, resp = c.call("fetch", chunk, HDR_FIELDS)
        for part in resp:
            if isinstance(part, tuple):
                k = msg_key(part[1])
                if k:
                    seen.add(k)
    return seen



# Папки, где письма не копируем, а только создаём пустые (нужны, если на них завязана ваша автоматика).
# Список через запятую в config.env: WORKFLOW_FOLDERS=папка1,папка2
WORKFLOW_FOLDERS = {x.strip() for x in _cfg("WORKFLOW_FOLDERS", "").split(",") if x.strip()}


def imap_utf7_decode(name):
    def repl(m):
        t = m.group(1)
        if t == "":
            return "&"
        raw = base64.b64decode(t.replace(",", "/") + "=" * (-len(t) % 4))
        return raw.decode("utf-16-be")
    return re.sub(r"&([^-]*)-", repl, name)

UNAVAIL_FILE = _cfg("UNAVAIL_FILE", os.path.join(HERE, "..", "unavailable.tsv"))


def append_once(dst, dfolder, flags, idt, body, key):
    """APPEND без слепого повтора: после обрыва сначала проверяем, не легло ли письмо уже (иначе будет дубль)."""
    try:
        return dst.m.append(q(dfolder), flags, idt, body)[0]
    except NET_ERRORS:
        dst.connect()
        if key and key in dst_message_ids(dst, dfolder):
            return "OK"
        return dst.m.append(q(dfolder), flags, idt, body)[0]


def note_unavailable(mailbox, folder, seq, src, kind="НЕДОСТУПНО"):
    """Письмо, содержимое которого старый сервер не отдаёт по IMAP: запоминаем (папка, UID, дата, размер) для повторной попытки."""
    try:
        typ, r = src.call("uid", "fetch", seq, "(UID INTERNALDATE RFC822.SIZE)")
        info = " ".join(x.decode(errors="replace") if isinstance(x, bytes) else str(x) for x in (r or []))
        with open(UNAVAIL_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')}\t{kind}\t{mailbox}\t{folder}\t{seq.decode() if isinstance(seq, bytes) else seq}\t{info}\n")
    except Exception:
        pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mailbox")
    ap.add_argument("--days", type=int, default=36500, help="за сколько последних дней переносить (по умолчанию все письма)")
    ap.add_argument("--include-junk", action="store_true", help="переносить также Спам и Корзину (по умолчанию пропускаются)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--folders-only", action="store_true", help="только создать папки, письма не копировать")
    ap.add_argument("--max-mb", type=float, default=0, help="не копировать письма крупнее N МБ (записать в список)")
    ap.add_argument("--newest-first", action="store_true", help="сначала самые новые письма")
    ap.add_argument("--shard", default="", help="K/N: параллельные потоки, каждый берёт свою долю ПИСЕМ (по номеру UID); итоговая сверка при этом не выполняется")
    a = ap.parse_args()

    k_sh, n_sh = ([int(x) for x in a.shard.split("/")] if a.shard else [0, 1])
    login = a.mailbox.split("@")[0].upper().replace("-", "_")
    env = load_env()
    src_pw, src_name = pick_password(env, [f"MAIL_{login}_PASS", f"MAIL_{login}_IMAP_PASS"])
    dst_pw, dst_name = pick_password(env, [f"MAIL_NEW_{login}_PASS"])
    since = (dt.date.today() - dt.timedelta(days=a.days)).strftime("%d-%b-%Y")
    print(f"Ящик {a.mailbox}: источник {SRC_HOST} ({src_name}), назначение {DST_HOST} ({dst_name}), с {since}, "
          f"{'ПРОБНЫЙ ПРОГОН, ничего не копируется' if a.dry_run else 'копирование'}", flush=True)

    src = Conn(SRC_HOST, SRC_PORT, a.mailbox, src_pw)
    dst = Conn(DST_HOST, DST_PORT, a.mailbox, dst_pw)

    src_folders, src_delim = list_folders(src)
    _, dst_delim = list_folders(dst)
    src_delim = src_delim or "|"
    dst_delim = dst_delim or "."
    print(f"Разделитель уровней: старый «{src_delim}», новый сервер «{dst_delim}»", flush=True)

    def dst_name_of(name):
        parts = name.split(src_delim)
        parts = [p.replace(dst_delim, "_") if dst_delim != src_delim else p for p in parts]
        return dst_delim.join(parts)

    total = {"найдено": 0, "скопировано": 0, "уже было": 0, "ошибок": 0}
    report = []
    for folder, flags in src_folders:
        if is_junk(folder, flags, a.include_junk):
            print(f"  [{folder}] пропущена (спам/корзина)", flush=True)
            continue
        dfolder = dst_name_of(folder)
        if imap_utf7_decode(folder) in WORKFLOW_FOLDERS:
            if not a.dry_run:
                dst.call("create", q(dfolder))
                dst.call("subscribe", q(dfolder))
            print(f"  [{folder}] рабочая папка автоматики: создана, письма не копируются", flush=True)
            continue
        try:
            typ, _ = src.select(folder, readonly=True)
            if typ != "OK":
                print(f"  [{folder}] не открывается, пропуск", flush=True)
                continue
            typ, data = src.call("uid", "search", None, *(("ALL",) if a.days >= 36500 else ("SINCE", since)))
            uids = data[0].split()
            found = len(uids)
            copied = dup = err = unavail = big_skipped = 0
            if not a.dry_run:
                dst.call("create", q(dfolder))
                dst.call("subscribe", q(dfolder))
            if found and not a.dry_run and not a.folders_only:
                have = dst_message_ids(dst, dfolder)
                src.select(folder, readonly=True)
                order = list(reversed(uids)) if a.newest_first else uids
                for n, uid in enumerate(order, 1):
                    if n_sh > 1 and int(uid) % n_sh != k_sh:
                        continue
                    try:
                        hdr = None
                        for attempt in range(2):
                            ht, hdr = src.call("uid", "fetch", uid, "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)])")
                            if ht == "OK" and hdr and isinstance(hdr[0], tuple):
                                break
                            hdr = None
                            time.sleep(1)
                        if hdr is None:
                            # Старый сервер отдаёт только служебные данные письма (содержимое NO [UNAVAILABLE]): пишем в список и идём дальше
                            note_unavailable(a.mailbox, folder, uid, src)
                            unavail += 1
                            continue
                        hkey = msg_key(hdr[0][1])
                        if hkey and hkey in have:
                            dup += 1
                            continue
                        if a.max_mb:
                            sm = re.search(r"RFC822\.SIZE (\d+)", hdr[0][0].decode(errors="replace"))
                            if sm and int(sm.group(1)) > a.max_mb * 1024 * 1024:
                                note_unavailable(a.mailbox, folder, uid, src, kind="КРУПНОЕ(>%gМБ)" % a.max_mb)
                                big_skipped += 1
                                continue
                        tup = None
                        for attempt in range(3):
                            typ, resp = src.call("uid", "fetch", uid, "(FLAGS INTERNALDATE BODY.PEEK[])")
                            tup = next((x for x in resp if isinstance(x, tuple)), None) if typ == "OK" else None
                            if tup is not None:
                                break
                            time.sleep(1.5 * (attempt + 1))
                        if tup is None:
                            note_unavailable(a.mailbox, folder, uid, src)
                            unavail += 1
                            continue
                        meta = tup[0].decode(errors="replace") + " " + " ".join(
                            x.decode(errors="replace") for x in resp if isinstance(x, bytes))
                        body = tup[1]
                        mid = msg_key(body[:20000]) or hkey
                        if mid and mid in have:
                            dup += 1
                            continue
                        fl = re.search(r"FLAGS \(([^)]*)\)", meta)
                        flags_s = " ".join(f for f in (fl.group(1).split() if fl else []) if f not in ("\\Recent", "\\Deleted"))
                        idate = re.search(r'INTERNALDATE "([^"]+)"', meta)
                        idt = imaplib.Internaldate2tuple(('INTERNALDATE "%s"' % idate.group(1)).encode()) if idate else None
                        try:
                            import mailstrip
                            if _cfg("STRIP_ATTACHMENTS", "no") != "yes":
                                raise ImportError  # хранить письма целиком
                            body, _rm = mailstrip.strip(body)
                            if _rm:
                                flags_s = (flags_s + " $Stripped").strip()
                        except Exception:
                            pass
                        typ = append_once(dst, dfolder, f"({flags_s})" if flags_s else None, idt, body, mid)
                        if typ == "OK":
                            copied += 1
                            if mid:
                                have.add(mid)
                        else:
                            err += 1
                    except Exception as e:
                        err += 1
                        print(f"    ошибка письма {uid.decode()} в [{folder}]: {type(e).__name__} {str(e)[:80]}", flush=True)
                    if n % 100 == 0:
                        print(f"    [{folder}] {n}/{found}", flush=True)
            print(f"  [{folder}] найдено {found}, скопировано {copied}, уже было {dup}, недоступно на старом сервере {unavail}, крупных пропущено {big_skipped}, ошибок {err}", flush=True)
            total["найдено"] += found
            total["скопировано"] += copied
            total["уже было"] += dup
            total["ошибок"] += err
            report.append((folder, dfolder, found))
        except Exception as e:
            total["ошибок"] += 1
            print(f"  [{folder}] ПАПКА НЕ ОБРАБОТАНА: {type(e).__name__} {str(e)[:100]}", flush=True)

    bad = 0
    if n_sh > 1:
        print("\nИтоговая сверка при параллельных потоках не выполняется: повторите migrate.py без --shard (он ничего не продублирует) и посмотрите итог. Два шарда могут продублировать письма с одним Message-ID.", flush=True)
    if not a.dry_run and n_sh == 1 and not a.folders_only:
        print("\nПроверка: писем за период в новом ящике против старого", flush=True)
        for folder, dfolder, found in report:
            if not found:
                continue
            typ, _ = dst.select(dfolder, readonly=True)
            n = len(dst.call("search", None, *(("ALL",) if a.days >= 36500 else ("SINCE", since)))[1][0].split()) if typ == "OK" else 0
            mark = "OK" if n >= found else "НЕ СХОДИТСЯ (проверьте unavailable.tsv, --max-mb и письма с одинаковым Message-ID, они копируются один раз)"
            if n < found:
                bad += 1
            print(f"  [{folder}] старый {found}, новый {n}: {mark}", flush=True)
    print(f"\nИТОГО {a.mailbox}: {total}." + (f" Папок с расхождением: {bad}" if (n_sh == 1 and not a.folders_only) else " Итоговая сверка не выполнялась."), flush=True)
    sys.exit(1 if total["ошибок"] or bad else 0)


if __name__ == "__main__":
    main()
