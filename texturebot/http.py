from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from email.utils import parsedate_to_datetime

import aiohttp

from .store import Store


class SourceError(RuntimeError):
    pass


class HTTP:
    def __init__(self, session: aiohttp.ClientSession, store: Store):
        self.session = session
        self.store = store
        self.locks = defaultdict(asyncio.Lock)
        self.next_request: dict[str, float] = {}
        self.blocked_until: dict[str, float] = {}

    async def get(self, source: str, url: str, *, params: dict | None = None, headers: dict | None = None):
        async with self.locks[source]:
            if time.time() < self.blocked_until.get(source, 0):
                raise SourceError(f"{source}: fuente en espera por límite o credencial inválida.")
            for attempt in range(3):
                delay = self.next_request.get(source, 0) - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                if not self.store.request_slot(source, 900 if source == "packsmc" else 5000):
                    raise SourceError(f"{source}: presupuesto de consultas de 24 horas agotado.")
                self.next_request[source] = time.monotonic() + 1.1
                try:
                    async with self.session.get(url, params=params, headers=headers, allow_redirects=False) as response:
                        if response.status == 429:
                            wait = retry_after(response.headers.get("Retry-After", "60"))
                            self.blocked_until[source] = time.time() + wait
                            raise SourceError(f"{source}: límite HTTP 429; espera {int(wait)} segundos.")
                        if response.status in (401, 403):
                            self.blocked_until[source] = time.time() + 3600
                            raise SourceError(f"{source}: HTTP {response.status}; revisa su clave de API.")
                        if response.status >= 500 and attempt < 2:
                            await asyncio.sleep(2**attempt)
                            continue
                        if response.status != 200:
                            raise SourceError(f"{source}: HTTP {response.status}.")
                        if response.content_length and response.content_length > 4_000_000:
                            raise SourceError(f"{source}: respuesta demasiado grande.")
                        chunks, size = [], 0
                        async for chunk in response.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > 4_000_000:
                                raise SourceError(f"{source}: respuesta demasiado grande.")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        try:
                            return json.loads(body)
                        except (ValueError, UnicodeError) as error:
                            raise SourceError(f"{source}: la API no devolvió JSON válido.") from error
                except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                    if attempt == 2:
                        raise SourceError(f"{source}: error de conexión ({type(error).__name__}).") from error
                    await asyncio.sleep(2**attempt)
        raise SourceError(f"{source}: no se pudo consultar la API.")


def retry_after(value: str) -> float:
    try:
        return max(1, float(value))
    except ValueError:
        try:
            return max(1, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 60
