import asyncio
import base64
import hashlib
import os
import json
import logging
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit

import aiohttp
import discord
import socket
from aiohttp import web
from discord import app_commands
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ROBLOX_API_KEY = os.environ["ROBLOX_API_KEY"]
UNIVERSE_ID = os.environ["ROBLOX_UNIVERSE_ID"]
ROBLOX_OAUTH_CLIENT_ID = os.getenv("ROBLOX_OAUTH_CLIENT_ID")
ROBLOX_OAUTH_CLIENT_SECRET = os.getenv("ROBLOX_OAUTH_CLIENT_SECRET")
ROBLOX_OAUTH_REDIRECT_URI = os.getenv("ROBLOX_OAUTH_REDIRECT_URI")
ROBLOX_OAUTH_TOKEN_KEY = os.getenv("ROBLOX_OAUTH_TOKEN_KEY")
ROBLOX_OAUTH_HOST = os.getenv("ROBLOX_OAUTH_HOST", "0.0.0.0")
ROBLOX_OAUTH_PORT = int(os.getenv("ROBLOX_OAUTH_PORT", "8080"))
ROBLOX_OAUTH_DATABASE = os.getenv("ROBLOX_OAUTH_DATABASE", "roblox_accounts.sqlite3")

TOPIC = "DcCommands"
OAUTH_AUTHORIZE_URL = "https://apis.roblox.com/oauth/v1/authorize"
OAUTH_TOKEN_URL = "https://apis.roblox.com/oauth/v1/token"
OAUTH_USERINFO_URL = "https://apis.roblox.com/oauth/v1/userinfo"
OAUTH_STATE_TTL_SECONDS = 600
logger = logging.getLogger(__name__)

# Replace with your Discord server ID.
GUILD_ID = 1084822318058053682

class QueensCountyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.oauth_states = {}
        self.oauth_lock = asyncio.Lock()
        self.oauth_runner = None
        self.oauth_database = None
        self.oauth_cipher = None

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

        oauth_settings = (
            ROBLOX_OAUTH_CLIENT_ID,
            ROBLOX_OAUTH_CLIENT_SECRET,
            ROBLOX_OAUTH_REDIRECT_URI,
            ROBLOX_OAUTH_TOKEN_KEY,
        )
        if not any(oauth_settings):
            return
        if not all(oauth_settings):
            raise RuntimeError(
                "Configure ROBLOX_OAUTH_CLIENT_ID, "
                "ROBLOX_OAUTH_CLIENT_SECRET, ROBLOX_OAUTH_REDIRECT_URI, "
                "and ROBLOX_OAUTH_TOKEN_KEY together."
            )

        redirect = urlsplit(ROBLOX_OAUTH_REDIRECT_URI)
        if (
            redirect.scheme not in ("https", "http")
            or not redirect.hostname
            or redirect.query
            or redirect.fragment
            or redirect.username
            or redirect.password
            or (
                redirect.scheme == "http"
                and redirect.hostname not in ("localhost", "127.0.0.1", "::1")
            )
        ):
            raise RuntimeError(
                "ROBLOX_OAUTH_REDIRECT_URI must be HTTPS, or HTTP on localhost, "
                "and must not contain a query or fragment."
            )

        try:
            self.oauth_cipher = Fernet(ROBLOX_OAUTH_TOKEN_KEY.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as error:
            raise RuntimeError(
                "ROBLOX_OAUTH_TOKEN_KEY must be a valid Fernet key."
            ) from error

        self.oauth_database = sqlite3.connect(ROBLOX_OAUTH_DATABASE)
        self.oauth_database.execute(
            """
            CREATE TABLE IF NOT EXISTS linked_accounts (
                discord_user_id TEXT PRIMARY KEY,
                roblox_user_id TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL,
                refresh_token TEXT NOT NULL
            )
            """
        )
        self.oauth_database.commit()

        callback_path = redirect.path or "/"
        app = web.Application()
        app.router.add_get(callback_path, self.oauth_callback)
        self.oauth_runner = web.AppRunner(app)
        await self.oauth_runner.setup()
        site = web.TCPSite(
            self.oauth_runner,
            ROBLOX_OAUTH_HOST,
            ROBLOX_OAUTH_PORT,
        )
        await site.start()
        logger.info(
            "Roblox OAuth callback listening on %s:%s%s",
            ROBLOX_OAUTH_HOST,
            ROBLOX_OAUTH_PORT,
            callback_path,
        )

    async def close(self):
        if self.oauth_runner is not None:
            await self.oauth_runner.cleanup()
        if self.oauth_database is not None:
            self.oauth_database.close()
        await super().close()

    def oauth_is_configured(self):
        return self.oauth_cipher is not None

    async def oauth_callback(self, request):
        state = request.query.get("state")
        authorization_code = request.query.get("code")
        if request.query.get("error") or not state or not authorization_code:
            return web.Response(
                text="Roblox account authorization was not completed. "
                "You can close this page.",
                status=400,
            )

        pending = self.oauth_states.pop(state, None)
        if pending is None or time.time() - pending["created_at"] > OAUTH_STATE_TTL_SECONDS:
            return web.Response(
                text="This authorization request is invalid or expired. "
                "Start again with /account link in Discord.",
                status=400,
            )

        form = {
            "grant_type": "authorization_code",
            "code": authorization_code,
            "client_id": ROBLOX_OAUTH_CLIENT_ID,
            "client_secret": ROBLOX_OAUTH_CLIENT_SECRET,
            "redirect_uri": ROBLOX_OAUTH_REDIRECT_URI,
            "code_verifier": pending["code_verifier"],
        }
        try:
            async with aiohttp.ClientSession( connector=aiohttp.TCPonnector( family=socket.AF_INET ) ) as session:
                async with session.post(
                    OAUTH_TOKEN_URL,
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            "Roblox OAuth code exchange failed with status %s",
                            response.status,
                        )
                        return web.Response(
                            text="Roblox authorization could not be completed. "
                            "Return to Discord and try /account link again.",
                            status=502,
                        )
                    tokens = await response.json()

                access_token = tokens.get("access_token")
                refresh_token = tokens.get("refresh_token")
                if not access_token or not refresh_token:
                    logger.error("Roblox OAuth response did not include required tokens")
                    return web.Response(
                        text="Roblox authorization returned an incomplete response. "
                        "Please try again.",
                        status=502,
                    )

                userinfo = await self.fetch_userinfo(session, access_token)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            logger.exception("Roblox OAuth callback request failed")
            return web.Response(
                text="Could not contact Roblox to finish linking. "
                "Return to Discord and try again.",
                status=502,
            )

        roblox_user_id = userinfo.get("sub")
        username = (
            userinfo.get("preferred_username")
            or userinfo.get("nickname")
            or userinfo.get("name")
        )
        if not roblox_user_id or not username:
            logger.error("Roblox userinfo response was missing account identity claims")
            return web.Response(
                text="Roblox did not return the account details needed for linking. "
                "Please try again.",
                status=502,
            )

        encrypted_refresh_token = self.oauth_cipher.encrypt(
            refresh_token.encode("utf-8")
        ).decode("ascii")
        try:
            self.oauth_database.execute(
                """
                INSERT INTO linked_accounts (
                    discord_user_id, roblox_user_id, username, refresh_token
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(discord_user_id) DO UPDATE SET
                    roblox_user_id = excluded.roblox_user_id,
                    username = excluded.username,
                    refresh_token = excluded.refresh_token
                """,
                (
                    pending["discord_user_id"],
                    str(roblox_user_id),
                    str(username),
                    encrypted_refresh_token,
                ),
            )
            self.oauth_database.commit()
        except sqlite3.IntegrityError:
            return web.Response(
                text="That Roblox account is already linked to another Discord account. "
                "You can close this page.",
                status=409,
            )

        return web.Response(
            text="Roblox account linked successfully. Return to Discord and use "
            "/account view to see your linked account.",
        )

    async def fetch_userinfo(self, session, access_token):
        async with session.get(
            OAUTH_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status != 200:
                logger.warning(
                    "Roblox OAuth userinfo request failed with status %s",
                    response.status,
                )
                raise aiohttp.ClientResponseError(
                    response.request_info,
                    response.history,
                    status=response.status,
                )
            return await response.json()

    async def get_account_access_token(self, discord_user_id):
        row = self.oauth_database.execute(
            "SELECT refresh_token FROM linked_accounts WHERE discord_user_id = ?",
            (str(discord_user_id),),
        ).fetchone()
        if row is None:
            return None, None

        try:
            refresh_token = self.oauth_cipher.decrypt(row[0].encode("ascii")).decode(
                "utf-8"
            )
        except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError):
            logger.exception("Stored Roblox OAuth token could not be decrypted")
            raise RuntimeError(
                "The stored authorization cannot be read. Please relink with "
                "/account link."
            )

        async with self.oauth_lock:
            async with aiohttp.ClientSession( connector=aiohttp.TCPonnector( family=socket.AF_INET ) ) as session:
                async with session.post(
                    OAUTH_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": ROBLOX_OAUTH_CLIENT_ID,
                        "client_secret": ROBLOX_OAUTH_CLIENT_SECRET,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        logger.warning(
                            "Roblox OAuth token refresh failed with status %s",
                            response.status,
                        )
                        raise RuntimeError(
                            "Could not refresh your Roblox authorization. "
                            "Please try again or relink with /account link."
                        )
                    tokens = await response.json()

                access_token = tokens.get("access_token")
                new_refresh_token = tokens.get("refresh_token")
                if not access_token or not new_refresh_token:
                    logger.error("Roblox OAuth refresh response was incomplete")
                    raise RuntimeError(
                        "Roblox returned an incomplete authorization response. "
                        "Please relink with /account link."
                    )

                encrypted_refresh_token = self.oauth_cipher.encrypt(
                    new_refresh_token.encode("utf-8")
                ).decode("ascii")
                self.oauth_database.execute(
                    "UPDATE linked_accounts SET refresh_token = ? "
                    "WHERE discord_user_id = ?",
                    (encrypted_refresh_token, str(discord_user_id)),
                )
                self.oauth_database.commit()
                userinfo = await self.fetch_userinfo(session, access_token)

        return access_token, userinfo


bot = QueensCountyBot()

account_group = app_commands.Group(
    name="account",
    description="Manage your linked Roblox account",
)


@account_group.command(name="link", description="Link your Roblox account")
async def account_link(interaction: discord.Interaction):
    if not bot.oauth_is_configured():
        await interaction.response.send_message(
            "Roblox account linking is not configured. Set the Roblox OAuth "
            "environment variables and restart the bot.",
            ephemeral=True,
        )
        return

    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state = secrets.token_urlsafe(32)
    now = time.time()
    bot.oauth_states = {
        key: pending
        for key, pending in bot.oauth_states.items()
        if now - pending["created_at"] <= OAUTH_STATE_TTL_SECONDS
    }
    bot.oauth_states[state] = {
        "discord_user_id": str(interaction.user.id),
        "code_verifier": verifier,
        "created_at": now,
    }
    authorization_url = OAUTH_AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": ROBLOX_OAUTH_CLIENT_ID,
            "redirect_uri": ROBLOX_OAUTH_REDIRECT_URI,
            "scope": "openid profile",
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    await interaction.response.send_message(
        f"Authorize the Roblox account you want to link: {authorization_url}",
        ephemeral=True,
    )


@account_group.command(name="view", description="View your linked Roblox account")
async def account_view(interaction: discord.Interaction):
    if not bot.oauth_is_configured():
        await interaction.response.send_message(
            "Roblox account linking is not configured. Set the Roblox OAuth "
            "environment variables and restart the bot.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    try:
        _, userinfo = await bot.get_account_access_token(interaction.user.id)
    except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
        await interaction.followup.send(str(error), ephemeral=True)
        return

    if userinfo is None:
        await interaction.followup.send(
            "You have not linked a Roblox account yet. Use /account link to connect one.",
            ephemeral=True,
        )
        return

    roblox_user_id = userinfo.get("sub")
    username = (
        userinfo.get("preferred_username")
        or userinfo.get("nickname")
        or userinfo.get("name")
    )
    if not roblox_user_id or not username:
        await interaction.followup.send(
            "Roblox did not return your account details. Please relink with "
            "/account link.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="Linked Roblox account",
        url=f"https://www.roblox.com/users/{roblox_user_id}/profile",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Username", value=str(username), inline=True)
    embed.add_field(name="Roblox user ID", value=str(roblox_user_id), inline=True)
    if userinfo.get("picture"):
        embed.set_thumbnail(url=userinfo["picture"])
    await interaction.followup.send(embed=embed, ephemeral=True)


bot.tree.add_command(account_group)


@bot.tree.command(
    name="announce",
    description="Broadcasts a message to all currently running servers",
)
@app_commands.describe(message="The announcement to broadcast")
async def announce(
    interaction: discord.Interaction,
    message: str,
):
    # Only allow trusted Discord administrators.
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True,
        )
        return

    if not message.strip() or len(message) > 500:
        await interaction.response.send_message(
            "Message must contain 1–500 characters.",
            ephemeral=True,
        )
        return

    url = (
        "https://apis.roblox.com/cloud/v2/universes/"
        f"{UNIVERSE_ID}:publishMessage"
    )

    headers = {
        "x-api-key": ROBLOX_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "topic": TOPIC,
        "message": json.dumps({
            "command": "announce",
            "message": message,
        }),
    }

    await interaction.response.defer(ephemeral=True)

    try:
        async with aiohttp.ClientSession( connector=aiohttp.TCPonnector( family=socket.AF_INET ) ) as session:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status not in (200, 204):
                    details = await response.text()
                    await interaction.followup.send(
                        f"Roblox API error ({response.status}): "
                        f"{details[:500]}",
                        ephemeral=True,
                    )
                    return

        await interaction.followup.send(
            "Announcement published to Roblox messaging.",
            ephemeral=True,
        )

    except (aiohttp.ClientError, TimeoutError) as error:
        await interaction.followup.send(
            f"Failed to contact Roblox: {error}",
            ephemeral=True,
        )


bot.run(DISCORD_TOKEN)