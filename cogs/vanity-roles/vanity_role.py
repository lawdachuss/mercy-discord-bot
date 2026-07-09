"""
Status Vanity Roles - Advanced Multi-Pattern Implementation
A production-ready Discord.py v2.x cog for automatic status-based vanity role assignment.
Features: Multiple patterns, role stacking, analytics, bulk management, and more.
"""

import logging
import asyncio
import os
import re
import hashlib
import time
import unicodedata
import json
from typing import Optional, Dict, Any, List, Set, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from enum import Enum
import uuid

import discord
from discord.ext import commands, tasks
from discord import app_commands
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import ConnectionFailure, OperationFailure

# Configure logging
logger = logging.getLogger(__name__)


class ConflictResolution(Enum):
    """Conflict resolution strategies for mutually exclusive roles."""
    NONE = "none"  # No conflict resolution
    PRIORITY = "priority"  # Higher priority wins
    FIRST_MATCH = "first_match"  # First matching rule wins
    LAST_MATCH = "last_match"  # Last matching rule wins


@dataclass
class StatusRule:
    """Individual status rule configuration."""
    rule_id: str
    name: str
    trigger_pattern: str
    is_regex: bool
    case_sensitive: bool
    word_boundary: bool
    exclude_pattern: Optional[str]
    exclude_is_regex: bool
    target_role_id: int
    priority: int
    enabled: bool
    temporary: bool
    duration_hours: Optional[int]
    time_restricted: bool
    start_hour: Optional[int]
    end_hour: Optional[int]
    user_specific: bool
    allowed_users: List[int]
    created_at: datetime
    updated_at: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MongoDB storage."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'StatusRule':
        """Create instance from dictionary."""
        if isinstance(data.get('created_at'), str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if isinstance(data.get('updated_at'), str):
            data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        return cls(**data)


@dataclass
class GuildConfig:
    """Configuration for a guild's status-based vanity role system."""
    guild_id: int
    log_channel_id: int
    rules: List[StatusRule]
    conflict_resolution: ConflictResolution
    mutual_exclusions: List[List[int]]  # Lists of role IDs that are mutually exclusive
    rate_limit_per_user: int  # Max role changes per user per hour
    analytics_enabled: bool
    bulk_processing: bool
    batch_size: int
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MongoDB storage."""
        data = asdict(self)
        data['_id'] = f"guild_{self.guild_id}"
        data['conflict_resolution'] = self.conflict_resolution.value
        data['rules'] = [rule.to_dict() for rule in self.rules]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GuildConfig':
        """Create instance from MongoDB document."""
        data.pop('_id', None)
        if isinstance(data.get('created_at'), str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if isinstance(data.get('updated_at'), str):
            data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        
        # Convert conflict resolution
        data['conflict_resolution'] = ConflictResolution(data.get('conflict_resolution', 'none'))
        
        # Convert rules
        rules_data = data.get('rules', [])
        data['rules'] = [StatusRule.from_dict(rule_data) for rule_data in rules_data]
        
        return cls(**data)


@dataclass
class UserRoleAssignment:
    """Track user role assignments for temporary roles and analytics."""
    user_id: int
    guild_id: int
    role_id: int
    rule_id: str
    assigned_at: datetime
    expires_at: Optional[datetime]
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MongoDB storage."""
        data = asdict(self)
        data['_id'] = f"{self.user_id}_{self.guild_id}_{self.role_id}"
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'UserRoleAssignment':
        """Create instance from dictionary."""
        data.pop('_id', None)
        if isinstance(data.get('assigned_at'), str):
            data['assigned_at'] = datetime.fromisoformat(data['assigned_at'])
        if isinstance(data.get('expires_at'), str):
            data['expires_at'] = datetime.fromisoformat(data['expires_at'])
        return cls(**data)


@dataclass
class AnalyticsData:
    """Analytics data for pattern usage and performance."""
    guild_id: int
    rule_id: str
    pattern: str
    matches_today: int
    matches_week: int
    matches_month: int
    unique_users_today: Set[int]
    unique_users_week: Set[int]
    unique_users_month: Set[int]
    last_match: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MongoDB storage."""
        data = asdict(self)
        data['_id'] = f"{self.guild_id}_{self.rule_id}"
        data['unique_users_today'] = list(self.unique_users_today)
        data['unique_users_week'] = list(self.unique_users_week)
        data['unique_users_month'] = list(self.unique_users_month)
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AnalyticsData':
        """Create instance from dictionary."""
        data.pop('_id', None)
        if isinstance(data.get('last_match'), str):
            data['last_match'] = datetime.fromisoformat(data['last_match'])
        if isinstance(data.get('created_at'), str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if isinstance(data.get('updated_at'), str):
            data['updated_at'] = datetime.fromisoformat(data['updated_at'])
        
        # Convert sets
        data['unique_users_today'] = set(data.get('unique_users_today', []))
        data['unique_users_week'] = set(data.get('unique_users_week', []))
        data['unique_users_month'] = set(data.get('unique_users_month', []))
        
        return cls(**data)


class StatusRoleSetupModal(discord.ui.Modal, title='Create Vanity Role Rule'):
    """Modal for setting up individual status rules."""
    
    def __init__(self, cog: 'StatusRoleCog', rule: Optional[StatusRule] = None):
        super().__init__()
        self.cog = cog
        self.editing_rule = rule
        
        # Pre-fill if editing
        if rule:
            self.title = f'Edit Rule: {rule.name}'
            self.rule_name.default = rule.name
            self.trigger_pattern.default = rule.trigger_pattern
            self.is_regex.default = 'yes' if rule.is_regex else 'no'
            self.role_id.default = str(rule.target_role_id)
    
    rule_name = discord.ui.TextInput(
        label='Rule Name',
        placeholder='e.g., "Streaming Role", "Gaming Role"',
        required=True,
        max_length=50
    )
    
    trigger_pattern = discord.ui.TextInput(
        label='Trigger Pattern',
        placeholder='e.g., "streaming", "playing", "watching"',
        required=True,
        max_length=200
    )
    
    role_id = discord.ui.TextInput(
        label='Target Role ID',
        placeholder='Right-click role → Copy ID (enable Developer Mode)',
        required=True,
        max_length=20
    )
    
    is_regex = discord.ui.TextInput(
        label='Use Regex? (yes/no)',
        placeholder='Type "yes" for regex pattern, "no" for simple text match',
        required=True,
        max_length=3,
        default='no'
    )
    
    log_channel_id = discord.ui.TextInput(
        label='Log Channel ID',
        placeholder='Right-click channel → Copy ID (for activity logs)',
        required=True,
        max_length=20
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            # Parse log channel ID
            try:
                log_channel_id = int(self.log_channel_id.value)
            except ValueError:
                await interaction.followup.send("❌ Log Channel ID must be a valid number!", ephemeral=True)
                return
            
            # Validate log channel
            log_channel = self.cog.bot.get_channel(log_channel_id)
            if not log_channel or log_channel.guild.id != interaction.guild.id:
                await interaction.followup.send("❌ Log channel not found or not in this server!", ephemeral=True)
                return
            
            bot_member = interaction.guild.get_member(self.cog.bot.user.id)
            if not log_channel.permissions_for(bot_member).send_messages:
                await interaction.followup.send(f"❌ Bot lacks permission to send messages in {log_channel.mention}!", ephemeral=True)
                return
            
            # Validation
            is_regex_bool = self.is_regex.value.lower() in ['yes', 'y', 'true', '1']
            
            try:
                role_id = int(self.role_id.value)
            except ValueError:
                await interaction.followup.send("❌ Role ID must be a valid number!", ephemeral=True)
                return
            
            # Validate regex if applicable
            if is_regex_bool:
                try:
                    re.compile(self.trigger_pattern.value, re.IGNORECASE)
                except re.error as e:
                    await interaction.followup.send(f"❌ Invalid regex pattern: {e}", ephemeral=True)
                    return
            
            # Validate role exists
            role = interaction.guild.get_role(role_id)
            if not role:
                await interaction.followup.send("❌ Role not found! Make sure the Role ID is correct.", ephemeral=True)
                return
            
            # Check bot permissions
            if not bot_member or not bot_member.guild_permissions.manage_roles:
                await interaction.followup.send("❌ Bot lacks 'Manage Roles' permission!", ephemeral=True)
                return
            
            if bot_member.top_role.position <= role.position:
                await interaction.followup.send(f"❌ Bot's role must be higher than {role.name} in the role hierarchy!", ephemeral=True)
                return
            
            # Get or create guild config
            config = await self.cog.get_config(interaction.guild.id)
            if not config:
                # Create new config with the log channel
                config = GuildConfig(
                    guild_id=interaction.guild.id,
                    log_channel_id=log_channel_id,
                    rules=[],
                    conflict_resolution=ConflictResolution.PRIORITY,
                    mutual_exclusions=[],
                    rate_limit_per_user=10,
                    analytics_enabled=True,
                    bulk_processing=True,
                    batch_size=50,
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )
            else:
                # Update log channel if it changed
                config.log_channel_id = log_channel_id
            
            # Create or update rule
            if self.editing_rule:
                # Update existing rule
                for i, rule in enumerate(config.rules):
                    if rule.rule_id == self.editing_rule.rule_id:
                        config.rules[i].name = self.rule_name.value
                        config.rules[i].trigger_pattern = self.trigger_pattern.value
                        config.rules[i].is_regex = is_regex_bool
                        config.rules[i].target_role_id = role_id
                        config.rules[i].updated_at = datetime.utcnow()
                        break
                action = "updated"
            else:
                # Create new rule
                new_rule = StatusRule(
                    rule_id=str(uuid.uuid4()),
                    name=self.rule_name.value,
                    trigger_pattern=self.trigger_pattern.value,
                    is_regex=is_regex_bool,
                    case_sensitive=False,
                    word_boundary=False,
                    exclude_pattern=None,
                    exclude_is_regex=False,
                    target_role_id=role_id,
                    priority=len(config.rules) + 1,
                    enabled=True,
                    temporary=False,
                    duration_hours=None,
                    time_restricted=False,
                    start_hour=None,
                    end_hour=None,
                    user_specific=False,
                    allowed_users=[],
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow()
                )
                config.rules.append(new_rule)
                action = "created"
            
            config.updated_at = datetime.utcnow()
            
            # Save configuration
            success = await self.cog.save_config(config)
            if not success:
                await interaction.followup.send("❌ Failed to save configuration to database!", ephemeral=True)
                return
            
            # Success response
            embed = discord.Embed(
                title=f"✅ Rule {action.title()}!",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            
            embed.add_field(name="📝 Rule Name", value=f"`{self.rule_name.value}`", inline=True)
            embed.add_field(name="🔍 Pattern", value=f"`{self.trigger_pattern.value}`", inline=True)
            embed.add_field(name="📋 Type", value="Regex" if is_regex_bool else "Text Match", inline=True)
            embed.add_field(name="🎭 Role", value=f"{role.mention}", inline=True)
            embed.add_field(name="📢 Log Channel", value=f"{log_channel.mention}", inline=True)
            
            embed.add_field(
                name="💡 How it works",
                value=f"When a user's status contains `{self.trigger_pattern.value}`, they'll get the {role.mention} role!",
                inline=False
            )
            
            view = StatusRoleManagementView(self.cog)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error in rule setup modal: {e}")
            await interaction.followup.send(f"❌ An error occurred: {str(e)[:1000]}", ephemeral=True)


class StatusRoleManagementView(discord.ui.View):
    """View with buttons for managing the status role system."""
    
    def __init__(self, cog: 'StatusRoleCog'):
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="Add Rule", style=discord.ButtonStyle.primary, emoji="➕")
    async def add_rule(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Add a new status rule."""
        modal = StatusRoleSetupModal(self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="View Rules", style=discord.ButtonStyle.secondary, emoji="📋")
    async def view_rules(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show all configured rules."""
        await interaction.response.defer(ephemeral=True)
        
        config = await self.cog.get_config(interaction.guild.id)
        
        if not config or not config.rules:
            embed = discord.Embed(
                title="📋 Status Rules",
                color=discord.Color.orange(),
                description="❌ **No Rules Configured**\n\nNo status rules found for this server."
            )
            embed.add_field(
                name="💡 Next Steps",
                value="Use the 'Add Rule' button to create your first rule.",
                inline=False
            )
        else:
            embed = discord.Embed(
                title="📋 Status Rules",
                color=discord.Color.blue(),
                description=f"Found {len(config.rules)} rule(s) configured:",
                timestamp=discord.utils.utcnow()
            )
            
            for i, rule in enumerate(config.rules[:10], 1):  # Limit to 10 rules for display
                role = interaction.guild.get_role(rule.target_role_id)
                status_icon = "✅" if rule.enabled else "❌"
                
                rule_info = (
                    f"**Pattern:** `{rule.trigger_pattern}`\n"
                    f"**Type:** {'Regex' if rule.is_regex else 'Text'}\n"
                    f"**Role:** {role.mention if role else '❌ Not Found'}\n"
                    f"**Priority:** {rule.priority}"
                )
                
                if rule.temporary:
                    rule_info += f"\n**Duration:** {rule.duration_hours}h"
                
                embed.add_field(
                    name=f"{status_icon} {i}. {rule.name}",
                    value=rule_info,
                    inline=True
                )
            
            if len(config.rules) > 10:
                embed.add_field(
                    name="📊 More Rules",
                    value=f"... and {len(config.rules) - 10} more rules",
                    inline=False
                )
        
        view = RuleManagementView(self.cog, config)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Analytics", style=discord.ButtonStyle.secondary, emoji="📊")
    async def view_analytics(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show analytics dashboard."""
        await interaction.response.defer(ephemeral=True)
        
        config = await self.cog.get_config(interaction.guild.id)
        if not config or not config.analytics_enabled:
            await interaction.followup.send("❌ Analytics not enabled for this server.", ephemeral=True)
            return
        
        # Get analytics data
        analytics = await self.cog.get_analytics_summary(interaction.guild.id)
        
        embed = discord.Embed(
            title="📊 Status Role Analytics",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        
        if not analytics:
            embed.description = "No analytics data available yet."
        else:
            total_matches_today = sum(data.matches_today for data in analytics)
            total_matches_week = sum(data.matches_week for data in analytics)
            total_unique_users = len(set().union(*[data.unique_users_today for data in analytics]))
            
            embed.add_field(
                name="📈 Today's Activity",
                value=f"**Matches:** {total_matches_today:,}\n**Unique Users:** {total_unique_users:,}",
                inline=True
            )
            
            embed.add_field(
                name="📅 This Week",
                value=f"**Total Matches:** {total_matches_week:,}",
                inline=True
            )
            
            # Top patterns
            top_patterns = sorted(analytics, key=lambda x: x.matches_today, reverse=True)[:5]
            if top_patterns:
                pattern_text = "\n".join([
                    f"`{data.pattern[:20]}...` - {data.matches_today} matches"
                    if len(data.pattern) > 20 else f"`{data.pattern}` - {data.matches_today} matches"
                    for data in top_patterns
                ])
                embed.add_field(
                    name="🔥 Top Patterns Today",
                    value=pattern_text,
                    inline=False
                )
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Bulk Actions", style=discord.ButtonStyle.secondary, emoji="⚡")
    async def bulk_actions(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show bulk management options."""
        embed = discord.Embed(
            title="⚡ Bulk Actions",
            color=discord.Color.orange(),
            description="Choose a bulk action to perform:"
        )
        
        view = BulkActionsView(self.cog)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Settings", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Show system settings."""
        await interaction.response.defer(ephemeral=True)
        
        config = await self.cog.get_config(interaction.guild.id)
        if not config:
            await interaction.followup.send("❌ No configuration found.", ephemeral=True)
            return
        
        log_channel = self.cog.bot.get_channel(config.log_channel_id)
        
        embed = discord.Embed(
            title="⚙️ System Settings",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        
        embed.add_field(
            name="📋 Log Channel",
            value=f"{log_channel.mention if log_channel else '❌ Not Found'}",
            inline=True
        )
        
        embed.add_field(
            name="🔄 Conflict Resolution",
            value=config.conflict_resolution.value.replace('_', ' ').title(),
            inline=True
        )
        
        embed.add_field(
            name="📊 Analytics",
            value="✅ Enabled" if config.analytics_enabled else "❌ Disabled",
            inline=True
        )
        
        embed.add_field(
            name="⚡ Bulk Processing",
            value=f"{'✅' if config.bulk_processing else '❌'} (Batch: {config.batch_size})",
            inline=True
        )
        
        embed.add_field(
            name="🚦 Rate Limit",
            value=f"{config.rate_limit_per_user} changes/hour per user",
            inline=True
        )
        
        embed.add_field(
            name="📈 Statistics",
            value=f"**Rules:** {len(config.rules)}\n**Exclusions:** {len(config.mutual_exclusions)}",
            inline=True
        )
        
        view = SettingsView(self.cog, config)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Reset System", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def reset_system(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Reset the entire system."""
        config = await self.cog.get_config(interaction.guild.id)
        if not config:
            await interaction.response.send_message("❌ No configuration found to reset.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="⚠️ Confirm System Reset",
            color=discord.Color.red(),
            description=(
                "**This action will:**\n"
                "• Delete ALL status role rules and configuration\n"
                "• Remove ALL status roles from members\n"
                "• Clear ALL analytics data\n"
                "• Disable the entire system\n\n"
                "**This action cannot be undone!**"
            )
        )
        
        # Count affected members
        total_affected = 0
        for rule in config.rules:
            role = interaction.guild.get_role(rule.target_role_id)
            if role:
                total_affected += len([m for m in interaction.guild.members if role in m.roles])
        
        embed.add_field(
            name="📊 Impact",
            value=(
                f"**Rules:** {len(config.rules)}\n"
                f"**Members affected:** ~{total_affected:,}"
            ),
            inline=False
        )
        
        view = ResetConfirmationView(self.cog, interaction.user, config)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ResetConfirmationView(discord.ui.View):
    """Confirmation view for reset operation."""
    
    def __init__(self, cog: 'StatusRoleCog', user: discord.Member, config: GuildConfig):
        super().__init__(timeout=60.0)
        self.cog = cog
        self.user = user
        self.config = config

    @discord.ui.button(label="Confirm Reset", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "❌ Only the user who initiated this command can confirm the reset.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        
        removed_count = 0
        role_ids = {r.target_role_id for r in (self.config.rules or [])}
        for role_id in role_ids:
            role = interaction.guild.get_role(role_id)
            if not role:
                continue

            members_with_role = [m for m in interaction.guild.members if role in m.roles]
            for member in members_with_role:
                try:
                    await member.remove_roles(role, reason="Status Vanity Role system reset")
                    removed_count += 1
                except Exception:
                    pass
        
        # Delete configuration
        try:
            await self.cog.collection.delete_one({"guild_id": interaction.guild.id})
            
            embed = discord.Embed(
                title="✅ System Reset Complete",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )
            
            embed.add_field(
                name="📊 Reset Results",
                value=(
                    f"**Roles Removed:** `{removed_count:,}`\n"
                    f"**Config Deleted:** `Yes`"
                ),
                inline=False
            )
            
            embed.add_field(
                name="✅ Complete",
                value="The vanity role system has been completely reset and is now inactive.",
                inline=False
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
            # Log to channel if possible
            if self.config.log_channel_id:
                log_channel = self.cog.bot.get_channel(self.config.log_channel_id)
                if log_channel:
                    log_embed = discord.Embed(
                        title="🗑️ System Reset",
                        color=discord.Color.red(),
                        timestamp=discord.utils.utcnow()
                    )
                    log_embed.add_field(name="👤 Administrator", value=f"{interaction.user.mention}", inline=True)
                    log_embed.add_field(name="📊 Roles Removed", value=f"`{removed_count:,}`", inline=True)
                    await log_channel.send(embed=log_embed)
            
        except Exception as e:
            logger.error(f"Error during reset: {e}")
            await interaction.followup.send(f"❌ Error during reset: {str(e)[:1000]}", ephemeral=True)
        
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message(
                "❌ Only the user who initiated this command can cancel.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="❌ Reset Cancelled",
            color=discord.Color.green(),
            description="The system reset has been cancelled. No changes were made."
        )
        
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()

    async def on_timeout(self):
        """Handle view timeout."""
        try:
            embed = discord.Embed(
                title="⏰ Reset Timeout",
                color=discord.Color.orange(),
                description="The reset confirmation has timed out. No changes were made."
            )
            # Note: We can't edit the message here as we don't have the interaction
        except:
            pass


class RuleManagementView(discord.ui.View):
    """View for managing rules."""
    
    def __init__(self, cog: 'StatusRoleCog', config: Optional[GuildConfig] = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.config = config

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go back to main menu."""
        embed = discord.Embed(
            title="⚙️ Status Vanity Roles",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.description = "Manage your server's status-based vanity role rules."
        view = StatusRoleManagementView(self.cog)
        await interaction.response.edit_message(embed=embed, view=view)


class BulkActionsView(discord.ui.View):
    """View for bulk actions."""
    
    def __init__(self, cog: 'StatusRoleCog'):
        super().__init__(timeout=300)
        self.cog = cog

    @discord.ui.button(label="Scan All Members", style=discord.ButtonStyle.primary, emoji="🔍")
    async def scan_members(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Scan all members and apply rules."""
        await interaction.response.defer(ephemeral=True)
        
        config = await self.cog.get_config(interaction.guild.id)
        if not config or not config.rules:
            await interaction.followup.send("❌ No rules configured.", ephemeral=True)
            return
        
        processed, updated = await self.cog.bulk_scan_members(interaction.guild, config)
        
        embed = discord.Embed(
            title="✅ Bulk Scan Complete",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="👥 Members Processed", value=f"`{processed}`", inline=True)
        embed.add_field(name="🔄 Roles Updated", value=f"`{updated}`", inline=True)
        
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Remove All Roles", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def remove_all_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Remove all vanity roles from all members."""
        embed = discord.Embed(
            title="⚠️ Confirm Bulk Role Removal",
            color=discord.Color.red(),
            description="This will remove all status vanity roles from all members. This action cannot be undone."
        )
        
        view = BulkRemovalConfirmView(self.cog)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class BulkRemovalConfirmView(discord.ui.View):
    """Confirmation view for bulk role removal."""
    
    def __init__(self, cog: 'StatusRoleCog'):
        super().__init__(timeout=60.0)
        self.cog = cog

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm bulk removal."""
        await interaction.response.defer(ephemeral=True)
        
        config = await self.cog.get_config(interaction.guild.id)
        if not config:
            await interaction.followup.send("❌ Configuration not found.", ephemeral=True)
            return
        
        removed_count = 0
        role_ids = {r.target_role_id for r in (config.rules or [])}
        
        for role_id in role_ids:
            role = interaction.guild.get_role(role_id)
            if not role:
                continue
            
            members_with_role = [m for m in interaction.guild.members if role in m.roles]
            for member in members_with_role:
                try:
                    await member.remove_roles(role, reason="Bulk removal via admin command")
                    removed_count += 1
                except Exception:
                    pass
        
        embed = discord.Embed(
            title="✅ Bulk Removal Complete",
            color=discord.Color.green(),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="🗑️ Roles Removed", value=f"`{removed_count}`", inline=False)
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel bulk removal."""
        embed = discord.Embed(
            title="❌ Cancelled",
            color=discord.Color.green(),
            description="Bulk removal has been cancelled."
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


class SettingsView(discord.ui.View):
    """View for managing settings."""
    
    def __init__(self, cog: 'StatusRoleCog', config: GuildConfig):
        super().__init__(timeout=300)
        self.cog = cog
        self.config = config

    @discord.ui.button(label="Change Log Channel", style=discord.ButtonStyle.primary, emoji="📝")
    async def change_log_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Change the log channel."""
        await interaction.response.send_modal(ChangeLogChannelModal(self.cog, self.config))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="⬅️")
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Go back to main menu."""
        embed = discord.Embed(
            title="⚙️ Status Vanity Roles",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.description = "Manage your server's status-based vanity role rules."
        view = StatusRoleManagementView(self.cog)
        await interaction.response.edit_message(embed=embed, view=view)


class ChangeLogChannelModal(discord.ui.Modal, title='Change Log Channel'):
    """Modal for changing the log channel."""
    
    def __init__(self, cog: 'StatusRoleCog', config: GuildConfig):
        super().__init__()
        self.cog = cog
        self.config = config
    
    log_channel_id = discord.ui.TextInput(
        label='New Log Channel ID',
        placeholder='Right-click channel → Copy ID',
        required=True,
        max_length=20
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            try:
                log_channel_id = int(self.log_channel_id.value)
            except ValueError:
                await interaction.followup.send("❌ Log Channel ID must be a valid number!", ephemeral=True)
                return
            
            log_channel = self.cog.bot.get_channel(log_channel_id)
            if not log_channel or log_channel.guild.id != interaction.guild.id:
                await interaction.followup.send("❌ Log channel not found or not in this server!", ephemeral=True)
                return
            
            bot_member = interaction.guild.get_member(self.cog.bot.user.id)
            if not log_channel.permissions_for(bot_member).send_messages:
                await interaction.followup.send(f"❌ Bot lacks permission to send messages in {log_channel.mention}!", ephemeral=True)
                return
            
            self.config.log_channel_id = log_channel_id
            self.config.updated_at = datetime.utcnow()
            
            success = await self.cog.save_config(self.config)
            if not success:
                await interaction.followup.send("❌ Failed to save configuration!", ephemeral=True)
                return
            
            embed = discord.Embed(
                title="✅ Log Channel Updated",
                color=discord.Color.green(),
                description=f"Log channel changed to {log_channel.mention}",
                timestamp=discord.utils.utcnow()
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error changing log channel: {e}")
            await interaction.followup.send(f"❌ An error occurred: {str(e)[:1000]}", ephemeral=True)


class DeleteRuleConfirmView(discord.ui.View):
    """Confirmation view for rule deletion."""
    
    def __init__(self, cog: 'StatusRoleCog', rule: StatusRule):
        super().__init__(timeout=60.0)
        self.cog = cog
        self.rule = rule

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Confirm rule deletion."""
        await interaction.response.defer(ephemeral=True)
        
        config = await self.cog.get_config(interaction.guild.id)
        if not config:
            await interaction.followup.send("❌ Configuration not found.", ephemeral=True)
            return
        
        # Remove rule
        config.rules = [r for r in config.rules if r.rule_id != self.rule.rule_id]
        config.updated_at = datetime.utcnow()
        
        success = await self.cog.save_config(config)
        if success:
            embed = discord.Embed(
                title="✅ Rule Deleted",
                color=discord.Color.green(),
                description=f"Rule '{self.rule.name}' has been deleted.",
                timestamp=discord.utils.utcnow()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to delete rule.", ephemeral=True)
        
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel rule deletion."""
        embed = discord.Embed(
            title="❌ Deletion Cancelled",
            color=discord.Color.green(),
            description="Rule deletion has been cancelled."
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.stop()


class IndividualRuleView(discord.ui.View):
    """View for managing an individual rule."""
    
    def __init__(self, cog: 'StatusRoleCog', rule: StatusRule):
        super().__init__(timeout=300)
        self.cog = cog
        self.rule = rule

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary, emoji="✏️")
    async def edit_rule(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Edit the rule."""
        modal = StatusRoleSetupModal(self.cog, self.rule)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Toggle", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def toggle_rule(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Toggle rule enabled/disabled."""
        await interaction.response.defer(ephemeral=True)
        
        config = await self.cog.get_config(interaction.guild.id)
        if not config:
            await interaction.followup.send("❌ Configuration not found.", ephemeral=True)
            return
        
        # Update rule
        updated_enabled: Optional[bool] = None
        for rule in config.rules:
            if rule.rule_id == self.rule.rule_id:
                rule.enabled = not rule.enabled
                rule.updated_at = datetime.utcnow()
                updated_enabled = rule.enabled
                break
        
        config.updated_at = datetime.utcnow()
        success = await self.cog.save_config(config)
        
        if success:
            if updated_enabled is not None:
                self.rule.enabled = updated_enabled
            status = "enabled" if self.rule.enabled else "disabled"
            await interaction.followup.send(f"✅ Rule '{self.rule.name}' {status}.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to update rule.", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_rule(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Delete the rule."""
        embed = discord.Embed(
            title="⚠️ Confirm Rule Deletion",
            color=discord.Color.red(),
            description=f"Are you sure you want to delete the rule **{self.rule.name}**?\n\nThis action cannot be undone."
        )
        
        view = DeleteRuleConfirmView(self.cog, self.rule)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class StatusRoleCog(commands.Cog):
    """Main cog for the Status Vanity Roles system."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mongo_client: Optional[AsyncIOMotorClient] = None
        self.database = None
        self.collection = None
        self._ready = False
        self._cache: Dict[Tuple[int, int], str] = {}  # (guild_id, user_id) -> status_hash for debouncing
        self._cache_timestamps: Dict[Tuple[int, int], float] = {}  # (guild_id, user_id) -> timestamp
        self.messages = self._load_messages()
        
        # Color management for random colors without repetition
        self._available_colors = self._generate_color_palette()
        self._used_colors = []
        
        # Task management
        self._reconnect_tasks: Dict[int, asyncio.Task] = {}
        self._startup_scan_completed: Set[int] = set()
        
        logger.info("StatusRoleCog initialized")

    def _generate_color_palette(self) -> List[int]:
        """Generate a diverse palette of colors for embeds."""
        colors = [
            0xFF6B6B,  # Red
            0x4ECDC4,  # Teal
            0x45B7D1,  # Blue
            0x96CEB4,  # Green
            0xFECA57,  # Yellow
            0xFF9FF3,  # Pink
            0x54A0FF,  # Light Blue
            0x5F27CD,  # Purple
            0x00D2D3,  # Cyan
            0xFF9F43,  # Orange
            0x1DD1A1,  # Mint
            0xFD79A8,  # Rose
            0x6C5CE7,  # Violet
            0xA29BFE,  # Lavender
            0xFD63C3,  # Magenta
            0x00B894,  # Emerald
            0xE17055,  # Coral
            0x81ECEC,  # Aqua
            0xFAB1A0,  # Peach
            0x74B9FF,  # Sky Blue
            0x55A3FF,  # Dodger Blue
            0xFF7675,  # Light Red
            0xFD79A8,  # Hot Pink
            0x00CEC9,  # Robin Blue
            0xE84393,  # Deep Pink
            0x2D3436,  # Dark Gray
            0x636E72,  # Gray
            0xDDD,     # Light Gray
            0x00B894,  # Persian Green
            0xE17055,  # Burnt Orange
            0x0984E3,  # Royal Blue
            0x6C5CE7,  # Blue Violet
            0xA29BFE,  # Medium Slate Blue
            0xFD79A8,  # Pale Violet Red
            0x00CEC9,  # Dark Turquoise
            0xE84393,  # Medium Violet Red
            0xFF7675,  # Light Coral
            0x74B9FF,  # Light Sky Blue
            0x55A3FF,  # Cornflower Blue
            0x81ECEC,  # Pale Turquoise
            0xFAB1A0,  # Dark Salmon
            0x00D2D3,  # Dark Cyan
            0xFF9F43,  # Dark Orange
            0x1DD1A1,  # Medium Spring Green
            0x5F27CD,  # Dark Slate Blue
            0x54A0FF,  # Deep Sky Blue
            0xFF9FF3,  # Plum
            0x96CEB4,  # Dark Sea Green
            0xFECA57,  # Gold
            0x4ECDC4,  # Medium Turquoise
            0x45B7D1,  # Steel Blue
        ]
        return colors

    def _get_random_color(self) -> int:
        """Get a random color that hasn't been used recently."""
        import random
        
        # If we've used all colors, reset the used colors list
        if len(self._used_colors) >= len(self._available_colors):
            self._used_colors = []
        
        # Get available colors (not recently used)
        available = [color for color in self._available_colors if color not in self._used_colors]
        
        # If somehow no colors are available, reset and use all
        if not available:
            available = self._available_colors
            self._used_colors = []
        
        # Pick a random color
        chosen_color = random.choice(available)
        
        # Add to used colors
        self._used_colors.append(chosen_color)
        
        # Keep only the last 20 used colors to prevent too much repetition
        if len(self._used_colors) > 20:
            self._used_colors = self._used_colors[-20:]
        
        return chosen_color

    def _load_messages(self) -> Dict[str, Any]:
        """Load messages from the messages.json file."""
        try:
            messages_file_path = os.path.join(os.path.dirname(__file__), 'messages.json')
            with open(messages_file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading messages: {e}")
            # Fallback messages if file loading fails
            return {
                "role_added": {
                    "messages": [
                        "We're really happy to have you here, {user} Your status matched perfectly, so you've been given the {role} role",
                        "It's great seeing your support, {user} Your status fits the vibe, and the {role} role is now yours",
                        "Seeing that made us smile, {user} Your status matched and you've received the {role} role",
                        "Thanks for being part of the vibe, {user} The {role} role has been added to you",
                        "We love that energy, {user} Your status matched and the {role} role is now active"
                    ]
                },
                "role_removed": {
                    "messages": [
                        "We noticed a change, {user} Your status no longer matches, so the {role} role was removed for now",
                        "All good, {user} Since your status changed, the {role} role has been taken off",
                        "Just a small update, {user} Your status changed, so we removed the {role} role for the moment",
                        "Nothing wrong at all, {user} Your status no longer matches, so the {role} role is gone for now",
                        "We'll be right here, {user} Your {role} role was removed due to a status update"
                    ]
                },
                "fallback": {
                    "added": "{user} received the {role} role",
                    "removed": "{user} lost the {role} role"
                }
            }

    async def setup_hook(self) -> None:
        """Setup hook called when the cog is loaded."""
        try:
            logger.info("StatusRoleCog setup starting...")
            
            # Connect to MongoDB
            mongo_url = os.getenv('MONGO_URL')
            if not mongo_url:
                logger.error("MONGO_URL environment variable not found")
                return
            
            self.mongo_client = AsyncIOMotorClient(mongo_url)
            await self.mongo_client.admin.command('ping')
            
            self.database = self.mongo_client.poison_bot
            self.collection = self.database.status_vanity_roles
            
            # Create index
            await self.collection.create_index("guild_id", unique=True)
            
            self._ready = True
            logger.info("StatusRoleCog setup completed successfully")
            
        except Exception as e:
            logger.error(f"Error during StatusRoleCog setup: {e}")
            self._ready = False

    async def save_config(self, config: GuildConfig) -> bool:
        """Save guild configuration to MongoDB."""
        if not self._ready or self.collection is None:
            return False
        
        try:
            document = config.to_dict()
            result = await self.collection.replace_one(
                {"guild_id": config.guild_id},
                document,
                upsert=True
            )
            return result.acknowledged
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            return False

    async def get_config(self, guild_id: int) -> Optional[GuildConfig]:
        """Get guild configuration from MongoDB."""
        if not self._ready or self.collection is None:
            return None
        
        try:
            document = await self.collection.find_one({"guild_id": guild_id})
            if document:
                return GuildConfig.from_dict(document)
            return None
        except Exception as e:
            logger.error(f"Error getting config: {e}")
            return None

    @tasks.loop(hours=1)
    async def periodic_maintenance(self):
        """Periodic maintenance task for cleanup and optimization."""
        if not self._ready or self.collection is None:
            return
        
        try:
            logger.info("Running periodic maintenance...")
            
            # Clean up expired temporary role assignments
            current_time = datetime.utcnow()
            
            # Clean up old cache entries
            current_timestamp = time.time()
            expired_keys = [k for k, ts in self._cache_timestamps.items() if current_timestamp - ts > 3600]  # 1 hour
            for k in expired_keys:
                self._cache.pop(k, None)
                self._cache_timestamps.pop(k, None)
            
            if expired_keys:
                logger.info(f"Cleaned up {len(expired_keys)} expired cache entries")
            
            # Reset color usage periodically to ensure variety
            if len(self._used_colors) > 30:
                self._used_colors = self._used_colors[-10:]  # Keep only last 10
                logger.info("Reset color usage cache for variety")
            
            logger.info("Periodic maintenance completed")
            
        except Exception as e:
            logger.error(f"Error during periodic maintenance: {e}")

    @periodic_maintenance.before_loop
    async def before_periodic_maintenance(self):
        """Wait for the bot to be ready before starting periodic maintenance."""
        await self.bot.wait_until_ready()
        # Wait a bit more to ensure everything is properly initialized
        await asyncio.sleep(60)

    async def bulk_scan_members(self, guild: discord.Guild, config: GuildConfig) -> Tuple[int, int]:
        """Scan all members in a guild and apply status role rules."""
        if not config.rules:
            return 0, 0
        
        processed = 0
        updated = 0
        
        try:
            # Process members in batches to avoid rate limits
            batch_size = config.batch_size if config.bulk_processing else 10
            members = list(guild.members)
            enabled_rules = [r for r in config.rules if r.enabled]
            
            if not enabled_rules:
                return 0, 0
            
            for i in range(0, len(members), batch_size):
                batch = members[i:i + batch_size]
                
                for member in batch:
                    if member.bot:
                        continue
                    
                    processed += 1
                    
                    # Extract custom status
                    custom_status = self.extract_custom_status(member.activities)
                    
                    # Find matching rules
                    matching_rules: List[StatusRule] = []
                    if custom_status:
                        for rule in enabled_rules:
                            if self.matches_pattern(custom_status, rule.trigger_pattern, rule.is_regex):
                                matching_rules.append(rule)
                    
                    # Determine desired roles using conflict resolution
                    desired_role_ids: Set[int] = set()
                    if config.conflict_resolution == ConflictResolution.NONE:
                        desired_role_ids = {r.target_role_id for r in matching_rules}
                    else:
                        chosen_rule: Optional[StatusRule] = None
                        if matching_rules:
                            if config.conflict_resolution == ConflictResolution.PRIORITY:
                                chosen_rule = max(matching_rules, key=lambda r: r.priority)
                            elif config.conflict_resolution == ConflictResolution.FIRST_MATCH:
                                chosen_rule = matching_rules[0]
                            elif config.conflict_resolution == ConflictResolution.LAST_MATCH:
                                chosen_rule = matching_rules[-1]
                        if chosen_rule:
                            desired_role_ids = {chosen_rule.target_role_id}
                    
                    # Apply mutual exclusions
                    if config.mutual_exclusions and desired_role_ids:
                        role_priority: Dict[int, int] = {}
                        for r in enabled_rules:
                            role_priority[r.target_role_id] = max(role_priority.get(r.target_role_id, 0), r.priority)
                        
                        for group in config.mutual_exclusions:
                            group_desired = [rid for rid in group if rid in desired_role_ids]
                            if len(group_desired) <= 1:
                                continue
                            
                            keep_role_id = max(group_desired, key=lambda rid: role_priority.get(rid, 0))
                            for rid in group_desired:
                                if rid != keep_role_id:
                                    desired_role_ids.discard(rid)
                    
                    # Process role changes
                    roles_to_check = {r.target_role_id for r in enabled_rules}
                    for role_id in roles_to_check:
                        target_role = guild.get_role(role_id)
                        if not target_role:
                            continue
                        
                        has_role = target_role in member.roles
                        should_have_role = role_id in desired_role_ids
                        
                        if should_have_role and not has_role:
                            try:
                                await member.add_roles(target_role, reason="Bulk scan - Status match")
                                updated += 1
                            except discord.Forbidden:
                                logger.warning(f"No permission to add role {target_role.name} to {member.display_name}")
                            except Exception as e:
                                logger.error(f"Error adding role during bulk scan: {e}")
                        
                        elif not should_have_role and has_role:
                            try:
                                await member.remove_roles(target_role, reason="Bulk scan - Status no longer matches")
                                updated += 1
                            except discord.Forbidden:
                                logger.warning(f"No permission to remove role {target_role.name} from {member.display_name}")
                            except Exception as e:
                                logger.error(f"Error removing role during bulk scan: {e}")
                
                # Small delay between batches to avoid rate limits
                if i + batch_size < len(members):
                    await asyncio.sleep(1)
            
            logger.info(f"Bulk scan completed: {processed} processed, {updated} updated")
            return processed, updated
            
        except Exception as e:
            logger.error(f"Error during bulk scan: {e}")
            return processed, updated

    async def get_analytics_summary(self, guild_id: int) -> List[AnalyticsData]:
        """Get analytics summary for a guild."""
        # This is a placeholder implementation since the full analytics system
        # would require additional database collections and complex tracking
        # For now, return empty list to prevent errors
        try:
            # In a full implementation, this would query analytics data from the database
            # and return actual usage statistics
            return []
        except Exception as e:
            logger.error(f"Error getting analytics summary: {e}")
            return []

    def normalize_text(self, text: str) -> str:
        """Normalize text by removing invisible characters and normalizing Unicode."""
        if not text:
            return ""
        
        # Unicode normalization
        normalized = unicodedata.normalize('NFC', text)
        
        # Remove invisible characters
        invisible_chars = {'\u200b', '\u200c', '\u200d', '\u2060', '\ufeff', '\u00ad'}
        for char in invisible_chars:
            normalized = normalized.replace(char, '')
        
        return normalized.strip()

    def matches_pattern(self, text: str, pattern: str, is_regex: bool) -> bool:
        """Check if text matches the pattern."""
        if not text or not pattern:
            return False
        
        normalized_text = self.normalize_text(text)
        normalized_pattern = self.normalize_text(pattern)
        
        if is_regex:
            try:
                return bool(re.search(normalized_pattern, normalized_text, re.IGNORECASE))
            except re.error:
                return False
        else:
            return normalized_pattern.lower() in normalized_text.lower()

    def should_process(self, guild_id: int, user_id: int, custom_status: Optional[str]) -> bool:
        """Check if we should process this presence update (debouncing)."""
        current_time = time.time()
        status_hash = hashlib.md5((custom_status or "").encode()).hexdigest()
        
        # Clean old cache entries (older than 30 seconds)
        expired_keys = [k for k, ts in self._cache_timestamps.items() if current_time - ts > 30]
        for k in expired_keys:
            self._cache.pop(k, None)
            self._cache_timestamps.pop(k, None)

        key = (guild_id, user_id)
        
        # Check if we should process
        if key in self._cache and self._cache[key] == status_hash:
            if current_time - self._cache_timestamps[key] < 30:  # 30 second debounce
                return False
        
        # Update cache
        self._cache[key] = status_hash
        self._cache_timestamps[key] = current_time
        return True

    def extract_custom_status(self, activities: List[discord.Activity]) -> Optional[str]:
        """Extract custom status from activities."""
        for activity in activities:
            if isinstance(activity, discord.CustomActivity):
                state = getattr(activity, "state", None)
                if state:
                    return state
                if activity.name:
                    return activity.name
        return None

    @commands.Cog.listener()
    async def on_presence_update(self, before: discord.Member, after: discord.Member) -> None:
        """Handle presence update events."""
        if not self._ready or after.bot or self.collection is None:
            return
        
        try:
            # Get guild configuration
            config = await self.get_config(after.guild.id)
            if not config or not config.rules:
                return
            
            # Extract custom status
            custom_status = self.extract_custom_status(after.activities)
            
            # Check debounce
            if not self.should_process(after.guild.id, after.id, custom_status):
                return

            enabled_rules = [r for r in config.rules if r.enabled]
            if not enabled_rules:
                return

            # Find matching rules
            matching_rules: List[StatusRule] = []
            if custom_status:
                for rule in enabled_rules:
                    if self.matches_pattern(custom_status, rule.trigger_pattern, rule.is_regex):
                        matching_rules.append(rule)

            # Determine desired roles using conflict resolution
            desired_role_ids: Set[int] = set()
            if config.conflict_resolution == ConflictResolution.NONE:
                desired_role_ids = {r.target_role_id for r in matching_rules}
            else:
                chosen_rule: Optional[StatusRule] = None
                if matching_rules:
                    if config.conflict_resolution == ConflictResolution.PRIORITY:
                        chosen_rule = max(matching_rules, key=lambda r: r.priority)
                    elif config.conflict_resolution == ConflictResolution.FIRST_MATCH:
                        chosen_rule = matching_rules[0]
                    elif config.conflict_resolution == ConflictResolution.LAST_MATCH:
                        chosen_rule = matching_rules[-1]
                if chosen_rule:
                    desired_role_ids = {chosen_rule.target_role_id}

            # Apply mutual exclusions
            if config.mutual_exclusions and desired_role_ids:
                role_priority: Dict[int, int] = {}
                for r in enabled_rules:
                    role_priority[r.target_role_id] = max(role_priority.get(r.target_role_id, 0), r.priority)

                for group in config.mutual_exclusions:
                    group_desired = [rid for rid in group if rid in desired_role_ids]
                    if len(group_desired) <= 1:
                        continue

                    keep_role_id = max(group_desired, key=lambda rid: role_priority.get(rid, 0))
                    for rid in group_desired:
                        if rid != keep_role_id:
                            desired_role_ids.discard(rid)

            # Only process roles that are managed by this system
            roles_to_check = {r.target_role_id for r in enabled_rules}
            
            for role_id in roles_to_check:
                target_role = after.guild.get_role(role_id)
                if not target_role:
                    continue

                has_role = target_role in after.roles
                should_have_role = role_id in desired_role_ids

                if should_have_role and not has_role:
                    try:
                        await after.add_roles(target_role, reason="Status Vanity Role - Custom status match")
                        await self.log_role_change(after, target_role, custom_status, config.log_channel_id, True)
                    except discord.Forbidden:
                        logger.warning(f"No permission to add role {target_role.name} to {after.display_name}")
                    except Exception as e:
                        logger.error(f"Error adding role: {e}")

                elif not should_have_role and has_role:
                    try:
                        await after.remove_roles(target_role, reason="Status Vanity Role - Custom status no longer matches")
                        await self.log_role_change(after, target_role, custom_status, config.log_channel_id, False)
                    except discord.Forbidden:
                        logger.warning(f"No permission to remove role {target_role.name} from {after.display_name}")
                    except Exception as e:
                        logger.error(f"Error removing role: {e}")
                    
        except Exception as e:
            logger.error(f"Error in presence update handler: {e}")

    async def log_role_change(self, member: discord.Member, role: discord.Role, 
                            custom_status: Optional[str], log_channel_id: int, added: bool) -> None:
        """Log role changes to the configured channel."""
        try:
            log_channel = self.bot.get_channel(log_channel_id)
            if not log_channel:
                return
            
            import random
            
            # Ensure message structure exists
            if not self.messages or "role_added" not in self.messages or "role_removed" not in self.messages:
                logger.warning("Message configuration is missing or malformed, using fallback")
                # Use fallback messages from JSON or hardcoded
                fallback_messages = self.messages.get("fallback", {}) if self.messages else {}
                if added:
                    template = fallback_messages.get("added", "{user} received the {role} role")
                    title_text = "## Role Added <a:Nycto_happ:1454417933575917822>"
                else:
                    template = fallback_messages.get("removed", "{user} lost the {role} role")
                    title_text = "## Role Removed <a:zz_uma_sa:1454417965184454707>"
                description = f"{title_text}\n\n{template.format(user=member.mention, role=role.mention)}"
            elif added:
                # Get messages for role added
                message_templates = self.messages.get("role_added", {}).get("messages", [])
                
                # Choose random message template and format it
                if message_templates:
                    template = random.choice(message_templates)
                    message_text = template.format(user=member.mention, role=role.mention)
                else:
                    # Fallback if no messages in JSON
                    message_text = f"{member.mention} received the {role.mention} role"
                
                title_text = "## Role Added <a:Nycto_happ:1454417933575917822>"
                description = f"{title_text}\n\n{message_text}"
            else:
                # Get messages for role removed
                message_templates = self.messages.get("role_removed", {}).get("messages", [])
                
                # Choose random message template and format it
                if message_templates:
                    template = random.choice(message_templates)
                    message_text = template.format(user=member.mention, role=role.mention)
                else:
                    # Fallback if no messages in JSON
                    message_text = f"{member.mention} lost the {role.mention} role"
                
                title_text = "## Role Removed <a:zz_uma_sa:1454417965184454707>"
                description = f"{title_text}\n\n{message_text}"
            
            embed = discord.Embed(
                title=None,
                description=description,
                color=self._get_random_color(),
                timestamp=discord.utils.utcnow()
            )
            
            # Set user avatar as thumbnail
            embed.set_thumbnail(url=member.display_avatar.url)
            
            # Add footer with bot info
            embed.set_footer(
                text=f"{self.bot.user.name}",
                icon_url=self.bot.user.display_avatar.url
            )
            
            await log_channel.send(embed=embed)
            
        except Exception as e:
            logger.error(f"Error logging role change: {e}")

    @app_commands.command(name="vanity-role", description="Setup and manage status-based vanity role system")
    async def status_role(self, interaction: discord.Interaction) -> None:
        """Main command for status vanity role management - opens setup modal."""
        
        # Check admin permissions
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You need Administrator permissions to use this command.",
                ephemeral=True
            )
            return

        # Show the management view directly
        embed = discord.Embed(
            title="⚙️ Vanity Roles",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )
        embed.description = "Create and manage vanity role rules based on user status."
        view = StatusRoleManagementView(self)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def cog_load(self) -> None:
        """Called when the cog is loaded."""
        await self.setup_hook()
        # Start the periodic maintenance task
        if not self.periodic_maintenance.is_running():
            self.periodic_maintenance.start()

    async def cog_unload(self) -> None:
        """Cleanup when the cog is unloaded."""
        try:
            self._ready = False
            
            # Stop periodic maintenance
            if self.periodic_maintenance.is_running():
                self.periodic_maintenance.cancel()
            
            # Cancel any ongoing reconnect tasks
            for guild_id, task in self._reconnect_tasks.items():
                if not task.done():
                    task.cancel()
                    logger.info(f"Cancelled reconnect task for guild {guild_id}")
            
            self._reconnect_tasks.clear()
            self._startup_scan_completed.clear()
            
            if self.mongo_client:
                self.mongo_client.close()
            logger.info("StatusRoleCog unloaded successfully")
        except Exception as e:
            logger.error(f"Error during StatusRoleCog unload: {e}")


async def setup(bot: commands.Bot) -> None:
    """Setup function for loading the cog."""
    await bot.add_cog(StatusRoleCog(bot))