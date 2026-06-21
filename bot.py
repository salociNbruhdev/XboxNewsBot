import os
import json
import logging
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
from google import genai

# ==========================================
# CONFIGURAÇÕES E INICIALIZAÇÃO
# ====================  ======================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
load_dotenv()

# Carregar variáveis de ambiente
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Inicializar IA e Bot
client = genai.Client(api_key=GEMINI_API_KEY)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Carregar config de canais e roles (config.json)
try:
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except:
    config = {"source_channel": 0, "dest_channel": 0, "roles": {}}

# ==========================================
# FUNÇÕES DE APOIO
# ==========================================
def mark_as_processed(message_id):
    """Guarda o ID da mensagem para evitar duplicação."""
    data = {"processed": []}
    if os.path.exists("processed.json"):
        try:
            with open("processed.json", "r", encoding="utf-8") as f:
                data = json.load(f)
        except: pass
    
    data["processed"].append(message_id)
    # Mantém apenas os últimos 500 para não encher o disco
    data["processed"] = data["processed"][-500:] 
    with open("processed.json", "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

async def processar_changelog_ia(projeto, texto):
    """Resumo via IA com tratamento automático de limite de taxa."""
    # Adicionado comando de concisão no final do prompt
    prompt = f"Traduza para português BR e crie um resumo organizado em tópicos curtos para o projeto {projeto.upper()}. Seja conciso e limite o resumo a no máximo 1500 caracteres. Aqui está o conteúdo:\n\n{texto}"
    
    for tentativa in range(3):
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model='gemini-2.5-flash',
                contents=prompt
            )
            return response.text
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                await asyncio.sleep(3) # Espera 3s e tenta de novo
                continue
            logging.error(f"Erro IA: {e}")
            return None
    return None

# ==========================================
# EVENTOS DO BOT
# ==========================================
@bot.event
async def on_message(message):
    # Ignora mensagens do próprio bot ou de canais errados
    if message.author == bot.user or message.channel.id != config.get("source_channel"):
        return

    # Lista de projetos monitorados (kyuubi corrigido)
    projetos_alvo = ["xbguard", "tethered", "proto", "kyuubi", "teapot"]
    projeto = next((p for p in projetos_alvo if p in message.content.lower()), None)
    
    if not projeto: return

    # Verifica se a mensagem já foi processada
    if os.path.exists("processed.json"):
        with open("processed.json", "r", encoding="utf-8") as f:
            try: data = json.load(f)
            except: data = {"processed": []}
        if message.id in data.get("processed", []): return

    # Inicia processamento visual
    await message.add_reaction("⚙️")
    
    texto_resumido = await processar_changelog_ia(projeto, message.content)
    
    if texto_resumido:
        dest_channel = bot.get_channel(config.get("dest_channel"))
        role_id = config.get("roles", {}).get(projeto)
        mention = f"\n\n<@&{role_id}>" if role_id else ""
        
# ... (resto do seu código on_message igual acima)

    if texto_resumido:
        dest_channel = bot.get_channel(config.get("dest_channel"))
        role_id = config.get("roles", {}).get(projeto)
        mention = f"\n\n<@&{role_id}>" if role_id else ""
        
        if dest_channel:
            # Monta a mensagem completa primeiro
            mensagem_completa = f"📢 **{projeto.upper()} - Novo Changelog**\n\n{texto_resumido}{mention}"
            
            # SISTEMA DE SEGURANÇA: Se passar de 2000 caracteres, divide o envio
            if len(mensagem_completa) > 2000:
                # Fatia em pedaços de 1900 caracteres para garantir uma margem de segurança
                fatias = [mensagem_completa[i:i+1900] for i in range(0, len(mensagem_completa), 1900)]
                for fatia in fatias:
                    await dest_channel.send(fatia)
            else:
                # Envio normal caso seja menor que 2000 caracteres
                await dest_channel.send(mensagem_completa)
                
            await message.add_reaction("✅")
            mark_as_processed(message.id)
        else:
            await message.add_reaction("❌")
            logging.error("Canal de destino não encontrado no config.json")
    else:
        await message.add_reaction("❌")
    
    await message.remove_reaction("⚙️", bot.user)

@bot.event
async def on_ready():
    logging.info(f"Bot online e operante. Monitorando changelogs!")

bot.run(DISCORD_TOKEN)
