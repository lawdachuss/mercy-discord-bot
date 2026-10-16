"""Reminder package.

Interactive, database-backed reminders for the bot: an admin dashboard built
from buttons, select menus and modals, one-off and recurring schedules with
timezone support, and a scheduler that survives restarts.

Modules
-------
``config``   constants (limits, colours, timezone shortlist)
``store``    time/schedule maths plus the MongoDB persistence layer
``views``    Discord UI (dashboard, wizard steps, modals)
``reminders`` the cog: slash commands, panels and the delivery loop
"""

__version__ = "1.0.0"
