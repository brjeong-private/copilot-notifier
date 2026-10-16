#!/usr/bin/env python3
"""Copilot completion notifications and setup tools."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import datetime as dt
import getpass
import hashlib
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from email.utils import parseaddr
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCAL = ROOT / ".local"
KAKAO_CONFIG = LOCAL / "kakao.json"
KAKAO_REDIRECT = "http://localhost:53682/"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
ACS_API_VERSION = "2023-03-31"
SECRET_PATTERN = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|password|secret|connection[_ -]?string|"
    r"authorization\s*:|bearer\s+|gh[pousr]_|github_pat_|sk-[a-z0-9]|sig=|-----BEGIN)"
)


def atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class FileLock:
    """Exclusive cross-process lock backed by an OS file lock."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.stream = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        if os.name == "nt":
            import msvcrt

            if not self.stream.read(1):
                self.stream.write(b"\0")
                self.stream.flush()
            self.stream.seek(0)
            try:
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                self.stream.close()
                self.stream = None
                raise RuntimeError("Notifier is already handling this operation.") from exc
        else:
            import fcntl

            try:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                self.stream.close()
                self.stream = None
                raise RuntimeError("Notifier is already handling this operation.") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.stream is None:
            return
        if os.name == "nt":
            import msvcrt

            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()
        self.stream = None


def protect_user_data(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Kakao credentials can only be protected using Windows DPAPI.")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(DataBlob), ctypes.c_wchar_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    protect.restype = ctypes.wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    source_buffer = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    source = DataBlob(len(data), source_buffer)
    result = DataBlob()
    if not protect(ctypes.byref(source), "Copilot Notifier", None, None, None, 0, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        local_free(result.pbData)


def unprotect_user_data(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Kakao credentials can only be decrypted using Windows DPAPI.")

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(DataBlob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    unprotect.restype = ctypes.wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    source_buffer = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    source = DataBlob(len(data), source_buffer)
    result = DataBlob()
    if not unprotect(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        local_free(result.pbData)


def save_kakao_config(path: pathlib.Path, config: dict[str, Any]) -> None:
    payload = json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    atomic_json(path, {"version": 1, "dpapi": protect_user_data(payload).hex()})


def load_kakao_config(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError("Kakao is not configured. Run `python scripts\\notifier.py connect-kakao` first.")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict):
            raise ValueError("invalid credential envelope")
        if envelope.get("version") != 1:
            raise ValueError("unsupported credential format")
        config = json.loads(unprotect_user_data(bytes.fromhex(envelope["dpapi"])))
        if not isinstance(config, dict):
            raise ValueError("invalid credential data")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Kakao credentials are invalid. Run connect-kakao again.") from exc
    for key in ("clientId", "clientSecret", "accessToken", "refreshToken", "expiresAt", "messageUrl"):
        if not config.get(key):
            raise RuntimeError(f"Missing encrypted Kakao configuration field: {key}. Run connect-kakao again.")
    validate_https_url(config["messageUrl"], "Kakao message link")
    parse_timestamp(config["expiresAt"])
    return config


def validate_https_url(value: str, label: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an absolute HTTPS URL without user information.")
    return value


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 15,
) -> Any:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read JSON configuration: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}.")
    return value


def write_receipt(path: pathlib.Path, **fields: Any) -> None:
    atomic_json(path, {**fields, "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat()})


def get_operation_id(event_key: str) -> str:
    return str(uuid.UUID(bytes=hashlib.sha256(event_key.encode("utf-8")).digest()[:16]))


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def truncate(text: str, maximum: int) -> str:
    if utf16_length(text) <= maximum:
        return text
    if maximum < 3:
        return "." * maximum
    result = []
    size = 0
    for char in text:
        width = utf16_length(char)
        if size + width > maximum - 3:
            break
        result.append(char)
        size += width
    return "".join(result) + "..."


def safe_lines(summary: str, limit: int = 2) -> list[str]:
    lines: list[str] = []
    in_code = False
    for line in summary.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            in_code = not in_code
            continue
        if in_code:
            continue
        text = re.sub(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+)", "", line).strip()
        if not text or SECRET_PATTERN.search(text):
            continue
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        lines.append(truncate(text, 240))
        if len(lines) == limit:
            break
    if not lines:
        raise ValueError("No safe nonempty summary lines are available.")
    return lines


def get_completion_candidate(transcript: pathlib.Path) -> dict[str, Any] | None:
    mode = None
    candidate = None
    task = None
    started_at = None
    saw_sdk_event = False
    try:
        stream = transcript.open(encoding="utf-8-sig")
    except OSError as exc:
        raise RuntimeError(f"Could not read Copilot transcript: {transcript}") from exc
    with stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Copilot transcript contains invalid JSON.") from exc
            if not isinstance(event, dict):
                continue
            data = event.get("data")
            if not event.get("type") or not isinstance(data, dict):
                continue
            if event.get("agentId") or data.get("parentAgentTaskId"):
                continue
            kind = event["type"]
            if kind == "session.start":
                saw_sdk_event = True
                mode = candidate = task = started_at = None
            elif kind == "session.resume":
                candidate = task = started_at = None
            elif kind == "session.mode_changed":
                saw_sdk_event = True
                mode = data.get("newMode")
                if mode != "autopilot":
                    candidate = None
            elif kind == "user.message":
                source = data.get("source") or ""
                if not isinstance(source, str):
                    source = ""
                if not data.get("isAutopilotContinuation") and not re.match(
                    r"^(system|agent-|skill-|notification|autopilot)", source
                ):
                    candidate = None
                    task = data.get("content")
                    started_at = event.get("timestamp")
                    if data.get("agentMode"):
                        saw_sdk_event = True
                        mode = data["agentMode"]
            elif kind == "session.task_complete":
                candidate = None
                if (
                    mode == "autopilot"
                    and data.get("success") is not False
                    and data.get("outcome") in (None, "", "completed")
                    and event.get("id")
                    and isinstance(data.get("summary"), str)
                    and data["summary"].strip()
                ):
                    candidate = {
                        "eventId": event["id"],
                        "summary": data["summary"],
                        "task": task,
                        "timestamp": event.get("timestamp"),
                        "startedAt": started_at,
                    }
    if not saw_sdk_event:
        raise RuntimeError("Unsupported transcript: SDK mode and task-completion events are required.")
    return candidate


def get_project_label(working_directory: str) -> str:
    path = pathlib.Path(working_directory).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("The hook working directory is not a directory.")
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        path = pathlib.Path(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    label = path.name
    if not label:
        raise ValueError("Cannot determine a project name from the hook working directory.")
    return label


def parse_timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$", value):
        raise ValueError("Task timestamps must be ISO 8601 values with a timezone.")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Task timestamps must include a timezone.")
    return parsed


def get_kakao_content(project: str, task: str, summary: str, started_at: str, completed_at: str) -> str:
    start, end = parse_timestamp(started_at), parse_timestamp(completed_at)
    elapsed = int((end - start).total_seconds())
    if elapsed < 0:
        raise ValueError("Task completion precedes its start.")
    duration = f"{elapsed // 3600:02}:{(elapsed % 3600) // 60:02}:{elapsed % 60:02}"
    project_label = truncate(safe_lines(project, 1)[0], 20)
    task_label = safe_lines(task, 1)[0]
    result = " ".join(safe_lines(summary))
    fmt = "%Y-%m-%d %H:%M:%S %z"
    metadata = (
        f"프로젝트: {project_label}\n시작: {start.astimezone().strftime(fmt)}"
        f"\n완료: {end.astimezone().strftime(fmt)}\n소요: {duration}"
    )
    prefix, separator = f"{metadata}\n요청 요약: ", "\n결과 요약: "
    budget = 200 - utf16_length(prefix) - utf16_length(separator)
    task_limit = budget // 2
    result_limit = budget - task_limit
    if task_limit < 4:
        raise ValueError("Task metadata exceeds the Kakao text template limit.")
    return f"{prefix}{truncate(task_label, task_limit)}{separator}{truncate(result, result_limit)}"


def resolve_kakao_token(path: pathlib.Path) -> tuple[dict[str, Any], str]:
    with FileLock(path.with_suffix(path.suffix + ".lock")):
        config = load_kakao_config(path)
        expires_at = parse_timestamp(config["expiresAt"])
        if expires_at <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1):
            form = urllib.parse.urlencode(
                {
                    "grant_type": "refresh_token",
                    "client_id": config["clientId"],
                    "client_secret": config["clientSecret"],
                    "refresh_token": config["refreshToken"],
                }
            ).encode()
            try:
                response = request_json(
                    KAKAO_TOKEN_URL, method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, data=form,
                )
            except (OSError, urllib.error.URLError, ValueError) as exc:
                raise RuntimeError("Kakao token refresh failed. Check connectivity or run connect-kakao again.") from exc
            save_token_response(config, response, path)
        return config, config["accessToken"]


def save_token_response(config: dict[str, Any], response: dict[str, Any], path: pathlib.Path) -> None:
    try:
        expires_in = int(response["expires_in"])
        access_token = response["access_token"]
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Kakao returned an invalid token response. Run connect-kakao again.") from exc
    if not access_token or expires_in <= 0:
        raise RuntimeError("Kakao returned an invalid token response. Run connect-kakao again.")
    config["accessToken"] = access_token
    config["expiresAt"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires_in)).isoformat()
    if response.get("refresh_token"):
        config["refreshToken"] = response["refresh_token"]
    if not config.get("refreshToken"):
        raise RuntimeError("Kakao did not issue a refresh token. Run connect-kakao again.")
    save_kakao_config(path, config)


def send_kakao(
    config_path: pathlib.Path, text: str, operation_id: str, receipt_path: pathlib.Path
) -> None:
    if utf16_length(text) > 200:
        raise ValueError("Kakao message exceeds the 200-character display limit.")
    if receipt_path.exists():
        receipt = read_json(receipt_path)
        if receipt.get("status") == "Succeeded":
            return
        raise RuntimeError("A prior Kakao send failed or is uncertain. Inspect the receipt and chat before retrying.")
    config, token = resolve_kakao_token(config_path)
    template = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": config["messageUrl"], "mobile_web_url": config["messageUrl"]},
        "button_title": "프로젝트 보기",
    }
    write_receipt(receipt_path, operationId=operation_id, provider="kakao", status="Pending")
    form = urllib.parse.urlencode({"template_object": json.dumps(template, ensure_ascii=False)}).encode()
    try:
        response = request_json(
            KAKAO_SEND_URL, method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            }, data=form,
        )
    except urllib.error.HTTPError as exc:
        error_code = None
        try:
            error = json.loads(exc.read())
            if isinstance(error, dict) and type(error.get("code")) is int:
                error_code = error["code"]
        except (OSError, ValueError):
            pass
        if exc.code in (400, 401, 403, 404):
            write_receipt(receipt_path, operationId=operation_id, provider="kakao", status="Failed")
            detail = f"HTTP {exc.code}, Kakao code {error_code}"
            if error_code == -402:
                raise RuntimeError(
                    f"Kakao rejected the message ({detail}): talk_message consent is missing. "
                    "Run connect-kakao again and accept the message-sending consent."
                ) from exc
            raise RuntimeError(f"Kakao rejected the message ({detail}). Check app settings and permissions.") from exc
        raise RuntimeError(
            f"Kakao submission failed (HTTP {exc.code}); delivery is uncertain. "
            "Check your chat before deleting the pending receipt."
        ) from exc
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(
            "Kakao submission failed; delivery is uncertain. Check your chat before deleting the pending receipt."
        ) from exc
    status = "Succeeded" if response.get("result_code") == 0 else "Failed"
    write_receipt(receipt_path, operationId=operation_id, provider="kakao", status=status)
    if status != "Succeeded":
        raise RuntimeError("Kakao did not confirm a successful send. Inspect your app permissions and quota.")


def email_summary(project: str, task: str, summary: str) -> dict[str, str]:
    project_label = truncate(safe_lines(project, 1)[0], 80)
    task_label = truncate(safe_lines(task, 1)[0], 120)
    result = " ".join(safe_lines(summary))
    return {
        "subject": f"[Copilot] {project_label} | {task_label}",
        "body": f"Project: {project_label} | Task: {task_label}\nResult: {truncate(result, 240)}",
    }


def validate_email_config(path: pathlib.Path) -> dict[str, str]:
    config = read_json(path)
    for name in ("subscriptionId", "endpoint", "sender", "recipient"):
        if not isinstance(config.get(name), str) or not config[name].strip():
            raise ValueError(f"Missing notifier configuration field: {name}")
    endpoint = urllib.parse.urlsplit(config["endpoint"])
    if (
        endpoint.scheme != "https" or not endpoint.hostname
        or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)?\.communication\.azure\.com", endpoint.hostname)
        or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment
        or endpoint.username or endpoint.password or endpoint.port not in (None, 443)
    ):
        raise ValueError("The notifier endpoint must be an HTTPS Azure Communication Services endpoint.")
    for name in ("sender", "recipient"):
        address = config[name]
        if parseaddr(address)[1] != address or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            raise ValueError(f"Invalid {name} address.")
    return config


def azure_token(subscription_id: str) -> str:
    try:
        result = subprocess.run(
            ["az", "account", "get-access-token", "--subscription", subscription_id,
             "--resource", "https://communication.azure.com", "--query", "accessToken",
             "--output", "tsv", "--only-show-errors"],
            check=True, capture_output=True, text=True, timeout=20, shell=os.name == "nt",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Azure authentication failed. Run az login and check the configured subscription.") from exc
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("Azure authentication returned no access token.")
    return token


def send_email(
    config: dict[str, str], subject: str, body: str, operation_id: str, receipt_path: pathlib.Path
) -> None:
    token = azure_token(config["subscriptionId"])
    endpoint = config["endpoint"].rstrip("/")
    operation_url = f"{endpoint}/emails/operations/{operation_id}?api-version={ACS_API_VERSION}"
    if not receipt_path.exists():
        payload = {
            "senderAddress": config["sender"],
            "recipients": {"to": [{"address": config["recipient"]}]},
            "content": {"subject": subject, "plainText": body},
            "userEngagementTrackingDisabled": True,
        }
        write_receipt(receipt_path, operationId=operation_id, status="Pending")
        try:
            request_json(
                f"{endpoint}/emails:send?api-version={ACS_API_VERSION}", method="POST",
                headers={"Authorization": f"Bearer {token}", "Operation-Id": operation_id,
                         "Content-Type": "application/json; charset=utf-8"},
                data=json.dumps(payload).encode("utf-8"),
            )
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise RuntimeError("ACS submission failed; status is uncertain. Inspect the operation before retrying.") from exc
    deadline = time.monotonic() + 25
    while True:
        try:
            result = request_json(operation_url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise RuntimeError("ACS operation lookup failed. The pending receipt is retained; email delivery is not confirmed.") from exc
        status = result.get("status")
        if status == "Succeeded":
            write_receipt(receipt_path, operationId=operation_id, status="Succeeded")
            return
        if status in ("Failed", "Canceled"):
            write_receipt(receipt_path, operationId=operation_id, status=status)
            raise RuntimeError("ACS reported that the send operation did not succeed.")
        if time.monotonic() >= deadline:
            raise RuntimeError("ACS send is still pending. The pending receipt is retained; email delivery is not confirmed.")
        time.sleep(2)


def connect_kakao(message_url: str, config_path: pathlib.Path) -> None:
    if os.name != "nt":
        raise RuntimeError("Kakao setup requires Windows DPAPI for encrypted token storage.")
    validate_https_url(message_url, "Message URL")
    client_id = getpass.getpass("Kakao REST API key (hidden): ").strip()
    client_secret = getpass.getpass("Kakao client secret (hidden): ").strip()
    if not client_id or not client_secret:
        raise ValueError("Both app credentials are required.")
    state = os.urandom(32).hex()
    query = urllib.parse.urlencode(
        {"response_type": "code", "scope": "talk_message", "client_id": client_id,
         "redirect_uri": KAKAO_REDIRECT, "state": state}
    )
    authorization_url = f"https://kauth.kakao.com/oauth/authorize?{query}"
    callback: dict[str, str] = {}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/" or params.get("state", [""])[0] != state:
                self.send_error(400, "Invalid OAuth state.")
                return
            if params.get("error") or not params.get("code"):
                self.send_error(400, "Kakao authorization was denied.")
                callback["error"] = "Kakao authorization was denied or returned no code."
            else:
                callback["code"] = params["code"][0]
                body = b"Login received. Return to the terminal to check setup."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, *_: Any) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 53682), CallbackHandler)
    server.timeout = 0.5
    try:
        with FileLock(config_path.with_suffix(config_path.suffix + ".lock")):
            print("Complete Kakao login and talk_message consent in your browser (3-minute timeout).")
            webbrowser.open(authorization_url)
            deadline = time.monotonic() + 180
            while not callback and time.monotonic() < deadline:
                server.handle_request()
            if callback.get("error"):
                raise RuntimeError(callback["error"])
            if not callback.get("code"):
                raise RuntimeError("Kakao login timed out. Run connect-kakao again.")
            form = urllib.parse.urlencode(
                {"grant_type": "authorization_code", "client_id": client_id,
                 "client_secret": client_secret, "redirect_uri": KAKAO_REDIRECT,
                 "code": callback["code"]}
            ).encode()
            try:
                response = request_json(
                    KAKAO_TOKEN_URL, method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, data=form,
                )
            except (OSError, urllib.error.URLError, ValueError) as exc:
                raise RuntimeError("Kakao token exchange failed. Check app credentials, redirect URI, and consent settings.") from exc
            config: dict[str, Any] = {
                "clientId": client_id, "clientSecret": client_secret, "messageUrl": message_url,
            }
            save_token_response(config, response, config_path)
    finally:
        server.server_close()
    print("Kakao credentials saved encrypted for this Windows user. Run send-test-kakao next.")


def install_hook() -> None:
    home = pathlib.Path(os.environ.get("COPILOT_HOME") or pathlib.Path.home() / ".copilot")
    hooks_dir = home / "hooks"
    target = hooks_dir / "tools-copilot-notifier.json"
    python = pathlib.Path(sys.executable).resolve()
    script = pathlib.Path(__file__).resolve()
    command = "& " + subprocess.list2cmdline([str(python), str(script), "notify-completion"])
    if target.exists():
        existing = read_json(target)
        hooks = existing.get("hooks", {}).get("Stop", [])
        ours = False
        if hooks and isinstance(hooks[0], dict):
            old = hooks[0].get("powershell", "")
            ours = str(script).lower() in old.lower() or "Notify-Completion.ps1" in old
        if not ours:
            raise RuntimeError("A different hook already uses this filename. It has not been overwritten.")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(target, {
        "version": 1,
        "hooks": {"Stop": [{"type": "command", "powershell": command, "timeoutSec": 90}]},
    })
    print("User-level Stop hook installed. Restart Copilot CLI and reload VS Code to load it.")


def notify_completion(config_path: pathlib.Path) -> None:
    try:
        hook = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Hook input is not valid JSON.") from exc
    if not isinstance(hook, dict):
        raise RuntimeError("Hook input must be a JSON object.")
    if any(hook.get(key) for key in ("agent_id", "agentId", "parent_agent_id", "parentAgentId")):
        return
    if hook.get("hook_event_name") and hook["hook_event_name"] != "Stop":
        return
    session_id = hook.get("session_id") or hook.get("sessionId")
    transcript = hook.get("transcript_path") or hook.get("transcriptPath")
    if not session_id or not transcript:
        raise ValueError("Completion hook is missing session or transcript metadata.")
    candidate = get_completion_candidate(pathlib.Path(transcript))
    if not candidate:
        return
    if not hook.get("cwd"):
        raise ValueError("Completion hook is missing the working directory.")
    project = get_project_label(hook["cwd"])
    if not isinstance(candidate.get("task"), str) or not candidate["task"].strip():
        raise ValueError("The accepted completion has no human task title. No message was submitted.")
    text = get_kakao_content(
        project, candidate["task"], candidate["summary"],
        candidate.get("startedAt"), candidate.get("timestamp"),
    )
    load_kakao_config(config_path)
    state_dir = config_path.parent / "kakao-receipts"
    operation_id = get_operation_id(f"{session_id}:{candidate['eventId']}")
    receipt_path = state_dir / f"{operation_id}.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(state_dir / f"{operation_id}.lock"):
            if receipt_path.exists():
                receipt = read_json(receipt_path)
                if receipt.get("status") == "Succeeded":
                    return
                raise RuntimeError("A prior Kakao send failed or is uncertain. Inspect the receipt and chat before retrying.")
            send_kakao(config_path, text, operation_id, receipt_path)
    except RuntimeError as exc:
        if "already handling" in str(exc):
            raise RuntimeError("Another notifier is handling this completion. No duplicate message was submitted.") from exc
        raise


def deploy_email(args: argparse.Namespace) -> None:
    try:
        uuid.UUID(args.subscription_id)
    except ValueError as exc:
        raise ValueError("Subscription ID must be a valid UUID.") from exc
    if not re.fullmatch(r"[A-Za-z0-9._()-]{1,90}", args.resource_group):
        raise ValueError("Resource group must be 1–90 letters, numbers, dots, underscores, parentheses or hyphens.")
    if not re.fullmatch(r"[A-Za-z0-9]+", args.location):
        raise ValueError("Azure location must contain only letters and numbers.")
    if parseaddr(args.recipient)[1] != args.recipient or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", args.recipient):
        raise ValueError("Invalid recipient email address.")
    suffix = args.subscription_id.replace("-", "")[:10]
    email_name, acs_name = f"copilot-tools-email-{suffix}", f"copilot-tools-acs-{suffix}"
    template = ROOT / "infra" / "email.json"

    def run_az(command: list[str], error: str) -> str:
        try:
            result = subprocess.run(
                ["az", *command, "--only-show-errors"], check=True, capture_output=True,
                text=True, timeout=1800, shell=os.name == "nt",
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(error) from exc
        return result.stdout

    run_az(
        ["group", "create", "--subscription", args.subscription_id, "--name", args.resource_group,
         "--location", args.location, "--output", "none"],
        "Could not create the dedicated resource group.",
    )
    output = run_az(
        ["deployment", "group", "create", "--subscription", args.subscription_id,
         "--resource-group", args.resource_group, "--name", "copilot-notifier-email",
         "--mode", "Incremental", "--template-file", str(template),
         "--parameters", f"emailServiceName={email_name}",
         f"communicationServiceName={acs_name}", "--query", "properties.outputs", "--output", "json"],
        "Email infrastructure deployment failed.",
    )
    try:
        deployment = json.loads(output)
        acs_id = deployment["communicationServiceId"]["value"]
        domain_id = deployment["domainId"]["value"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError("Azure deployment returned invalid resource outputs.") from exc
    acs = json.loads(run_az(
        ["resource", "show", "--subscription", args.subscription_id, "--ids", acs_id,
         "--api-version", ACS_API_VERSION, "--output", "json"],
        "Could not read the deployed communication resource.",
    ))
    domain = json.loads(run_az(
        ["resource", "show", "--subscription", args.subscription_id, "--ids", domain_id,
         "--api-version", ACS_API_VERSION, "--output", "json"],
        "Could not read the managed email domain.",
    ))
    if (
        acs.get("properties", {}).get("provisioningState") != "Succeeded"
        or domain.get("properties", {}).get("provisioningState") != "Succeeded"
        or not domain.get("properties", {}).get("mailFromSenderDomain")
    ):
        raise RuntimeError("The deployed email resources are not ready.")
    config_path = LOCAL / "config.json"
    atomic_json(config_path, {
        "subscriptionId": args.subscription_id,
        "resourceGroup": args.resource_group,
        "endpoint": f"https://{acs['properties']['hostName']}",
        "sender": f"DoNotReply@{domain['properties']['mailFromSenderDomain']}",
        "recipient": args.recipient,
    })
    validate_email_config(config_path)
    print("Email resources are ready. Local configuration saved; no access keys were retrieved or stored.")


def test_kakao(config_path: pathlib.Path) -> None:
    load_kakao_config(config_path)
    receipts = config_path.parent / "kakao-receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    operation_id = str(uuid.uuid4())
    now = dt.datetime.now(dt.timezone.utc)
    text = get_kakao_content(
        "tools", "카카오톡 알림 연결 확인", "작업 완료 알림을 나와의 채팅으로 보냅니다.",
        (now - dt.timedelta(seconds=5)).isoformat(), now.isoformat(),
    )
    send_kakao(config_path, text, operation_id, receipts / f"{operation_id}.json")
    print("Kakao confirmed the test send. Check your My Chat conversation.")


def test_email(config_path: pathlib.Path) -> None:
    config = validate_email_config(config_path)
    receipts = LOCAL / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    operation_id = str(uuid.uuid4())
    content = email_summary(
        "tools", "copilot-notifier readiness check",
        "Azure email is ready. Only accepted Autopilot completions trigger notifications.",
    )
    send_email(
        config, content["subject"], content["body"], operation_id,
        receipts / f"{operation_id}.json",
    )
    print("ACS confirmed a successful test send operation. Inbox delivery is not independently confirmed.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("test", help="Run offline notifier tests.")
    connect = sub.add_parser("connect-kakao", help="Connect Kakao and securely save tokens.")
    connect.add_argument("--message-url", required=True)
    connect.add_argument("--config-path", type=pathlib.Path, default=KAKAO_CONFIG)
    send_kakao_parser = sub.add_parser("send-test-kakao", help="Send a Kakao test message.")
    send_kakao_parser.add_argument("--config-path", type=pathlib.Path, default=KAKAO_CONFIG)
    sub.add_parser("install-hook", help="Install the user-level Copilot Stop hook.")
    deploy = sub.add_parser("deploy-email", help="Deploy Azure Communication Services Email resources.")
    deploy.add_argument("--subscription-id", required=True)
    deploy.add_argument("--recipient", required=True)
    deploy.add_argument("--resource-group", default="rg-copilot-tools")
    deploy.add_argument("--location", default="westus3")
    sub.add_parser("send-test-email", help="Send a manual ACS email test.")
    notify = sub.add_parser("notify-completion", help=argparse.SUPPRESS)
    notify.add_argument("--config-path", type=pathlib.Path, default=KAKAO_CONFIG)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "test":
            from test_notifier import run_tests

            run_tests()
        elif args.command == "connect-kakao":
            connect_kakao(args.message_url, args.config_path)
        elif args.command == "send-test-kakao":
            test_kakao(args.config_path)
        elif args.command == "install-hook":
            install_hook()
        elif args.command == "deploy-email":
            deploy_email(args)
        elif args.command == "send-test-email":
            test_email(LOCAL / "config.json")
        elif args.command == "notify-completion":
            notify_completion(args.config_path)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Copilot notifier: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
