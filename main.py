import os
import json
import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
ROBLOX_API_KEY = os.environ["ROBLOX_API_KEY"]
UNIVERSE_ID = os.environ["ROBLOX_UNIVERSE_ID"]

TOPIC = "DcCommands"

# Replace with your Discord server ID.
GUILD_ID = 1084822318058053682


class QueensCountyBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)


bot = QueensCountyBot()


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
        async with aiohttp.ClientSession() as session:
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