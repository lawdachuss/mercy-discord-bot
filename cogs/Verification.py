import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json
import os
from typing import Optional
from datetime import datetime, timezone
import io

class VerificationTicketSystem(commands.Cog):
    """
    A cog for handling female identity verification tickets with full customization.
    """
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config_file = "database/verification_config.json"
        self.ensure_database_folder()
        self.config = self.load_config()
        
    def ensure_database_folder(self):
        """Ensure database folder exists"""
        os.makedirs("database", exist_ok=True)
    
    def load_config(self):
        """Load configuration from JSON file"""
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    
    def save_config(self):
        """Save configuration to JSON file"""
        with open(self.config_file, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=4)
    
    def get_server_config(self, guild_id: int):
        """Get configuration for a specific server"""
        return self.config.get(str(guild_id), {})
    
    def set_server_config(self, guild_id: int, key: str, value):
        """Set configuration for a specific server"""
        guild_id_str = str(guild_id)
        if guild_id_str not in self.config:
            self.config[guild_id_str] = {}
        self.config[guild_id_str][key] = value
        self.save_config()
        
    def get_verification_example_path(self):
        """Get the path to the verification example image"""
        return os.path.join("assets", "verification_example.png")
    
    async def cog_load(self):
        """Add persistent views when cog loads"""
        self.bot.add_view(VerifyButton(self))
        self.bot.add_view(TicketControls(self))
        self.bot.add_view(TranscriptControls(self))
    
    @app_commands.command(name="setup-verify", description="Setup the verification ticket system")
    @app_commands.describe(
        embed_channel="Channel where the verification embed will be sent",
        ticket_category="Category where verification tickets will be created",
        verified_role="Role to assign after successful verification",
        staff_role="Role that can manage tickets (approve/decline/delete)",
        log_channel="Channel where transcripts will be sent",
        ticket_message="Custom message shown when ticket opens (use {user} for user mention)",
        decline_message="Custom DM sent when verification is declined (use {server} for server name, {reason} for decline reason)"
    )
    @app_commands.default_permissions(administrator=True)
    async def setup_verify(
        self,
        interaction: discord.Interaction,
        embed_channel: discord.TextChannel,
        ticket_category: discord.CategoryChannel,
        verified_role: discord.Role,
        staff_role: discord.Role,
        log_channel: discord.TextChannel,
        ticket_message: Optional[str] = "Welcome {user}! Please upload your verification selfie following the instructions below.",
        decline_message: Optional[str] = "Your verification in {server} has been declined.\n\n**Reason:** {reason}\n\nPlease open a new ticket with a proper verification image that meets all requirements."
    ):
        """Setup command to configure the entire verification system"""
        
        guild_id = interaction.guild.id
        
        # Save configuration
        self.set_server_config(guild_id, 'embed_channel_id', embed_channel.id)
        self.set_server_config(guild_id, 'ticket_category_id', ticket_category.id)
        self.set_server_config(guild_id, 'verified_role_id', verified_role.id)
        self.set_server_config(guild_id, 'staff_role_id', staff_role.id)
        self.set_server_config(guild_id, 'log_channel_id', log_channel.id)
        self.set_server_config(guild_id, 'ticket_message', ticket_message)
        self.set_server_config(guild_id, 'decline_message', decline_message)
        
        # Create and send the verification embed
        banner_embed = discord.Embed(color=0x2F3136)
        banner_embed.set_image(url="https://cdn.discordapp.com/attachments/1422976219917582396/1422990800593227776/Copy_of_Copy_of_Copy_of_MOD_1.gif")
        
        main_embed = discord.Embed(
            description=(
                "**──・The Originals Selfie Verification Tickets! ♡  ̖́-**\n\n"
                "**<a:originals_purple:1422973620648546466> __Verification Tickets__**\n"
                "**<a:originals_blue:1422973611400102029> __Follow the steps below__**"
            ),
            color=0x2F3136
        )
        
        main_embed.add_field(
            name="ㅤ",
            value=(
                "**<a:emoji_9:1421723513043161210> R__ead before opening a Ticket__**\n\n"
                "<:imported_good:1420254860711956510> Write down your `Discord Tag` on paper\n"
                "<:imported_good:1420254860711956510> Do not type your Discord Tag in the `Chat`\n"
                "<:imported_good:1420254860711956510> Write down `The Originals` as well\n"
                "<:imported_good:1420254860711956510> Snap a `Selfie` with your note\n"
                "<:imported_good:1420254860711956510> `Open a Ticket` and submit it\n"
                "<:imported_good:1420254860711956510> Get your `verified role`"
            ),
            inline=False
        )
        
        # Check if verification example exists
        example_path = self.get_verification_example_path()
        if os.path.exists(example_path):
            example_text = "**<a:originals_yellow:1422974161994649670> Example Here: [Click Me](attachment://verification_example.png)**"
        else:
            example_text = "**<a:originals_yellow:1422974161994649670> Example Here: [Click Me](https://cdn.discordapp.com/attachments/1405225685596901526/1424429041028567110/ChatGPT_Image_Oct_1_2025_08_56_11_PM.png?ex=68e3ea6b&is=68e298eb&hm=a6e4352dbc7b4f9faf44041adab92379bdeff9de3d2df842bce42c33f8ed55b3&)**"
        
        main_embed.add_field(
            name="ㅤ",
            value=example_text,
            inline=False
        )
        
        main_embed.set_footer(
            text="Invite your friends to The Originals! discord.gg/originals",
            icon_url="https://images-ext-1.discordapp.net/external/gE7ec4SNyGE7uNClt_KeHox_HCN0bSYACxqrtP8NOMs/https/cdn.discordapp.com/emojis/1420116428606148659.png"
        )
        
        main_embed.set_image(url="https://images-ext-1.discordapp.net/external/2AyKRHIS4qHrCYNN0HmKXsrM3oEIrlsOeSOBQ9OTzl8/https/64.media.tumblr.com/9288a05b854e436bef70d8e196e3b86c/bf13a1241e747191-88/s2048x3072/7a5d1988023581452e99bb7e625883fc99710f96.pnj")
        
        # Send embeds with persistent button and verification example
        view = VerifyButton(self)
        example_path = self.get_verification_example_path()
        
        if os.path.exists(example_path):
            # Send with local verification example attached
            with open(example_path, 'rb') as f:
                example_file = discord.File(f, filename="verification_example.png")
                await embed_channel.send(embeds=[banner_embed, main_embed], view=view, file=example_file)
        else:
            # Send without attachment if file doesn't exist
            await embed_channel.send(embeds=[banner_embed, main_embed], view=view)
        
        # Confirmation message
        await interaction.response.send_message(
            f"<a:sukoon_whitetick:1323992464058482729> **Verification System Setup Complete!**\n\n"
            f"<:sukoon_hom:1333443376946745493> **Embed Channel:** {embed_channel.mention}\n"
            f"<:GlacierTicketSupportEmojiForBo:1424440770232057906> **Ticket Category:** {ticket_category.mention}\n"
            f"<:sukoon_btfl:1335856043477041204> **Verified Role:** {verified_role.mention}\n"
            f"<:Owner_Crow:1375731826093461544> **Staff Role:** {staff_role.mention}\n"
            f"<:sukoon_info:1323251063910043659> **Log Channel:** {log_channel.mention}",
            ephemeral=True
        )

class VerifyButton(discord.ui.View):
    """Persistent view with the verify button"""
    
    def __init__(self, cog: VerificationTicketSystem):
        super().__init__(timeout=None)
        self.cog = cog
    
    @discord.ui.button(
        label="Verify Me",
        style=discord.ButtonStyle.gray,
        emoji=discord.PartialEmoji(name="zd_cc", id=1420245871127302205),
        custom_id="verify_me_button"
    )
    async def verify_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        """Handle verify button click - creates ticket channel"""
        
        guild = interaction.guild
        guild_id = guild.id
        config = self.cog.get_server_config(guild_id)
        
        if not config:
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Verification system not configured! Ask an administrator to run `/setup-verify`.",
                ephemeral=True
            )
            return
        
        # Check if user already has a ticket open using topic search
        for channel in guild.text_channels:
            if channel.topic and f"TICKET_OWNER:{interaction.user.id}" in channel.topic:
                await interaction.response.send_message(
                    f"<a:sukoon_crossss:1323992622955626557> You already have a verification ticket open at {channel.mention}!",
                    ephemeral=True
                )
                return
        
        # Check if user already has verified role
        verified_role_id = config.get('verified_role_id')
        verified_role = guild.get_role(verified_role_id) if verified_role_id else None
        
        if verified_role and verified_role in interaction.user.roles:
            await interaction.response.send_message(
                "<a:sukoon_whitetick:1323992464058482729> You are already verified!",
                ephemeral=True
            )
            return
        
        # Get ticket category
        ticket_category_id = config.get('ticket_category_id')
        ticket_category = guild.get_channel(ticket_category_id) if ticket_category_id else None
        
        if not ticket_category:
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Ticket category not found! Contact an administrator.",
                ephemeral=True
            )
            return
        
        # Get staff role
        staff_role_id = config.get('staff_role_id')
        staff_role = guild.get_role(staff_role_id) if staff_role_id else None
        
        try:
            # Copy category overwrites to inherit permissions
            category_overwrites = ticket_category.overwrites.copy()
            
            # Add user permission to see their ticket
            category_overwrites[interaction.user] = discord.PermissionOverwrite(
                view_channel=True,
                read_message_history=True,
                send_messages=True,
                attach_files=True,
                embed_links=True
            )
            
            # Create ticket channel - NAME USES USERNAME, TOPIC STORES USER ID
            ticket_channel = await guild.create_text_channel(
                name=f"verification-{interaction.user.name}",
                category=ticket_category,
                overwrites=category_overwrites,
                topic=f"TICKET_OWNER:{interaction.user.id}",  # Store user ID in topic for backend
                reason=f"Verification ticket for {interaction.user}"
            )
            
            # Send custom ticket message with instructions
            ticket_message = config.get('ticket_message', 'Welcome {user}!')
            ticket_message = ticket_message.replace('{user}', interaction.user.mention)
            
            ticket_embed = discord.Embed(
                title="<:GlacierTicketSupportEmojiForBo:1424440770232057906> Verification Ticket",
                description=ticket_message,
                color=0x2F3136
            )
            
            ticket_embed.add_field(
                name="<:sukoon_inf:1323250061643350126> Verification Instructions",
                value=(
                    "**Please follow these steps carefully:**\n\n"
                    "**<:1_n0:1424441235963121746>** Write your Discord Tag on paper\n"
                    "**<:Jd_2_whit:1424441496018354378>** Write **'The Originals'** on the same paper\n"
                    "**<:ebd_3_whit:1424441593188057232>** Take a clear selfie showing:\n"
                    "   • Your face clearly visible\n"
                    "   • The paper with both written items\n"
                    "   • Everything must be readable\n"
                    "**<:Jd_4_whit:1424441662985470012>** Upload the photo in this ticket\n"
                    "**<:ebd_:1424441738449129513>** Wait for staff to review\n\n"
                    "<:sukoon_question_mar:1332583902380032071> **Do NOT type your Discord Tag in chat**\n"
                    "<:sukoon_question_mar:1332583902380032071> **Everything must be written on paper**"
                ),
                inline=False
            )
            
            # Check if verification example exists
            example_path = self.cog.get_verification_example_path()
            if os.path.exists(example_path):
                example_link = "[Click here to see an example](attachment://verification_example.png)"
            else:
                example_link = "[Click here to see an example](https://cdn.discordapp.com/attachments/1405225685596901526/1424429041028567110/ChatGPT_Image_Oct_1_2025_08_56_11_PM.png?ex=68e3ea6b&is=68e298eb&hm=a6e4352dbc7b4f9faf44041adab92379bdeff9de3d2df842bce42c33f8ed55b3&)"
            
            ticket_embed.add_field(
                name="📸 Example Reference",
                value=example_link,
                inline=False
            )
            
            ticket_embed.set_footer(text=f"Ticket opened by {interaction.user.name}")
            
            # Ping staff role
            staff_ping = staff_role.mention if staff_role else "Staff"
            view = TicketControls(self.cog)
            
            # Send ticket message with verification example
            example_path = self.cog.get_verification_example_path()
            if os.path.exists(example_path):
                with open(example_path, 'rb') as f:
                    example_file = discord.File(f, filename="verification_example.png")
                    await ticket_channel.send(
                        content=f"{staff_ping} New verification ticket from {interaction.user.mention}",
                        embed=ticket_embed,
                        view=view,
                        file=example_file
                    )
            else:
                await ticket_channel.send(
                    content=f"{staff_ping} New verification ticket from {interaction.user.mention}",
                    embed=ticket_embed,
                    view=view
                )
            
            # Send log to log channel - TICKET OPENED
            log_channel_id = config.get('log_channel_id')
            log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
            
            if log_channel:
                opened_log_embed = discord.Embed(
                    title="<:GlacierTicketSupportEmojiForBo:1424440770232057906> Ticket Opened",
                    description=f"**User:** {interaction.user.mention} (`{interaction.user.name}`)\n**Ticket:** {ticket_channel.mention}",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                await log_channel.send(embed=opened_log_embed)
            
            await interaction.response.send_message(
                f"<a:sukoon_whitetick:1323992464058482729> Verification ticket created! Go to {ticket_channel.mention}",
                ephemeral=True
            )
            
        except discord.Forbidden:
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> I don't have permission to create channels!",
                ephemeral=True
            )
        except Exception as e:
            await interaction.response.send_message(
                f"<a:sukoon_crossss:1323992622955626557> Error: {str(e)}",
                ephemeral=True
            )

class DeclineModal(discord.ui.Modal, title="Decline Verification"):
    """Modal for staff to provide decline reason"""
    
    reason = discord.ui.TextInput(
        label="Reason for Decline",
        placeholder="Why is this verification being declined?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )
    
    def __init__(self, cog, member, staff_member):
        super().__init__()
        self.cog = cog
        self.member = member
        self.staff_member = staff_member
    
    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        guild = interaction.guild
        config = self.cog.get_server_config(guild.id)
        
        # Send decline message in DM as EMBED with banner
        decline_message = config.get('decline_message', 'Your verification has been declined.')
        decline_message = decline_message.replace('{server}', guild.name)
        decline_message = decline_message.replace('{reason}', self.reason.value)
        
        decline_dm_embed = discord.Embed(
            title="<a:sukoon_crossss:1323992622955626557> Verification Declined",
            description=decline_message,
            color=discord.Color.red()
        )
        decline_dm_embed.set_image(url="https://cdn.discordapp.com/attachments/1422976219917582396/1422990800593227776/Copy_of_Copy_of_Copy_of_MOD_1.gif")
        
        try:
            await self.member.send(embed=decline_dm_embed)
            dm_status = "<a:sukoon_whitetick:1323992464058482729> DM sent successfully"
        except:
            dm_status = "<a:sukoon_crossss:1323992622955626557> Could not send DM (user has DMs disabled)"
        
        # Send EMBED ONLY in ticket
        ticket_decline_embed = discord.Embed(
            title="<a:sukoon_crossss:1323992622955626557> Verification Declined",
            description=f"**Reason:** {self.reason.value}\n\n{dm_status}",
            color=discord.Color.red()
        )
        
        await interaction.response.send_message(embed=ticket_decline_embed)
        
        # Send SEPARATE log for decline to log channel
        log_channel_id = config.get('log_channel_id')
        log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
        
        if log_channel:
            decline_log_embed = discord.Embed(
                title="<a:sukoon_crossss:1323992622955626557> Verification Declined",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            decline_log_embed.add_field(
                name="👤 User",
                value=f"{self.member.mention} (`{self.member.name}`)",
                inline=False
            )
            decline_log_embed.add_field(
                name="👮 Declined By",
                value=f"{self.staff_member.mention} (`{self.staff_member.name}`)",
                inline=False
            )
            decline_log_embed.add_field(
                name="📝 Reason",
                value=self.reason.value,
                inline=False
            )
            decline_log_embed.add_field(
                name="📅 Date",
                value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:F>",
                inline=False
            )
            await log_channel.send(embed=decline_log_embed)
        
        # Show transcript/delete buttons
        view = TranscriptControls(self.cog)
        await interaction.channel.send(view=view)

class TicketControls(discord.ui.View):
    """Persistent view with approve/deny buttons"""
    
    def __init__(self, cog: VerificationTicketSystem):
        super().__init__(timeout=None)
        self.cog = cog
    
    def check_staff_permission(self, interaction: discord.Interaction) -> bool:
        """Check if user has staff permissions or is administrator"""
        # Check if user is administrator
        if interaction.user.guild_permissions.administrator:
            return True
        
        # Check if user has staff role
        config = self.cog.get_server_config(interaction.guild.id)
        staff_role_id = config.get('staff_role_id')
        if not staff_role_id:
            return False
        user_role_ids = [role.id for role in interaction.user.roles]
        return staff_role_id in user_role_ids
    
    def get_ticket_owner_id(self, channel: discord.TextChannel) -> int:
        """Extract user ID from channel topic"""
        try:
            if channel.topic and "TICKET_OWNER:" in channel.topic:
                return int(channel.topic.split("TICKET_OWNER:")[1].split()[0])
            return None
        except:
            return None
    
    @discord.ui.button(
        label="Approve",
        style=discord.ButtonStyle.green,
        emoji="<:sukoon_tick:1322894604898664478>",
        custom_id="approve_verification_button"
    )
    async def approve_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        """Approve verification and assign verified role"""
        
        if not self.check_staff_permission(interaction):
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Only staff members can approve verifications!",
                ephemeral=True
            )
            return
        
        if not interaction.channel.name.startswith("verification-"):
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> This can only be used in verification tickets!",
                ephemeral=True
            )
            return
        
        guild = interaction.guild
        config = self.cog.get_server_config(guild.id)
        
        # Get user ID from channel topic
        user_id = self.get_ticket_owner_id(interaction.channel)
        if not user_id:
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Could not extract user ID from channel topic!",
                ephemeral=True
            )
            return
        
        member = guild.get_member(user_id)
        
        if not member:
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Could not find the user! They may have left the server.",
                ephemeral=True
            )
            return
        
        # Assign verified role
        verified_role_id = config.get('verified_role_id')
        verified_role = guild.get_role(verified_role_id) if verified_role_id else None
        
        if not verified_role:
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Verified role not found!",
                ephemeral=True
            )
            return
        
        try:
            await member.add_roles(verified_role, reason=f"Approved by {interaction.user}")
            
            # EMBED ONLY
            success_embed = discord.Embed(
                title="<a:sukoon_whitetick:1323992464058482729> Verification Approved",
                description=f"{member.mention} has been verified!\n\n**Approved by:** {interaction.user.mention}",
                color=discord.Color.green()
            )
            
            await interaction.response.send_message(embed=success_embed)
            
            # Send success DM as EMBED with banner
            try:
                approval_dm_embed = discord.Embed(
                    title="<a:sukoon_whitetick:1323992464058482729> Verification Approved!",
                    description=(
                        f"**Congratulations!**\n\n"
                        f"Your verification in **{guild.name}** has been approved!\n\n"
                        f"You now have access to all verified channels.\n"
                        f"Welcome to The Originals! 💜"
                    ),
                    color=discord.Color.green()
                )
                approval_dm_embed.set_image(url="https://cdn.discordapp.com/attachments/1422976219917582396/1422990800593227776/Copy_of_Copy_of_Copy_of_MOD_1.gif")
                await member.send(embed=approval_dm_embed)
            except:
                pass
            
            # Send SEPARATE log for approval to log channel
            log_channel_id = config.get('log_channel_id')
            log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
            
            if log_channel:
                approval_log_embed = discord.Embed(
                    title="<a:sukoon_whitetick:1323992464058482729> Verification Approved",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc)
                )
                approval_log_embed.add_field(
                    name="👤 User",
                    value=f"{member.mention} (`{member.name}`)",
                    inline=False
                )
                approval_log_embed.add_field(
                    name="👮 Approved By",
                    value=f"{interaction.user.mention} (`{interaction.user.name}`)",
                    inline=False
                )
                approval_log_embed.add_field(
                    name="🎭 Role Assigned",
                    value=f"{verified_role.mention}",
                    inline=False
                )
                approval_log_embed.add_field(
                    name="📅 Date",
                    value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:F>",
                    inline=False
                )
                await log_channel.send(embed=approval_log_embed)
            
            # Show transcript/delete buttons
            view = TranscriptControls(self.cog)
            await interaction.channel.send(view=view)
            
        except Exception as e:
            await interaction.response.send_message(
                f"<a:sukoon_crossss:1323992622955626557> Error: {str(e)}",
                ephemeral=True
            )
    
    @discord.ui.button(
        label="Decline",
        style=discord.ButtonStyle.red,
        emoji="<:sukoon_cross:1322894630684983307>",
        custom_id="decline_verification_button"
    )
    async def decline_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        """Decline verification - opens modal for reason"""
        
        if not self.check_staff_permission(interaction):
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Only staff members can decline verifications!",
                ephemeral=True
            )
            return
        
        if not interaction.channel.name.startswith("verification-"):
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> This can only be used in verification tickets!",
                ephemeral=True
            )
            return
        
        guild = interaction.guild
        
        # Get user ID from channel topic
        user_id = self.get_ticket_owner_id(interaction.channel)
        if not user_id:
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Could not extract user ID from channel topic!",
                ephemeral=True
            )
            return
        
        member = guild.get_member(user_id)
        
        if not member:
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Could not find the user! They may have left the server.",
                ephemeral=True
            )
            return
        
        # Open modal for decline reason
        modal = DeclineModal(self.cog, member, interaction.user)
        await interaction.response.send_modal(modal)

class TranscriptControls(discord.ui.View):
    """View with transcript and delete buttons"""
    
    def __init__(self, cog: VerificationTicketSystem):
        super().__init__(timeout=None)
        self.cog = cog
    
    def check_staff_permission(self, interaction: discord.Interaction) -> bool:
        """Check if user has staff permissions or is administrator"""
        # Check if user is administrator
        if interaction.user.guild_permissions.administrator:
            return True
        
        # Check if user has staff role
        config = self.cog.get_server_config(interaction.guild.id)
        staff_role_id = config.get('staff_role_id')
        if not staff_role_id:
            return False
        user_role_ids = [role.id for role in interaction.user.roles]
        return staff_role_id in user_role_ids
    
    def get_ticket_owner_id(self, channel: discord.TextChannel) -> int:
        """Extract user ID from channel topic"""
        try:
            if channel.topic and "TICKET_OWNER:" in channel.topic:
                return int(channel.topic.split("TICKET_OWNER:")[1].split()[0])
            return None
        except:
            return None
    
    @discord.ui.button(
        label="Transcript & Delete",
        style=discord.ButtonStyle.blurple,
        emoji="<:GlacierTicketSupportEmojiForBo:1424440770232057906>",
        custom_id="transcript_delete_button"
    )
    async def transcript_delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        """Create transcript and delete ticket"""
        
        if not self.check_staff_permission(interaction):
            await interaction.response.send_message(
                "<a:sukoon_crossss:1323992622955626557> Only staff members can delete tickets!",
                ephemeral=True
            )
            return
        
        await interaction.response.defer()
        
        guild = interaction.guild
        config = self.cog.get_server_config(guild.id)
        log_channel_id = config.get('log_channel_id')
        log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
        
        if not log_channel:
            await interaction.followup.send(
                "<a:sukoon_crossss:1323992622955626557> Log channel not found!",
                ephemeral=True
            )
            return
        
        try:
            # Extract user ID from channel topic
            user_id = self.get_ticket_owner_id(interaction.channel)
            if not user_id:
                await interaction.followup.send(
                    "<a:sukoon_crossss:1323992622955626557> Could not extract user ID from channel topic!",
                    ephemeral=True
                )
                return
                
            ticket_opener = guild.get_member(user_id)
            
            # Generate transcript
            transcript_lines = []
            transcript_lines.append("=" * 80)
            transcript_lines.append(f"VERIFICATION TICKET TRANSCRIPT")
            transcript_lines.append(f"Channel: #{interaction.channel.name}")
            transcript_lines.append(f"Ticket Opened By: {ticket_opener.name if ticket_opener else 'Unknown'} (ID: {user_id})")
            transcript_lines.append(f"Ticket Closed By: {interaction.user.name} (ID: {interaction.user.id})")
            transcript_lines.append(f"Closed At: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
            transcript_lines.append("=" * 80)
            transcript_lines.append("")
            
            # Fetch all messages
            messages = []
            async for message in interaction.channel.history(limit=None, oldest_first=True):
                messages.append(message)
            
            # Format messages
            for message in messages:
                timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S")
                author = f"{message.author.name} (ID: {message.author.id})"
                
                transcript_lines.append(f"[{timestamp}] {author}")
                
                if message.content:
                    transcript_lines.append(f"  Content: {message.content}")
                
                if message.attachments:
                    transcript_lines.append(f"  Attachments:")
                    for att in message.attachments:
                        transcript_lines.append(f"    - {att.filename}: {att.url}")
                
                if message.embeds:
                    transcript_lines.append(f"  Embeds: {len(message.embeds)} embed(s)")
                
                transcript_lines.append("")
            
            transcript_lines.append("=" * 80)
            transcript_lines.append("END OF TRANSCRIPT")
            
            transcript_text = "\n".join(transcript_lines)
            
            # Create file
            transcript_file = discord.File(
                io.BytesIO(transcript_text.encode('utf-8')),
                filename=f"transcript-{interaction.channel.name}.txt"
            )
            
            # Send log - TICKET CLOSED AND TRANSCRIPTED
            log_embed = discord.Embed(
                title="🔒 Ticket Closed & Transcripted",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            
            log_embed.add_field(
                name="📝 Ticket Information",
                value=(
                    f"**Channel:** {interaction.channel.name}\n"
                    f"**User:** {ticket_opener.mention if ticket_opener else 'Unknown'} (`{ticket_opener.name if ticket_opener else 'Unknown'}`)\n"
                    f"**Total Messages:** {len(messages)}"
                ),
                inline=False
            )
            
            log_embed.add_field(
                name="🔒 Closed By",
                value=f"{interaction.user.mention} (`{interaction.user.name}`)",
                inline=False
            )
            
            await log_channel.send(embed=log_embed, file=transcript_file)
            
            # Delete ticket
            await interaction.followup.send("<a:sukoon_whitetick:1323992464058482729> Transcript saved! Deleting ticket in 3 seconds...")
            await asyncio.sleep(3)
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
            
        except Exception as e:
            await interaction.followup.send(
                f"<a:sukoon_crossss:1323992622955626557> Error: {str(e)}",
                ephemeral=True
            )

async def setup(bot: commands.Bot):
    """Required function to load the cog"""
    await bot.add_cog(VerificationTicketSystem(bot))