import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ext.commands import Cog
import logging
from datetime import datetime, timedelta
from typing import Optional, List
import asyncio
import zoneinfo
import calendar

logger = logging.getLogger(__name__)

DEFAULT_TZ = zoneinfo.ZoneInfo("Asia/Kolkata")


class MonthSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="January", value="1", emoji="❄️"),
            discord.SelectOption(label="February", value="2", emoji="💝"),
            discord.SelectOption(label="March", value="3", emoji="🌸"),
            discord.SelectOption(label="April", value="4", emoji="🌷"),
            discord.SelectOption(label="May", value="5", emoji="🌺"),
            discord.SelectOption(label="June", value="6", emoji="☀️"),
            discord.SelectOption(label="July", value="7", emoji="🎆"),
            discord.SelectOption(label="August", value="8", emoji="🌻"),
            discord.SelectOption(label="September", value="9", emoji="🍂"),
            discord.SelectOption(label="October", value="10", emoji="🎃"),
            discord.SelectOption(label="November", value="11", emoji="🍁"),
            discord.SelectOption(label="December", value="12", emoji="🎄"),
        ]
        super().__init__(placeholder="Select birth month...", options=options)
    
    async def callback(self, interaction: discord.Interaction):
        try:
            self.view.selected_month = int(self.values[0])
            await self.view.show_day_selection(interaction)
        except Exception as e:
            logger.error(f"Error in MonthSelect callback: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
            except:
                pass


class DaySelectView(discord.ui.View):
    def __init__(self, cog, friend: discord.User, month: int):
        super().__init__(timeout=300)  # 5 minutes
        self.cog = cog
        self.friend = friend
        self.selected_month = month
        self.selected_day = None
        self.selected_year = None
        
        # Add day select menus (split into 2 groups due to 25 option limit)
        self.add_item(DaySelect1())
        self.add_item(DaySelect2())
    
    async def show_year_selection(self, interaction: discord.Interaction):
        # Validate day is valid for selected month
        max_day = calendar.monthrange(2000, self.selected_month)[1]
        if self.selected_day > max_day:
            month_name = calendar.month_name[self.selected_month]
            try:
                await interaction.response.edit_message(
                    content=f"❌ {month_name} only has {max_day} days. Please start over.",
                    view=None
                )
            except discord.errors.InteractionResponded:
                await interaction.followup.send(
                    f"❌ {month_name} only has {max_day} days. Please start over.",
                    ephemeral=True
                )
            return
        
        year_view = discord.ui.View(timeout=300)  # 5 minutes
        
        current_year = datetime.now(DEFAULT_TZ).year
        # Show recent years as buttons
        years = [current_year - i for i in range(0, 5)]  # Last 5 years
        years.extend([1995, 2000, 2005, 2010])
        years = sorted(set(years), reverse=True)[:8]  # Max 8 year buttons
        
        for year in years:
            year_view.add_item(YearButton(str(year), year))
        
        # Add "Skip" button for no year
        year_view.add_item(YearButton("Skip (no year)", None))
        
        # Store references
        year_view.selected_month = self.selected_month
        year_view.selected_day = self.selected_day
        year_view.save_birthday = self.save_birthday
        year_view.cog = self.cog
        year_view.friend = self.friend
        
        month_name = calendar.month_name[self.selected_month]
        def ordinal(n):
            if 10 <= n % 100 <= 20:
                suffix = 'th'
            else:
                suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
            return f"{n}{suffix}"
        
        try:
            await interaction.response.edit_message(
                content=f"**Month:** {month_name}\n**Day:** {ordinal(self.selected_day)}\n\nSelect birth year (optional):",
                view=year_view
            )
        except discord.errors.InteractionResponded:
            await interaction.followup.send(
                f"**Month:** {month_name}\n**Day:** {ordinal(self.selected_day)}\n\nSelect birth year (optional):",
                view=year_view,
                ephemeral=True
            )
    
    async def save_birthday(self, interaction: discord.Interaction):
        # Defer to prevent timeout on slow DB operations
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.errors.InteractionResponded:
            pass  # Already responded
        
        month = self.selected_month
        day = self.selected_day
        year = self.selected_year
        
        # Validate
        error = self.cog._validate_date(month, day, year)
        if error:
            try:
                await interaction.edit_original_response(content=error, view=None)
            except:
                await interaction.followup.send(content=error, ephemeral=True)
            return
        
        # Save to database
        try:
            await self.cog.collection.update_one(
                {'user_id': interaction.user.id, 'friend_id': self.friend.id},
                {'$set': {
                    'user_id': interaction.user.id,
                    'friend_id': self.friend.id,
                    'friend_name': self.friend.name,
                    'month': month,
                    'day': day,
                    'year': year,
                    'updated_at': datetime.now(DEFAULT_TZ),
                }},
                upsert=True
            )
        except Exception as e:
            logger.error(f"DB error setting birthday for {interaction.user.id}: {e}")
            try:
                await interaction.edit_original_response(
                    content="❌ Failed to save birthday. Please try again later.",
                    view=None
                )
            except:
                await interaction.followup.send(
                    content="❌ Failed to save birthday. Please try again later.",
                    ephemeral=True
                )
            return
        
        month_name = calendar.month_name[month]
        def ordinal(n):
            if 10 <= n % 100 <= 20:
                suffix = 'th'
            else:
                suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
            return f"{n}{suffix}"
        
        date_str = f"{month_name} {ordinal(day)}"
        if year:
            date_str += f", {year}"
        
        response = f"✅ Birthday set for **{self.friend.name}**: **{date_str}**."
        response += f"\n🌍 Timezone: **IST (Asia/Kolkata)**"
        response += f"\n🔔 You'll be notified when it's their birthday!"
        
        try:
            await interaction.edit_original_response(content=response, view=None)
        except:
            await interaction.followup.send(content=response, ephemeral=True)
    
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.error(f"View error: {error}")
        try:
            await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
        except:
            try:
                await interaction.followup.send("❌ An error occurred. Please try again.", ephemeral=True)
            except:
                pass
    
    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
    
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.error(f"DaySelectView error: {error}")
        try:
            await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
        except:
            try:
                await interaction.followup.send("❌ An error occurred. Please try again.", ephemeral=True)
            except:
                pass


class DaySelect1(discord.ui.Select):
    def __init__(self):
        def ordinal(n):
            if 10 <= n % 100 <= 20:
                suffix = 'th'
            else:
                suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
            return f"{n}{suffix}"
        
        options = [discord.SelectOption(label=ordinal(i), value=str(i)) for i in range(1, 16)]
        super().__init__(placeholder="Select day (1st - 15th)...", options=options)
    
    async def callback(self, interaction: discord.Interaction):
        try:
            self.view.selected_day = int(self.values[0])
            await self.view.show_year_selection(interaction)
        except Exception as e:
            logger.error(f"Error in DaySelect1 callback: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
            except:
                pass



class DaySelect2(discord.ui.Select):
    def __init__(self):
        def ordinal(n):
            if 10 <= n % 100 <= 20:
                suffix = 'th'
            else:
                suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')
            return f"{n}{suffix}"
        
        options = [discord.SelectOption(label=ordinal(i), value=str(i)) for i in range(16, 32)]
        super().__init__(placeholder="Select day (16th - 31st)...", options=options)
    
    async def callback(self, interaction: discord.Interaction):
        try:
            self.view.selected_day = int(self.values[0])
            await self.view.show_year_selection(interaction)
        except Exception as e:
            logger.error(f"Error in DaySelect2 callback: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
            except:
                pass



class YearButton(discord.ui.Button):
    def __init__(self, label: str, year: Optional[int]):
        super().__init__(label=label, style=discord.ButtonStyle.primary if year else discord.ButtonStyle.secondary)
        self.year_value = year
    
    async def callback(self, interaction: discord.Interaction):
        try:
            self.view.selected_year = self.year_value
            await self.view.save_birthday(interaction)
        except Exception as e:
            logger.error(f"Error in YearButton callback: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
            except:
                pass


class MonthSelectView(discord.ui.View):
    def __init__(self, cog, friend: discord.User):
        super().__init__(timeout=300)  # 5 minutes
        self.cog = cog
        self.friend = friend
        self.selected_month = None
        self.selected_day = None
        self.selected_year = None
        
        self.add_item(MonthSelect())
    
    async def show_day_selection(self, interaction: discord.Interaction):
        day_view = DaySelectView(self.cog, self.friend, self.selected_month)
        
        month_name = calendar.month_name[self.selected_month]
        try:
            await interaction.response.edit_message(
                content=f"**Month:** {month_name}\n\nNow select the day:",
                view=day_view
            )
        except discord.errors.InteractionResponded:
            await interaction.followup.send(
                content=f"**Month:** {month_name}\n\nNow select the day:",
                view=day_view,
                ephemeral=True
            )
    
    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
    
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logger.error(f"View error: {error}")
        try:
            await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
        except:
            try:
                await interaction.followup.send("❌ An error occurred. Please try again.", ephemeral=True)
            except:
                pass


class BirthdayUserInstall(Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = None
        self.collection = None
        self._db_ready = False
        self._initialize_db()

    def _initialize_db(self):
        if hasattr(self.bot, 'mongo_client') and self.bot.mongo_client:
            self.db = self.bot.mongo_client['discord_bot']
            self.collection = self.db['birthdays']
            self._db_ready = True

    def _ensure_db(self):
        """Retry DB initialization if it wasn't ready at __init__ time."""
        if not self._db_ready:
            self._initialize_db()
        return self._db_ready

    async def cog_load(self):
        self.birthday_check.start()

    def _get_now(self, tz):
        return datetime.now(tz)

    def _get_tz(self, data):
        """Always use IST timezone for all users."""
        return DEFAULT_TZ
    

    def _get_next_birthday(self, month, day, tz):
        now = self._get_now(tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        year = today.year

        bday = self._make_birthday_date(year, month, day, tz)

        if bday <= today:
            bday = self._make_birthday_date(year + 1, month, day, tz)

        return bday

    @staticmethod
    def _make_birthday_date(year, month, day, tz):
        """
        Create a birthday datetime for the given year.
        Handles Feb 29 birthdays in non-leap years by falling back to Feb 28.
        """
        if month == 2 and day == 29 and not calendar.isleap(year):
            return datetime(year, 2, 28, tzinfo=tz)
        return datetime(year, month, day, tzinfo=tz)

    @staticmethod
    def _validate_date(month: int, day: int, year: Optional[int] = None) -> str | None:
        """Validate a birthday date. Returns an error message string, or None if valid."""
        if not (1 <= month <= 12):
            return "❌ Month must be between 1 and 12."

        # Use a leap year for validation to allow Feb 29
        max_day = calendar.monthrange(2000, month)[1]
        if not (1 <= day <= max_day):
            return f"❌ Day must be between 1 and {max_day} for month {month}."

        if year is not None:
            current_year = datetime.now(DEFAULT_TZ).year
            if year < 1900 or year > current_year:
                return f"❌ Year must be between 1900 and {current_year}."
            # Validate the full date with the actual year (catches Feb 29 on non-leap years)
            try:
                datetime(year, month, day)
            except ValueError:
                return f"❌ {month}/{day}/{year} is not a valid date."

        return None

    async def _dm_only(self, interaction: discord.Interaction) -> bool:
        """Check if command is used in DMs only."""
        if interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used in DMs.", ephemeral=True
            )
            return False
        return True

    async def _check_db(self, interaction: discord.Interaction) -> bool:
        if not self._ensure_db():
            await interaction.response.send_message(
                "❌ Database is not available. Please try again later.", ephemeral=True
            )
            return False
        return True

    birthday_group = app_commands.Group(name="birthday", description="Manage friends' birthdays")

    @birthday_group.command(name="set", description="Set a friend's birthday")
    @app_commands.describe(friend="The friend whose birthday to save")
    @app_commands.allowed_installs(users=True)
    @app_commands.allowed_contexts(dms=True, private_channels=True, guilds=True)
    async def set_birthday(
        self, 
        interaction: discord.Interaction, 
        friend: discord.User
    ):
        if not await self._check_db(interaction):
            return

        # Prevent setting your own birthday
        if friend.id == interaction.user.id:
            return await interaction.response.send_message(
                "❌ You cannot set your own birthday. This is for tracking friends' birthdays.", 
                ephemeral=True
            )
        
        # Prevent setting bot birthdays
        if friend.bot:
            return await interaction.response.send_message(
                "❌ You cannot set birthdays for bots.", 
                ephemeral=True
            )

        # Show month selection menu
        view = MonthSelectView(self, friend)
        await interaction.response.send_message(
            f"Setting birthday for **{friend.name}**\n\nSelect birth month:",
            view=view,
            ephemeral=True
        )

    @birthday_group.command(name="view", description="View saved birthdays")
    @app_commands.allowed_installs(users=True)
    @app_commands.allowed_contexts(dms=True, private_channels=True, guilds=True)
    async def view(self, interaction: discord.Interaction):
        if not await self._check_db(interaction):
            return

        # Defer for slow DB operations
        await interaction.response.defer(ephemeral=True)

        try:
            # Find all birthdays saved by this user
            data_list = await self.collection.find({'user_id': interaction.user.id}).to_list(None)
        except Exception as e:
            logger.error(f"DB error viewing birthdays for {interaction.user.id}: {e}")
            return await interaction.followup.send(
                "❌ Failed to fetch birthdays. Please try again later.", ephemeral=True
            )

        if not data_list:
            return await interaction.followup.send(
                "❌ No birthdays saved. Use `/birthday set` to add friends' birthdays.", ephemeral=True
            )

        # Sort by next birthday date
        birthday_info = []
        for data in data_list:
            if 'month' not in data or 'day' not in data:
                continue
            
            # Update friend name if changed
            if data.get('friend_id'):
                try:
                    friend_user = await self.bot.fetch_user(data['friend_id'])
                    if friend_user.name != data.get('friend_name'):
                        await self.collection.update_one(
                            {'_id': data['_id']},
                            {'$set': {'friend_name': friend_user.name}}
                        )
                        data['friend_name'] = friend_user.name
                except Exception:
                    pass  # Not critical, use stored name
                
            tz = self._get_tz(data)
            now = self._get_now(tz)
            next_bday = self._get_next_birthday(data['month'], data['day'], tz)
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            delta = next_bday - today
            days = delta.days

            birthday_info.append((data, tz, now, next_bday, today, days))
        
        # Sort by days until birthday
        birthday_info.sort(key=lambda x: x[5])
        
        embed = discord.Embed(title="🎂 Saved Birthdays", color=discord.Color.blurple())
        
        # Discord embeds have a limit of 25 fields
        for data, tz, now, next_bday, today, days in birthday_info[:25]:
            # Format the date nicely
            month_name = calendar.month_name[data['month']]
            date_str = f"{month_name} {data['day']}"
            if data.get('year'):
                date_str += f", {data['year']}"
            
            friend_name = data.get('friend_name', f"User {data.get('friend_id', 'Unknown')}")
            
            if days == 0:
                value = f"📅 {date_str}\n🎉 **Today! It's their birthday!**"
            elif days == 1:
                value = f"📅 {date_str}\n⏰ **Tomorrow!**"
            else:
                value = f"📅 {date_str}\n⏰ In **{days}** days"
            
            if data.get('year'):
                age = now.year - data['year']
                try:
                    bday_this_year = self._make_birthday_date(now.year, data['month'], data['day'], tz)
                except ValueError:
                    bday_this_year = datetime(now.year, 2, 28, tzinfo=tz)
                # If birthday hasn't happened yet this year, they're still the previous age
                if today < bday_this_year:
                    age -= 1
                if age >= 0:
                    # If birthday is today, they just turned this age
                    # If birthday is in the future, they will turn age+1
                    if days == 0:
                        value += f"\n🎈 Just turned {age}!"
                    else:
                        turning = age + 1
                        value += f"\n🎈 Turning {turning}"
            
            embed.add_field(name=friend_name, value=value, inline=False)
        
        if len(birthday_info) > 25:
            embed.set_footer(text=f"Showing 25 of {len(birthday_info)} birthdays")

        await interaction.followup.send(embed=embed, ephemeral=True)

    @birthday_group.command(name="remove", description="Remove a saved birthday")
    @app_commands.describe(friend="The friend whose birthday to remove")
    @app_commands.allowed_installs(users=True)
    @app_commands.allowed_contexts(dms=True, private_channels=True, guilds=True)
    async def remove_birthday(self, interaction: discord.Interaction, friend: discord.User):
        if not await self._check_db(interaction):
            return

        try:
            result = await self.collection.delete_one({
                'user_id': interaction.user.id,
                'friend_id': friend.id
            })
        except Exception as e:
            logger.error(f"DB error removing birthday for {interaction.user.id}: {e}")
            return await interaction.response.send_message(
                "❌ Failed to remove birthday. Please try again later.", ephemeral=True
            )

        if result.deleted_count == 0:
            return await interaction.response.send_message(
                f"❌ No birthday saved for **{friend.name}**.", ephemeral=True
            )

        await interaction.response.send_message(f"✅ Birthday removed for **{friend.name}**.", ephemeral=True)

    @tasks.loop(minutes=30)
    async def birthday_check(self):
        if not self._ensure_db():
            return

        try:
            birthdays = await self.collection.find({
                'month': {'$exists': True}, 
                'day': {'$exists': True},
                'friend_id': {'$exists': True}
            }).to_list(None)
        except Exception as e:
            logger.error(f"DB error during birthday check: {e}")
            return

        for entry in birthdays:
            try:
                tz = self._get_tz(entry)
                now = self._get_now(tz)
                today = now.replace(hour=0, minute=0, second=0, microsecond=0)
                yesterday = today - timedelta(days=1)

                today_key = today.strftime("%Y-%m-%d")
                yesterday_key = yesterday.strftime("%Y-%m-%d")

                month = entry['month']
                day = entry['day']

                # Check if today is their birthday (handle Feb 29 in non-leap years)
                is_birthday_today = False
                if month == now.month and day == now.day:
                    is_birthday_today = True
                elif month == 2 and day == 29 and not calendar.isleap(now.year):
                    # Feb 29 birthday in non-leap year -> trigger on Feb 28
                    if now.month == 2 and now.day == 28:
                        is_birthday_today = True

                last_wished = entry.get('last_birthday_wish', '')
                
                # Send birthday wish if:
                # 1. Today is their birthday AND we haven't sent today
                # 2. OR yesterday was their birthday and we missed it (bot downtime catchup)
                should_send_birthday = False
                catchup_mode = False
                
                if is_birthday_today and last_wished != today_key:
                    should_send_birthday = True
                elif is_birthday_today and last_wished == '':
                    # First time setup
                    should_send_birthday = True
                elif last_wished != today_key and last_wished != yesterday_key:
                    # Check if we missed the birthday (bot was down)
                    # Only catchup if birthday was within last 3 days
                    for days_ago in range(1, 4):
                        check_date = today - timedelta(days=days_ago)
                        check_key = check_date.strftime("%Y-%m-%d")
                        
                        # Check if that date was their birthday
                        was_birthday = False
                        if month == check_date.month and day == check_date.day:
                            was_birthday = True
                        elif month == 2 and day == 29 and not calendar.isleap(check_date.year):
                            if check_date.month == 2 and check_date.day == 28:
                                was_birthday = True
                        
                        if was_birthday and last_wished != check_key:
                            should_send_birthday = True
                            catchup_mode = True
                            break
                
                if should_send_birthday:
                    try:
                        # Update friend name if it changed
                        try:
                            friend_user = await self.bot.fetch_user(entry['friend_id'])
                            if friend_user.name != entry.get('friend_name'):
                                await self.collection.update_one(
                                    {'_id': entry['_id']},
                                    {'$set': {'friend_name': friend_user.name}}
                                )
                                entry['friend_name'] = friend_user.name
                        except Exception:
                            pass  # Friend name update is not critical
                        
                        # Notify the user who saved this birthday
                        user = await self.bot.fetch_user(entry['user_id'])
                        friend_name = entry.get('friend_name', f"User {entry.get('friend_id', 'Unknown')}")
                        age_str = ""
                        if entry.get('year'):
                            age = now.year - entry['year']
                            # Adjust age if birthday hasn't occurred yet this year
                            try:
                                bday_this_year = self._make_birthday_date(now.year, month, day, tz)
                            except ValueError:
                                bday_this_year = datetime(now.year, 2, 28, tzinfo=tz)
                            if today <= bday_this_year:
                                age -= 1
                            if age >= 0:
                                age_str = f" They're turning **{age}**! 🎈"
                        
                        message = f"🎉🎂 **It's {friend_name}'s Birthday!**{age_str}"
                        if catchup_mode:
                            message = f"🎂 **{friend_name}'s Birthday was recently!**{age_str}\n_(Notification was delayed)_"
                        
                        await user.send(message)
                        await self.collection.update_one(
                            {'_id': entry['_id']},
                            {'$set': {'last_birthday_wish': today_key}}
                        )
                    except discord.Forbidden:
                        logger.debug(f"Cannot DM user {entry['user_id']} (DMs closed)")
                    except discord.NotFound:
                        logger.debug(f"User {entry['user_id']} not found, cleaning up")
                        await self.collection.delete_one({'_id': entry['_id']})
                    except Exception as e:
                        logger.error(f"Error sending birthday notification to {entry['user_id']}: {e}")

                # Check if tomorrow is their birthday (reminder)
                tomorrow = today + timedelta(days=1)
                is_birthday_tomorrow = False
                if month == tomorrow.month and day == tomorrow.day:
                    is_birthday_tomorrow = True
                elif month == 2 and day == 29 and not calendar.isleap(tomorrow.year):
                    if tomorrow.month == 2 and tomorrow.day == 28:
                        is_birthday_tomorrow = True

                last_reminder = entry.get('last_birthday_reminder', '')
                if is_birthday_tomorrow and last_reminder != today_key:
                    try:
                        user = await self.bot.fetch_user(entry['user_id'])
                        friend_name = entry.get('friend_name', f"User {entry.get('friend_id', 'Unknown')}")
                        await user.send(f"⏰ **Reminder:** {friend_name}'s birthday is **tomorrow**! 🎂")
                        await self.collection.update_one(
                            {'_id': entry['_id']},
                            {'$set': {'last_birthday_reminder': today_key}}
                        )
                    except discord.Forbidden:
                        logger.debug(f"Cannot DM user {entry['user_id']} (DMs closed)")
                    except discord.NotFound:
                        logger.debug(f"User {entry['user_id']} not found, cleaning up")
                        await self.collection.delete_one({'_id': entry['_id']})
                    except Exception as e:
                        logger.error(f"Error sending birthday reminder to {entry['user_id']}: {e}")

            except Exception as e:
                logger.error(f"Error processing birthday for entry {entry.get('_id', 'unknown')}: {e}")
                continue

    @birthday_check.before_loop
    async def before_loop(self):
        await self.bot.wait_until_ready()
        # Align to the next :00 or :30 minute mark
        now = datetime.now(DEFAULT_TZ)
        if now.minute < 30:
            next_run = now.replace(minute=30, second=0, microsecond=0)
        else:
            next_run = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        delay = (next_run - now).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

    def cog_unload(self):
        self.birthday_check.cancel()


async def setup(bot):
    await bot.add_cog(BirthdayUserInstall(bot))