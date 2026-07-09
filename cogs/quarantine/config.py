"""
Configuration for Quarantine and Appeal System
"""

# ============================================================================
# QUARANTINE SYSTEM CONFIGURATION
# ============================================================================

# Role & Channel Names
MUTED_ROLE_NAME = "Muted"
JAIL_CHANNEL_NAME = "jail"
LOG_CHANNEL_NAME = "punishment-logs"

# DM Configuration
DM_AUTO_DELETE_MINUTES = 10  # Minutes before DM auto-deletes
DM_REASON_MAX_LENGTH = 80  # Max characters for reason in DM

# Permission Update Configuration
PERMISSION_BASE_SLEEP = 0.3  # Base delay between permission updates (seconds)
PERMISSION_LARGE_SERVER_SLEEP = 0.4  # Delay for servers with 100-200 channels
PERMISSION_HUGE_SERVER_SLEEP = 0.5  # Delay for servers with 200+ channels
PERMISSION_MAX_RETRIES = 3  # Max retries for failed permission updates
LARGE_SERVER_THRESHOLD = 100  # Channel count to consider server "large"
HUGE_SERVER_THRESHOLD = 200  # Channel count to consider server "huge"

# Jail Message
JAIL_WELCOME_MESSAGE = "You have been muted. Please wait for staff to review your case."

# ============================================================================
# APPEAL SYSTEM CONFIGURATION
# ============================================================================

# Appeal Limits
APPEAL_COOLDOWN_HOURS = 168  # Hours between appeal submissions (1 week = 7 days * 24 hours)
MAX_APPEAL_LENGTH = 1000  # Maximum characters in appeal message
MIN_APPEAL_LENGTH = 50  # Minimum characters in appeal message
APPEAL_REVIEW_TIMEOUT_DAYS = 7  # Days before appeal auto-expires

# Appeal Modal Configuration
APPEAL_REASON_PLACEHOLDER = "Explain why you believe your punishment should be lifted..."
APPEAL_ADDITIONAL_INFO_PLACEHOLDER = "Any additional information (optional)..."

# ============================================================================
# STATUS & TYPE ENUMS
# ============================================================================

# Appeal Status
class AppealStatus:
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"

# Appeal Types
class AppealType:
    MUTE = "mute"
    BAN = "ban"
    KICK = "kick"

# ============================================================================
# COLORS
# ============================================================================

# Embed Colors
class Colors:
    SUCCESS = 0x2f3136  # Dark Grey
    ERROR = 0x2f3136  # Dark Grey
    WARNING = 0x2f3136  # Dark Grey
    INFO = 0x2f3136  # Dark Grey
    PENDING = 0x2f3136  # Dark Grey
    MUTE = 0x2f3136  # Dark Grey (for mute embeds)
    UNMUTE = 0x2f3136  # Dark Grey (for unmute embeds)

# ============================================================================
# EMOJIS
# ============================================================================

# Quarantine System Emojis
class QuarantineEmojis:
    MUTED = "<a:heartspark_ogs:1427918324066422834>"
    UNMUTED = "<a:heartspark_ogs:1427918324066422834>"
    JAIL = "<:FairyBadg:1426484412870295714>"
    LOG = "<:FairyBadg:1426484412870295714>"
    CASE = "<a:reddot:1427539828521697282>"
    MODERATOR = "<:original_vc_mo:1427922033211342878>"
    MEMBER = "<:Ogs_member:1427922355879022672>"
    REASON = "<:ogs_bell:1427918360401940552>"
    EXPIRES = "<:sukoon_blackdot:1427918583136260136>"
    DURATION = "<:sukoon_blackdot:1427918583136260136>"
    TIMESTAMP = "<:sukoon_blackdot:1427918583136260136>"
    SUCCESS = "<a:white_tick:1426439810733572136>"
    ERROR = "<:alert:1426440385269338164>"
    WARNING = "<:alert:1426440385269338164>"
    INFO = "<:ogs_info:1427918257226121288>"
    SETUP = "<:ogs_bell:1427918360401940552>"
    STATS = "<:sukoon_statss:1427918633082032138>"
    TIP = "<a:heartspark_ogs:1427918324066422834>"
    CLEANUP = "<a:heartspark_ogs:1427918324066422834>"
    AUTO = "<a:heartspark_ogs:1427918324066422834>"
    ACTIVE = "<a:originals_rejec:1426484169227108434>"
    RESOLVED = "<a:originals_accep:1426484148884865190>"

# Appeal System Emojis
class AppealEmojis:
    SUBMIT = "<a:heartspark_ogs:1427918324066422834>"
    APPROVED = "<:ogs_tick:1427918161327558736>"
    DENIED = "<:ogs_cross:1427918018196930642>"
    PENDING = "<:ogs_info:1427918257226121288>"
    EXPIRED = "<a:reddot:1427539828521697282>"
    REVIEW = "<:alert:1426440385269338164>"
    APPEAL = "<:FairyBadg:1426484412870295714>"
    RESULT = "<:FairyBadg:1426484412870295714>"
    NOTE = "<:FairyBadg:1426484412870295714>"
    SERVER = "<:FairyBadg:1426484412870295714>"
    REVIEWER = "<:original_vc_mo:1427922033211342878>"
    USER = "<:Ogs_member:1427922355879022672>"

# ============================================================================
# EMBED TEMPLATES
# ============================================================================

# Quarantine Embed Titles
class QuarantineTitles:
    MUTE_LOG = "{emoji} Member Muted — Case #{case}"
    UNMUTE_LOG = "{emoji} Member Unmuted — Case #{case}"
    AUTO_UNMUTE = "{emoji} Auto-Unmute — Case #{case}"
    MUTE_SUCCESS = "{emoji} Member Muted Successfully"
    UNMUTE_SUCCESS = "{emoji} Member Unmuted Successfully"
    SETUP_COMPLETE = "{emoji} Mute System Setup Complete"
    CONFIG_CHECK_PASS = "{emoji} Configuration Check Passed"
    CONFIG_CHECK_FAIL = "{emoji} Configuration Issues Found"
    PERMISSIONS_REAPPLIED = "{emoji} Permissions Reapplied"
    MODROLE_UPDATED = "{emoji} Moderator Role Updated"
    CLEANUP_COMPLETE = "{emoji} Database Cleanup Complete"
    CASE_INFO = "{emoji} Case #{case} — {status}"
    ACTIVE_MUTES = "{emoji} Active Mutes"
    JAIL_HISTORY = "{emoji} Jail Message History"

# Appeal Embed Titles
class AppealTitles:
    APPEAL_SUBMITTED = "{emoji} Appeal Submitted Successfully"
    APPEAL_APPROVED = "{emoji} Appeal Approved"
    APPEAL_DENIED = "{emoji} Appeal Denied"
    APPEAL_DETAILS = "{emoji} Appeal #{id} Details"
    APPEAL_LIST = "{emoji} Your Appeals"
    PENDING_APPEALS = "{emoji} Pending Appeals"
    APPEAL_REVIEW = "{emoji} Review Appeal #{id}"

# Embed Descriptions
class EmbedDescriptions:
    # Quarantine
    MUTE_LOG = "**{member}** has been muted and moved to the quarantine zone."
    UNMUTE_LOG = "**{member}** has been unmuted and can now access the server."
    AUTO_UNMUTE = "Temporary mute has expired and been automatically removed."
    MUTE_SUCCESS = "{member} has been muted and moved to {jail}"
    UNMUTE_SUCCESS = "{member} has been unmuted and can now access the server."
    SETUP_COMPLETE = "The quarantine system has been successfully configured!"
    CONFIG_CHECK_PASS = "All basic configuration checks passed successfully!"
    CONFIG_CHECK_FAIL = "The following problems were detected with your mute system:"
    PERMISSIONS_REAPPLIED = "Muted role overwrites have been reapplied across all categories and channels."
    MODROLE_UPDATED = "The moderator role has been set to {role}"
    CLEANUP_COMPLETE = "Cleared old inactive mute records from the database."
    CASE_INFO = "Detailed information for mute case **#{case}**"
    
    # Appeal
    APPEAL_SUBMITTED = "Your appeal has been submitted and is pending review by moderators."
    APPEAL_APPROVED_DM = "Your punishment has been lifted"
    APPEAL_DENIED_DM = "Punishment remains in effect"
    APPEAL_DETAILS = "Status: {emoji} **{status}**"
    APPEAL_LIST = "Showing {count} appeal(s)"
    PENDING_APPEALS = "Showing {count} pending appeal(s)"

# Footer Messages
class FooterMessages:
    MUTE_DM_SENT = "✅ DM sent to user"
    MUTE_DM_FAILED = "⚠️ Could not send DM to user"
    MUTE_SUCCESS_REMOVED = "✅ Mute successfully removed"
    SETUP_BY = "Setup by {user}"
    REQUESTED_BY = "Requested by {user}"
    MUTED_BY = "Muted by {user}"
    UNMUTED_BY = "Unmuted by {user}"
    CLEANUP_BY = "Cleanup by {user}"
    CONFIG_CHECK = "Please resolve these issues to ensure proper functionality"
    MUTE_READY = "Your mute system is ready to use!"
    AUTO_UNMUTE = "✅ Automatic unmute completed"
    APPEAL_SUBMITTED = "Moderators will review your appeal soon"
    USE_APPEAL_STATUS = "Use /appeal-status <appeal_id> for detailed information"
    USE_APPEAL_REVIEW = "Use /appeal-review <appeal_id> to review an appeal"
