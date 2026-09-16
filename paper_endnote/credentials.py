from __future__ import annotations

import json
from typing import Any


SERVICE = "paper-endnote"
_memory: dict[tuple[str, str], str] | None = None


class CredentialError(RuntimeError):
    pass


def use_memory_backend() -> dict[tuple[str, str], str]:
    global _memory
    _memory = {}
    return _memory


def _key(institution_id: str) -> str:
    identifier = (institution_id or "default").strip() or "default"
    return f"institution/{identifier}"


def _get_password(service: str, key: str) -> str | None:
    if _memory is not None:
        return _memory.get((service, key))
    try:
        import keyring
    except Exception as exc:
        raise CredentialError(f"无法使用 Windows 凭据管理器：{exc}") from exc
    try:
        return keyring.get_password(service, key)
    except Exception as exc:
        raise CredentialError(f"读取机构凭据失败：{exc}") from exc


def _set_password(service: str, key: str, value: str) -> None:
    if _memory is not None:
        _memory[(service, key)] = value
        return
    try:
        import keyring
    except Exception as exc:
        raise CredentialError(f"无法使用 Windows 凭据管理器：{exc}") from exc
    try:
        keyring.set_password(service, key, value)
    except Exception as exc:
        raise CredentialError(f"保存机构凭据失败：{exc}") from exc


def _delete_password(service: str, key: str) -> None:
    if _memory is not None:
        _memory.pop((service, key), None)
        return
    try:
        import keyring
        keyring.delete_password(service, key)
    except Exception:
        return


def load_credentials(institution_id: str) -> dict[str, str] | None:
    raw = _get_password(SERVICE, _key(institution_id))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        return None
    return {"username": username, "password": password}


def save_credentials(institution_id: str, username: str, password: str) -> None:
    user = username.strip()
    if not user:
        raise CredentialError("请填写机构登录用户名")
    if not password:
        raise CredentialError("请填写机构登录密码")
    _set_password(SERVICE, _key(institution_id), json.dumps({"username": user, "password": password}))


def clear_credentials(institution_id: str) -> None:
    _delete_password(SERVICE, _key(institution_id))


def credential_status(institution_id: str) -> dict[str, Any]:
    data = load_credentials(institution_id)
    return {
        "username": data["username"] if data else "",
        "password_saved": bool(data),
    }
