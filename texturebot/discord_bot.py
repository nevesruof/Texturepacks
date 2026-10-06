from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Literal

import aiohttp
import discord
from discord import app_commands

from .config import Config, ConfigError, Section
from .engine import Engine, SendError
from .models import Pack, canonical_url, clean_text, safe_url
from .store import Store

log = logging.getLogger(__name__)
SectionName = Literal["inventarios", "boxpvp"]
SOURCE_LABELS = {"modrinth": "Modrinth", "packsmc": "PacksMC", "curseforge": "CurseForge",
                 "texturehub": "TextureHub", "curated": "Selección de la comunidad"}
EMBED_FOOTER = "made by: ake, if you want this DM -Free-"


def pack_embed(pack: Pack, section: Section) -> discord.Embed:
    description = discord.utils.escape_markdown(clean_text(pack.description, 900)) or "Consulta las imágenes y los detalles en la página del pack."
    embed = discord.Embed(title=clean_text(pack.title, 250), url=pack.url, description=description,
                          color=section.color)
    embed.add_field(name="Creador", value=discord.utils.escape_markdown(pack.author[:300]) or "No indicado por la fuente", inline=True)
    versions = ", ".join(pack.versions)
    embed.add_field(name="Minecraft", value=versions[:950] or "Consulta la página del pack", inline=True)
    if pack.resolution:
        embed.add_field(name="Resolución", value=pack.resolution[:40], inline=True)
    if pack.credits:
        embed.add_field(name="Créditos", value=discord.utils.escape_markdown(", ".join(pack.credits))[:900], inline=False)
    if safe_url(pack.image_url):
        embed.set_image(url=pack.image_url)
    embed.set_footer(text=EMBED_FOOTER)
    return embed


class AdminGroup(app_commands.Group):
    def __init__(self, bot: "TextureBot"):
        super().__init__(name="packs", description="Control del bot de texturas",
                         default_permissions=discord.Permissions(manage_guild=True), guild_only=True)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.bot.target_guild or not interaction.permissions.manage_guild:
            await interaction.response.send_message("Necesitas Administrar servidor en la comunidad configurada.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        message = "No se pudo ejecutar el comando. Revisa los permisos y la configuración del bot."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        log.warning("Error de comando: %s", type(error).__name__)


class TextureBot(discord.Client):
    def __init__(self, config: Config, store: Store, engine: Engine):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config, self.store, self.engine = config, store, engine
        self.tree = app_commands.CommandTree(self)
        self.target_guild = 0
        self.worker: asyncio.Task | None = None
        self.started = False
        self.last_recovery = 0.0
        self.startup_error = ""
        self.register_commands()

    def register_commands(self):
        group = AdminGroup(self)

        @group.command(name="estado", description="Colas, cooldowns y estado de las fuentes")
        async def status(interaction: discord.Interaction):
            info = self.store.status()
            lines = []
            for state in info["sections"]:
                section = self.config.sections[state["name"]]
                counts = state["counts"]
                due = state["last_sent"] + state["cooldown"] if state["last_sent"] else time.time()
                lines.append(f"**{section.label}** · <#{section.channel_id}>\n"
                             f"{'Pausado' if state['paused'] else 'Activo'} · {state['cooldown'] // 60} min · "
                             f"{counts.get('pending', 0)} pendientes · {counts.get('posted', 0)} publicados\n"
                             f"Próximo turno: <t:{int(max(due, time.time()))}:R> · "
                             f"{counts.get('uncertain', 0) + counts.get('sending', 0)} envíos por comprobar")
            for source in info["sources"]:
                lines.append(f"**{SOURCE_LABELS.get(source['source'], source['source'])}**: "
                             + (source["error"] or "Última consulta correcta"))
            await interaction.response.send_message("\n\n".join(lines)[:1900], ephemeral=True)

        @group.command(name="pausar", description="Pausa las publicaciones de una sección")
        async def pause(interaction: discord.Interaction, seccion: SectionName):
            self.store.pause(seccion, True)
            await interaction.response.send_message(f"{seccion}: publicaciones pausadas.", ephemeral=True)

        @group.command(name="reanudar", description="Reanuda una sección conservando su cooldown")
        async def resume(interaction: discord.Interaction, seccion: SectionName):
            self.store.pause(seccion, False)
            await interaction.response.send_message(f"{seccion}: publicaciones reanudadas.", ephemeral=True)

        @group.command(name="cooldown", description="Cambia el intervalo de una sección, en minutos")
        async def cooldown(interaction: discord.Interaction, seccion: SectionName,
                           minutos: app_commands.Range[int, 3, 10080]):
            self.store.cooldown(seccion, minutos)
            await interaction.response.send_message(f"{seccion}: un pack cada {minutos} minutos. Cambio guardado.", ephemeral=True)

        @group.command(name="buscar", description="Actualiza las colas desde las fuentes disponibles")
        async def search(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self.engine.refresh()
            await interaction.followup.send("Búsqueda terminada o ya en curso. Consulta /packs estado.", ephemeral=True)

        @group.command(name="preview", description="Muestra el siguiente pack sin publicarlo")
        async def preview(interaction: discord.Interaction, seccion: SectionName):
            packs = self.store.queue(seccion, 1)
            if not packs:
                await interaction.response.send_message("La cola de esta sección está vacía.", ephemeral=True)
                return
            await interaction.response.send_message(embed=pack_embed(packs[0], self.config.sections[seccion]), ephemeral=True)

        @group.command(name="agregar", description="Añade un pack revisado a la sección que elijas")
        async def add(interaction: discord.Interaction, seccion: SectionName, enlace: str, titulo: str,
                      autor: str, imagen: str = ""):
            if not safe_url(enlace) or (imagen and not safe_url(imagen)):
                await interaction.response.send_message("Usa enlaces HTTP/HTTPS válidos.", ephemeral=True)
                return
            pack = Pack("curated", canonical_url(enlace), clean_text(titulo, 250), enlace,
                        clean_text(autor, 300), image_url=imagen, approved_section=seccion)
            added = self.store.offer(seccion, pack, self.config.queue_limit)
            await interaction.response.send_message("Pack añadido a la cola." if added else "Ya estaba registrado o la cola está llena.", ephemeral=True)

        self.tree.add_command(group)

    async def on_ready(self):
        if self.started:
            return
        self.started = True
        try:
            guilds = set()
            for section in self.config.sections.values():
                channel = self.get_channel(section.channel_id) or await self.fetch_channel(section.channel_id)
                if not isinstance(channel, discord.TextChannel):
                    raise ConfigError(f"{section.name}: el ID debe ser de un canal de texto del servidor.")
                guilds.add(channel.guild.id)
                member = channel.guild.me or await channel.guild.fetch_member(self.user.id)
                permissions = channel.permissions_for(member)
                if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links, permissions.read_message_history)):
                    raise ConfigError(f"{section.name}: faltan Ver canal, Enviar mensajes, Insertar enlaces o Leer historial.")
            if len(guilds) != 1 or (self.config.guild_id and self.config.guild_id not in guilds):
                raise ConfigError("Los dos canales deben pertenecer al mismo servidor configurado.")
            self.target_guild = guilds.pop()
            guild = discord.Object(id=self.target_guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.worker = asyncio.create_task(self.engine.run(self), name="texture-relay")
            self.worker.add_done_callback(self.worker_finished)
            log.info("%s conectado. Canales y permisos verificados.", self.user.name)
        except (ConfigError, discord.HTTPException) as error:
            self.startup_error = str(error) if isinstance(error, ConfigError) else f"Discord HTTP {error.status}; revisa token, invitación y permisos."
            log.error("%s", self.startup_error)
            await self.close()

    def worker_finished(self, task: asyncio.Task):
        if not task.cancelled() and task.exception():
            log.error("El proceso de publicaciones se detuvo: %s", type(task.exception()).__name__)
            self.startup_error = "El proceso del bot se detuvo. Revisa los logs."
            asyncio.create_task(self.close())

    async def send_pack(self, section: Section, job: dict) -> str:
        if not self.is_ready():
            raise SendError("Discord se está reconectando.")
        channel = self.get_channel(section.channel_id)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.target_guild:
            raise SendError("Canal no disponible en el servidor configurado.")
        pack = job["pack"]
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Ver pack / descargar", url=pack.url, style=discord.ButtonStyle.link))
        try:
            message = await channel.send(embed=pack_embed(pack, section), view=view,
                                         nonce=job["pack_key"], silent=True,
                                         allowed_mentions=discord.AllowedMentions.none())
            return str(message.id)
        except (discord.Forbidden, discord.NotFound) as error:
            raise SendError(f"Discord HTTP {error.status}; revisa el canal y los permisos.") from error
        except discord.HTTPException as error:
            raise SendError(f"Discord HTTP {error.status} al publicar.", uncertain=error.status >= 500) from error
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            raise SendError("Conexión interrumpida: se comprobará el historial antes de reenviar.", uncertain=True) from error

    async def recover_interrupted(self):
        if not self.is_ready() or time.monotonic() - self.last_recovery < 60:
            return
        self.last_recovery = time.monotonic()
        allowed_channels = {s.channel_id for s in self.config.sections.values()}
        for job in self.store.interrupted():
            channel_id = int(job["channel_id"] or 0)
            if channel_id not in allowed_channels:
                continue
            channel = self.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            after = datetime.fromtimestamp(max(0, job["started"] - 5), timezone.utc)
            found, scanned = None, 0
            try:
                async for message in channel.history(after=after, limit=500, oldest_first=True):
                    scanned += 1
                    if message.author.id == self.user.id and (
                        str(message.nonce or "") == job["pack_key"]
                        or any(
                            (safe_url(embed.url) and canonical_url(embed.url) == canonical_url(job["pack"].url))
                            or (embed.footer.text and embed.footer.text.endswith(f"ref:{job['pack_key']}"))
                            for embed in message.embeds
                        )
                    ):
                        found = message
                        break
                if found:
                    self.store.complete(job, str(found.id), found.created_at.timestamp())
                elif scanned < 500 and time.time() - job["started"] > 120:
                    self.store.defer(job, "Historial comprobado; el envío no llegó.")
            except (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError):
                # Ambiguous sends stay blocked rather than risk a second publication.
                continue

    async def close(self):
        if self.worker and self.worker is not asyncio.current_task():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await super().close()
