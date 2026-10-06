from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


def load_env(path: Path) -> None:
    """Read simple dotenv assignments without executing shell expressions."""
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ConfigError(f".env, línea {number}: usa CLAVE=valor.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError(f".env, línea {number}: comillas incompletas.")
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        # Empty environment placeholders must not hide values from the file.
        if not os.environ.get(key):
            os.environ[key] = value


@dataclass(frozen=True)
class Section:
    name: str
    label: str
    channel_id: int
    cooldown_minutes: int
    queries: tuple[str, ...]
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    versions: tuple[str, ...]
    color: int


@dataclass(frozen=True)
class Config:
    root: Path
    token: str
    guild_id: int
    database: Path
    curated_file: Path
    user_agent: str
    discovery_interval: int
    page_size: int
    queue_limit: int
    detail_budget: int
    timeout: int
    sources: dict[str, bool]
    sections: dict[str, Section]


def env_id(name: str) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return 0
    if not value.isdigit() or not 10**15 <= int(value) < 2**64:
        raise ConfigError(f"{name} debe contener un ID válido de Discord.")
    return int(value)


def strings(value: object, field: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ConfigError(f"{field} debe ser una lista de textos.")
    if required and not value:
        raise ConfigError(f"{field} no puede estar vacío.")
    return tuple(dict.fromkeys(x.strip() for x in value))


def integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{field} debe estar entre {minimum} y {maximum}.")
    return value


def load_config(path: Path, *, require_discord: bool = False) -> Config:
    path = path.resolve()
    root = path.parent
    load_env(root / ".env")
    try:
        with path.open("rb") as file:
            raw = tomllib.load(file)
        bot, discovery = raw["bot"], raw["discovery"]
        sections = {}
        for name, values in raw["sections"].items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
                raise ConfigError("Nombre de sección inválido.")
            color = values.get("color", "#8B5CF6")
            if not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
                raise ConfigError(f"{name}.color debe usar #RRGGBB.")
            sections[name] = Section(
                name, str(values["label"]), env_id(values["channel_env"]),
                integer(values["cooldown_minutes"], f"{name}.cooldown_minutes", 3, 10080),
                strings(values["queries"], f"{name}.queries", required=True),
                strings(values["include"], f"{name}.include", required=True),
                strings(values.get("exclude", []), f"{name}.exclude"),
                strings(values.get("versions", []), f"{name}.versions"), int(color[1:], 16),
            )
        if set(sections) != {"inventarios", "boxpvp"}:
            raise ConfigError("Configura las dos secciones: inventarios y boxpvp.")
        enabled = raw.get("sources", {})
        known = {"modrinth", "packsmc", "curseforge", "texturehub", "curated"}
        if set(enabled) - known or any(type(v) is not bool for v in enabled.values()):
            raise ConfigError("sources contiene una fuente o valor desconocido.")
        config = Config(
            root, os.getenv("DISCORD_BOT_TOKEN", "").strip(), env_id("DISCORD_GUILD_ID"),
            root / bot["database"], root / bot["curated_file"], str(bot["user_agent"]),
            integer(discovery["interval_minutes"], "interval_minutes", 5, 1440) * 60,
            integer(discovery["page_size"], "page_size", 1, 50),
            integer(discovery["max_queue_per_section"], "max_queue_per_section", 1, 10000),
            integer(discovery["detail_budget_per_source"], "detail_budget_per_source", 0, 50),
            integer(discovery["request_timeout_seconds"], "request_timeout_seconds", 5, 60),
            {name: enabled.get(name, False) for name in known}, sections,
        )
    except (KeyError, TypeError, ValueError, OSError) as error:
        if isinstance(error, ConfigError):
            raise
        raise ConfigError(f"Revisa config.toml: {error}") from error
    channel_ids = [s.channel_id for s in sections.values() if s.channel_id]
    if len(channel_ids) != len(set(channel_ids)):
        raise ConfigError("Inventarios y BoxPvP deben apuntar a canales distintos.")
    if require_discord and (not config.token or len(channel_ids) != 2):
        raise ConfigError("Completa DISCORD_BOT_TOKEN, INVENTORY_CHANNEL_ID y BOXPVP_CHANNEL_ID en .env.")
    return config
