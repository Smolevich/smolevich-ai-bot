from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from typing import Any

from agent.text import split_telegram_text


def tg_request(token: str, method: str, data: dict[str, Any] | None = None, log: logging.Logger | None = None) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urllib.request.Request(url, json.dumps(data).encode() if data else None, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        if log:
            log.error(f"TG API error ({method}): HTTP {e.code} {e.reason}. Body: {body[:500]}")
        return {"ok": False, "error_code": e.code, "description": body or str(e)}
    except Exception as e:
        if log:
            log.error(f"TG API error ({method}): {e}")
        return {"ok": False}


def tg_send_text(token: str, chat_id: int, text: str, parse_mode: str | None = None, reply_markup: dict[str, Any] | None = None, log: logging.Logger | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = tg_request(token, "sendMessage", payload, log=log)
    if res.get("ok") or not parse_mode:
        return res
    desc = (res.get("description") or "").lower()
    if "can't parse entities" in desc or "can't find end of" in desc:
        payload.pop("parse_mode", None)
        return tg_request(token, "sendMessage", payload, log=log)
    return res


def tg_send_long_text(token: str, chat_id: int, text: str, parse_mode: str | None = None, log: logging.Logger | None = None) -> dict[str, Any]:
    chunks = split_telegram_text(text)
    last: dict[str, Any] = {"ok": True}
    all_ok = True
    for chunk in chunks:
        last = tg_send_text(token, chat_id, chunk, parse_mode=parse_mode, log=log)
        if not last.get("ok"):
            all_ok = False
    out = dict(last)
    out["ok"] = all_ok
    return out


def multipart_body(fields: dict[str, Any], files: list[dict[str, Any]]) -> tuple[str, bytes]:
    boundary = "----WebKitFormBoundary" + uuid.uuid4().hex
    out = bytearray()
    for name, value in fields.items():
        out.extend(f"--{boundary}\r\n".encode())
        out.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.extend(str(value).encode())
        out.extend(b"\r\n")
    for file_info in files:
        out.extend(f"--{boundary}\r\n".encode())
        out.extend(f'Content-Disposition: form-data; name="{file_info["name"]}"; filename="{file_info["filename"]}"\r\n'.encode())
        out.extend(f'Content-Type: {file_info.get("content_type", "application/octet-stream")}\r\n\r\n'.encode())
        out.extend(file_info["content"])
        out.extend(b"\r\n")
    out.extend(f"--{boundary}--\r\n".encode())
    return boundary, bytes(out)


def tg_send_document_bytes(token: str, chat_id: int, filename: str, content: bytes, caption: str | None = None, log: logging.Logger | None = None) -> dict[str, Any]:
    try:
        url = f"https://api.telegram.org/bot{token}/sendDocument"
        fields: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
        files = [{"name": "document", "filename": filename, "content": content, "content_type": "application/octet-stream"}]
        boundary, body = multipart_body(fields, files)
        req = urllib.request.Request(url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        if log:
            log.error(f"tg_send_document_bytes error: {e}")
        return {"ok": False, "description": str(e)}


def tg_get_file_bytes(token: str, file_id: str, log: logging.Logger | None = None) -> tuple[str, bytes]:
    meta = tg_request(token, "getFile", {"file_id": file_id}, log=log)
    if not meta.get("ok"):
        raise RuntimeError(f"getFile failed: {meta}")
    file_path = meta["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return file_path, resp.read()

