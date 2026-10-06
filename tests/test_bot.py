from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiohttp
import discord
from aiohttp import web

from texturebot.config import ConfigError, load_config
from texturebot.discord_bot import TextureBot, pack_embed
from texturebot.engine import Engine, SendError
from texturebot.http import HTTP, SourceError
from texturebot.models import Pack, canonical_url, matches, safe_url
from texturebot.sources import CurseForge, Modrinth, PacksMC, TextureHub, Page, curated_packs
from texturebot.store import Store

ROOT = Path(__file__).resolve().parent.parent


def test_config():
    return load_config(ROOT / "config.toml")


def pack(title="Blue Inventory", source="modrinth", source_id="a", author="Creator"):
    return Pack(source, source_id, title, f"https://example.org/{source}/{source_id}", author,
                versions=("1.21.4",), image_url="https://example.org/image.png")


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.sections = test_config().sections

    def test_separate_inventory_and_boxpvp(self):
        inventory = pack()
        box = pack("Red BoxPvP", source_id="b")
        self.assertTrue(matches(inventory, self.sections["inventarios"]))
        self.assertFalse(matches(inventory, self.sections["boxpvp"]))
        self.assertTrue(matches(box, self.sections["boxpvp"]))
        self.assertFalse(matches(box, self.sections["inventarios"]))

    def test_generic_pvp_is_not_boxpvp(self):
        self.assertFalse(matches(pack("Blue PvP 16x"), self.sections["boxpvp"]))

    def test_boxpvp_spelling_and_word_boundaries(self):
        for title in ("BoxPvP", "Box-PvP", "BOX_PVP", "box pvp"):
            self.assertTrue(matches(pack(title), self.sections["boxpvp"]), title)
        self.assertFalse(matches(pack("anti-boxpvpish texture"), self.sections["boxpvp"]))
        self.assertFalse(matches(pack("Guinea textures"), self.sections["inventarios"]))

    def test_bedrock_and_configured_versions(self):
        self.assertFalse(matches(pack("Inventory Bedrock"), self.sections["inventarios"]))
        section = replace(self.sections["inventarios"], versions=("1.8.9",))
        self.assertFalse(matches(pack(), section))
        self.assertTrue(matches(replace(pack(), versions=("1.8.9",)), section))

    def test_manual_approval_goes_only_to_chosen_section(self):
        approved = replace(pack("Reviewed PvP"), approved_section="boxpvp")
        self.assertTrue(matches(approved, self.sections["boxpvp"]))
        self.assertFalse(matches(approved, self.sections["inventarios"]))

    def test_invalid_urls_and_tracking_parameters(self):
        for url in ("javascript:alert(1)", "https://user:secret@example.org/a", "file:///a", "https://[", "https://example.org/bad path"):
            self.assertEqual(safe_url(url), "")
        self.assertEqual(canonical_url("https://www.example.org/a/?utm_source=x#test"), "https://example.org/a")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db.sqlite"
        self.store = Store(self.path)
        self.sections = {name: replace(section, channel_id=1000000000000000 + index)
                         for index, (name, section) in enumerate(test_config().sections.items())}
        self.store.configure(self.sections)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_no_repeated_project_even_if_metadata_changes(self):
        self.assertTrue(self.store.offer("inventarios", pack(), 20))
        self.assertFalse(self.store.offer("inventarios", replace(pack(), title="Renamed Inventory"), 20))
        self.assertEqual(self.store.pending_count("inventarios"), 1)

    def test_dedup_between_sources_and_after_restart(self):
        self.store.offer("inventarios", pack(), 20, now=100)
        other = pack(source="packsmc", source_id="b")
        self.assertFalse(self.store.offer("inventarios", other, 20, now=101))
        job = self.store.reserve(self.sections["inventarios"], now=200)
        self.store.complete(job, "123", sent_at=200)
        self.store.close()
        self.store = Store(self.path)
        self.store.configure(self.sections)
        self.assertFalse(self.store.offer("inventarios", other, 20))
        self.assertEqual(self.store.status()["sections"][1]["counts"]["posted"], 1)

    def test_different_creators_are_not_merged(self):
        self.assertTrue(self.store.offer("inventarios", pack(), 20))
        self.assertTrue(self.store.offer("inventarios", pack(source_id="b", author="Other"), 20))

    def test_cooldowns_independent_and_no_catch_up_burst(self):
        self.store.offer("inventarios", pack(), 20)
        self.store.offer("inventarios", pack(source_id="b", title="Second Inventory"), 20)
        self.store.offer("boxpvp", pack(source_id="c", title="BoxPvP"), 20)
        job = self.store.reserve(self.sections["inventarios"], now=1000)
        self.store.complete(job, "1", sent_at=1000)
        self.assertIsNone(self.store.reserve(self.sections["inventarios"], now=1001))
        self.assertIsNotNone(self.store.reserve(self.sections["boxpvp"], now=1001))
        self.assertIsNotNone(self.store.reserve(self.sections["inventarios"], now=8200))

    def test_pause_and_runtime_cooldown_survive_restart(self):
        self.store.pause("inventarios", True)
        self.store.cooldown("inventarios", 240)
        self.store.close()
        self.store = Store(self.path)
        self.store.configure(self.sections)
        state = next(s for s in self.store.status()["sections"] if s["name"] == "inventarios")
        self.assertEqual((state["paused"], state["cooldown"]), (1, 14400))

    def test_ambiguous_delivery_blocks_section_and_cross_channel_duplicates(self):
        p = pack()
        self.store.offer("inventarios", p, 20)
        self.store.offer("boxpvp", p, 20)
        self.store.offer("inventarios", pack(source_id="b"), 20)
        job = self.store.reserve(self.sections["inventarios"], now=100)
        self.store.defer(job, "timeout", uncertain=True, now=100)
        self.assertIsNone(self.store.reserve(self.sections["inventarios"], now=10000))
        self.assertIsNone(self.store.reserve(self.sections["boxpvp"], now=10000))
        self.assertEqual(len(self.store.interrupted()), 1)
        self.store.complete(job, "message", sent_at=100)
        self.assertEqual(self.store.pending_count("boxpvp"), 0)

    def test_queue_limit_and_failed_delivery_backoff(self):
        self.store.offer("inventarios", pack(), 1)
        self.assertFalse(self.store.offer("inventarios", pack(source_id="b"), 1))
        job = self.store.reserve(self.sections["inventarios"], now=100)
        self.store.defer(job, "forbidden", now=100)
        self.assertIsNone(self.store.reserve(self.sections["inventarios"], now=200))
        self.assertIsNotNone(self.store.reserve(self.sections["inventarios"], now=401))

    def test_quota_is_persistent_rolling_24_hours(self):
        self.assertTrue(self.store.request_slot("packsmc", 2))
        self.assertTrue(self.store.request_slot("packsmc", 2))
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.request_slot("packsmc", 2))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_modrinth_search_is_resourcepack_only(self):
        class FakeHTTP:
            async def get(self, source, url, **kwargs):
                self.params = kwargs["params"]
                return {"hits": [{"project_id": "a", "slug": "blue-inventory", "title": "Blue Inventory",
                                  "description": "Inventory GUI", "author": "A", "versions": ["1.8.9"],
                                  "gallery": ["https://example.org/screenshot.png"]}], "total_hits": 50}
        http = FakeHTTP()
        page = await Modrinth(http, test_config()).search("inventory", "backfill", "25")
        self.assertEqual(json.loads(http.params["facets"]), [["project_type:resourcepack"]])
        self.assertEqual(http.params["offset"], 25)
        self.assertEqual(page.cursor, "26")
        self.assertEqual(page.packs[0].url, "https://modrinth.com/resourcepack/blue-inventory")

    async def test_packsmc_keeps_canonical_download_page_and_credits(self):
        source = PacksMC(None, test_config())
        decoded = source.decode({"id": "a", "name": "BoxPvP", "download_url": "https://packsmc.com/pack/a",
                                 "author": {"username": "A"}, "credits": [{"username": "B"}],
                                 "versions": [{"mc_version": "1.21.4"}], "gamemodes": ["BOX_PVP"]})
        self.assertEqual(decoded.credits, ("B",))
        self.assertEqual(decoded.versions, ("1.21.4",))
        self.assertTrue(matches(decoded, test_config().sections["boxpvp"]))
        self.assertIsNone(source.decode({"is_exclusive": True}))

    async def test_curseforge_finds_resourcepack_class_instead_of_guessing(self):
        class FakeHTTP:
            async def get(self, source, url, **kwargs):
                if url.endswith("categories"):
                    return {"data": [{"id": 999, "slug": "texture-packs"}]}
                self.params = kwargs["params"]
                return {"data": [{"id": 1, "name": "Inventory", "links": {"websiteUrl": "https://www.curseforge.com/minecraft/texture-packs/a"},
                                  "authors": [{"name": "Creator"}], "latestFiles": [{"gameVersions": ["1.21.4"]}],
                                  "screenshots": [{"url": "https://example.org/preview.png"}]}], "pagination": {"totalCount": 1}}
        http = FakeHTTP()
        page = await CurseForge(http, test_config()).search("inventory", "recent")
        self.assertEqual(http.params["classId"], 999)
        self.assertEqual(page.packs[0].versions, ("1.21.4",))

    async def test_texturehub_documented_version_schema(self):
        decoded = TextureHub(None, test_config()).decode({"id": "a", "slug": "inventory", "name": "Inventory",
                                                         "versions": [{"minecraftVer": "1.21", "sha256": "a" * 64}]})
        self.assertEqual(decoded.url, "https://texturehub.pl/en/pack/inventory")
        self.assertEqual(decoded.versions, ("1.21",))
        self.assertIn("sha256:" + "a" * 64, decoded.identities)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = replace(test_config(), database=Path(self.temp.name) / "db.sqlite")
        self.store = Store(self.config.database)
        self.engine = Engine(self.config, self.store, None)
        self.engine.sources = {}

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_correct_section_is_passed_to_sender(self):
        self.store.offer("inventarios", pack(), 20)
        self.store.offer("boxpvp", pack("BoxPvP", source_id="b"), 20)
        class Sender:
            calls = []
            async def send_pack(self, section, job):
                self.calls.append((section.name, job["pack"].title))
                return "123"
        sender = Sender()
        await self.engine.publish_due(sender)
        self.assertEqual(sender.calls, [("inventarios", "Blue Inventory"), ("boxpvp", "BoxPvP")])
        await self.engine.publish_due(sender)
        self.assertEqual(len(sender.calls), 2)

    async def test_network_uncertainty_does_not_repost(self):
        self.store.offer("inventarios", pack(), 20)
        class Sender:
            count = 0
            async def send_pack(self, section, job):
                self.count += 1
                raise SendError("timeout", uncertain=True)
        sender = Sender()
        await self.engine.publish_due(sender)
        await self.engine.publish_due(sender)
        self.assertEqual(sender.count, 1)
        self.assertEqual(self.store.interrupted()[0]["status"], "uncertain")

    async def test_one_failing_source_does_not_stop_other_sources(self):
        class Broken:
            name, available, supports_backfill = "broken", True, False
            async def search(self, *args):
                raise SourceError("unavailable")
        class Working:
            name, available, supports_backfill = "working", True, False
            async def search(self, query, *args):
                return Page([replace(pack(), description="Inventory GUI")])
        self.engine.sources = {"broken": Broken(), "working": Working()}
        await self.engine.refresh()
        self.assertEqual(self.store.pending_count("inventarios"), 1)
        self.assertEqual(self.store.pending_count("boxpvp"), 0)

    async def test_backfill_advances_and_reuses_detail_cache(self):
        class Source:
            name, available, supports_backfill = "fake", True, True
            details = 0
            async def search(self, query, mode, cursor=""):
                self.last_cursor = cursor
                return Page([pack(title="Unnamed", source="fake")], "next")
            async def enrich(self, p):
                self.details += 1
                return replace(p, description="Inventory GUI")
        source = Source()
        await self.engine.discover_source(source)
        await self.engine.discover_source(source)
        self.assertEqual(source.details, 1)
        self.assertEqual(self.store.cursor("fake:inventarios:inventory"), "next")

    async def test_backfill_does_not_skip_metadata_when_budget_is_exhausted(self):
        self.engine.config = replace(self.config, detail_budget=0)
        class Source:
            name, available, supports_backfill = "fake", True, True
            async def search(self, *args):
                return Page([pack(title="Unnamed", source="fake")], "next")
        await self.engine.discover_source(Source())
        self.assertEqual(self.store.cursor("fake:inventarios:inventory"), "")
        self.assertEqual(self.store.pending_count("inventarios"), 0)

    async def test_recovery_finds_already_delivered_message(self):
        section = self.config.sections["inventarios"]
        self.store.offer("inventarios", pack(), 20)
        job = self.store.reserve(section, now=time.time() - 180)
        message = MagicMock()
        message.id = 123
        message.author.id = 999
        message.created_at.timestamp.return_value = time.time() - 179
        message.nonce = None
        message.embeds = [pack_embed(pack(), section)]
        async def history(**kwargs):
            yield message
        channel = MagicMock(spec=discord.TextChannel)
        channel.history = history
        with patch.object(TextureBot, "is_ready", return_value=True), patch.object(TextureBot, "get_channel", return_value=channel):
            async with TextureBot(self.config, self.store, self.engine) as bot:
                bot._connection.user = MagicMock(id=999)
                await bot.recover_interrupted()
        self.assertFalse(self.store.interrupted())
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs").fetchone()[0], "posted")

    async def test_recovery_only_retries_after_complete_history_check(self):
        section = self.config.sections["inventarios"]
        self.store.offer("inventarios", pack(), 20)
        self.store.reserve(section, now=time.time() - 180)
        async def history(**kwargs):
            for _ in []:
                yield None
        channel = MagicMock(spec=discord.TextChannel)
        channel.history = history
        with patch.object(TextureBot, "is_ready", return_value=True), patch.object(TextureBot, "get_channel", return_value=channel):
            async with TextureBot(self.config, self.store, self.engine) as bot:
                bot._connection.user = MagicMock(id=999)
                await bot.recover_interrupted()
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs").fetchone()[0], "pending")

    async def test_recovery_does_not_retry_if_history_is_truncated(self):
        section = self.config.sections["inventarios"]
        self.store.offer("inventarios", pack(), 20)
        self.store.reserve(section, now=time.time() - 180)
        message = MagicMock()
        message.author.id = 1
        async def history(**kwargs):
            for _ in range(500):
                yield message
        channel = MagicMock(spec=discord.TextChannel)
        channel.history = history
        with patch.object(TextureBot, "is_ready", return_value=True), patch.object(TextureBot, "get_channel", return_value=channel):
            async with TextureBot(self.config, self.store, self.engine) as bot:
                bot._connection.user = MagicMock(id=999)
                await bot.recover_interrupted()
        self.assertEqual(len(self.store.interrupted()), 1)

    async def test_discord_commands_and_nonce_payload(self):
        async with TextureBot(self.config, self.store, self.engine) as bot:
            group = bot.tree.get_command("packs")
            self.assertEqual({c.name for c in group.commands},
                             {"estado", "pausar", "reanudar", "cooldown", "buscar", "preview", "agregar"})
        # Installed dependency must preserve enforce_nonce for retries of the same message.
        with discord.http.handle_message_parameters(nonce="abc") as parameters:
            self.assertTrue(parameters.payload["enforce_nonce"])
            self.assertEqual(parameters.payload["nonce"], "abc")
        for section in self.config.sections.values():
            embed = pack_embed(pack(), section)
            self.assertEqual(embed.footer.text, "made by: ake, if you want this DM -Free-")
            self.assertLessEqual(len(embed), 6000)


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "db.sqlite")
        self.app = web.Application()
        self.app.router.add_get("/chunks", self.chunks)
        self.app.router.add_get("/limited", self.limited)
        self.app.router.add_get("/private", self.private)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.session = aiohttp.ClientSession()
        self.http = HTTP(self.session, self.store)

    async def asyncTearDown(self):
        await self.session.close()
        await self.runner.cleanup()
        self.store.close()
        self.temp.cleanup()

    async def chunks(self, request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(b'{"data":')
        await asyncio.sleep(0.01)
        await response.write(b'[1,2,3]}')
        await response.write_eof()
        return response

    async def limited(self, request):
        return web.Response(status=429, headers={"Retry-After": "120"})

    async def private(self, request):
        return web.Response(status=401, text="NEVER_LOG_THIS_SECRET")

    async def test_reads_full_chunked_json_response(self):
        result = await self.http.get("test", self.base + "/chunks")
        self.assertEqual(result, {"data": [1, 2, 3]})

    async def test_429_is_respected_without_retry_storm(self):
        with self.assertRaises(SourceError):
            await self.http.get("test", self.base + "/limited")
        self.assertGreater(self.http.blocked_until["test"], time.time() + 110)
        with self.assertRaises(SourceError):
            await self.http.get("test", self.base + "/chunks")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)

    async def test_auth_failure_does_not_expose_response_body(self):
        with self.assertRaises(SourceError) as caught:
            await self.http.get("packsmc", self.base + "/private")
        self.assertNotIn("NEVER_LOG_THIS_SECRET", str(caught.exception))


class ConfigurationTests(unittest.TestCase):
    def test_two_channels_cannot_have_same_id(self):
        with patch.dict("os.environ", {"INVENTORY_CHANNEL_ID": "123456789012345678", "BOXPVP_CHANNEL_ID": "123456789012345678"}):
            with self.assertRaises(ConfigError):
                test_config()

    def test_curated_entry_requires_explicit_valid_section(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "curated.json"
            path.write_text(json.dumps([{"url": "https://example.org/pack", "title": "Reviewed", "section": "wrong"}]))
            with self.assertRaises(SourceError):
                curated_packs(replace(test_config(), curated_file=path))


if __name__ == "__main__":
    unittest.main()
