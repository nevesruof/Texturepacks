from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import asdict, dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import Section


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", text).replace("_", " ").split())


def clean_text(value: object, limit: int = 3000) -> str:
    text = re.sub(r"<[^>]*>", " ", str(value or ""))
    return " ".join(html.unescape(text).split())[:limit]


def safe_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 512 or any(c.isspace() for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return ""
        # Accessing .port validates malformed ports and bracketed hosts.
        _ = parsed.port
        return value
    except ValueError:
        return ""


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.netloc.casefold().removeprefix("www.")
    query = [(k, v) for k, v in parse_qsl(parsed.query) if not k.startswith("utm_") and k not in {"ref", "source"}]
    return urlunsplit(("https", host, parsed.path.rstrip("/"), urlencode(sorted(query)), ""))


@dataclass(frozen=True)
class Pack:
    source: str
    source_id: str
    title: str
    url: str
    author: str = ""
    description: str = ""
    versions: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    image_url: str = ""
    credits: tuple[str, ...] = ()
    approved_section: str = ""
    hashes: tuple[str, ...] = ()
    resolution: str = ""

    def __post_init__(self):
        if not self.source_id or not self.title.strip() or not safe_url(self.url):
            raise ValueError("Pack sin identificador, título o enlace válido.")

    @property
    def identities(self) -> tuple[str, ...]:
        result = [f"source:{self.source}:{self.source_id}", f"url:{canonical_url(self.url)}"]
        if self.author.strip():
            result.append(f"name:{normalize(self.title)}|{normalize(self.author)}")
        result.extend(f"sha256:{h.casefold()}" for h in self.hashes if re.fullmatch(r"[a-fA-F0-9]{64}", h))
        return tuple(result)

    @property
    def key(self) -> str:
        return hashlib.sha256(self.identities[0].encode()).hexdigest()[:24]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Pack":
        data = dict(data)
        for field_name in ("versions", "tags", "credits", "hashes"):
            data[field_name] = tuple(data.get(field_name, ()))
        return cls(**data)


def has_phrase(text: str, phrase: str) -> bool:
    # BoxPvP may be written with spaces, hyphens, or underscores.
    if normalize(phrase).replace(" ", "") == "boxpvp":
        return bool(re.search(r"(?<!\w)box\s*pvp(?!\w)", text))
    phrase = normalize(phrase)
    return bool(phrase and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text))


def matches(pack: Pack, section: Section) -> bool:
    if pack.approved_section:
        if pack.approved_section != section.name:
            return False
    else:
        text = normalize(" ".join((pack.title, pack.description, *pack.tags)))
        if any(has_phrase(text, p) for p in section.exclude):
            return False
        if not any(has_phrase(text, p) for p in section.include):
            return False
    if section.versions and not set(section.versions).intersection(pack.versions):
        return False
    return True
