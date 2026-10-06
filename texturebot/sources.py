from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from urllib.parse import quote

from .config import Config
from .http import HTTP, SourceError
from .models import Pack, clean_text, safe_url


def texts(values: object, key: str = "") -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return ()
    result = []
    for value in values:
        if isinstance(value, dict):
            value = value.get(key, "") if key else ""
        if isinstance(value, str) and value.strip():
            result.append(clean_text(value))
    return tuple(dict.fromkeys(result))


def author_name(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("display_name") or value.get("displayName") or value.get("username") or value.get("name", "")
    return clean_text(value) if isinstance(value, str) else ""


def rows(data: object) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("data", data.get("packs"))
    if not isinstance(data, list):
        raise SourceError("La fuente cambió el formato de su catálogo.")
    return [row for row in data if isinstance(row, dict)]


@dataclass
class Page:
    packs: list[Pack]
    cursor: str = ""


class Source:
    name = ""
    supports_backfill = True
    key_env = ""

    def __init__(self, http: HTTP, config: Config):
        self.http, self.config = http, config
        self.key = os.getenv(self.key_env, "").strip() if self.key_env else ""

    @property
    def available(self) -> bool:
        return not self.key_env or bool(self.key)

    def decode_many(self, raw: list[dict]) -> list[Pack]:
        result = []
        for value in raw:
            try:
                pack = self.decode(value)
                if pack is not None:
                    result.append(pack)
            except (ValueError, TypeError, KeyError):
                continue
        return result

    async def enrich(self, pack: Pack) -> Pack:
        return pack


class Modrinth(Source):
    name = "modrinth"

    def decode(self, value: dict) -> Pack:
        source_id = str(value.get("project_id") or value.get("id") or "")
        return Pack(
            self.name, source_id, clean_text(value.get("title"), 250),
            f"https://modrinth.com/resourcepack/{quote(str(value.get('slug') or source_id), safe='')}",
            author_name(value.get("author")), clean_text(value.get("description")),
            texts(value.get("versions", value.get("game_versions", []))),
            texts(value.get("categories", [])),
            safe_url(value.get("featured_gallery")) or next((safe_url(x) for x in (value.get("gallery") or []) if isinstance(x, str) and safe_url(x)), ""),
        )

    async def search(self, query: str, mode: str, cursor: str = "") -> Page:
        offset = int(cursor or 0) if mode == "backfill" else 0
        data = await self.http.get(self.name, "https://api.modrinth.com/v2/search", params={
            "query": query, "facets": json.dumps([["project_type:resourcepack"]]),
            "index": "downloads" if mode == "backfill" else "newest",
            "offset": offset, "limit": self.config.page_size,
        })
        if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
            raise SourceError("modrinth: respuesta de búsqueda incompatible.")
        next_offset = offset + len(data["hits"])
        more = bool(data["hits"]) and next_offset < int(data.get("total_hits", next_offset))
        return Page(self.decode_many(data["hits"]), str(next_offset) if more else "")

    async def enrich(self, pack: Pack) -> Pack:
        data = await self.http.get(self.name, f"https://api.modrinth.com/v2/project/{quote(pack.source_id, safe='')}")
        if not isinstance(data, dict):
            raise SourceError("modrinth: detalle incompatible.")
        gallery = data.get("gallery") or []
        image = next((safe_url(g.get("url")) for g in gallery if isinstance(g, dict) and g.get("featured") and safe_url(g.get("url"))), "")
        image = image or next((safe_url(g.get("url")) for g in gallery if isinstance(g, dict) and safe_url(g.get("url"))), "")
        return replace(pack, image_url=image or pack.image_url,
                       versions=texts(data.get("game_versions", [])) or pack.versions)


class PacksMC(Source):
    name = "packsmc"
    key_env = "PACKSMC_API_KEY"

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.key}"}

    def decode(self, value: dict) -> Pack | None:
        if value.get("is_exclusive"):
            return None
        versions = texts(value.get("mc_versions", [])) or texts(value.get("versions", []), "mc_version")
        return Pack(
            self.name, str(value.get("id") or value.get("slug") or ""),
            clean_text(value.get("name"), 250), safe_url(value.get("download_url")),
            author_name(value.get("author")), clean_text(value.get("description")), versions,
            texts(value.get("gamemodes", [])) + texts(value.get("tags", [])),
            next((safe_url(x) for x in (value.get("gallery") or []) if safe_url(x)), "") or safe_url(value.get("thumbnail_url")),
            texts(value.get("credits", []), "username"), resolution=clean_text(value.get("resolution"), 40),
        )

    async def search(self, query: str, mode: str, cursor: str = "") -> Page:
        params = {"q": query, "sort": "recent", "limit": self.config.page_size}
        if mode == "backfill" and cursor:
            params["cursor"] = cursor
        data = await self.http.get(self.name, "https://packsmc.com/api/v1/packs", params=params, headers=self.headers)
        return Page(self.decode_many(rows(data)), str(data.get("next_cursor") or "") if isinstance(data, dict) else "")

    async def enrich(self, pack: Pack) -> Pack:
        data = await self.http.get(self.name, f"https://packsmc.com/api/v1/packs/{quote(pack.source_id, safe='')}", headers=self.headers)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            raise SourceError("packsmc: detalle incompatible.")
        detailed = self.decode(data)
        if detailed is None:
            raise SourceError("packsmc: el pack pasó a ser exclusivo.")
        return replace(detailed, image_url=detailed.image_url or pack.image_url,
                       versions=detailed.versions or pack.versions)


class CurseForge(Source):
    name = "curseforge"
    key_env = "CURSEFORGE_API_KEY"

    def __init__(self, http: HTTP, config: Config):
        super().__init__(http, config)
        self.class_id = 0

    @property
    def headers(self):
        return {"x-api-key": self.key}

    def decode(self, value: dict) -> Pack:
        authors = texts(value.get("authors", []), "name")
        versions = []
        for file in value.get("latestFiles") or []:
            if isinstance(file, dict):
                versions.extend(texts(file.get("gameVersions", [])))
        for file in value.get("latestFilesIndexes") or []:
            if isinstance(file, dict) and isinstance(file.get("gameVersion"), str):
                versions.append(file["gameVersion"])
        screenshots = value.get("screenshots") or []
        image = next((safe_url(x.get("url")) for x in screenshots if isinstance(x, dict) and safe_url(x.get("url"))), "")
        return Pack(
            self.name, str(value.get("id") or ""), clean_text(value.get("name"), 250),
            safe_url((value.get("links") or {}).get("websiteUrl")), authors[0] if authors else "",
            clean_text(value.get("summary")), tuple(dict.fromkeys(versions)),
            texts(value.get("categories", []), "name"), image, authors[1:],
        )

    async def search(self, query: str, mode: str, cursor: str = "") -> Page:
        if not self.class_id:
            data = await self.http.get(self.name, "https://api.curseforge.com/v1/categories",
                                       params={"gameId": 432, "classesOnly": "true"}, headers=self.headers)
            self.class_id = next((int(x["id"]) for x in rows(data) if x.get("slug") in {"texture-packs", "resource-packs"}), 0)
            if not self.class_id:
                raise SourceError("curseforge: no se encontró la categoría Resource Packs.")
        offset = int(cursor or 0) if mode == "backfill" else 0
        data = await self.http.get(self.name, "https://api.curseforge.com/v1/mods/search", params={
            "gameId": 432, "classId": self.class_id, "searchFilter": query,
            "sortField": 6 if mode == "backfill" else 3, "sortOrder": "desc",
            "index": offset, "pageSize": self.config.page_size,
        }, headers=self.headers)
        raw = rows(data)
        total = data.get("pagination", {}).get("totalCount", offset + len(raw)) if isinstance(data, dict) else 0
        next_offset = offset + len(raw)
        more = bool(raw) and next_offset < min(int(total), 10000)
        return Page(self.decode_many(raw), str(next_offset) if more else "")


class TextureHub(Source):
    name = "texturehub"
    supports_backfill = False

    def decode(self, value: dict) -> Pack:
        slug = str(value.get("slug") or "")
        if not slug:
            raise ValueError("TextureHub no proporcionó el slug del pack.")
        versions = texts(value.get("versions", []), "minecraftVer")
        version = value.get("minecraftVersion")
        if not versions and isinstance(version, str):
            versions = (version,)
        tags = texts(value.get("tags", []))
        if isinstance(value.get("category"), str):
            tags += (value["category"],)
        return Pack(
            self.name, str(value.get("id") or slug), clean_text(value.get("name"), 250),
            f"https://texturehub.pl/en/pack/{quote(slug, safe='')}",
            author_name(value.get("author") or value.get("creator")), clean_text(value.get("description")),
            versions, tags, safe_url(value.get("image_url") or value.get("imageUrl") or value.get("thumbnail")),
            hashes=texts(value.get("versions", []), "sha256"), resolution=clean_text(value.get("resolution"), 40),
        )

    async def search(self, query: str, mode: str, cursor: str = "") -> Page:
        data = await self.http.get(self.name, "https://texturehub.pl/api/packs", params={"q": query})
        return Page(self.decode_many(rows(data)[:500]))

    async def enrich(self, pack: Pack) -> Pack:
        slug = pack.url.rsplit("/", 1)[-1]
        data = await self.http.get(self.name, f"https://texturehub.pl/api/packs/{slug}")
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            raise SourceError("texturehub: detalle incompatible.")
        merged = {**data, "id": pack.source_id, "slug": slug}
        detailed = self.decode(merged)
        return replace(detailed, description=detailed.description or pack.description,
                       author=detailed.author or pack.author, tags=detailed.tags or pack.tags,
                       image_url=detailed.image_url or pack.image_url)


def make_sources(http: HTTP, config: Config) -> dict[str, Source]:
    return {cls.name: cls(http, config) for cls in (Modrinth, PacksMC, CurseForge, TextureHub)
            if config.sources.get(cls.name)}


def curated_packs(config: Config) -> list[Pack]:
    try:
        data = json.loads(config.curated_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SourceError("curated.json no existe o tiene JSON inválido.") from error
    if not isinstance(data, list):
        raise SourceError("curated.json debe contener una lista.")
    result = []
    for index, value in enumerate(data, 1):
        try:
            if not isinstance(value, dict) or value.get("section") not in config.sections:
                raise ValueError("sección inválida")
            pack = Pack(
                "curated", safe_url(value.get("url")), clean_text(value.get("title"), 250),
                safe_url(value.get("url")), clean_text(value.get("author")), clean_text(value.get("description")),
                texts(value.get("versions", [])), texts(value.get("tags", [])), safe_url(value.get("image_url")),
                texts(value.get("credits", [])), approved_section=value["section"],
                resolution=clean_text(value.get("resolution"), 40),
            )
            result.append(pack)
        except (TypeError, ValueError) as error:
            raise SourceError(f"curated.json: revisa la entrada {index}.") from error
    return result
