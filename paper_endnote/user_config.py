from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .inputs import normalize_doi


ALLOWED_SOURCES = ("open_access", "institution")
DEFAULT_SOURCES = ("open_access",)
DISALLOWED_SOURCE_TOKENS = ("scihub", "sci-hub", "sci_hub", "sci-hub.tw", "scihub.tw")
PRESET_DIR = Path(__file__).resolve().parent / "presets"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class InstitutionProfile:
    id: str
    name: str
    ezproxy_login: str
    ezproxy_hosts: tuple[str, ...]
    openurl: str = ""
    login_url_markers: tuple[str, ...] = ()
    preset: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "ezproxy_login": self.ezproxy_login,
            "ezproxy_hosts": list(self.ezproxy_hosts),
            "openurl": self.openurl,
            "login_url_markers": list(self.login_url_markers),
            "preset": self.preset,
        }


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
    institution: InstitutionProfile = field(default_factory=lambda: load_preset("mcgill"))
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


def _institution_from_mapping(
    data: dict[str, object], *, preset: str = "", resolve_preset: bool = True
) -> InstitutionProfile:
    preset_name = str(data.get("preset") or preset or "").strip()
    base: dict[str, object] = {}
    if resolve_preset and preset_name:
        base = dict(load_preset(preset_name).as_dict())
    merged = {**base, **{key: value for key, value in data.items() if value not in (None, "")}}
    identifier = str(merged.get("id") or preset_name or "custom").strip()
    name = str(merged.get("name") or identifier).strip()
    login = str(merged.get("ezproxy_login") or "").strip()
    if not identifier or not login:
        raise ConfigError("机构配置需要 id 和 ezproxy_login")
    hosts = _string_list(merged.get("ezproxy_hosts"))
    if not hosts:
        host = urlparse(login.replace("{url}", "https://example.invalid")).hostname
        hosts = (host,) if host else ()
    markers = _string_list(merged.get("login_url_markers")) or (
        "login.microsoftonline.com",
        "shibboleth",
        "saml",
    )
    return InstitutionProfile(
        id=identifier,
        name=name,
        ezproxy_login=login,
        ezproxy_hosts=hosts,
        openurl=str(merged.get("openurl") or "").strip(),
        login_url_markers=markers,
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
    institution_raw = data.get("institution") if has_explicit_institution else {"preset": "mcgill"}
    institution = _institution_from_mapping(institution_raw)

    # A saved, valid institution profile is an opt-in signal for older or
    # hand-written configuration files that omit the acquisition switches.
    # Fresh installs remain neutral, and every explicitly stored switch wins.
    if "sources" in acquisition_raw:
        sources = normalize_sources(acquisition_raw["sources"])
    elif "sources" in data:  # Legacy top-level setting.
        sources = normalize_sources(data["sources"])
    elif has_explicit_institution:
        sources = ALLOWED_SOURCES
    else:
        sources = DEFAULT_SOURCES

    if "auto_institution" in acquisition_raw:
        auto_institution = bool(acquisition_raw["auto_institution"])
    else:
        auto_institution = has_explicit_institution
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
        f"ezproxy_login = {json.dumps(institution.ezproxy_login)}",
        *lines_for_list("ezproxy_hosts", institution.ezproxy_hosts),
        f"openurl = {json.dumps(institution.openurl)}",
        *lines_for_list("login_url_markers", institution.login_url_markers),
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
