"""
Leaderboard Configuration
=========================
Extracted from JSON templates - all styling, emojis, images, and text templates.
"""

# ==================== COLORS ====================
EMBED_COLOR = 3092790  # Main embed color


# ==================== CUSTOM EMOJIS ====================
class Emojis:
    """Custom emoji IDs from your Discord server"""
    GOL = "<:ogs_gol:1429054112552452158>"
    MOON = "<:ogs_moon:1429070446153961493>"
    TIME = "<:ogs_time:1428638675608141906>"
    CROW = "<:ogs_crow:1428639113317453825>"
    ARROW = "<a:originals_arrow:1429069655959539802>"
    STARS = "<a:ogs_stars:1429072029696655370>"
    PIN = "<:ogs_pin:1428605997395480636>"
    HEARTSPARK = "<a:heartspark_ogs:1427918324066422834>"
    MIC = "<:ogs_mic:1429054112552452158>"  # For voice leaderboard
    USER = "<a:original_Peek:1429151221939441776>"  # Custom user emoji - UPDATE THIS WITH YOUR EMOJI ID
    TROPHY = "<:ogsstprize:1429349697817935962>"  # Trophy for champion
    STAR = "<:ogs_starr:1429348961885360149>"  # Star for winner
    OGS_SYMBO = "<:ogs_symbo:1429051663766913165>"  # OGS symbol for stats
    
    # Button emojis
    LEFT_BUTTON_ID = 1426488800875380818
    LEFT_BUTTON_NAME = "og_left"
    RIGHT_BUTTON_ID = 1426489179159658578
    RIGHT_BUTTON_NAME = "og_right"


# ==================== IMAGES ====================
class Images:
    """Image URLs from templates"""
    # Chat leaderboard header
    CHAT_HEADER = "https://media.discordapp.net/attachments/1428636041538965607/1429086007604809749/2.png?ex=68f4db8f&is=68f38a0f&hm=b5f9006945551396eadb40e1383dd93d04f9ccc2551f43895d24ff9acc7decb7&=&format=webp&quality=lossless&width=1125&height=250"
    
    # Voice leaderboard header
    VOICE_HEADER = "https://media.discordapp.net/attachments/1428636041538965607/1429086008011653171/3.png?ex=68f4db8f&is=68f38a0f&hm=3ee61ff5809bedb6ba8dd70e009cd5e6a180cc77b105d615258f830dd53b3f25&=&format=webp&quality=lossless&width=1125&height=250"
    
    # Divider (used in all embeds)
    DIVIDER = "https://media.discordapp.net/attachments/1428636041538965607/1429077668909027389/dividerr.gif?ex=68f4d3cb&is=68f3824b&hm=299635589a2ff2e5cd9990c8fa57fa0c5fe93500fa6dbd2aacfd2fc8281a70b9&=&width=1848&height=64"
    
    # Footer icon
    FOOTER_ICON = "https://media.discordapp.net/attachments/1428636041538965607/1429070329954701372/6805-gd-moon.png?ex=68f4ccf5&is=68f37b75&hm=9304520c12872a0d7b286ac97bce7aaad8bf7f9d3da9c4d25b5d813fcd337a01&=&format=webp&quality=lossless&width=214&height=225"


# ==================== TEXT TEMPLATES ====================
class ChatTemplates:
    """Chat leaderboard text templates"""
    
    DIVIDER_LINE = "────────────────────────────"
    
    # Title templates
    TITLE_MONTHLY = "Chat Leaderboard — Monthly Rankings"
    TITLE_WEEKLY = "Chat Leaderboard — Weekly Rankings"
    TITLE_DAILY = "Chat Leaderboard — Today's Rankings"
    
    # Subtitle templates
    SUBTITLE_MONTHLY = "This month's most active members are here!"
    SUBTITLE_WEEKLY = "This week's most active members are here!"
    SUBTITLE_DAILY = "Today's most active members are here!"
    
    # Top user message
    TOP_USER_MESSAGE = "Consistently leading the conversations and keeping the chat alive!"
    
    # Footer text
    FOOTER_TEXT = "Updates every 5 minutes"
    
    @staticmethod
    def build_description(period_title: str, total_messages: int, period_display: str,
                         top_user_name: str, top_user_count: int, 
                         leaderboard_text: str, reset_timestamp: int, 
                         subtitle: str, last_month_winner: str = None) -> str:
        """Build complete chat leaderboard description"""
        
        # Build last month winner section if provided
        last_month_section = ""
        if last_month_winner:
            last_month_section = f"""
{ChatTemplates.DIVIDER_LINE}

**🏆 Last Month's Champion:**
 {last_month_winner}
"""
        
        return f"""# {Emojis.GOL} {period_title} 
‎ 
**{Emojis.MOON} Total Messages Tracked:** `{total_messages:,}`
**{Emojis.TIME}  Period:** `{period_display}`
{last_month_section}
{ChatTemplates.DIVIDER_LINE}

**{Emojis.CROW} Most active chatter is `{top_user_name}` {Emojis.ARROW}`{top_user_count:,} messages` **
 {ChatTemplates.TOP_USER_MESSAGE}

{ChatTemplates.DIVIDER_LINE}

## {Emojis.STARS} **LEADERBOARD**
**{Emojis.PIN} {subtitle}**
{leaderboard_text}

{ChatTemplates.DIVIDER_LINE}

{Emojis.HEARTSPARK} Resets after <t:{reset_timestamp}:R>"""


class VoiceTemplates:
    """Voice leaderboard text templates"""
    
    DIVIDER_LINE = "────────────────────────────"
    
    # Title templates
    TITLE_MONTHLY = "Voice Leaderboard — Monthly Rankings"
    TITLE_WEEKLY = "Voice Leaderboard — Weekly Rankings"
    TITLE_DAILY = "Voice Leaderboard — Today's Rankings"
    
    # Subtitle templates
    SUBTITLE_MONTHLY = "This month's most active voice members are here!"
    SUBTITLE_WEEKLY = "This week's most talkative members are here!"
    SUBTITLE_DAILY = "Today's most active voice users are here!"
    
    # Top user messages (varies by period)
    TOP_USER_MESSAGE_MONTHLY = "Always in the VC, keeping the conversations alive!"
    TOP_USER_MESSAGE_WEEKLY = "Always active in calls and vibing with the group!"
    TOP_USER_MESSAGE_DAILY = "Top of the voice chat again — talkative as ever!"
    
    # Footer text
    FOOTER_TEXT = "Updates every 5 minutes"
    
    @staticmethod
    def get_top_user_message(period: str) -> str:
        """Get appropriate top user message based on period"""
        messages = {
            'monthly': VoiceTemplates.TOP_USER_MESSAGE_MONTHLY,
            'weekly': VoiceTemplates.TOP_USER_MESSAGE_WEEKLY,
            'daily': VoiceTemplates.TOP_USER_MESSAGE_DAILY
        }
        return messages.get(period, VoiceTemplates.TOP_USER_MESSAGE_MONTHLY)
    
    @staticmethod
    def build_description(period_title: str, total_hours: int, period_display: str,
                         top_user_name: str, top_user_time: str, 
                         leaderboard_text: str, reset_timestamp: int, 
                         subtitle: str, period: str, last_month_winner: str = None) -> str:
        """Build complete voice leaderboard description"""
        top_message = VoiceTemplates.get_top_user_message(period)
        
        # Build last month winner section if provided
        last_month_section = ""
        if last_month_winner:
            last_month_section = f"""
{VoiceTemplates.DIVIDER_LINE}

**🏆 Last Month's Champion:**
 {last_month_winner}
"""
        
        return f"""# {Emojis.MIC} {period_title} 
‎ 
**{Emojis.MOON} Total Time Tracked:** `{total_hours:,} hours`
**{Emojis.TIME}  Period:** `{period_display}`
{last_month_section}
{VoiceTemplates.DIVIDER_LINE}

**{Emojis.CROW} Most active speaker is `{top_user_name}` {Emojis.ARROW}`{top_user_time}` **
 {top_message}

{VoiceTemplates.DIVIDER_LINE}

## {Emojis.STARS} **LEADERBOARD**
**{Emojis.PIN} {subtitle}**
{leaderboard_text}

{VoiceTemplates.DIVIDER_LINE}

{Emojis.HEARTSPARK} Resets after <t:{reset_timestamp}:R>"""


# ==================== PERIOD MAPPINGS ====================
class PeriodConfig:
    """Period-specific configurations"""
    
    CHAT_TITLES = {
        'monthly': ChatTemplates.TITLE_MONTHLY,
        'weekly': ChatTemplates.TITLE_WEEKLY,
        'daily': ChatTemplates.TITLE_DAILY
    }
    
    CHAT_SUBTITLES = {
        'monthly': ChatTemplates.SUBTITLE_MONTHLY,
        'weekly': ChatTemplates.SUBTITLE_WEEKLY,
        'daily': ChatTemplates.SUBTITLE_DAILY
    }
    
    VOICE_TITLES = {
        'monthly': VoiceTemplates.TITLE_MONTHLY,
        'weekly': VoiceTemplates.TITLE_WEEKLY,
        'daily': VoiceTemplates.TITLE_DAILY
    }
    
    VOICE_SUBTITLES = {
        'monthly': VoiceTemplates.SUBTITLE_MONTHLY,
        'weekly': VoiceTemplates.SUBTITLE_WEEKLY,
        'daily': VoiceTemplates.SUBTITLE_DAILY
    }
    
    PERIOD_DISPLAY_NAMES = {
        'daily': 'Today',
        'weekly': 'This Week',
        # 'monthly' is handled dynamically with datetime.strftime('%B %Y')
    }


# ==================== BUTTON CONFIGURATION ====================
class ButtonConfig:
    """Button styling configuration"""
    
    STYLE = 2  # Secondary style (gray)
    
    # Custom IDs for persistence
    CHAT_LEFT_PREFIX = "chat_left"
    CHAT_RIGHT_PREFIX = "chat_right"
    VOICE_LEFT_PREFIX = "voice_left"
    VOICE_RIGHT_PREFIX = "voice_right"


# ==================== LEADERBOARD SETTINGS ====================
class LeaderboardSettings:
    """General leaderboard settings"""
    
    MEMBERS_PER_PAGE = 10
    MAX_MEMBERS_FETCH = 100
    UPDATE_INTERVAL_MINUTES = 5
    
    # Reset times
    WEEKLY_RESET_DAY = 6  # Sunday (0 = Monday, 6 = Sunday)
    WEEKLY_RESET_HOUR = 12  # 12 PM
    
    # Default timezone
    DEFAULT_TIMEZONE = 'UTC'



# ==================== SYSTEM CONFIGURATION ====================
class SystemConfig:
    """System-wide configuration constants for bug fixes and optimizations"""
    
    # Voice session limits
    VOICE_SESSION_MAX_MINUTES = 10080  # 7 days in minutes
    
    # Pagination settings
    PAGINATION_PAGE_SIZE = 10
    
    # Cache settings
    CACHE_MAX_SIZE = 100
    
    # Database retry settings
    RETRY_MAX_ATTEMPTS = 3
    RETRY_BACKOFF_BASE = 0.5  # seconds
    
    # Database timeout settings
    DATABASE_TIMEOUT_SECONDS = 5
    
    # Session cleanup settings
    SESSION_CLEANUP_INTERVAL_HOURS = 1
    STALE_SESSION_THRESHOLD_HOURS = 8
