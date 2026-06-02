# bot.py
import os
import discord
from discord.ext import commands, tasks
from discord import app_commands, Embed
from discord.ui import View, Button, Modal, TextInput
from datetime import datetime, timezone
import asyncio

# ---------------- CONFIG ----------------
MANAGER_ROLE_ID = 1511382570871558246

# Mapping: manager_role_id -> (team_role_id, country_name, flag_emoji)
TEAM_MANAGERS = {
    1511393975247044668: (1511393975247044668, "Italy", "🇮🇹"),
    1511394012219834459: (1511394012219834459, "France", "🇫🇷"),
    1511394035149836309: (1511394035149836309, "Argentina", "🇦🇷"),
    1511394088056914071: (1511394088056914071, "Germany", "🇩🇪"),
    1511394139755909252: (1511394139755909252, "England", "🇬🇧"),
    1511394338943533056: (1511394338943533056, "Brazil", "🇧🇷"),
    1511394424167338064: (1511394424167338064, "Mexico", "🇲🇽"),
    1511394201802248463: (1511394201802248463, "Spain", "🇪🇸"),
}

# Reverse mapping: team_role_id -> manager_role_id
TEAM_ROLE_TO_MANAGER_ROLE = {v[0]: k for k, v in TEAM_MANAGERS.items()}

# Public channels and guild
PUBLIC_GUILD_ID = 1511190077714464898
PUBLIC_CONTRACT_CHANNEL_ID = 1511198594147946658
FREEAGENTS_CHANNEL_ID = 1511198490431062097
SCOUTING_CHANNEL_ID = 1511198519946379315
SQUAD_CHANNEL_ID = 1511428965506748618

TEAM_CAPACITY = 14
OFFER_TIMEOUT = 24 * 60 * 60
TRANSFER_TIMEOUT = 24 * 60 * 60

# ----------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# In-memory tracking
pending_offers = {}      # dm_message_id -> {target_id, team_role_id, manager_id, expires_at}
pending_transfers = {}   # dm_message_id -> {player_id, from_team_role_id, to_manager_id, to_team_role_id, expires_at}
# hired is derived from actual guild roles; keep a small cache for quick checks (not persistent)
hired_cache = {v[0]: set() for v in TEAM_MANAGERS.values()}

# ---------------- Helpers ----------------

def is_main_manager(member: discord.Member) -> bool:
    return any(r.id == MANAGER_ROLE_ID for r in member.roles)

def get_manager_team(member: discord.Member):
    """Return (team_role_id, country, flag) if the member has a manager role for a team."""
    for mgr_role_id, (team_role_id, country, flag) in TEAM_MANAGERS.items():
        if any(r.id == mgr_role_id for r in member.roles):
            return team_role_id, country, flag
    return None

def find_player_team_role(guild: discord.Guild, member: discord.Member):
    """Return the team_role_id if the member belongs to one of the managed teams."""
    for _, (team_role_id, _, _) in TEAM_MANAGERS.items():
        role = guild.get_role(team_role_id)
        if role and role in member.roles:
            return team_role_id
    return None

def team_count(guild: discord.Guild, role_id: int) -> int:
    role = guild.get_role(role_id)
    if not role:
        return 0
    return sum(1 for m in role.members if not m.bot)

def cleanup_expired_pending():
    """Remove expired offers and transfers from memory."""
    now_ts = datetime.now(timezone.utc).timestamp()
    expired_offers = [k for k, v in pending_offers.items() if v.get("expires_at", 0) <= now_ts]
    for k in expired_offers:
        pending_offers.pop(k, None)
    expired_transfers = [k for k, v in pending_transfers.items() if v.get("expires_at", 0) <= now_ts]
    for k in expired_transfers:
        pending_transfers.pop(k, None)

# ---------------- Views ----------------

class OfferView(View):
    def __init__(self, guild: discord.Guild, team_role_id: int, manager: discord.Member, target: discord.Member):
        super().__init__(timeout=OFFER_TIMEOUT)
        self.guild = guild
        self.team_role_id = team_role_id
        self.manager = manager
        self.target = target
        self.message_obj = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message_obj:
            try:
                await self.message_obj.edit(view=self)
            except Exception:
                pass
        # cleanup pending_offers
        to_remove = [k for k, v in pending_offers.items()
                     if v["target_id"] == self.target.id and v["team_role_id"] == self.team_role_id]
        for k in to_remove:
            pending_offers.pop(k, None)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("Only the offer recipient can respond to this offer.", ephemeral=True)
            return

        role = self.guild.get_role(self.team_role_id)
        if not role:
            await interaction.response.send_message("Team role not found on the server.", ephemeral=True)
            return

        current = team_count(self.guild, self.team_role_id)
        if current >= TEAM_CAPACITY:
            await interaction.response.send_message("Team is full. Cannot accept the offer.", ephemeral=True)
            return

        try:
            await self.target.add_roles(role, reason=f"Accepted offer from {self.manager} (VPL)")
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to assign that role.", ephemeral=True)
            return

        hired_cache.setdefault(self.team_role_id, set()).add(self.target.id)

        for child in self.children:
            child.disabled = True
        if self.message_obj:
            try:
                await self.message_obj.edit(view=self)
            except Exception:
                pass

        await interaction.response.send_message("You accepted the offer. Congratulations.", ephemeral=True)

        # Public confirmation in contract channel
        try:
            channel = bot.get_channel(PUBLIC_CONTRACT_CHANNEL_ID)
            if channel:
                embed = Embed(
                    title=f"{self.target.display_name} accepted the offer from {self.manager.display_name}",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                embed.set_thumbnail(url=self.target.display_avatar.url if self.target.display_avatar else None)
                embed.add_field(name="Team", value=role.name, inline=True)
                embed.add_field(name="Hired", value=self.target.mention, inline=True)
                embed.add_field(name="Hired on", value=datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"), inline=False)
                embed.add_field(name="Team capacity", value=f"{team_count(self.guild, self.team_role_id)}/{TEAM_CAPACITY}", inline=False)
                await channel.send(content=f"{self.target.mention} accepted the offer from {self.manager.mention}", embed=embed)
        except Exception:
            pass

        # Update squad channel
        try:
            await post_squad(self.guild, self.team_role_id)
        except Exception:
            pass

        # cleanup pending_offers
        to_remove = [k for k, v in pending_offers.items()
                     if v["target_id"] == self.target.id and v["team_role_id"] == self.team_role_id]
        for k in to_remove:
            pending_offers.pop(k, None)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.target.id:
            await interaction.response.send_message("Only the offer recipient can respond to this offer.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        if self.message_obj:
            try:
                await self.message_obj.edit(view=self)
            except Exception:
                pass

        await interaction.response.send_message("You declined the offer.", ephemeral=True)

        to_remove = [k for k, v in pending_offers.items()
                     if v["target_id"] == self.target.id and v["team_role_id"] == self.team_role_id]
        for k in to_remove:
            pending_offers.pop(k, None)

class TransferDecisionView(View):
    def __init__(self, guild: discord.Guild, player: discord.Member, from_team_role_id: int, to_manager: discord.Member, to_team_role_id: int):
        super().__init__(timeout=TRANSFER_TIMEOUT)
        self.guild = guild
        self.player = player
        self.from_team_role_id = from_team_role_id
        self.to_manager = to_manager
        self.to_team_role_id = to_team_role_id
        self.message_obj = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message_obj:
            try:
                await self.message_obj.edit(view=self)
            except Exception:
                pass
        to_remove = [k for k, v in pending_transfers.items()
                     if v["player_id"] == self.player.id and v["from_team_role_id"] == self.from_team_role_id]
        for k in to_remove:
            pending_transfers.pop(k, None)

    @discord.ui.button(label="Accept Transfer", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: Button):
        member = interaction.user
        manager_role_id = TEAM_ROLE_TO_MANAGER_ROLE.get(self.from_team_role_id)
        if not manager_role_id or not any(r.id == manager_role_id for r in member.roles):
            await interaction.response.send_message("Only the manager of this player's team can accept the transfer.", ephemeral=True)
            return

        from_role = self.guild.get_role(self.from_team_role_id)
        to_role = self.guild.get_role(self.to_team_role_id)
        if not from_role or not to_role:
            await interaction.response.send_message("Team roles not found.", ephemeral=True)
            return

        try:
            await self.player.remove_roles(from_role, reason=f"Transferred to {self.to_manager} by manager decision")
            await self.player.add_roles(to_role, reason=f"Transferred to {self.to_manager} by manager decision")
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to change roles for this player.", ephemeral=True)
            return

        hired_cache.get(self.from_team_role_id, set()).discard(self.player.id)
        hired_cache.setdefault(self.to_team_role_id, set()).add(self.player.id)

        for child in self.children:
            child.disabled = True
        if self.message_obj:
            try:
                await self.message_obj.edit(view=self)
            except Exception:
                pass

        await interaction.response.send_message(f"Transfer accepted. {self.player.display_name} moved to {to_role.name}.", ephemeral=True)

        try:
            await self.to_manager.send(f"Your transfer request for {self.player.mention} was accepted by {member.mention}.")
        except Exception:
            pass
        try:
            await self.player.send(f"You have been transferred to {to_role.name}.")
        except Exception:
            pass

        try:
            channel = bot.get_channel(PUBLIC_CONTRACT_CHANNEL_ID)
            if channel:
                embed = Embed(
                    title=f"{self.player.display_name} transferred to {to_role.name}",
                    description=f"Transfer approved by {member.display_name}",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                embed.set_thumbnail(url=self.player.display_avatar.url if self.player.display_avatar else None)
                embed.add_field(name="From", value=from_role.name, inline=True)
                embed.add_field(name="To", value=to_role.name, inline=True)
                await channel.send(content=f"{self.player.mention} has been transferred to {self.to_manager.mention}", embed=embed)
        except Exception:
            pass

        try:
            await post_squad(self.guild, self.from_team_role_id)
            await post_squad(self.guild, self.to_team_role_id)
        except Exception:
            pass

        to_remove = [k for k, v in pending_transfers.items()
                     if v["player_id"] == self.player.id and v["from_team_role_id"] == self.from_team_role_id]
        for k in to_remove:
            pending_transfers.pop(k, None)

    @discord.ui.button(label="Decline Transfer", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: Button):
        member = interaction.user
        manager_role_id = TEAM_ROLE_TO_MANAGER_ROLE.get(self.from_team_role_id)
        if not manager_role_id or not any(r.id == manager_role_id for r in member.roles):
            await interaction.response.send_message("Only the manager of this player's team can decline the transfer.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        if self.message_obj:
            try:
                await self.message_obj.edit(view=self)
            except Exception:
                pass

        await interaction.response.send_message("Transfer request declined.", ephemeral=True)

        try:
            await self.to_manager.send(f"Your transfer request for {self.player.mention} was declined by {member.mention}.")
        except Exception:
            pass

        to_remove = [k for k, v in pending_transfers.items()
                     if v["player_id"] == self.player.id and v["from_team_role_id"] == self.from_team_role_id]
        for k in to_remove:
            pending_transfers.pop(k, None)

# ---------------- Free Agent Modal ----------------

class FreeAgentModal(Modal, title="Post to Free Agents"):
    message = TextInput(
        label="Your message",
        style=discord.TextStyle.paragraph,
        placeholder="Write what you want teams to know about you (skills, position, availability, links...)",
        required=True,
        max_length=1000
    )

    def __init__(self, author: discord.Member):
        super().__init__()
        self.author = author

    async def on_submit(self, interaction: discord.Interaction):
        embed = Embed(
            title="Free Agent Post",
            description=self.message.value,
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_author(name=self.author.display_name, icon_url=self.author.display_avatar.url if self.author.display_avatar else None)
        embed.set_thumbnail(url=self.author.display_avatar.url if self.author.display_avatar else None)
        embed.add_field(name="Posted by", value=self.author.mention, inline=True)
        embed.add_field(name="When", value=datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC"), inline=True)

        channel = bot.get_channel(FREEAGENTS_CHANNEL_ID)
        if channel:
            try:
                await channel.send(embed=embed)
                await interaction.response.send_message("Your free agent post was submitted.", ephemeral=True)
            except Exception:
                await interaction.response.send_message("Failed to post to the free agents channel.", ephemeral=True)
        else:
            await interaction.response.send_message("Free agents channel not found.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        try:
            await interaction.response.send_message("An error occurred while submitting your post.", ephemeral=True)
        except Exception:
            pass

# ---------------- Commands ----------------

@tree.command(name="contract", description="Send a contract offer to a player (managers only)")
@app_commands.describe(player="Player to send the offer to")
async def contract(interaction: discord.Interaction, player: discord.Member):
    await interaction.response.defer(ephemeral=True)
    author = interaction.user
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    member_obj = guild.get_member(author.id)
    if not member_obj or not is_main_manager(member_obj):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    team_info = get_manager_team(member_obj)
    if not team_info:
        await interaction.followup.send("You are not assigned to any team manager role.", ephemeral=True)
        return

    team_role_id, country_name, flag = team_info

    player_team_role_id = find_player_team_role(guild, player)
    if player_team_role_id:
        if player_team_role_id == team_role_id:
            await interaction.followup.send("This player is already in your team.", ephemeral=True)
            return
        else:
            await interaction.followup.send("This player is already contracted by another team. Use /transfer to request a transfer.", ephemeral=True)
            return

    try:
        dm = await player.create_dm()
    except Exception:
        await interaction.followup.send("Could not open DM with that user.", ephemeral=True)
        return

    embed = Embed(
        title=f"You received an offer from {country_name} (VPL)",
        description=f"Manager: {author.mention} {flag}",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=author.display_avatar.url if author.display_avatar else None)
    embed.add_field(name="Offer", value="You have 24 hours to accept or decline.", inline=False)

    view = OfferView(guild, team_role_id, guild.get_member(author.id), player)
    try:
        dm_msg = await dm.send(embed=embed, view=view)
        view.message_obj = dm_msg
    except Exception:
        await interaction.followup.send("Failed to send DM to the player.", ephemeral=True)
        return

    pending_offers[dm_msg.id] = {
        "target_id": player.id,
        "team_role_id": team_role_id,
        "manager_id": author.id,
        "expires_at": datetime.now(timezone.utc).timestamp() + OFFER_TIMEOUT
    }

    await interaction.followup.send(f"Offer sent to {player.mention}. It will expire in 24 hours.", ephemeral=True)

@tree.command(name="transfer", description="Request a transfer for a player contracted by another team (managers only)")
@app_commands.describe(player="Player to request transfer for")
async def transfer(interaction: discord.Interaction, player: discord.Member):
    """
    Manager uses /transfer <player>.
    The bot will DM the manager(s) of the player's current team asking to accept or decline the transfer.
    """
    await interaction.response.defer(ephemeral=True)
    requester = interaction.user
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    requester_member = guild.get_member(requester.id)
    if not requester_member or not is_main_manager(requester_member):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    requester_team_info = get_manager_team(requester_member)
    if not requester_team_info:
        await interaction.followup.send("You are not assigned to any team manager role.", ephemeral=True)
        return
    requester_team_role_id, requester_country, requester_flag = requester_team_info

    # Block if target is a manager or not contracted
    player_team_role_id = find_player_team_role(guild, player)
    if not player_team_role_id:
        await interaction.followup.send("This player is not contracted by any team. Use /contract instead.", ephemeral=True)
        return

    # Prevent requesting transfer for a manager account (someone who has a manager role)
    if any(r.id in TEAM_MANAGERS.keys() for r in player.roles):
        await interaction.followup.send("You cannot request a transfer for a manager account.", ephemeral=True)
        return

    if player_team_role_id == requester_team_role_id:
        await interaction.followup.send("This player is already in your team.", ephemeral=True)
        return

    manager_role_id = TEAM_ROLE_TO_MANAGER_ROLE.get(player_team_role_id)
    if not manager_role_id:
        await interaction.followup.send("Could not determine the manager for the player's team.", ephemeral=True)
        return

    # Find all members who have the manager role for the player's team
    managers = [m for m in guild.members if any(r.id == manager_role_id for r in m.roles)]
    if not managers:
        await interaction.followup.send("No manager found for the player's team to receive the transfer request.", ephemeral=True)
        return

    sent_any = False
    for mgr in managers:
        try:
            dm = await mgr.create_dm()
        except Exception:
            continue

        requester_team_role = guild.get_role(requester_team_role_id)
        requester_team_name = requester_team_role.name if requester_team_role else requester_country

        embed = Embed(
            title="Transfer Request",
            description=f"{requester.display_name} wants to sign your player {player.display_name}.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_thumbnail(url=player.display_avatar.url if player.display_avatar else None)
        embed.add_field(name="Player", value=player.mention, inline=True)
        embed.add_field(name="Requested by", value=requester.mention, inline=True)
        embed.add_field(name="Target team", value=requester_team_name, inline=True)
        embed.add_field(name="Note", value="You have 24 hours to accept or decline this transfer request.", inline=False)

        view = TransferDecisionView(guild, player, player_team_role_id, requester_member, requester_team_role_id)
        try:
            dm_msg = await dm.send(embed=embed, view=view)
            view.message_obj = dm_msg
            pending_transfers[dm_msg.id] = {
                "player_id": player.id,
                "from_team_role_id": player_team_role_id,
                "to_manager_id": requester.id,
                "to_team_role_id": requester_team_role_id,
                "expires_at": datetime.now(timezone.utc).timestamp() + TRANSFER_TIMEOUT
            }
            sent_any = True
        except Exception:
            continue

    if sent_any:
        await interaction.followup.send("Transfer request sent to the player's manager(s). They have 24 hours to respond.", ephemeral=True)
    else:
        await interaction.followup.send("Failed to send transfer request to the player's manager(s).", ephemeral=True)

@tree.command(name="release", description="Release a player from the team (players can self-release; managers can release their players)")
@app_commands.describe(player="Player to release from the team (managers only)")
async def release(interaction: discord.Interaction, player: discord.Member = None):
    """
    - If a manager runs /release <player>, they can only release players from their own team.
    - If a normal player runs /release (no argument), they release themselves from their current team.
    """
    await interaction.response.defer(ephemeral=True)
    author = interaction.user
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    author_member = guild.get_member(author.id)

    # Self-release (player)
    if player is None:
        player = author
        player_member = author_member
        player_team_role_id = find_player_team_role(guild, player_member)
        if not player_team_role_id:
            await interaction.followup.send("You are not part of any managed team.", ephemeral=True)
            return
        role = guild.get_role(player_team_role_id)
        try:
            await player_member.remove_roles(role, reason=f"Self-released by {author}")
            hired_cache.get(player_team_role_id, set()).discard(player.id)
            await interaction.followup.send(f"You have been released from {role.name}.", ephemeral=True)
            try:
                await post_squad(guild, player_team_role_id)
            except Exception:
                pass
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to remove that role.", ephemeral=True)
        return

    # Manager releasing someone else
    member_obj = guild.get_member(author.id)
    if not member_obj or not is_main_manager(member_obj):
        await interaction.followup.send("You do not have permission to use this command to release other players.", ephemeral=True)
        return

    manager_team_info = get_manager_team(member_obj)
    if not manager_team_info:
        await interaction.followup.send("You are not assigned to any team manager role.", ephemeral=True)
        return
    manager_team_role_id = manager_team_info[0]

    target_team_role_id = find_player_team_role(guild, player)
    if not target_team_role_id:
        await interaction.followup.send("That user is not part of any managed team.", ephemeral=True)
        return

    if target_team_role_id != manager_team_role_id:
        await interaction.followup.send("You can only release players from your own team.", ephemeral=True)
        return

    role = guild.get_role(target_team_role_id)
    try:
        await player.remove_roles(role, reason=f"Released by {author}")
        hired_cache.get(target_team_role_id, set()).discard(player.id)
        await interaction.followup.send(f"{player.mention} has been released from {role.name}.", ephemeral=True)
        try:
            await post_squad(guild, target_team_role_id)
        except Exception:
            pass
    except discord.Forbidden:
        await interaction.followup.send("I don't have permission to remove that role.", ephemeral=True)

@tree.command(name="team", description="View players hired for your team (managers only)")
async def team(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    author = interaction.user
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    member_obj = guild.get_member(author.id)
    if not member_obj or not is_main_manager(member_obj):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    team_info = get_manager_team(member_obj)
    if not team_info:
        await interaction.followup.send("You are not assigned to any team manager role.", ephemeral=True)
        return

    team_role_id, country_name, flag = team_info
    role = guild.get_role(team_role_id)
    if not role:
        await interaction.followup.send("Team role not found.", ephemeral=True)
        return

    members = [m for m in role.members if not m.bot]
    hired_count = len(members)
    embed = Embed(title=f"Team - {role.name}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Hired", value=str(hired_count), inline=True)
    embed.add_field(name="Capacity", value=f"{hired_count}/{TEAM_CAPACITY}", inline=True)
    if members:
        embed.add_field(name="Players", value="\n".join(m.mention for m in members), inline=False)
    else:
        embed.add_field(name="Players", value="None", inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="freeagents", description="Post a free agent message to the free agents channel (not for managers)")
async def freeagents(interaction: discord.Interaction):
    author = interaction.user
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return

    member_obj = guild.get_member(author.id)
    if member_obj and is_main_manager(member_obj):
        await interaction.response.send_message("Managers cannot use /freeagents.", ephemeral=True)
        return

    modal = FreeAgentModal(author=guild.get_member(author.id))
    await interaction.response.send_modal(modal)

@tree.command(name="scouting", description="Post a scouting message to the scouting channel (managers only)")
@app_commands.describe(message="Scouting message to post")
async def scouting(interaction: discord.Interaction, message: str):
    await interaction.response.defer(ephemeral=True)
    author = interaction.user
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    member_obj = guild.get_member(author.id)
    if not member_obj or not is_main_manager(member_obj):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    team_info = get_manager_team(member_obj)
    team_name = team_info[1] if team_info else "Unknown Team"

    embed = Embed(
        title="Scouting Report",
        description=message,
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_author(name=author.display_name, icon_url=author.display_avatar.url if author.display_avatar else None)
    embed.add_field(name="Team", value=team_name, inline=True)
    embed.add_field(name="Manager", value=author.mention, inline=True)

    channel = bot.get_channel(SCOUTING_CHANNEL_ID)
    if channel:
        try:
            await channel.send(embed=embed)
            await interaction.followup.send("Scouting message posted.", ephemeral=True)
        except Exception:
            await interaction.followup.send("Failed to post scouting message.", ephemeral=True)
    else:
        await interaction.followup.send("Scouting channel not found.", ephemeral=True)

@tree.command(name="squad", description="Post your current squad to the squad channel (managers only)")
async def squad(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    author = interaction.user
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return

    member_obj = guild.get_member(author.id)
    if not member_obj or not is_main_manager(member_obj):
        await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
        return

    team_info = get_manager_team(member_obj)
    if not team_info:
        await interaction.followup.send("You are not assigned to any team manager role.", ephemeral=True)
        return

    team_role_id, country_name, flag = team_info
    await post_squad(guild, team_role_id)
    await interaction.followup.send("Squad posted to the squad channel.", ephemeral=True)

# ---------------- Utility: post_squad & post_all_squads ----------------

async def post_squad(guild: discord.Guild, team_role_id: int):
    role = guild.get_role(team_role_id)
    if not role:
        return
    members = [m for m in role.members if not m.bot]
    hired_count = len(members)
    embed = Embed(
        title=f"Squad - {role.name}",
        color=discord.Color.teal(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(url=None)
    embed.add_field(name="Team capacity", value=f"{hired_count}/{TEAM_CAPACITY}", inline=True)
    if members:
        embed.add_field(name="Players", value="\n".join(f"{m.mention}" for m in members), inline=False)
    else:
        embed.add_field(name="Players", value="None", inline=False)

    channel = bot.get_channel(SQUAD_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)

async def post_all_squads():
    guild = bot.get_guild(PUBLIC_GUILD_ID)
    if not guild:
        return
    for _, (team_role_id, country, flag) in TEAM_MANAGERS.items():
        try:
            await post_squad(guild, team_role_id)
            await asyncio.sleep(0.5)
        except Exception:
            pass

# ---------------- Background cleanup task ----------------

@tasks.loop(minutes=10)
async def cleanup_task():
    cleanup_expired_pending()

@cleanup_task.before_loop
async def before_cleanup():
    await bot.wait_until_ready()

# ---------------- Events ----------------

@bot.event
async def on_ready():
    try:
        await tree.sync()
    except Exception:
        pass
    print(f"Bot online as {bot.user} (ID: {bot.user.id})")
    # Post squads for all teams on startup/update
    try:
        await post_all_squads()
    except Exception:
        pass
    if not cleanup_task.is_running():
        cleanup_task.start()

# ---------------- Run ----------------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("ERROR: Set the DISCORD_TOKEN environment variable before running the bot.")
    else:
        bot.run(token)
