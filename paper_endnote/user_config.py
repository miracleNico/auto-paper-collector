from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .inputs import normalize_doi
from .redaction import transient_auth_query_keys


ALLOWED_SOURCES = ("open_access", "institution")
DEFAULT_SOURCES = ("open_access",)
INSTITUTION_ACCESS_TYPES = ("ezproxy", "carsi_saml", "manual_browser")
DISALLOWED_SOURCE_TOKENS = ("scihub", "sci-hub", "sci_hub", "sci-hub.tw", "scihub.tw")
PRESET_DIR = Path(__file__).resolve().parent / "presets"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class InstitutionProfile:
    id: str = ""
    name: str = ""
    access_type: str = "ezproxy"
    login_url: str = ""
    login_url_markers: tuple[str, ...] = ()
    openurl: str = ""
    ezproxy_login: str = ""
    ezproxy_hosts: tuple[str, ...] = ()
    school_aliases: tuple[str, ...] = ()
    entity_id: str = ""
    publisher_login_urls: dict[str, str] = field(default_factory=dict)
    preset: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "access_type": self.access_type,
            "login_url": self.login_url,
            "login_url_markers": list(self.login_url_markers),
            "openurl": self.openurl,
            "ezproxy_login": self.ezproxy_login,
            "ezproxy_hosts": list(self.ezproxy_hosts),
            "school_aliases": list(self.school_aliases),
            "entity_id": self.entity_id,
            "publisher_login_urls": dict(self.publisher_login_urls),
            "preset": self.preset,
        }


def default_institution_profile() -> InstitutionProfile:
    """Return the unconfigured institution used by fresh installations."""

    return InstitutionProfile()


@dataclass(frozen=True)
class OcrOptions:
    enabled: bool = True
    languages: str = "eng"
    max_pages: int = 2

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _clamp_login_wait(value: object) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError("login_wait_seconds 必须是整数") from exc
    return max(30, min(seconds, 1800))


@dataclass(frozen=True)
class AcquisitionConfig:
    sources: tuple[str, ...] = DEFAULT_SOURCES
    ocr: OcrOptions = field(default_factory=OcrOptions)
    institution: InstitutionProfile = field(default_factory=default_institution_profile)
    auto_institution: bool = False
    auto_commit: bool = True
    login_wait_seconds: int = 600


def default_config_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / "config.toml"


def list_presets() -> dict[str, InstitutionProfile]:
    presets: dict[str, InstitutionProfile] = {}
    if not PRESET_DIR.is_dir():
        return presets
    for path in sorted(PRESET_DIR.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        profile = _institution_from_mapping(data, preset=path.stem, resolve_preset=False)
        presets[path.stem] = profile
    return presets


def load_preset(name: str) -> InstitutionProfile:
    presets = list_presets()
    if name not in presets:
        raise ConfigError(f"未知机构预设：{name}")
    return presets[name]


def institution_from_payload(data: dict[str, object]) -> InstitutionProfile:
    return _institution_from_mapping(data, resolve_preset=bool(str(data.get("preset") or "").strip()))


def normalize_sources(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not values:
        return DEFAULT_SOURCES
    seen: list[str] = []
    for raw in values:
        token = str(raw).strip().casefold().replace(" ", "_")
        compact = token.replace("-", "").replace("_", "")
        if token in DISALLOWED_SOURCE_TOKENS or compact == "scihub":
            raise ConfigError("不支持 Sci-Hub 或其他未授权全文源")
        if token not in ALLOWED_SOURCES:
            raise ConfigError(f"未知获取来源：{raw}。允许值为 open_access、institution")
        if token not in seen:
            seen.append(token)
    if not seen:
        raise ConfigError("至少选择一个获取来源")
    return tuple(seen)


def _string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [item.strip() for item in value.split(",") if item.strip()]
        return tuple(parts)
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    raise ConfigError("列表字段格式无效")


def _string_map(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("publisher_login_urls 必须是键值表")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip().casefold()
        url = str(raw_value or "").strip()
        if key and url:
            result[key] = url
    return result


def _complete_official_url(value: str, field_name: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if "{" in value or "}" in value or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{field_name} 必须是完整的 http(s) URL，不能使用模板")
    _reject_transient_auth_query(value, field_name)
    return value


def _reject_transient_auth_query(value: str, field_name: str) -> None:
    transient_keys = transient_auth_query_keys(value)
    if transient_keys:
        names = "、".join(sorted(transient_keys))
        raise ConfigError(f"{field_name} 不能保存一次性认证参数：{names}")


def _institution_from_mapping(
    data: dict[str, object], *, preset: str = "", resolve_preset: bool = True
) -> InstitutionProfile:
    preset_name = str(data.get("preset") or preset or "").strip()
    base: dict[str, object] = {}
    if resolve_preset and preset_name:
        base = dict(load_preset(preset_name).as_dict())
    # Preserve explicitly supplied empty values. This lets the settings API
    # clear an optional value inherited from a preset instead of silently
    # restoring it on the next load.
    merged = dict(base)
    merged.update({key: value for key, value in data.items() if value is not None})

    identifier = str(merged.get("id") or preset_name or "").strip()
    name = str(merged.get("name") or identifier).strip()
    login = str(merged.get("ezproxy_login") or "").strip()
    access_type_raw = str(merged.get("access_type") or "").strip().casefold()
    # Migration for configuration files written before access_type existed.
    access_type = access_type_raw or "ezproxy"
    if access_type not in INSTITUTION_ACCESS_TYPES:
        allowed = "、".join(INSTITUTION_ACCESS_TYPES)
        raise ConfigError(f"未知机构访问方式：{access_type}。允许值为 {allowed}")

    configured = bool(identifier or name or login or str(merged.get("login_url") or "").strip())
    if configured and not identifier:
        raise ConfigError("机构配置需要 id")
    if configured and access_type == "ezproxy" and not login:
        raise ConfigError("EZproxy 机构配置需要 ezproxy_login")

    login_url = str(merged.get("login_url") or "").strip()
    publisher_login_urls = _string_map(merged.get("publisher_login_urls"))
    _reject_transient_auth_query(login_url, "login_url")
    for key, value in publisher_login_urls.items():
        _reject_transient_auth_query(value, f"publisher_login_urls.{key}")
    if access_type in {"carsi_saml", "manual_browser"}:
        login_url = _complete_official_url(login_url, "login_url")
        publisher_login_urls = {
            key: _complete_official_url(value, f"publisher_login_urls.{key}")
            for key, value in publisher_login_urls.items()
        }
    if access_type != "carsi_saml":
        publisher_login_urls = {}

    if "ezproxy_hosts" in merged:
        hosts = _string_list(merged.get("ezproxy_hosts"))
    else:
        hosts = ()
        host = urlparse(login.replace("{url}", "https://example.invalid")).hostname
        hosts = (host,) if host else ()
    if "login_url_markers" in merged:
        markers = _string_list(merged.get("login_url_markers"))
    elif configured:
        markers = ("login.microsoftonline.com", "shibboleth", "saml")
    else:
        markers = ()
    return InstitutionProfile(
        id=identifier,
        name=name,
        access_type=access_type,
        login_url=login_url,
        login_url_markers=markers,
        openurl=str(merged.get("openurl") or "").strip(),
        ezproxy_login=login,
        ezproxy_hosts=hosts,
        school_aliases=(
            _string_list(merged.get("school_aliases"))
            if access_type == "carsi_saml"
            else ()
        ),
        entity_id=(
            str(merged.get("entity_id") or "").strip()
            if access_type == "carsi_saml"
            else ""
        ),
        publisher_login_urls=publisher_login_urls,
        preset=preset_name,
    )


def _ocr_from_mapping(data: dict[str, object] | None) -> OcrOptions:
    payload = data or {}
    languages = str(payload.get("languages") or "eng").strip() or "eng"
    try:
        max_pages = int(payload.get("max_pages") or 2)
    except (TypeError, ValueError) as exc:
        raise ConfigError("ocr.max_pages 必须是整数") from exc
    enabled = bool(payload.get("enabled", True))
    return OcrOptions(enabled=enabled, languages=languages, max_pages=max(1, min(max_pages, 5)))


def load_acquisition_config(path: Path, *, create: bool = True) -> AcquisitionConfig:
    path = Path(path)
    if not path.is_file():
        config = AcquisitionConfig()
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump_acquisition_config(config), encoding="utf-8", newline="\n")
        return config
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    acquisition_raw = data.get("acquisition") if isinstance(data.get("acquisition"), dict) else {}
    ocr = _ocr_from_mapping(data.get("ocr") if isinstance(data.get("ocr"), dict) else None)
    has_explicit_institution = isinstance(data.get("institution"), dict)
    institution_raw = data.get("institution") if has_explicit_institution else {}
    institution = _institution_from_mapping(institution_raw)
    has_configured_institution = bool(institution.id)

    # A saved, valid institution profile is an opt-in signal for older or
    # hand-written configuration files that omit the acquisition switches.
    # Fresh installs remain neutral, and every explicitly stored switch wins.
    if "sources" in acquisition_raw:
        sources = normalize_sources(acquisition_raw["sources"])
    elif "sources" in data:  # Legacy top-level setting.
        sources = normalize_sources(data["sources"])
    elif has_configured_institution:
        sources = ALLOWED_SOURCES
    else:
        sources = DEFAULT_SOURCES

    if "auto_institution" in acquisition_raw:
        auto_institution = bool(acquisition_raw["auto_institution"])
    else:
        auto_institution = has_configured_institution
    auto_commit = bool(acquisition_raw.get("auto_commit", True))
    login_wait_seconds = _clamp_login_wait(acquisition_raw.get("login_wait_seconds", 600))
    return AcquisitionConfig(
        sources=sources,
        ocr=ocr,
        institution=institution,
        auto_institution=auto_institution,
        auto_commit=auto_commit,
        login_wait_seconds=login_wait_seconds,
    )


def dump_acquisition_config(config: AcquisitionConfig) -> str:
    institution = config.institution

    def lines_for_list(name: str, values: tuple[str, ...] | list[str]) -> list[str]:
        if not values:
            return [f"{name} = []"]
        rendered = ", ".join(json.dumps(item) for item in values)
        return [f"{name} = [{rendered}]"]

    rows = [
        "[acquisition]",
        f"sources = [{', '.join(json.dumps(item) for item in config.sources)}]",
        f"auto_institution = {'true' if config.auto_institution else 'false'}",
        f"auto_commit = {'true' if config.auto_commit else 'false'}",
        f"login_wait_seconds = {int(config.login_wait_seconds)}",
        "",
        "[ocr]",
        f"enabled = {'true' if config.ocr.enabled else 'false'}",
        f"languages = {json.dumps(config.ocr.languages)}",
        f"max_pages = {int(config.ocr.max_pages)}",
        "",
        "[institution]",
        f"preset = {json.dumps(institution.preset)}",
        f"id = {json.dumps(institution.id)}",
        f"name = {json.dumps(institution.name)}",
        f"access_type = {json.dumps(institution.access_type)}",
        f"login_url = {json.dumps(institution.login_url)}",
        *lines_for_list("login_url_markers", institution.login_url_markers),
        f"openurl = {json.dumps(institution.openurl)}",
        f"ezproxy_login = {json.dumps(institution.ezproxy_login)}",
        *lines_for_list("ezproxy_hosts", institution.ezproxy_hosts),
        *lines_for_list("school_aliases", institution.school_aliases),
        f"entity_id = {json.dumps(institution.entity_id)}",
        "publisher_login_urls = {"
        + ", ".join(
            f"{json.dumps(key)} = {json.dumps(value)}"
            for key, value in sorted(institution.publisher_login_urls.items())
        )
        + "}",
        "",
    ]
    return "\n".join(rows)


def save_acquisition_config(path: Path, config: AcquisitionConfig) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_acquisition_config(config), encoding="utf-8", newline="\n")


def institution_proxy_url(profile: InstitutionProfile, target_url: str) -> str:
    encoded = quote(target_url, safe=":/")
    template = profile.ezproxy_login
    if "{url}" in template:
        return template.replace("{url}", encoded)
    separator = "&" if "?" in template else "?"
    return f"{template}{separator}url={encoded}"


def institution_openurl(profile: InstitutionProfile, doi: str) -> str | None:
    doi = normalize_doi(doi)
    if not profile.openurl or not doi:
        return None
    template = profile.openurl
    if "info:doi/{doi}" in template:
        return template.replace("info:doi/{doi}", quote(f"info:doi/{doi}", safe=""))
    if "{doi}" in template:
        return template.replace("{doi}", quote(doi, safe=""))
    return template


def extract_doi_from_ezproxy_url(entry_url: str) -> str | None:
    target = parse_qs(urlparse(entry_url).query).get("url", [""])[0]
    parsed = urlparse(target)
    if parsed.hostname not in {"doi.org", "dx.doi.org"}:
        return None
    return normalize_doi(parsed.path.lstrip("/"))
