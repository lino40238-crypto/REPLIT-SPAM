# Bot Telegram - Sistema de Bots Pai/Filho

## Overview
Sistema de bots Telegram com bot pai e bots filhos, gerenciamento de sessions Telethon, disparo de mensagens e verificação anti-robô via WebApp ou Fluxo Chat.

## Recent Changes
- **2026-02-19**: Implementado sistema de Links Redirect
  - Novo botão "🔗 Links Redirect" no painel admin do Bot Pai
  - Coleção `redirects` no MongoDB (code, telegram_url, label, clicks, created_at)
  - Rota Flask `/r/<code>` que abre página no navegador e redireciona pro Telegram após 2.5s
  - Painel para criar links (nome + URL Telegram → gera código aleatório)
  - Contagem de cliques por link
  - Botão para deletar links individuais
  - Template `redirect.html` com design bonito e spinner de loading
- **2026-02-16**: Implementado "Login Session" no Bot Pai
  - Novo botão "🔑 Login Session" no painel admin
  - Lista sessions válidas com número de telefone
  - Solicita código de login via Telethon e captura da mensagem do 777000
  - Botão para desconectar/encerrar session (log_out + remoção do banco)
  - Usa telethon_loop dedicado para operações Telethon, com timeouts e cleanup
  - Comparação de timezone UTC-aware para validar idade do código
- **2026-02-12**: Implementado Bot Fluxo Chat (bot_type="chatflow") - verificação via conversa no chat sem mini app
  - Novo botão "Criar Bot Fluxo Chat" no painel do Bot Pai
  - Handlers: chatflow_start, chatflow_contact, chatflow_text, chatflow_join_request
  - Fluxo: join request → welcome com link start=s → contato → código via teclado numérico → aprovação
  - Telethon usa API_ID/API_HASH do Bot Pai (não do bot filho)
  - Suporte a 2FA, aceita código colado inteiro ou digitado dígito a dígito
  - Limpeza automática de chatflow_states expirados
- **2026-02-10**: Migrado banco de dados de PostgreSQL para MongoDB

## Architecture
- **main.py**: Código principal - Flask server (porta 5000) + Bot Pai + Bots Filhos em threads
  - Bots tipo "miniapp": verificação via WebApp (mini app)
  - Bots tipo "chatflow": verificação via conversa no chat (sem mini app)
- **db.py**: Camada de dados usando MongoDB (pymongo) conectando ao banco `telegram_bots`
- **templates/miniapp.html**: WebApp do Telegram para verificação (usado por bots miniapp)

## Database (MongoDB)
- **Banco**: `telegram_bots`
- **Coleções**:
  - `sessions`: user_id, phone, session_data, bot_id, created_at, updated_at, pending_disparo
  - `bots`: token, name, api_id, api_hash, webapp_url, bot_type (miniapp|chatflow), active, created_at, updated_at
  - `config`: bot_id, msg_disparo, intervalo_horas, mensagem, botao, intervalo_minutos
  - `redirects`: code, telegram_url, label, clicks, created_at, updated_at

## Secrets
- `MONGODB_URI`: URI de conexão MongoDB
- `BOT_TOKEN`: Token do bot pai
- `API_ID`: Telegram API ID
- `API_HASH`: Telegram API Hash
- `ADMIN_ID`: ID do admin no Telegram
- `WEBAPP_URL`: URL do WebApp

## Stack
- Python 3.11
- Flask (servidor web)
- python-telegram-bot (bot pai/filhos)
- Telethon (sessions de usuário)
- pymongo (MongoDB)
