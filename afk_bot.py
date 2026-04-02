import discord
from discord.ext import commands
from discord import app_commands
import os

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN           = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
AFK_CATEGORY_ID = 1488959625461239818
CASE_CHANNEL_ID = 1488962187841245204
STAFF_ROLE_ID   = 1488600755916116090
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states    = True
intents.members         = True

bot = commands.Bot(command_prefix="?unused?", intents=intents)

# user_id → VoiceChannel
afk_channels: dict[int, discord.VoiceChannel] = {}

# message_id (case embed) → case metadata dict
open_cases: dict[int, dict] = {}


# ─────────────────────────────── helpers ─────────────────────────────────────

def is_staff(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.id == STAFF_ROLE_ID for r in member.roles)


async def cleanup_afk(user_id: int):
    channel = afk_channels.pop(user_id, None)
    if channel:
        try:
            await channel.delete(reason="AFK session ended")
        except discord.NotFound:
            pass


# ─────────────────────────────── on_ready ────────────────────────────────────

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅  Logged in as {bot.user} ({bot.user.id}) — slash commands synced")


# ─────────────────────────────── /afk ────────────────────────────────────────

@bot.tree.command(name="afk", description="Move yourself into a private AFK voice channel.")
async def afk(interaction: discord.Interaction):
    member = interaction.user
    guild  = interaction.guild

    if member.id in afk_channels:
        await interaction.response.send_message(
            "You already have an AFK channel! Use `/rafk` to remove it.", ephemeral=True
        )
        return

    category = guild.get_channel(AFK_CATEGORY_ID)
    if category is None or not isinstance(category, discord.CategoryChannel):
        await interaction.response.send_message(
            "❌ AFK category not found. Check the category ID.", ephemeral=True
        )
        return

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False),
        member:             discord.PermissionOverwrite(connect=True,  view_channel=True),
    }
    if guild.owner:
        overwrites[guild.owner] = discord.PermissionOverwrite(connect=True, view_channel=True)
    for role in guild.roles:
        if role.permissions.administrator or role.id == STAFF_ROLE_ID:
            overwrites[role] = discord.PermissionOverwrite(connect=True, view_channel=True)

    try:
        afk_channel = await category.create_voice_channel(
            name=f"{member.display_name} - afk_channel",
            overwrites=overwrites,
            reason=f"AFK channel for {member}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to create channels in that category.", ephemeral=True
        )
        return

    afk_channels[member.id] = afk_channel

    # Move the member — works even if they are not currently in a VC.
    # Discord will pull them in as long as the bot has Move Members permission.
    try:
        await member.move_to(afk_channel, reason="/afk command")
        await interaction.response.send_message(
            "🌙 You've been moved to your private AFK channel. Use `/rafk` to remove it.",
            ephemeral=True,
        )
    except discord.HTTPException:
        # Member not in any VC — send them a clickable link instead
        await interaction.response.send_message(
            f"🌙 Your private AFK channel is ready: {afk_channel.mention}\n"
            "Click it to join. Use `/rafk` to remove it.",
            ephemeral=True,
        )


# ─────────────────────────────── /rafk ───────────────────────────────────────

@bot.tree.command(name="rafk", description="Remove your AFK channel.")
async def rafk(interaction: discord.Interaction):
    member = interaction.user
    if member.id not in afk_channels:
        await interaction.response.send_message(
            "You don't have an active AFK channel.", ephemeral=True
        )
        return
    await cleanup_afk(member.id)
    await interaction.response.send_message("✅ AFK channel removed.", ephemeral=True)


# ─────────────────── voice state → auto-delete AFK channel ───────────────────

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after:  discord.VoiceState,
):
    if member.id not in afk_channels:
        return
    afk_channel = afk_channels[member.id]
    if before.channel and before.channel.id == afk_channel.id:
        if after.channel is None or after.channel.id != afk_channel.id:
            await cleanup_afk(member.id)


# ─────────────────────────────── /case ───────────────────────────────────────

class CaseButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent across restarts

    @discord.ui.button(label="Open Case", style=discord.ButtonStyle.green, custom_id="case_open")
    async def open_case(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Only staff can open cases.", ephemeral=True
            )
            return

        msg_id = interaction.message.id
        if msg_id not in open_cases:
            await interaction.response.send_message("❌ Case data not found.", ephemeral=True)
            return

        case = open_cases[msg_id]
        if case.get("thread_id"):
            await interaction.response.send_message("This case is already open.", ephemeral=True)
            return

        guild      = interaction.guild
        reporter   = guild.get_member(case["reporter_id"])
        staff_role = guild.get_role(STAFF_ROLE_ID)

        ch_overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me:           discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        if reporter:
            ch_overwrites[reporter] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )
        if staff_role:
            ch_overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )
        for role in guild.roles:
            if role.permissions.administrator:
                ch_overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                )

        case_channel = await guild.create_text_channel(
            name=f"case-{msg_id}",
            overwrites=ch_overwrites,
            reason=f"Case #{msg_id} opened by {interaction.user}",
        )

        case["thread_id"] = case_channel.id

        intro = (
            f"## 📁 Case Opened\n"
            f"**Reported User:** {case.get('target', 'Unknown')}\n"
            f"**Reason:** {case.get('reason', 'No reason given')}\n"
        )
        if case.get("proof"):
            intro += f"**Proof:** {case['proof']}\n"
        if reporter:
            intro += f"\n{reporter.mention} — staff will be with you shortly."

        await case_channel.send(intro)

        # Update embed status
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.set_footer(text="Status: Open")

        new_view = CaseButtons()
        for child in new_view.children:
            if child.custom_id == "case_open":
                child.disabled = True
            elif child.custom_id == "case_close":
                child.disabled = False

        await interaction.message.edit(embed=embed, view=new_view)
        await interaction.response.send_message(
            f"✅ Case opened → {case_channel.mention}", ephemeral=True
        )

    @discord.ui.button(
        label="Close Case", style=discord.ButtonStyle.red,
        custom_id="case_close", disabled=True
    )
    async def close_case(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ Only staff can close cases.", ephemeral=True
            )
            return

        msg_id = interaction.message.id
        if msg_id not in open_cases:
            await interaction.response.send_message("❌ Case data not found.", ephemeral=True)
            return

        case      = open_cases[msg_id]
        thread_id = case.get("thread_id")

        if thread_id:
            ch = interaction.guild.get_channel(thread_id)
            if ch:
                try:
                    await ch.delete(reason=f"Case closed by {interaction.user}")
                except discord.NotFound:
                    pass
            case["thread_id"] = None

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.set_footer(text="Status: Closed")

        new_view = CaseButtons()
        for child in new_view.children:
            if child.custom_id == "case_open":
                child.disabled = False
            elif child.custom_id == "case_close":
                child.disabled = True

        await interaction.message.edit(embed=embed, view=new_view)
        await interaction.response.send_message("🔒 Case closed.", ephemeral=True)


@bot.tree.command(name="case", description="File a case/report against a user.")
@app_commands.describe(
    who    = "The user you are reporting",
    reason = "Reason for the report",
    proof  = "Optional: link to screenshot, video, or other evidence",
)
async def case_cmd(
    interaction: discord.Interaction,
    who:    discord.Member,
    reason: str,
    proof:  str = None,
):
    guild        = interaction.guild
    case_channel = guild.get_channel(CASE_CHANNEL_ID)

    if case_channel is None:
        await interaction.response.send_message("❌ Case channel not found.", ephemeral=True)
        return

    embed = discord.Embed(title="📋 New Case Filed", color=discord.Color.orange())
    embed.add_field(name="Reported User", value=f"{who.mention} (`{who}`)", inline=False)
    embed.add_field(name="Reported By",   value=interaction.user.mention,   inline=False)
    embed.add_field(name="Reason",        value=reason,                     inline=False)
    if proof:
        embed.add_field(name="Proof", value=proof, inline=False)
    embed.set_footer(text="Status: Pending")
    embed.set_thumbnail(url=who.display_avatar.url)

    view = CaseButtons()
    msg  = await case_channel.send(embed=embed, view=view)

    open_cases[msg.id] = {
        "reporter_id": interaction.user.id,
        "target":      str(who),
        "reason":      reason,
        "proof":       proof,
        "thread_id":   None,
    }

    await interaction.response.send_message(
        f"✅ Your case has been filed in {case_channel.mention}.", ephemeral=True
    )


# ─────────────────────────────── run ─────────────────────────────────────────

bot.add_view(CaseButtons())  # re-register persistent buttons on restart
bot.run(TOKEN)
