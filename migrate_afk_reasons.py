"""Repair AFK reasons mangled by the old escape_markdown call.

Before commit 0d65a6a the AFK cog stored reasons via
``discord.utils.escape_markdown``, which escapes ``_``. A reason containing a
custom emoji was written as ``<:eclairs\\_cutecry:123>``, which Discord renders
as literal text instead of the emoji.

This script rewrites only the ``reason`` field, unescaping backslashes that sit
inside an emoji mention and leaving everything else untouched. It is safe to
re-run: rows that are already correct are skipped.

Usage:
    python migrate_afk_reasons.py --dry-run    # report only, no writes
    python migrate_afk_reasons.py              # apply

Requires MONGO_URL in the environment or a .env file.
"""
import argparse
import os
import re
import sys

from pymongo import MongoClient, errors

# An emoji mention that still contains markdown escapes inside it, e.g.
# <:eclairs\_cutecry:123> or <a:zz\_uma\_sa:123>.
BROKEN_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_\\]{2,40}:\d{17,20}>")


def load_mongo_url() -> str:
    url = os.getenv("MONGO_URL")
    if not url:
        from dotenv import load_dotenv
        load_dotenv()
        url = os.getenv("MONGO_URL")
    if not url:
        print("MONGO_URL not found in environment or .env file")
        sys.exit(1)
    return url


def fix_reason(reason: str) -> str:
    """Restore emoji mentions, leave the rest of the string as-is.

    Only backslashes *within* an emoji mention are removed. A literal ``\\_``
    in plain text is a legitimately escaped underscore and must survive, so
    this deliberately does not touch anything outside the mention.
    """
    if not isinstance(reason, str) or not reason:
        return reason
    if not BROKEN_EMOJI_RE.search(reason):
        return reason
    return BROKEN_EMOJI_RE.sub(lambda m: m.group(0).replace("\\_", "_"), reason)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change without writing")
    args = parser.parse_args()

    client = MongoClient(load_mongo_url(), serverSelectionTimeoutMS=10000)
    try:
        client.admin.command("ping")
    except errors.ServerSelectionTimeoutError:
        print("Failed to connect to MongoDB")
        return 1

    # Matches the AFK cog: database discord_bot, collection afk.
    collection = client["discord_bot"]["afk"]

    total = collection.count_documents({})
    if not total:
        print("No AFK records found; nothing to do.")
        return 0

    print(f"Scanning {total} AFK record(s)...")

    changed = 0
    for doc in collection.find({}, {"_id": 1, "user_id": 1, "reason": 1}):
        original = doc.get("reason")
        repaired = fix_reason(original)
        if repaired == original:
            continue
        changed += 1
        print(f"  user {doc.get('user_id')}: {original!r} -> {repaired!r}")
        if not args.dry_run:
            collection.update_one(
                {"_id": doc["_id"]}, {"$set": {"reason": repaired}}
            )

    if args.dry_run:
        print(f"\nDry run: {changed} record(s) would be updated. Nothing written.")
    else:
        print(f"\nUpdated {changed} record(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
