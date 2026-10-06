from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import aiohttp
import discord

from .config import ConfigError, load_config
from .discord_bot import TextureBot
from .engine import Engine
from .http import HTTP
from .locking import InstanceLock
from .store import Store


async def execute(args, config):
    store = Store(config.database)
    try:
        timeout = aiohttp.ClientTimeout(total=config.timeout)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": config.user_agent}) as session:
            engine = Engine(config, store, HTTP(session, store))
            if args.command == "preview":
                if args.refresh:
                    await engine.refresh()
                for name, section in config.sections.items():
                    print(f"\n{section.label} — {store.pending_count(name)} en cola")
                    for pack in store.queue(name, 5):
                        print(f"  {pack.title} | {pack.author or 'Autor no indicado'} | {pack.source}\n  {pack.url}")
                print("\nNo se ha enviado ningún mensaje a Discord.")
                return 0
            if args.command == "status":
                print(json.dumps(store.status(), indent=2, ensure_ascii=False))
                return 0
            async with TextureBot(config, store, engine) as bot:
                await bot.start(config.token, reconnect=True)
                return 1 if bot.startup_error else 0
    finally:
        store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Texturas de inventarios y packs de BoxPvP para Discord.")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parent.parent / "config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Conectar y publicar automáticamente")
    commands.add_parser("check", help="Revisar configuración sin conectar a Discord")
    commands.add_parser("status", help="Mostrar el estado persistente")
    preview = commands.add_parser("preview", help="Ver la cola sin enviar mensajes")
    preview.add_argument("--refresh", action="store_true", help="Consultar las fuentes para actualizar la cola")
    args = parser.parse_args()
    try:
        config = load_config(args.config, require_discord=args.command == "run")
        if args.command == "check":
            for section in config.sections.values():
                print(f"{section.label}: canal {section.channel_id or 'PENDIENTE'}, "
                      f"cooldown inicial {section.cooldown_minutes} minutos")
            print("Token Discord:", "configurado" if config.token else "PENDIENTE")
            for name, enabled in sorted(config.sources.items()):
                key_name = {"packsmc": "PACKSMC_API_KEY", "curseforge": "CURSEFORGE_API_KEY"}.get(name)
                state = "desactivada" if not enabled else (
                    f"PENDIENTE: {key_name}" if key_name and not os.getenv(key_name) else "configurada")
                print(f"{name}: {state}")
            print("La comprobación local no verifica credenciales ni disponibilidad de las APIs.")
            return 0
        log_dir = config.root / "logs"
        log_dir.mkdir(exist_ok=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
            logging.StreamHandler(), RotatingFileHandler(log_dir / "bot.log", maxBytes=2_000_000,
                                                        backupCount=3, encoding="utf-8"),
        ])
        # HTTP credentials and bodies are never logged.
        logging.getLogger("discord").setLevel(logging.WARNING)
        if args.command == "run":
            with InstanceLock(config.database.with_suffix(".lock")):
                return asyncio.run(execute(args, config))
        return asyncio.run(execute(args, config))
    except (ConfigError, RuntimeError) as error:
        print(f"No se pudo iniciar: {error}", file=sys.stderr)
        return 1
    except discord.LoginFailure:
        print("Token de bot inválido. Revisa DISCORD_BOT_TOKEN en .env.", file=sys.stderr)
        return 1
    except discord.HTTPException as error:
        print(f"Discord HTTP {error.status}. Revisa conexión, token y permisos.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Bot detenido.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
