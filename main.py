from __future__ import annotations

import asyncio
import logging
import os
import sys
import ctypes
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

from bot.audit import AuditLogger, format_interaction_context
from bot.config import BotConfig
from bot.db import Database


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("nycrpp-bot")


def _acquire_single_instance_lock() -> int | None:
    # Windows-only named mutex to prevent multiple bot instances.
    if os.name != "nt":
        return None
    # Use Local namespace to avoid cross-session/global collisions.
    mutex_name = "Local\\NYCRPP_Federal_Reserve_Bot_MainPy"
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        return None
    err = ctypes.windll.kernel32.GetLastError()
    if err == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return handle


class NYCRPPBot(commands.Bot):
    def __init__(self, config: BotConfig):
        intents = discord.Intents.default()
        intents.members = config.enable_members_intent
        intents.message_content = config.enable_message_content_intent
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.db = Database(config.database_path)
        self.audit = AuditLogger(config.bot_audit_webhook_url)
        self._web_runner: web.AppRunner | None = None
        self.started_at_monotonic = time.monotonic()

    async def setup_hook(self) -> None:
        await self.db.init()
        await self.audit.start()
        await self._start_health_server_if_needed()
        for extension in (
            "cogs.embeds",
            "cogs.tickets",
            "cogs.applications",
            "cogs.staff",
            "cogs.utility",
        ):
            await self.load_extension(extension)
            log.info("Loaded extension: %s", extension)

        async def safe_sync_global() -> None:
            try:
                synced_global = await asyncio.wait_for(self.tree.sync(), timeout=20)
                log.info("Synced %s global commands", len(synced_global))
            except asyncio.TimeoutError:
                log.warning("Global command sync timed out after 20s; continuing startup.")
            except Exception:
                log.exception("Global command sync failed; continuing startup.")

        if self.config.dev_guild_id:
            guild_obj = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            synced_ok = False
            for attempt in range(1, 4):
                try:
                    synced = await asyncio.wait_for(self.tree.sync(guild=guild_obj), timeout=30)
                    log.info(
                        "Synced %s commands to dev guild %s (attempt %s)",
                        len(synced),
                        self.config.dev_guild_id,
                        attempt,
                    )
                    synced_ok = True
                    break
                except asyncio.TimeoutError:
                    log.warning("Dev guild sync timed out on attempt %s.", attempt)
                except Exception:
                    log.exception("Dev guild sync failed on attempt %s.", attempt)
            if not synced_ok:
                log.warning("Dev guild sync failed after 3 attempts; continuing startup.")
        else:
            await safe_sync_global()
            # No DEV_GUILD_ID configured: sync to all connected guilds for immediate visibility.
            for guild in self.guilds:
                self.tree.copy_global_to(guild=guild)
                try:
                    synced = await asyncio.wait_for(self.tree.sync(guild=guild), timeout=20)
                    log.info("Synced %s commands to guild %s (%s)", len(synced), guild.name, guild.id)
                except asyncio.TimeoutError:
                    log.warning("Guild sync timed out for %s (%s); continuing.", guild.name, guild.id)
                except Exception:
                    log.exception("Guild sync failed for %s (%s); continuing.", guild.name, guild.id)

    async def on_ready(self) -> None:
        if self.user is None:
            return
        log.info("Bot online as %s (%s)", self.user, self.user.id)
        await self.audit.send(
            "Bot Online",
            f"Connected as {self.user} ({self.user.id})",
            color=0x1F8B4C,
        )

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command[Any, Any, Any] | app_commands.ContextMenu,
    ) -> None:
        now = int(time.time())
        fields = format_interaction_context(interaction)
        fields.append(("Command", f"/{command.qualified_name}"))
        fields.append(("Executed At", f"<t:{now}:F> (<t:{now}:R>)"))
        await self.audit.send(
            "Command Executed",
            "A slash command completed successfully.",
            color=0x0B1E3D,
            fields=fields,
        )

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        cmd_name = interaction.command.qualified_name if interaction.command else "unknown"
        now = int(time.time())
        fields = format_interaction_context(interaction)
        fields.append(("Command", f"/{cmd_name}"))
        fields.append(("Executed At", f"<t:{now}:F> (<t:{now}:R>)"))
        fields.append(("Error", str(error)))
        await self.audit.send(
            "Command Error",
            "A slash command raised an error.",
            color=0xB32020,
            fields=fields,
        )
        # Always send a user-visible error so interactions do not appear as "did not respond".
        msg = "Command failed. Please try again."
        if isinstance(error, app_commands.CheckFailure):
            msg = "You do not have permission to use this command."
        elif isinstance(error, app_commands.CommandOnCooldown):
            msg = "This command is on cooldown. Try again shortly."
        elif isinstance(error, app_commands.CommandInvokeError):
            msg = f"Command failed: {error.original}"

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type not in (discord.InteractionType.component, discord.InteractionType.modal_submit):
            return
        data = interaction.data or {}
        custom_id = str(data.get("custom_id", "unknown"))
        fields = format_interaction_context(interaction)
        fields.append(("Custom ID", custom_id))
        await self.audit.send(
            "Interaction Used",
            "A button/modal interaction was used.",
            color=0x2B2D31,
            fields=fields,
        )

    async def close(self) -> None:
        if self._web_runner is not None:
            await self._web_runner.cleanup()
            self._web_runner = None
        await self.audit.close()
        await self.db.close()
        await super().close()

    async def _start_health_server_if_needed(self) -> None:
        port_raw = os.getenv("PORT", "").strip()
        if not port_raw.isdigit():
            return
        port = int(port_raw)

        async def health(_: web.Request) -> web.Response:
            return web.json_response({"ok": True, "service": "nycrpp-bot"})

        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/healthz", health)

        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        site = web.TCPSite(self._web_runner, host="0.0.0.0", port=port)
        await site.start()
        log.info("Health server listening on 0.0.0.0:%s", port)


async def main() -> None:
    lock_handle = _acquire_single_instance_lock()
    if os.name == "nt" and lock_handle is None:
        log.error("Another bot instance is already running. Exiting this instance.")
        return

    config = BotConfig.from_env()
    token = config.token
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN in environment.")

    bot = NYCRPPBot(config)
    async with bot:
        try:
            await bot.start(token)
        finally:
            if lock_handle is not None:
                ctypes.windll.kernel32.CloseHandle(lock_handle)


if __name__ == "__main__":
    asyncio.run(main())
