"""
Purge cog package for bulk message deletion functionality.
"""

from .purge import PurgeCog

async def setup(bot):
    """Load the PurgeCog."""
    await bot.add_cog(PurgeCog(bot))
