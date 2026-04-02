import discord
from discord.ext import commands
import os

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
AFK_CATEGORY_ID = 1488959625461239818
# ────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Maps user_id -> VoiceChannel they own
afk_channels: dict[int, discord.VoiceChannel] = {}


async def cleanup_afk(user_id: int):
    """Delete the AFK channel for a user if it exists."""
    channel = afk_channels.pop(user_id, None)
    if channel:
        try:
            await channel.delete(reason="AFK session ended")
        except discord.NotFound:
            pass  # Already deleted


@bot.event
async def on_ready():
    print(f"✅  Logged in as {bot.user} ({bot.user.id})")


@bot.command(name="afk")
async def afk(ctx: commands.Context):
    """Move the caller into a private AFK voice channel."""
    member = ctx.author

    # Must be in a voice channel first so we can move them
    if member.voice is None or member.voice.channel is None:
        await ctx.send(
            f"{member.mention} You need to be in a voice channel first!", delete_after=8
        )
        return

    # Don't double-create
    if member.id in afk_channels:
        await ctx.send(
            f"{member.mention} You already have an AFK channel! Use `!rafk` to remove it.",
            delete_after=8,
        )
        return

    guild: discord.Guild = ctx.guild
    category = guild.get_channel(AFK_CATEGORY_ID)

    if category is None or not isinstance(category, discord.CategoryChannel):
        await ctx.send("❌ AFK category not found. Please check the category ID.", delete_after=10)
        return

    channel_name = f"{member.display_name} - afk_channel"

    # Permission overwrites:
    #   @everyone  → cannot connect
    #   the member → can connect + view
    #   admins / owner get their permissions from the category automatically
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
        member: discord.PermissionOverwrite(connect=True, view_channel=True),
    }

    # Also explicitly allow server owner (redundant but clear)
    if guild.owner:
        overwrites[guild.owner] = discord.PermissionOverwrite(connect=True, view_channel=True)

    # Give admins access via their roles
    for role in guild.roles:
        if role.permissions.administrator:
            overwrites[role] = discord.PermissionOverwrite(connect=True, view_channel=True)

    try:
        afk_channel = await category.create_voice_channel(
            name=channel_name,
            overwrites=overwrites,
            reason=f"AFK channel for {member}",
        )
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to create channels in that category.", delete_after=10)
        return

    afk_channels[member.id] = afk_channel

    try:
        await member.move_to(afk_channel, reason="AFK command")
    except discord.Forbidden:
        await ctx.send("❌ I don't have permission to move you.", delete_after=10)
        await cleanup_afk(member.id)
        return

    await ctx.send(
        f"🌙 {member.mention} moved to your private AFK channel. Use `!rafk` to remove it.",
        delete_after=15,
    )

    # Try to delete the command message for cleanliness
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass


@bot.command(name="rafk")
async def rafk(ctx: commands.Context):
    """Remove (delete) the caller's AFK channel."""
    member = ctx.author

    if member.id not in afk_channels:
        await ctx.send(f"{member.mention} You don't have an active AFK channel.", delete_after=8)
        return

    await cleanup_afk(member.id)
    await ctx.send(f"✅ {member.mention} AFK channel removed.", delete_after=8)

    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    """Auto-delete the AFK channel when the owner leaves it."""
    if member.id not in afk_channels:
        return

    afk_channel = afk_channels[member.id]

    # User left the AFK channel (moved somewhere else or disconnected)
    if before.channel and before.channel.id == afk_channel.id:
        if after.channel is None or after.channel.id != afk_channel.id:
            await cleanup_afk(member.id)


bot.run(TOKEN)
