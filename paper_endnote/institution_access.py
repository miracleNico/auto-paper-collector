from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping
from urllib.parse import urlparse

from .redaction import redact_diagnostic_text, redact_url


class SessionState(StrEnum):
    """Ephemeral authentication state for one institution/publisher pair."""

    WAITING = "waiting"
    READY = "ready"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ManualInstitutionAction:
    reason: str
    publisher: str
    paper_id: str | None = None
    page_url: str | None = None
    action: str = "continue_institution"
    detail: str = ""

    def as_dict(self) -> dict[str, str | None]:
        return {
            "reason": self.reason,
            "publisher": self.publisher,
            "paper_id": self.paper_id,
            "page_url": redact_url(self.page_url),
            "action": self.action,
            "detail": redact_diagnostic_text(self.detail),
        }


class NeedsManualInstitutionAction(RuntimeError):
    """Raised when a visible institution page needs a user's intervention."""

    def __init__(
        self,
        reason: str,
        *,
        publisher: str,
        paper_id: str | None = None,
        page_url: str | None = None,
        action: str = "continue_institution",
        detail: str = "",
    ) -> None:
        self.action_info = ManualInstitutionAction(
            reason=reason,
            publisher=publisher,
            paper_id=paper_id,
            page_url=page_url,
            action=action,
            detail=detail,
        )
        message = detail or _default_message(reason)
        super().__init__(message)

    @property
    def reason(self) -> str:
        return self.action_info.reason

    @property
    def publisher(self) -> str:
        return self.action_info.publisher

    @property
    def paper_id(self) -> str | None:
        return self.action_info.paper_id

    @property
    def page_url(self) -> str | None:
        return self.action_info.page_url

    @property
    def action(self) -> str:
        return self.action_info.action

    def as_dict(self) -> dict[str, str | None]:
        return self.action_info.as_dict()


def _default_message(reason: str) -> str:
    return {
        "login_required": "请在已打开的 Chrome 页面完成机构登录，然后继续检查",
        "school_ambiguous": "学校选择结果不唯一，请在 Chrome 中选择正确机构",
        "school_not_found": "未能自动匹配学校，请在 Chrome 中手动选择机构",
        "institution_entry_not_found": "未识别到机构登录入口，请在 Chrome 中手动导航",
        "pdf_entry_not_found": "登录后的页面没有可识别 PDF 入口，请检查访问权限或手动下载",
        "session_expired": "机构登录会话已过期，请重新登录后继续检查",
        "manual_navigation_required": "请在当前机构页面导航到目标论文，再继续检查",
    }.get(reason, "机构访问需要在 Chrome 中继续操作")


def profile_access_type(profile: Any) -> str:
    if not hasattr(profile, "access_type"):
        return "ezproxy"
    value = str(getattr(profile, "access_type", "") or "").strip().casefold()
    if value in {"carsi_saml", "manual_browser", "ezproxy"}:
        return value
    # Backward compatibility: every legacy profile was an EZproxy profile.
    if str(getattr(profile, "ezproxy_login", "") or "").strip():
        return "ezproxy"
    return "manual_browser"


def profile_school_aliases(profile: Any) -> tuple[str, ...]:
    values: list[str] = []
    name = str(getattr(profile, "name", "") or "").strip()
    if name:
        values.append(name)
    raw = (
        getattr(profile, "institution_aliases", ())
        or getattr(profile, "school_aliases", ())
        or ()
    )
    if isinstance(raw, str):
        raw = raw.split(",")
    for item in raw:
        value = str(item).strip()
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value)
    return tuple(values)


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def configured_entry_url(profile: Any, publisher: str, target_url: str) -> str | None:
    """Choose a complete CARSI/manual URL without applying template expansion."""

    per_publisher = (
        _mapping(getattr(profile, "publisher_login_urls", None))
        if profile_access_type(profile) == "carsi_saml"
        else {}
    )
    value = str(per_publisher.get(publisher) or per_publisher.get("generic") or "").strip()
    if not value:
        value = str(getattr(profile, "login_url", "") or "").strip()
    if not value and profile_access_type(profile) == "manual_browser":
        value = target_url.strip()
    if not value:
        return None
    if "{" in value or "}" in value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def session_key(profile: Any, publisher: str) -> tuple[str, str]:
    institution_id = str(getattr(profile, "id", "") or "custom").strip().casefold()
    return institution_id, publisher.casefold()
