# Reminders Cog

A production-ready scheduled reminder system with a fully interactive admin
dashboard. Everything is configured with slash commands, buttons, select menus
and modals — no prefix commands, no copy-pasted JSON.

Reminders are stored in MongoDB, so they survive restarts and code reloads.

## Commands

All commands live in the `/reminder` group, are **server-only**, and are hidden
from members without **Manage Server** (Administrators always see them).

| Command | What it does |
| --- | --- |
| `/reminder dashboard` | Opens the interactive dashboard (the main entry point) |
| `/reminder create` | Opens the six-step creation wizard |
| `/reminder manage` | Paginated manager: pick a reminder to edit |
| `/reminder list` | Read-only listing of every reminder, paginated |
| `/reminder preview <reminder>` | Shows the exact message that will be posted, without posting it |
| `/reminder pause <reminder>` | Pauses one reminder |
| `/reminder resume <reminder>` | Resumes one reminder (recomputes its next run) |
| `/reminder delete <reminder>` | Deletes one reminder, behind a confirmation panel |
| `/reminder help` | In-Discord explanation of schedules, pings and limits |

`<reminder>` options autocomplete over the guild's reminder names and IDs.

## The dashboard

```
⏰ Reminder dashboard
  Overview: 4 total • 3 active • 1 paused
  Next up:  • Standup — in 2 hours (#general)
            • Weekly report — in 3 days (#logs)
  [➕ Create reminder] [📋 Manage reminders] [🔄 Refresh]
  [⏸️ Pause all] [▶️ Resume all] [❓ Help]
```

Every panel is **ephemeral** (only the admin who opened it can see or use it)
and step changes edit the same message instead of spamming new ones. Panels
disarm themselves after 15 minutes idle, and each interaction re-checks both
that the clicker is the opener and that they still have **Manage Server**.

### Creating a reminder

Six steps, each one a select menu, a modal, or a picker:

1. **Name & message** — modal
2. **Frequency** — select: one time · repeating interval · daily · weekly · monthly
3. **When** — modal for a date/time (`2026-10-20 18:30` or `in 2h30m`), an
   interval (`30m`, `2h`, `1d 6h`), or a time of day (`18:30`, `6pm`).
   Weekly and monthly add a weekday multi-select / day-of-month picker.
4. **Timezone** — paginated select of common zones (with live UTC offsets),
   plus **Other** to type any IANA name
5. **Channel** — native channel picker (text, announcement, voice, threads),
   plus an ID fallback
6. **Role** — native role picker, with an explicit **No role (no ping)** option

A final review panel shows everything configured and requires an explicit
**Create reminder** press before anything is written.

### Managing a reminder

Pressing a reminder in the manager opens its detail panel with live status,
next run (in the reader's own local time), delivery counters, last run and last
error — and these buttons:

| Button | Effect |
| --- | --- |
| ✏️ Name & message | Modal, prefilled |
| 🕒 Reschedule | Re-runs the timing step for the reminder's current schedule type |
| 🔁 Frequency | Change the schedule type (then review and apply) |
| 🌐 Timezone | Paginated timezone picker |
| 📢 Channel | Native channel picker |
| 🔔 Role | Native role picker / no ping |
| ⏸️ Pause / ▶️ Resume | Toggles delivery; resume recomputes the next run |
| 🗑️ Delete | Confirmation panel before anything is removed |
| 🔍 Preview | The exact outgoing message, including the ping line, without posting |
| 🧪 Send test | Posts a real test message (with a confirmation step) |

Every edit lands on a review panel with **Apply changes**, so a mistake is one
back-button away from being discarded.

## Schedule types

| Type | Behaviour |
| --- | --- |
| `once` | Fires once and is marked completed (never replayed, even after a restart) |
| `interval` | Every `30m` / `2h` / `1d` … aligned to its original cadence |
| `daily` | Every day at a wall-clock time in the reminder's timezone |
| `weekly` | On the chosen weekdays at a wall-clock time |
| `monthly` | On the chosen day of the month; shorter months use their last day |

Timezones use the IANA database via `zoneinfo`. Days on which clocks jump
forward still produce a real delivery instant rather than silently skipping.

## Pings and permissions

* The configured role is pinged with `<@&role>` — **only** when the role is
  mentionable, or when the bot has **Mention @everyone**.
* If the role cannot be pinged, the reminder posts the role's *name* instead and
  says so on the detail panel. A reminder never falls back to an `@everyone`
  mention.
* Every send passes an explicit `AllowedMentions(everyone=False, users=False,
  roles=…)`, so nothing in a reminder message can ping more than intended.
* Reminders never ping `@everyone` or `@here`.

**What the bot needs in the target channel:** View Channel, Send Messages.

## Data model

Collection `discord_bot.reminders` (database name overridable with
`REMINDER_DB_NAME`), one document per reminder:

```js
{
  guild_id, reminder_id,            // scoped lookup + short public ID
  name, message,                    // content
  schedule_type,                    // once | interval | daily | weekly | monthly
  once_at, interval_seconds,        // once / interval
  hour, minute, weekdays, day_of_month,  // daily / weekly / monthly
  timezone,                         // IANA name
  channel_id, role_id,              // destination + optional ping target
  status,                           // active | paused | failed | completed
  next_run_at,                      // the authoritative scheduling field (UTC)
  created_by, created_at, updated_at,
  last_run_key, claimed_at,         // duplicate-prevention bookkeeping
  last_delivered_at, last_failed_at, delivered_count, failure_count, last_error
}
```

Indexes (created on cog load, idempotent):

* `{guild_id, reminder_id}` — **unique**, so a reminder ID cannot collide
* `{status, next_run_at}` — the scheduler's due query
* `{guild_id, created_at}` — dashboard listings

## Reliability

* **Survives restarts.** The schedule lives in the database, not in memory.
  `next_run_at` is the single source of truth and the scheduler simply looks for
  `status: active, next_run_at <= now`.
* **No duplicate notifications.** Claiming is a compare-and-set: the update that
  advances `next_run_at` only matches the *previous* value, so the same
  occurrence can be handed out at most once — even with overlapping ticks, two
  bot processes on one database, or a crash mid-delivery. A one-off is claimed
  by being marked `completed`, so it can never replay.
* **Sensible catch-up.** After downtime, an overdue reminder fires exactly once
  and then resynchronises to its normal cadence instead of flooding the channel
  with every missed occurrence.
* **Failure handling.** Missing permissions or a deleted channel mark the
  reminder `failed` and DM the creator instead of retrying forever. Transient
  Discord errors are retried with backoff for one-off reminders, and after
  `MAX_CONSECUTIVE_FAILURES` a reminder pauses itself.
* **Gateway safety.** Nothing is claimed until the client is `ready`, so a
  reconnect cannot mark good reminders as failed because the channel cache is
  empty.

## Validation and limits

| Limit | Value |
| --- | --- |
| Reminders per server | 100 |
| Name length | 100 characters |
| Message length | 1500 characters |
| Minimum interval | 60 seconds |
| Maximum interval | ~1 year |
| Scheduler tick | every 30 seconds |
| Deliveries per tick | 25 |

Inputs are validated before anything is saved: empty names/messages, malformed
durations and times, unknown timezones, past one-off times, intervals that are
too short or too long, weekly schedules with no weekdays, and missing channel
permissions are all rejected with a specific, human-readable reason.

## Files

| File | Responsibility |
| --- | --- |
| `config.py` | Constants: limits, colours, timezone shortlist, schedule types |
| `store.py` | Time/schedule maths + the MongoDB store and its atomic claim |
| `views.py` | All UI: dashboard, manager, detail panel, wizard steps, modals |
| `reminders.py` | The cog: slash commands, panel navigation, delivery loop |

`store.py` and `views.py` are helper modules, not extensions — they are listed
in `main.py`'s `skip_files` alongside the other shared modules.

## Tests

```bash
python -m pytest tests/test_reminders.py -q
```

89 tests cover duration/time/date parsing, timezone resolution, the
next-occurrence engine (including DST and month clamping), schedule validation,
the delivery path and its failure modes, duplicate-notification prevention
against both an in-memory collection and real Mongo-API semantics, every
interactive component's construction and wiring, and the serialised command
tree. `mongomock-motor` is optional — that one test skips if it is missing.
