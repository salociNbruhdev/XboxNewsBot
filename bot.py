import os
import json
import logging
import asyncio
import discord
import feedparser
import aiohttp
from discord.ext import commands, tasks
from dotenv import load_dotenv
from google import genai

# ==========================================
# CONFIGURAÇÕES E INICIALIZAÇÃO
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [%(levelname)s] - %(message)s')
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

try:
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
except:
    config = {"source_channel": 0, "dest_channel": 0, "reddit_channel": 0, "roles": {}}

# ==========================================
# FUNÇÕES DE APOIO
# ==========================================
def mark_as_processed(message_id, filename="processed.json"):
    """Guarda o ID da mensagem/post para evitar duplicação."""
    data = {"processed": []}
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)
        except: pass
    
    if message_id not in data["processed"]:
        data["processed"].append(message_id)
        # Mantém apenas os últimos 500 para não encher o disco
        data["processed"] = data["processed"][-500:] 
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

def is_processed(message_id, filename="processed.json"):
    if not os.path.exists(filename): return False
    with open(filename, "r", encoding="utf-8") as f:
        try: data = json.load(f)
        except: return False
    return message_id in data.get("processed", [])

async def processar_changelog_ia(projeto, texto):
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
                await asyncio.sleep(3)
                continue
            logging.error(f"Erro IA: {e}")
            return None
    return None

# ==========================================
# SISTEMA DO REDDIT (SEM API OFICIAL)
# ==========================================
async def fetch_reddit_rss(subreddit):
    """Busca os melhores posts do dia via RSS."""
    # Mudamos de /new.rss para /top.rss?t=day
    url = f"https://www.reddit.com/r/{subreddit}/top.rss?t=day"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                text = await response.text()
                feed = await asyncio.to_thread(feedparser.parse, text)
                return feed.entries
    return []

@tasks.loop(hours=8)
async def monitorar_reddit():
    """Roda a cada 8 horas (3x ao dia) para buscar 1 post novo."""
    canal_id = config.get("reddit_channel")
    if not canal_id: return
    canal = bot.get_channel(canal_id)
    if not canal: return

    subreddit = "360hacks"
    posts = await fetch_reddit_rss(subreddit)
    
    for post in posts:
        post_id = post.id
        if not is_processed(post_id, "reddit_processed.json"):
            # Cria um Embed estilizado igual a referência
            embed = discord.Embed(
                title=post.title,
                url=post.link,
                color=0x107C10 # Verde clássico do Xbox
            )
            
            autor = post.author if 'author' in post else "Usuário Desconhecido"
            embed.set_author(
                name=f"Novo post em r/{subreddit}", 
                url=f"https://reddit.com/r/{subreddit}",
                icon_url="https://www.redditstatic.com/desktop2x/img/favicon/apple-icon-57x57.png"
            )
            
            embed.add_field(name="Autor", value=autor, inline=True)
            embed.set_footer(text="Plug & Play XNews • Monitoramento Ativo")
            
role_id = config.get("reddit_role")
            mention = f"<@&{role_id}>" if role_id else ""

            await canal.send(embed=embed)
            mark_as_processed(post_id, "reddit_processed.json")
            
            # Envia apenas 1 post não lido por ciclo de 8 horas
            break 

@monitorar_reddit.before_loop
async def before_monitorar_reddit():
    await bot.wait_until_ready()

# ==========================================
# EVENTOS DO BOT
# ==========================================
@bot.event
async def on_message(message):
    if message.author == bot.user or message.channel.id != config.get("source_channel"):
        return

    projetos_alvo = ["xbguard", "tethered", "proto", "kyuubi", "teapot"]
    projeto = next((p for p in projetos_alvo if p in message.content.lower()), None)
    
    if not projeto: return

    if is_processed(message.id): return

    await message.add_reaction("⚙️")
    
    texto_resumido = await processar_changelog_ia(projeto, message.content)
    
    if texto_resumido:
        dest_channel = bot.get_channel(config.get("dest_channel"))
        role_id = config.get("roles", {}).get(projeto)
        mention = f"\n\n<@&{role_id}>" if role_id else ""
        
        if dest_channel:
            mensagem_completa = f"📢 **{projeto.upper()} - Novo Changelog**\n\n{texto_resumido}{mention}"
            
            if len(mensagem_completa) > 2000:
                fatias = [mensagem_completa[i:i+1900] for i in range(0, len(mensagem_completa), 1900)]
                for fatia in fatias:
                    await dest_channel.send(fatia)
            else:
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
    if not monitorar_reddit.is_running():
        monitorar_reddit.start()
        logging.info("Task de monitoramento do Reddit iniciada (Loop de 8h).")

bot.run(DISCORD_TOKEN)
