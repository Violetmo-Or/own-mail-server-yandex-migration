#!/usr/bin/env python3
"""Постоянная досинхронизация новых писем со старого сервера на свой, пока MX ещё указывает на старый.

Запуск: sync.py                    — все ящики из mailboxes.txt
        sync.py user@example.ru    — один ящик
Идёт по UID: помнит, до какого письма дошла в каждой папке (БД sync.db рядом со скриптами).
Первый проход по папке берёт письма за последние SYNC_INITIAL_DAYS дней (по умолчанию 7) и сверяет с тем, что уже
лежит на новом сервере (по Message-ID, а без него по дате+отправителю+теме), чтобы не было дублей после основного переноса.
Письмо, которое не удалось добрать (старый сервер не отдал или новый не принял), пробуется ещё несколько запусков,
потом записывается в unavailable.tsv. Спам и Корзина пропускаются (INCLUDE_JUNK=yes в config.env включает их).
Изменения флагов и удаления на старом сервере не синхронизируются. Старый сервер только читается.
Тревога: после трёх неудачных запусков подряд вызывается команда ALARM_CMD из config.env
(получает текст аргументом; например, скрипт отправки в Telegram). Не задана — пишется только в журнал.
"""
import datetime as dt
import importlib.util
import os
import re
import sqlite3
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mailstrip  # noqa: E402
spec = importlib.util.spec_from_file_location("mm", os.path.join(HERE, "migrate.py"))
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)

DB = mm._cfg("SYNC_DB", os.path.join(HERE, "..", "sync.db"))
LIST = os.path.join(HERE, "..", "mailboxes.txt")
INITIAL_DAYS = int(mm._cfg("SYNC_INITIAL_DAYS", "7"))   # если папку видим впервые: сколько дней назад смотреть (должно перекрывать время после migrate.py)
FAILS_BEFORE_ALARM = 3


def db():
    c = sqlite3.connect(DB, timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS folder_state (mailbox TEXT, folder TEXT, uidvalidity TEXT, last_uid INTEGER,"
              " PRIMARY KEY (mailbox, folder))")
    c.execute("CREATE TABLE IF NOT EXISTS src_map (mailbox TEXT, mid TEXT, folder TEXT, src_uid INTEGER, PRIMARY KEY (mailbox, mid))")
    c.execute("CREATE TABLE IF NOT EXISTS retry (mailbox TEXT, folder TEXT, uid INTEGER, tries INTEGER, PRIMARY KEY (mailbox, folder, uid))")
    c.execute("CREATE TABLE IF NOT EXISTS run_state (mailbox TEXT PRIMARY KEY, fails INTEGER, last_ok TEXT, last_copied INTEGER)")
    return c


def alarm(text):
    print("ТРЕВОГА:", text, flush=True)
    cmd = mm._cfg("ALARM_CMD", "")
    if cmd:
        try:
            subprocess.run(shlex.split(cmd) + [text], timeout=30)
        except Exception:
            pass


MAX_TRIES = 5   # столько запусков подряд пробуем добрать письмо, которое старый сервер не отдал или новый не принял


class Unavailable(Exception):
    pass


def sync_mailbox(mb, c):
    login = mb.split("@")[0].upper().replace("-", "_")
    env = mm.load_env()
    src_pw, _ = mm.pick_password(env, [f"MAIL_{login}_PASS", f"MAIL_{login}_IMAP_PASS"])
    dst_pw, _ = mm.pick_password(env, [f"MAIL_NEW_{login}_PASS"])
    src = mm.Conn(mm.SRC_HOST, mm.SRC_PORT, mb, src_pw)
    dst = mm.Conn(mm.DST_HOST, mm.DST_PORT, mb, dst_pw)
    include_junk = mm._cfg("INCLUDE_JUNK", "no") == "yes"
    strip = mm._cfg("STRIP_ATTACHMENTS", "no") == "yes"

    folders, sdelim = mm.list_folders(src)
    _, ddelim = mm.list_folders(dst)
    sdelim, ddelim = sdelim or "|", ddelim or "/"

    def dname(n):
        parts = n.split(sdelim)
        parts = [p.replace(ddelim, "_") if ddelim != sdelim else p for p in parts]
        return ddelim.join(parts)

    since = (dt.date.today() - dt.timedelta(days=INITIAL_DAYS)).strftime("%d-%b-%Y")
    copied = 0
    for folder, flags in folders:
        if mm.is_junk(folder, flags, include_junk):
            continue   # спам и корзина не переносятся (включить: INCLUDE_JUNK=yes в config.env)
        if mm.imap_utf7_decode(folder) in mm.WORKFLOW_FOLDERS:
            dst.call("create", mm.q(dname(folder)))
            dst.call("subscribe", mm.q(dname(folder)))
            continue
        typ, _ = src.select(folder, readonly=True)
        if typ != "OK":
            continue
        uv = (src.m.response("UIDVALIDITY")[1] or [b"?"])[0]
        uv = uv.decode() if isinstance(uv, bytes) else str(uv)
        row = c.execute("SELECT uidvalidity, last_uid FROM folder_state WHERE mailbox=? AND folder=?", (mb, folder)).fetchone()
        dfolder = dname(folder)
        dst.call("create", mm.q(dfolder))
        dst.call("subscribe", mm.q(dfolder))

        all_uids = [int(x) for x in src.call("uid", "search", None, "ALL")[1][0].split()]
        top = max(all_uids) if all_uids else 0
        if row and row[0] == uv:
            todo = [u for u in all_uids if u > row[1]]
        else:
            todo = [int(x) for x in src.call("uid", "search", None, "SINCE", since)[1][0].split()]

        known = None      # ключи писем, уже лежащих на новом сервере; загружаем один раз и только если есть что проверять
        hold = None       # самый ранний UID, который не удалось добрать: последний обработанный UID не уходит выше него
        skipped = 0

        def fail(uid, why):
            """Письмо не добрали: пробуем ещё MAX_TRIES запусков, потом записываем в unavailable.tsv и идём дальше."""
            nonlocal hold, skipped
            r = c.execute("SELECT tries FROM retry WHERE mailbox=? AND folder=? AND uid=?", (mb, folder, uid)).fetchone()
            tries = (r[0] if r else 0) + 1
            skipped += 1
            if tries >= MAX_TRIES:
                mm.note_unavailable(mb, folder, str(uid).encode(), src, kind=f"ОТКАЗ после {tries} попыток: {why}")
                c.execute("DELETE FROM retry WHERE mailbox=? AND folder=? AND uid=?", (mb, folder, uid))
            else:
                c.execute("INSERT OR REPLACE INTO retry VALUES (?,?,?,?)", (mb, folder, uid, tries))
                hold = uid if hold is None else min(hold, uid)

        for uid in todo:
            try:
                # дешёвая проверка по заголовкам: письмо уже есть на новом сервере — тело не качаем
                typ, h = src.call("uid", "fetch", str(uid), "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM SUBJECT)])")
                hdr = next((x for x in (h or []) if isinstance(x, tuple)), None) if typ == "OK" else None
                key = mm.msg_key(hdr[1]) if hdr else None
                if key:
                    if known is None:
                        known = mm.dst_message_ids(dst, dfolder)
                    if key in known:
                        c.execute("DELETE FROM retry WHERE mailbox=? AND folder=? AND uid=?", (mb, folder, uid))
                        continue
                tup = None
                for attempt in range(2):
                    typ, resp = src.call("uid", "fetch", str(uid), "(FLAGS INTERNALDATE BODY.PEEK[])")
                    tup = next((x for x in (resp or []) if isinstance(x, tuple)), None) if typ == "OK" else None
                    if tup is not None:
                        break
                    time.sleep(1.5)
                if tup is None:
                    raise Unavailable("старый сервер не отдал содержимое (у Яндекса бывает NO [UNAVAILABLE])")
                meta = tup[0].decode(errors="replace") + " " + " ".join(
                    x.decode(errors="replace") for x in resp if isinstance(x, bytes))
                body = tup[1]
                key = mm.msg_key(body[:20000]) or key
                fl = re.search(r"FLAGS \(([^)]*)\)", meta)
                flags_s = " ".join(f for f in (fl.group(1).split() if fl else []) if f not in ("\\Recent", "\\Deleted"))
                idate = re.search(r'INTERNALDATE "([^"]+)"', meta)
                idt = mm.imaplib.Internaldate2tuple(('INTERNALDATE "%s"' % idate.group(1)).encode()) if idate else None
                removed = []
                if strip:
                    body, removed = mailstrip.strip(body)
                flags_out = (flags_s + " $Synced" + (" $Stripped" if removed else "")).strip()
                typ = mm.append_once(dst, dfolder, f"({flags_out})", idt, body, key)
                if typ != "OK":
                    raise RuntimeError(f"новый сервер не принял письмо (ответ {typ}; квота, размер?)")
                copied += 1
                if known is not None and key:
                    known.add(key)
                c.execute("DELETE FROM retry WHERE mailbox=? AND folder=? AND uid=?", (mb, folder, uid))
                if key and folder == "INBOX":
                    c.execute("INSERT OR REPLACE INTO src_map VALUES (?,?,?,?)", (mb, key.decode(errors="replace"), folder, uid))
            except mm.NetDown:
                raise   # сервер лёг: это сбой запуска, попытки письма не тратим
            except (Unavailable, RuntimeError, mm.imaplib.IMAP4.error, ValueError, UnicodeError) as e:
                # одно «ядовитое» письмо не должно останавливать остальные
                fail(uid, f"{type(e).__name__}: {str(e)[:80]}")
        if skipped:
            print(f"{mb} [{folder}]: не добрано писем {skipped} (пробуем ещё до {MAX_TRIES} запусков, потом запись в {mm.UNAVAIL_FILE})", flush=True)
        new_last = (hold - 1) if hold is not None else top
        c.execute("INSERT OR REPLACE INTO folder_state VALUES (?,?,?,?)", (mb, folder, uv, new_last))
        c.commit()
    return copied


def main():
    boxes = sys.argv[1:] or [l.strip() for l in open(LIST, encoding="utf-8") if l.strip() and not l.startswith("#")]
    c = db()
    for mb in boxes:
        t0 = time.time()
        try:
            n = sync_mailbox(mb, c)
            c.execute("INSERT OR REPLACE INTO run_state VALUES (?,?,?,?)", (mb, 0, dt.datetime.now().isoformat(timespec="seconds"), n))
            c.commit()
            print(f"{mb}: новых писем скопировано {n}, {time.time() - t0:.0f} с", flush=True)
        except Exception as e:
            r = c.execute("SELECT fails FROM run_state WHERE mailbox=?", (mb,)).fetchone()
            fails = (r[0] if r else 0) + 1
            c.execute("INSERT OR REPLACE INTO run_state (mailbox, fails, last_ok, last_copied) VALUES (?,?,"
                      "(SELECT last_ok FROM run_state WHERE mailbox=?),0)", (mb, fails, mb))
            c.commit()
            print(f"{mb}: ОШИБКА ({fails} подряд): {type(e).__name__} {str(e)[:150]}", flush=True)
            if fails == FAILS_BEFORE_ALARM:
                alarm(f"⚠️ Синхронизация почты {mb} (старый → новый сервер) не проходит {fails} запуска подряд: "
                      f"{type(e).__name__} {str(e)[:100]}. Новые письма на новый сервер не копируются.")


if __name__ == "__main__":
    main()
