import discord
from discord.ext import commands
from discord import app_commands
import os

# ── Config ───────────────────────────────────────────────────────────────────
TOKEN                  = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
AFK_CATEGORY_ID        = 1488959625461239818
CASE_CHANNEL_ID        = 1488962187841245204
STAFF_ROLE_ID          = 1488600755916116090
COMMUNITY_ROLE_ID      = 1489454244246327327   # required to create communities
TICKET_CHANNEL_ID      = 1488958946726117619   # only channel where /ticket works
SERVER_OWNER_USER_ID   = 1487316298969911409   # hard-coded server owner
# ─────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states    = True
intents.members         = True

bot = commands.Bot(command_prefix="?unused?", intents=intents)

# ── In-memory stores ──────────────────────────────────────────────────────────
afk_channels:  dict[int, discord.VoiceChannel]  = {}   # user_id → VC
open_cases:    dict[int, dict]                  = {}   # msg_id  → case data
# community_channel_id → { owner_id, name, open: bool }
communities:   dict[int, dict]                  = {}
# ticket_channel_id → { opener_id }
open_tickets:  dict[int, dict]                  = {}


# ─────────────────────────────── helpers ─────────────────────────────────────

def is_staff(member: discord.Member) -> bool:
    if member.id == SERVER_OWNER_USER_ID:
        return True
    if member.guild_permissions.administrator:
        return True
    return any(r.id == STAFF_ROLE_ID for r in member.roles)

def has_community_role(member: discord.Member) -> bool:
    if is_staff(member):
        return True
    return any(r.id == COMMUNITY_ROLE_ID for r in member.roles)

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


# ══════════════════════════════════════════════════════════════════════════════
#  AFK
# ══════════════════════════════════════════════════════════════════════════════

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

    try:
        await member.move_to(afk_channel, reason="/afk command")
        await interaction.response.send_message(
            "🌙 You've been moved to your private AFK channel. Use `/rafk` to remove it.",
            ephemeral=True,
        )
    except discord.HTTPException:
        await interaction.response.send_message(
            f"🌙 Your private AFK channel is ready: {afk_channel.mention}\n"
            "Click it to join. Use `/rafk` to remove it.",
            ephemeral=True,
        )


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


# ══════════════════════════════════════════════════════════════════════════════
#  CASE SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

class CaseButtons(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Case", style=discord.ButtonStyle.green, custom_id="case_open")
    async def open_case(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can open cases.", ephemeral=True)
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
            guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        if reporter:
            ch_overwrites[reporter] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        if staff_role:
            ch_overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        for role in guild.roles:
            if role.permissions.administrator:
                ch_overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

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

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.set_footer(text="Status: Open")
        new_view = CaseButtons()
        for child in new_view.children:
            if child.custom_id == "case_open":  child.disabled = True
            elif child.custom_id == "case_close": child.disabled = False
        await interaction.message.edit(embed=embed, view=new_view)
        await interaction.response.send_message(f"✅ Case opened → {case_channel.mention}", ephemeral=True)

    @discord.ui.button(label="Close Case", style=discord.ButtonStyle.red, custom_id="case_close", disabled=True)
    async def close_case(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ Only staff can close cases.", ephemeral=True)
            return

        msg_id = interaction.message.id
        if msg_id not in open_cases:
            await interaction.response.send_message("❌ Case data not found.", ephemeral=True)
            return

        case = open_cases[msg_id]
        if case.get("thread_id"):
            ch = interaction.guild.get_channel(case["thread_id"])
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
            if child.custom_id == "case_open":  child.disabled = False
            elif child.custom_id == "case_close": child.disabled = True
        await interaction.message.edit(embed=embed, view=new_view)
        await interaction.response.send_message("🔒 Case closed.", ephemeral=True)


@bot.tree.command(name="case", description="File a case/report against a user.")
@app_commands.describe(
    who    = "The user you are reporting",
    reason = "Reason for the report",
    proof  = "Optional: link to screenshot, video, or other evidence",
)
async def case_cmd(interaction: discord.Interaction, who: discord.Member, reason: str, proof: str = None):
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
        "target": str(who), "reason": reason, "proof": proof, "thread_id": None,
    }
    await interaction.response.send_message(f"✅ Your case has been filed in {case_channel.mention}.", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
#  COMMUNITY SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

class CommunityJoinView(discord.ui.View):
    def __init__(self, community_channel_id: int):
        super().__init__(timeout=None)
        self.community_channel_id = community_channel_id
        # Give the button a unique custom_id so it survives restarts
        self.join_btn.custom_id = f"community_join_{community_channel_id}"

    @discord.ui.button(label="Join Community", style=discord.ButtonStyle.blurple, custom_id="community_join_placeholder")
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = communities.get(self.community_channel_id)
        if not data:
            await interaction.response.send_message("❌ Community data not found.", ephemeral=True)
            return

        if not data.get("open", False):
            await interaction.response.send_message("🔒 This community is currently closed.", ephemeral=True)
            return

        channel = interaction.guild.get_channel(self.community_channel_id)
        if channel is None:
            await interaction.response.send_message("❌ Community channel not found.", ephemeral=True)
            return

        member = interaction.user
        # Grant view + send permission to this member
        await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
        await interaction.response.send_message(f"✅ You've joined **{data['name']}**! → {channel.mention}", ephemeral=True)


@bot.tree.command(name="community", description="Create a new community text channel. Requires the Community role.")
@app_commands.describe(name="Name of your community (used as the channel name)")
async def community_cmd(interaction: discord.Interaction, name: str):
    member = interaction.user
    guild  = interaction.guild

    if not has_community_role(member):
        await interaction.response.send_message(
            "❌ You need the **Community** role to create a community.", ephemeral=True
        )
        return

    # Sanitise name for channel naming
    safe_name = name.lower().replace(" ", "-")[:80]

    # Community channels are private by default; owner controls who can join via the join button
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        member:             discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True),
    }
    for role in guild.roles:
        if role.permissions.administrator or role.id == STAFF_ROLE_ID:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    try:
        ch = await guild.create_text_channel(
            name=f"🏘️・{safe_name}",
            overwrites=overwrites,
            reason=f"Community '{name}' created by {member}",
        )
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to create channels.", ephemeral=True)
        return

    communities[ch.id] = {
        "owner_id": member.id,
        "name":     name,
        "open":     False,   # closed until owner opens it
    }

    # Post a join embed inside the new channel
    join_view = CommunityJoinView(ch.id)
    embed = discord.Embed(
        title=f"🏘️ {name}",
        description=(
            f"**Owner:** {member.mention}\n\n"
            "Click **Join Community** below to request access.\n"
            f"The owner can open or close this community with `/community-open` and `/community-close`."
        ),
        color=discord.Color.blurple(),
    )
    await ch.send(embed=embed, view=join_view)

    await interaction.response.send_message(
        f"✅ Community **{name}** created! → {ch.mention}\n"
        "It's **closed** by default. Use `/community-open` inside it to let people join.",
        ephemeral=True,
    )


@bot.tree.command(name="community-open", description="Open your community so members can join.")
async def community_open(interaction: discord.Interaction):
    ch_id = interaction.channel_id
    data  = communities.get(ch_id)

    if data is None:
        await interaction.response.send_message("❌ This isn't a community channel.", ephemeral=True)
        return

    member = interaction.user
    if member.id != data["owner_id"] and not is_staff(member):
        await interaction.response.send_message("❌ Only the community owner can do this.", ephemeral=True)
        return

    data["open"] = True
    await interaction.response.send_message("✅ Your community is now **open**. Members can join via the button.", ephemeral=False)


@bot.tree.command(name="community-close", description="Close your community so no new members can join.")
async def community_close(interaction: discord.Interaction):
    ch_id = interaction.channel_id
    data  = communities.get(ch_id)

    if data is None:
        await interaction.response.send_message("❌ This isn't a community channel.", ephemeral=True)
        return

    member = interaction.user
    if member.id != data["owner_id"] and not is_staff(member):
        await interaction.response.send_message("❌ Only the community owner can do this.", ephemeral=True)
        return

    data["open"] = False
    await interaction.response.send_message("🔒 Your community is now **closed**.", ephemeral=False)


@bot.tree.command(name="community-delete", description="Permanently delete your community channel.")
async def community_delete(interaction: discord.Interaction):
    ch_id = interaction.channel_id
    data  = communities.get(ch_id)

    if data is None:
        await interaction.response.send_message("❌ This isn't a community channel.", ephemeral=True)
        return

    member = interaction.user
    if member.id != data["owner_id"] and not is_staff(member):
        await interaction.response.send_message("❌ Only the community owner or staff can delete this.", ephemeral=True)
        return

    communities.pop(ch_id, None)
    channel = interaction.guild.get_channel(ch_id)
    if channel:
        await channel.delete(reason=f"Community deleted by {member}")
    # No response needed — channel is gone


# ══════════════════════════════════════════════════════════════════════════════
#  TICKET SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

class TicketCloseView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=None)
        self.close_btn.custom_id = f"ticket_close_{ticket_channel_id}"

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.red, custom_id="ticket_close_placeholder")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        # Only admins or the hard-coded server owner can close
        if not (member.guild_permissions.administrator or member.id == SERVER_OWNER_USER_ID):
            await interaction.response.send_message("❌ Only admins or the server owner can close tickets.", ephemeral=True)
            return

        ticket_data = open_tickets.get(interaction.channel_id)
        if not ticket_data:
            await interaction.response.send_message("❌ Ticket data not found.", ephemeral=True)
            return

        open_tickets.pop(interaction.channel_id, None)

        # Delete only the ticket channel, NOT the parent 🎫Ticket Requests 🚨 channel
        ch = interaction.guild.get_channel(interaction.channel_id)
        if ch:
            await interaction.response.send_message("🔒 Closing ticket...", ephemeral=False)
            import asyncio
            await asyncio.sleep(2)
            try:
                await ch.delete(reason=f"Ticket closed by {member}")
            except discord.NotFound:
                pass


async def get_or_create_ticket_category(guild: discord.Guild) -> discord.CategoryChannel:
    """Return the 🎫Ticket Requests 🚨 category, creating it if it doesn't exist."""
    name = "🎫Ticket Requests 🚨"
    for cat in guild.categories:
        if cat.name == name:
            return cat

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    for role in guild.roles:
        if role.permissions.administrator:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    return await guild.create_category(name=name, overwrites=overwrites, reason="Ticket system setup")


@bot.tree.command(name="ticket", description="Open a support ticket. Must be used in the tickets channel.")
@app_commands.describe(issue="Describe your issue or request")
async def ticket_cmd(interaction: discord.Interaction, issue: str):
    # Enforce channel restriction
    if interaction.channel_id != TICKET_CHANNEL_ID:
        ticket_ch = interaction.guild.get_channel(TICKET_CHANNEL_ID)
        mention   = ticket_ch.mention if ticket_ch else f"<#{TICKET_CHANNEL_ID}>"
        await interaction.response.send_message(
            f"❌ You can only open tickets in {mention}.", ephemeral=True
        )
        return

    member = interaction.user
    guild  = interaction.guild

    category = await get_or_create_ticket_category(guild)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        member:             discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    for role in guild.roles:
        if role.permissions.administrator:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    # Also give the hard-coded server owner explicit access
    owner_member = guild.get_member(SERVER_OWNER_USER_ID)
    if owner_member:
        overwrites[owner_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    safe_name = f"ticket-{member.display_name[:20].lower().replace(' ', '-')}-{member.discriminator}"
    try:
        ticket_ch = await category.create_text_channel(
            name=safe_name,
            overwrites=overwrites,
            reason=f"Ticket opened by {member}",
        )
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to create ticket channels.", ephemeral=True)
        return

    open_tickets[ticket_ch.id] = {"opener_id": member.id}

    close_view = TicketCloseView(ticket_ch.id)

    embed = discord.Embed(
        title="🎫 New Ticket",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Opened By", value=member.mention,  inline=True)
    embed.add_field(name="Issue",     value=issue,            inline=False)
    embed.set_footer(text="Only admins or the server owner can close this ticket.")

    await ticket_ch.send(
        content=f"{member.mention} — staff will be with you shortly.",
        embed=embed,
        view=close_view,
    )

    await interaction.response.send_message(
        f"✅ Your ticket has been created → {ticket_ch.mention}", ephemeral=True
    )


# ══════════════════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════════════════

bot.add_view(CaseButtons())
bot.run(TOKEN)
