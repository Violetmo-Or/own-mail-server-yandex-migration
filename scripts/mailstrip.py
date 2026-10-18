"""Урезание письма до «текст + список вложений»: файлы вложений заменяются короткой пометкой.

strip(raw) -> (new_raw, removed)   removed = [(имя, тип, размер_байт), ...]; если вложений нет, new_raw is raw.
Заголовки (From, To, Date, Message-ID и т.д.) и текстовые части сохраняются как есть.
"""
import email
import email.header
import email.policy
from email.message import Message

KEEP_TYPES = {"text/plain", "text/html", "text/calendar"}
MIN_SAVE = 20 * 1024  # если выигрыш меньше 20 КБ, письмо не переписываем


def _is_attachment(part):
    if part.is_multipart():
        return False
    ctype = part.get_content_type()
    fname = part.get_filename()
    disp = str(part.get("Content-Disposition") or "").lower()
    if fname or disp.startswith("attachment"):
        return True
    return ctype not in KEEP_TYPES


def strip(raw):
    try:
        return _strip(raw)
    except Exception:
        return raw, []


def _strip(raw):
    msg = email.message_from_bytes(raw)
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("multipart/signed", "multipart/encrypted", "application/pkcs7-mime", "application/x-pkcs7-mime"):
            return raw, []   # подписанные и зашифрованные письма (на любой глубине) не трогаем: подпись сломается
    if not msg.is_multipart():
        # одиночное письмо-вложение (например, PDF без текста): трогаем только если оно большое
        if len(raw) > 200 * 1024 and msg.get_content_maintype() not in ("text",):
            payload = msg.get_payload(decode=True) or b""
            removed = [(msg.get_filename() or "без имени", msg.get_content_type(), len(payload))]
            note = _note(removed)
            for h in ("Content-Type", "Content-Transfer-Encoding", "Content-Disposition"):
                if h in msg:
                    del msg[h]
            msg["Content-Type"] = 'text/plain; charset="utf-8"'
            msg["Content-Transfer-Encoding"] = "8bit"
            msg["X-Mail-Stripped"] = f"1 вложение, {len(payload)} байт"
            msg.set_payload(note, charset="utf-8")
            return msg.as_bytes(), removed
        return raw, []
    removed = []
    for part in msg.walk():
        if _is_attachment(part):
            payload = part.get_payload(decode=True) or b""
            removed.append((part.get_filename() or "без имени", part.get_content_type(), len(payload)))
    if not removed or sum(r[2] for r in removed) < MIN_SAVE:
        return raw, []
    for part in msg.walk():
        if _is_attachment(part):
            payload = part.get_payload(decode=True) or b""
            name = part.get_filename() or "без имени"
            ctype = part.get_content_type()
            for h in list(part.keys()):
                if h.lower() in ("content-type", "content-transfer-encoding", "content-disposition", "content-id"):
                    del part[h]
            part["Content-Type"] = 'text/plain; charset="utf-8"'
            part["Content-Transfer-Encoding"] = "8bit"
            part["X-Original-Filename"] = email.header.Header(name, "utf-8").encode()
            part["X-Original-Content-Type"] = ctype
            part["X-Original-Size"] = str(len(payload))
            part.set_payload(_note([(name, ctype, len(payload))]), charset="utf-8")
    msg["X-Mail-Stripped"] = f"{len(removed)} вложений, {sum(r[2] for r in removed)} байт"
    return msg.as_bytes(), removed


def _note(items):
    lines = []
    for name, ctype, size in items:
        kb = size / 1024
        s = f"{kb/1024:.1f} МБ" if kb >= 1024 else f"{kb:.0f} КБ"
        lines.append(f"[Вложение: {name} ({ctype}, {s}). Файл не хранится в архиве, оригинал на старом сервере.]")
    return "\r\n".join(lines) + "\r\n"
