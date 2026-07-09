import sqlite3
import json
import os
import sys
from pymongo import MongoClient, errors
from datetime import datetime

MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    from dotenv import load_dotenv
    load_dotenv()
    MONGO_URL = os.getenv("MONGO_URL")

if not MONGO_URL:
    print("MONGO_URL not found in environment or .env file")
    sys.exit(1)

print(f"Connecting to MongoDB...")
client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=10000)

try:
    client.admin.command('ping')
    print("Connected to MongoDB successfully!")
except errors.ServerSelectionTimeoutError as e:
    print(f"Failed to connect to MongoDB: {e}")
    sys.exit(1)

db = client['discord_bot']
counting_db = client['counting_bot']
leaderboard_db = client['poison_bot']
confessions_db = client['confessions']
mute_db = client['discord_mute_system']
threads_db = client['threads']
giveaways_db = client['giveaways']
media_db = client['media_only_bot']
dragme_db = client['dragmebot']
skull_db = client['skullboard_db']
role_db = client['role_manager']

def migrate_sqlite(db_path, mongo_db, collection_name, table_name=None):
    if not os.path.exists(db_path):
        print(f"  [SKIP] {db_path} not found")
        return 0
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        tables = [table_name] if table_name else []
        if not table_name:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
        
        total = 0
        for table in tables:
            try:
                cursor.execute(f"SELECT * FROM \"{table}\"")
                rows = [dict(row) for row in cursor.fetchall()]
                if rows:
                    collection = mongo_db[collection_name or table]
                    collection.delete_many({})
                    result = collection.insert_many(rows)
                    total += len(result.inserted_ids)
                    print(f"  Migrated {len(result.inserted_ids)} rows -> {mongo_db.name}.{collection.name}")
                else:
                    print(f"  [EMPTY] Table '{table}' has no data")
            except sqlite3.OperationalError as e:
                print(f"  [ERROR] Table '{table}': {e}")
        
        conn.close()
        return total
    except Exception as e:
        print(f"  [ERROR] {db_path}: {e}")
        return 0

def migrate_json(json_path, mongo_db, collection_name):
    if not os.path.exists(json_path):
        print(f"  [SKIP] {json_path} not found")
        return 0
    
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
        
        if not data:
            print(f"  [EMPTY] {json_path} has no data")
            return 0
        
        if isinstance(data, dict):
            docs = [{"key": k, "value": v} for k, v in data.items()]
            collection = mongo_db[collection_name]
            collection.delete_many({})
            result = collection.insert_many(docs)
            print(f"  Migrated {len(result.inserted_ids)} keys -> {mongo_db.name}.{collection.name}")
            return len(result.inserted_ids)
        elif isinstance(data, list):
            collection = mongo_db[collection_name]
            collection.delete_many({})
            result = collection.insert_many(data)
            print(f"  Migrated {len(result.inserted_ids)} docs -> {mongo_db.name}.{collection.name}")
            return len(result.inserted_ids)
        else:
            collection = mongo_db[collection_name]
            collection.delete_many({})
            collection.insert_one({"data": data})
            print(f"  Migrated 1 doc -> {mongo_db.name}.{collection.name}")
            return 1
    except Exception as e:
        print(f"  [ERROR] {json_path}: {e}")
        return 0

print("\n" + "="*60)
print("STARTING DATABASE MIGRATION")
print("="*60)

total_records = 0
print("\n--- Migrating ban_config.db ---")
total_records += migrate_sqlite(
    "database/ban_config.db", db, "ban_config", "ban_config"
)

print("\n--- Migrating autoresponses.db ---")
total_records += migrate_sqlite(
    "database/autoresponses.db", db, "autoresponses", "autoresponses"
)

print("\n--- Migrating deleted_messages.db ---")
total_records += migrate_sqlite(
    "database/deleted_messages.db", db, "deleted_messages", "deleted_messages"
)

print("\n--- Migrating greetings.db ---")
total_records += migrate_sqlite(
    "database/greetings.db", db, "greeting_channels", "greeting_channels"
)
total_records += migrate_sqlite(
    "database/greetings.db", db, "greeting_history", "greeting_history"
)

print("\n--- Migrating drops.db ---")
total_records += migrate_sqlite(
    "database/drops.db", db, "drops", "drops"
)
total_records += migrate_sqlite(
    "database/drops.db", db, "drop_cooldowns", "cooldowns"
)
total_records += migrate_sqlite(
    "database/drops.db", db, "drop_claim_logs", "claim_logs"
)

print("\n--- Migrating activity_config.db ---")
total_records += migrate_sqlite(
    "database/activity_config.db", db, "activity_config", "guild_config"
)

print("\n--- Migrating matchmaker.sqlite ---")
matchmaker_tables = [
    "guild_config", "waiting_queue", "matches", "recent_blocks",
    "match_skips", "queue_history", "queue_panels", "pending_deletions",
    "user_prefs", "dm_messages"
]
for table in matchmaker_tables:
    total_records += migrate_sqlite(
        "database/matchmaker.sqlite", db, f"matchmaker_{table}", table
    )

print("\n--- Migrating vc_data.json ---")
total_records += migrate_json("database/vc_data.json", db, "vc_data")

print("\n--- Migrating command_sync_cache.json ---")
total_records += migrate_json("database/command_sync_cache.json", db, "command_sync_cache")

print("\n" + "="*60)
print(f"MIGRATION COMPLETE: {total_records} total records migrated")
print("="*60)

client.close()
print("MongoDB connection closed.")
