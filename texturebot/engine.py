from __future__ import annotations

import asyncio
import logging

from .config import Config
from .http import HTTP, SourceError
from .models import Pack, matches
from .sources import Source, curated_packs, make_sources
from .store import Store

log = logging.getLogger(__name__)


class SendError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.uncertain = uncertain


class Engine:
    def __init__(self, config: Config, store: Store, http: HTTP):
        self.config, self.store, self.http = config, store, http
        self.sources = make_sources(http, config)
        self.refresh_lock = asyncio.Lock()
        self.publish_lock = asyncio.Lock()
        store.configure(config.sections)

    async def enrich(self, pack: Pack, source: Source) -> Pack:
        cached = self.store.cached(pack)
        if cached:
            return cached
        detailed = await source.enrich(pack)
        self.store.cache(detailed)
        return detailed

    async def discover_source(self, source: Source):
        if not source.available:
            self.store.source_health(source.name, f"Falta {source.key_env} en .env.")
            return
        budget = self.config.detail_budget
        added = 0
        try:
            for section in self.config.sections.values():
                if self.store.pending_count(section.name) >= self.config.queue_limit:
                    continue
                for query in section.queries:
                    cursor_name = f"{source.name}:{section.name}:{query}"
                    modes = ["recent", "backfill"] if source.supports_backfill else ["recent"]
                    for mode in modes:
                        if self.store.pending_count(section.name) >= self.config.queue_limit:
                            break
                        cursor = self.store.cursor(cursor_name) if mode == "backfill" else ""
                        page = await source.search(query, mode, cursor)
                        complete = True
                        for pack in page.packs:
                            if self.store.pending_count(section.name) >= self.config.queue_limit:
                                complete = False
                                break
                            cached = self.store.cached(pack)
                            if cached:
                                pack = cached
                            elif not pack.description and not matches(pack, section):
                                if budget <= 0:
                                    complete = False
                                    continue
                                budget -= 1
                                pack = await self.enrich(pack, source)
                            if matches(pack, section):
                                added += int(self.store.offer(section.name, pack, self.config.queue_limit))
                        if mode == "backfill" and complete:
                            self.store.set_cursor(cursor_name, page.cursor)
            self.store.source_health(source.name)
            log.info("%s: %s packs nuevos en cola", source.name, added)
        except (SourceError, ValueError, TypeError, KeyError) as error:
            # Only controlled HTTP errors include details; never expose response bodies or API secrets.
            message = str(error) if isinstance(error, SourceError) else f"Formato incompatible ({type(error).__name__})."
            self.store.source_health(source.name, message)
            log.warning("%s: %s", source.name, message)

    async def refresh(self):
        if self.refresh_lock.locked():
            return
        async with self.refresh_lock:
            if self.config.sources.get("curated"):
                try:
                    for pack in curated_packs(self.config):
                        section = self.config.sections[pack.approved_section]
                        if matches(pack, section):
                            self.store.offer(section.name, pack, self.config.queue_limit)
                    self.store.source_health("curated")
                except SourceError as error:
                    self.store.source_health("curated", str(error))
                    log.warning("%s", error)
            await asyncio.gather(*(self.discover_source(source) for source in self.sources.values()))

    async def publish_due(self, sender):
        if self.publish_lock.locked():
            return
        async with self.publish_lock:
            for section in self.config.sections.values():
                job = self.store.reserve(section)
                if not job:
                    continue
                try:
                    pack = job["pack"]
                    source = self.sources.get(pack.source)
                    if source and source.available:
                        pack = await self.enrich(pack, source)
                    if not matches(pack, section):
                        self.store.discard(job, "El pack ya no coincide con esta sección o versión.")
                        continue
                    self.store.replace_payload(job["pack_key"], pack)
                    job["pack"] = pack
                    message_id = await sender.send_pack(section, job)
                    self.store.complete(job, str(message_id))
                    log.info("Publicado en %s: %s", section.name, pack.title)
                except SendError as error:
                    self.store.defer(job, str(error), uncertain=error.uncertain)
                    log.warning("%s: %s", section.name, error)
                except (SourceError, ValueError, TypeError, KeyError) as error:
                    message = str(error) if isinstance(error, SourceError) else "Error al preparar el pack."
                    self.store.defer(job, message)
                    log.warning("%s: %s", section.name, message)

    async def discovery_loop(self):
        while True:
            await asyncio.sleep(self.config.discovery_interval)
            await self.refresh()

    async def publishing_loop(self, sender):
        while True:
            await sender.recover_interrupted()
            await self.publish_due(sender)
            await asyncio.sleep(15)

    async def run(self, sender):
        # Do the first discovery before publishing; never catch up missed intervals in a burst.
        await self.refresh()
        collector = asyncio.create_task(self.discovery_loop(), name="pack-discovery")
        publisher = asyncio.create_task(self.publishing_loop(sender), name="pack-publisher")
        try:
            await asyncio.gather(collector, publisher)
        finally:
            collector.cancel()
            publisher.cancel()
            await asyncio.gather(collector, publisher, return_exceptions=True)
