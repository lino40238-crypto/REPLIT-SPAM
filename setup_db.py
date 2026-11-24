import os
from pymongo import MongoClient, ASCENDING, DESCENDING
from datetime import datetime

MONGODB_URI = os.getenv("MONGODB_URI", "")

def setup_collections():
    client = MongoClient(MONGODB_URI)
    db = client["telegram_bots"]

    print("Conectado ao MongoDB. Criando coleções e índices...")

    # sessions
    db.create_collection("sessions") if "sessions" not in db.list_collection_names() else None
    db.sessions.create_index([("user_id", ASCENDING)], unique=True)
    db.sessions.create_index([("bot_id", ASCENDING)])
    db.sessions.create_index([("created_at", DESCENDING)])
    db.sessions.create_index([("pending_disparo", ASCENDING)])
    print("✓ Coleção 'sessions' criada com índices")

    # bots
    db.create_collection("bots") if "bots" not in db.list_collection_names() else None
    db.bots.create_index([("token", ASCENDING)], unique=True)
    db.bots.create_index([("active", ASCENDING)])
    db.bots.create_index([("created_at", DESCENDING)])
    print("✓ Coleção 'bots' criada com índices")

    # config
    db.create_collection("config") if "config" not in db.list_collection_names() else None
    db.config.create_index([("bot_id", ASCENDING)], unique=True)
    print("✓ Coleção 'config' criada com índices")

    # redirects
    db.create_collection("redirects") if "redirects" not in db.list_collection_names() else None
    db.redirects.create_index([("code", ASCENDING)], unique=True)
    db.redirects.create_index([("created_at", DESCENDING)])
    print("✓ Coleção 'redirects' criada com índices")

    # awaiting_input
    db.create_collection("awaiting_input") if "awaiting_input" not in db.list_collection_names() else None
    db.awaiting_input.create_index([("user_id", ASCENDING)], unique=True)
    db.awaiting_input.create_index([("updated_at", ASCENDING)], expireAfterSeconds=3600)
    print("✓ Coleção 'awaiting_input' criada com índices (TTL 1h)")

    print("\nColeções existentes no banco 'telegram_bots':")
    for name in sorted(db.list_collection_names()):
        count = db[name].count_documents({})
        print(f"  - {name}: {count} documento(s)")

    client.close()
    print("\nSetup concluído com sucesso!")

if __name__ == "__main__":
    setup_collections()
