"""Shared, correct interaction helpers.

Every interaction handler in this bot has the same three obligations, and
getting any of them wrong produces a broken feature that is very hard to see
from logs alone:

1. Answer within Discord's 3 second window, or defer first.
2. Never turn a private (ephemeral) reply into a public one.
3. Register a modal before showing it, or its submission is discarded.

These helpers exist so each of those is one correct call rather than a pattern
to re-derive. They are intentionally dependency-free so any cog can import them.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import discord

logger = logging.getLogger(__name__)

__all__ = [
    "safe_defer",
    "safe_edit",
    "safe_reply",
    "send_modal",
    "deregister_modal",
]


async def safe_defer(interaction: discord.Interaction, ephemeral: bool = True) -> bool:
    """Claim Discord's interaction window before doing any slow work.

    Discord allows a handler three seconds to send its first response. A single
    database round trip can outlast that under a cold index, pool exhaustion or
    a reconnect, and when it does Discord shows the user "The application didn't
    respond in time" and discards whatever the bot tried to say.

    Deferring first raises the ceiling from three seconds to fifteen minutes.
    Call this before any database or network work, then finish with
    ``safe_edit``.

    On a button or modal interaction ``ephemeral`` is silently ignored by
    discord.py unless ``thinking`` is also set: those two types defer with
    ``deferred_message_update`` - literally "I will edit the message this
    button/modal came from" - and never attach the ephemeral flag. The
    follow-up ``safe_edit`` would then rewrite the clicked message itself
    instead of replying to the clicker, which is how the matchmaking panel
    lost its embed to "Queue Position" on every "Get A Match" press. Asking
    for the thinking state whenever a private reply was requested makes the
    defer create a new ephemeral message, so the edit lands on that message
    and the clicked one is never touched.

    Returns True if the window is now claimed by this interaction.
    """
    try:
        if not interaction.response.is_done():
            wants_thinking = ephemeral and interaction.type in (
                discord.InteractionType.component,
                discord.InteractionType.modal_submit,
            )
            await interaction.response.defer(ephemeral=ephemeral, thinking=wants_thinking)
        return interaction.response.is_done()
    except discord.HTTPException as e:
        # Almost always an expired token: the user took too long to press
        # the button, or the token was already spent.
        logger.debug(
            "Could not defer interaction %s (likely expired): %s",
            getattr(interaction, "id", "?"), e,
        )
        return False
    except Exception:
        logger.exception("Unexpected failure deferring interaction %s", getattr(interaction, "id", "?"))
        return False


async def safe_edit(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
) -> bool:
    """Finish an interaction that ``safe_defer`` already claimed.

    The edit goes to whatever the first response created. With
    ``safe_defer(interaction, ephemeral=True)`` that is a private message, so
    this can never rewrite a public message the caller only meant to read.
    """
    kwargs: dict[str, Any] = {}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    if not kwargs:
        # edit_original_response requires at least one of these.
        kwargs["content"] = None

    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(**kwargs)
            return True
    except discord.HTTPException as e:
        logger.debug("Could not edit interaction %s: %s", getattr(interaction, "id", "?"), e)
    except Exception:
        logger.exception("Unexpected failure editing interaction %s", getattr(interaction, "id", "?"))

    # Never responded, or the edit failed: try a followup, still private.
    return await safe_reply(interaction, content=content, embed=embed, ephemeral=True, view=view)


async def safe_reply(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    ephemeral: bool = True,
    view: Optional[discord.ui.View] = None,
) -> bool:
    """Reply to an interaction, honouring ``ephemeral``.

    Note there is deliberately no ``interaction.channel.send`` fallback. That is
    an ordinary public channel message, so it would silently publish a reply the
    caller explicitly asked to keep private - and the most common reason the
    response path fails is an expired token, which is exactly when a handler
    that already did its work falls through to the fallback.
    """
    kwargs: dict[str, Any] = {}
    if embed is not None:
        kwargs["embed"] = embed
    else:
        kwargs["content"] = content

    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(ephemeral=ephemeral, view=view, **kwargs)
            return True
        await interaction.followup.send(ephemeral=ephemeral, view=view, **kwargs)
        return True
    except discord.HTTPException as e:
        logger.debug(
            "Could not reply to interaction %s (ephemeral=%s): %s",
            getattr(interaction, "id", "?"), ephemeral, e,
        )
    except Exception:
        logger.exception("Unexpected failure replying to interaction %s", getattr(interaction, "id", "?"))

    # Last resort, still private.
    try:
        await interaction.followup.send(ephemeral=ephemeral, view=view, **kwargs)
        return True
    except Exception:
        logger.debug(
            "Interaction %s could not be answered at all (almost certainly expired)",
            getattr(interaction, "id", "?"), exc_info=True,
        )
        return False


async def send_modal(interaction: discord.Interaction, modal: discord.ui.Modal) -> bool:
    """Register a modal and then show it.

    Registering is the part that is easy to miss. discord.py resolves a modal
    submission by looking its ``custom_id`` up in the view store, and discards
    anything it cannot find:

        _log.debug('Modal interaction referencing unknown item custom_id %s. Discarding', custom_id)

    That is DEBUG level, so in production the submission vanishes with no trace:
    the user fills the form in, presses submit, and gets "The application did
    not respond" while the handler behind it never runs. Each ``Modal()``
    instance generates a fresh random ``custom_id``, so registration has to
    happen per submission and cannot be hoisted to cog_load.

    The client comes from ``interaction.client`` rather than a passed-in cog,
    because the call sites are mostly inside ``discord.ui.View`` subclasses that
    hold ``self.cog`` and not ``self.bot``.

    The modal should deregister itself once handled, so the store does not grow
    for the life of the process.
    """
    client = getattr(interaction, "client", None)
    if client is None:
        logger.error("Cannot show modal for interaction %s: no bot client available", getattr(interaction, "id", "?"))
        return False
    try:
        client.add_view(modal)
    except Exception:
        logger.exception("Could not register modal %s; its submission would be discarded", type(modal).__name__)
        return False
    try:
        await interaction.response.send_modal(modal)
        return True
    except discord.HTTPException as e:
        logger.warning("Could not send modal for interaction %s: %s", getattr(interaction, "id", "?"), e)
        # Do not leave a registration behind for a modal nobody will ever see.
        try:
            client.remove_view(modal)
        except Exception:
            pass
        return False
    except Exception:
        logger.exception("Unexpected failure sending modal for interaction %s", getattr(interaction, "id", "?"))
        try:
            client.remove_view(modal)
        except Exception:
            pass
        return False


def deregister_modal(interaction: discord.Interaction, modal: discord.ui.Modal) -> None:
    """Drop a modal's store entry once it has been handled.

    Registration is per-submission because every ``Modal()`` instance gets a
    fresh random ``custom_id``. That means the store entry is also per-submission:
    nothing removes it on its own, so a bot that shows modals steadily would
    otherwise hold one dead entry for every modal it has ever shown, for the
    life of the process.

    ``remove_view`` keys on class name plus the sorted child ``custom_id``s, so
    it must be given the same instance that was registered - not a fresh one.

    Call this as the first statement of ``on_submit``: it must happen even if
    the handler goes on to fail.
    """
    client = getattr(interaction, "client", None)
    if client is None:
        return
    try:
        client.remove_view(modal)
    except Exception:
        # Nothing actionable - the store is in-memory and per-instance.
        logger.debug("Could not deregister modal %s", type(modal).__name__, exc_info=True)