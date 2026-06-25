import os
import json
import logging
import asyncio
import discord
import feedparser
import aiohttp
import random
import sqlite3
from datetime import datetime, timedelta
from discord import app_commands
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'banco_bot.db')

client = genai.Client(api_key=GEMINI_API_KEY)
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=["!", "/"], intents=intents)

# Carregar config original
try:
    config_path = os.path.join(BASE_DIR, "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
except:
    config = {"source_channel": 0, "dest_channel": 0, "reddit_channel": 0, "roles": {}}

# Carregar o banco de perguntas do Quiz
try:
    quiz_path = os.path.join(BASE_DIR, "perguntas_xbox.json")
    with open(quiz_path, "r", encoding="utf-8") as f:
        lista_perguntas = json.load(f)
except Exception as e:
    logging.error(f"Erro ao carregar perguntas_xbox.json: {e}")
    lista_perguntas = []

# ==========================================
# SISTEMA DE BANCO DE DADOS (SQLITE)
# ==========================================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        quiz_points INTEGER DEFAULT 0,
                        daily_streak INTEGER DEFAULT 0,
                        max_streak INTEGER DEFAULT 0,
                        last_daily TEXT
                    )''')
        conn.commit()

init_db()

def get_or_create_user(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT quiz_points, daily_streak, max_streak, last_daily FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row: return row
        c.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
        return (0, 0, 0, None)

def add_quiz_points(user_id, points=1):
    get_or_create_user(user_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET quiz_points = quiz_points + ? WHERE user_id = ?", (points, user_id))
        conn.commit()

def process_diaria(user_id, acertou: bool):
    _, daily_streak, max_streak, last_daily = get_or_create_user(user_id)
    hoje = datetime.now().date()
    ontem = hoje - timedelta(days=1)
    
    last_date = datetime.strptime(last_daily, "%Y-%m-%d").date() if last_daily else None
    
    novo_streak = 0
    if acertou:
        if last_date == ontem:
            novo_streak = daily_streak + 1
        else:
            novo_streak = 1 # Primeiro dia ou perdeu dias no meio
    else:
        novo_streak = 0 # Errou, reseta o streak
        
    novo_max = max(max_streak, novo_streak)
    
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE users SET daily_streak = ?, max_streak = ?, last_daily = ? WHERE user_id = ?", 
                     (novo_streak, novo_max, str(hoje), user_id))
        conn.commit()
    return novo_streak

# ==========================================
# FUNÇÕES DE APOIO
# ==========================================
def mark_as_processed(message_id, filename="processed.json"):
    filepath = os.path.join(BASE_DIR, filename)
    data = {"processed": []}
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except: pass
    
    if message_id not in data["processed"]:
        data["processed"].append(message_id)
        data["processed"] = data["processed"][-500:] 
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

def is_processed(message_id, filename="processed.json"):
    filepath = os.path.join(BASE_DIR, filename)
    if not os.path.exists(filepath): return False
    with open(filepath, "r", encoding="utf-8") as f:
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
# SISTEMA DO REDDIT
# ==========================================
async def fetch_reddit_rss(subreddit):
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
    canal_id = config.get("reddit_channel")
    if not canal_id: return
    canal = bot.get_channel(canal_id)
    if not canal: return

    subreddit = "360hacks"
    posts = await fetch_reddit_rss(subreddit)
    
    for post in posts:
        post_id = post.id
        if not is_processed(post_id, "reddit_processed.json"):
            embed = discord.Embed(title=post.title, url=post.link, color=0x107C10)
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
            await canal.send(content=mention, embed=embed)
            mark_as_processed(post_id, "reddit_processed.json")
            break

@monitorar_reddit.before_loop
async def before_monitorar_reddit():
    await bot.wait_until_ready()


# ==========================================
# INTERFACE DE BOTÕES DO QUIZ (VIEW)
# ==========================================
class QuizBotoes(discord.ui.View):
    def __init__(self, lancar_interaction: discord.Interaction, pergunta_atual, is_diaria=False):
        super().__init__(timeout=30.0)
        self.lancar_interaction = lancar_interaction
        self.pergunta_atual = pergunta_atual
        self.resposta_certa = pergunta_atual['resposta_correta']
        self.is_diaria = is_diaria
        self.foi_respondido = False

    async def responder(self, interaction: discord.Interaction, opcao_escolhida: str):
        if self.foi_respondido: return
        self.foi_respondido = True
        self.stop()

        acertou = (opcao_escolhida == self.resposta_certa)
        user_id = interaction.user.id

        if acertou:
            if self.is_diaria:
                streak = process_diaria(user_id, True)
                embed_resultado = discord.Embed(
                    title="🏆 Acertaste a Diária!", 
                    description=f"🔥 O teu streak agora é de **{streak} dia(s)** consecutivos!",
                    color=discord.Color.blue()
                )
            else:
                add_quiz_points(user_id, 1)
                embed_resultado = discord.Embed(
                    title="🏆 Acertaste em cheio!", 
                    description="+1 Ponto adicionado ao teu perfil!",
                    color=discord.Color.blue()
                )
        else:
            if self.is_diaria:
                process_diaria(user_id, False)
                embed_resultado = discord.Embed(
                    title="❌ Erraste...", 
                    description=f"Perdeste o teu streak diário. A resposta correta era a **{self.resposta_certa}**.",
                    color=discord.Color.red()
                )
            else:
                embed_resultado = discord.Embed(
                    title="❌ Erraste...", 
                    description=f"A resposta correta era a **{self.resposta_certa}**.",
                    color=discord.Color.red()
                )
        
        embed_resultado.add_field(name="💡 Curiosidade:", value=self.pergunta_atual['curiosidade'], inline=False)
        
        for item in self.children:
            item.disabled = True
            if item.label == self.resposta_certa:
                item.style = discord.ButtonStyle.success
            elif item.label == opcao_escolhida:
                item.style = discord.ButtonStyle.danger

        await interaction.response.edit_message(embed=embed_resultado, view=self)

    @discord.ui.button(label="A", style=discord.ButtonStyle.primary)
    async def click_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.responder(interaction, "A")

    @discord.ui.button(label="B", style=discord.ButtonStyle.primary)
    async def click_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.responder(interaction, "B")

    @discord.ui.button(label="C", style=discord.ButtonStyle.primary)
    async def click_c(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.responder(interaction, "C")

    @discord.ui.button(label="D", style=discord.ButtonStyle.primary)
    async def click_d(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.responder(interaction, "D")

    async def on_timeout(self):
        if not self.foi_respondido:
            self.foi_respondido = True
            user_id = self.lancar_interaction.user.id
            
            # Se der timeout na diária, conta como erro e perde o streak
            if self.is_diaria:
                process_diaria(user_id, False)
                desc = f"O tempo esgotou e perdeste o teu streak! A resposta era **{self.resposta_certa}**."
            else:
                desc = f"Demoraste muito tempo. A resposta correta era **{self.resposta_certa}**."

            for item in self.children:
                item.disabled = True
            
            embed_timeout = discord.Embed(
                title="⏰ O tempo esgotou!", 
                description=desc,
                color=discord.Color.orange()
            )
            embed_timeout.add_field(name="💡 Curiosidade:", value=self.pergunta_atual['curiosidade'], inline=False)
            
            try:
                await self.lancar_interaction.edit_original_response(embed=embed_timeout, view=self)
            except:
                pass


# ==========================================
# COMANDOS (QUIZ, DIARIA, RANK)
# ==========================================
@bot.tree.command(name="quiz", description="Testa os teus conhecimentos e responde a uma pergunta sobre Xbox 360!")
async def quiz_xbox(interaction: discord.Interaction):
    if not lista_perguntas:
        await interaction.response.send_message("❌ As perguntas não foram carregadas com sucesso.", ephemeral=True)
        return

    pergunta_atual = random.choice(lista_perguntas)
    texto_opcoes = "\n".join(pergunta_atual['opcoes'])
    
    embed_pergunta = discord.Embed(
        title="🎮 Quiz Xbox 360!", 
        description=f"**{pergunta_atual['pergunta']}**", 
        color=0x107C10
    )
    embed_pergunta.add_field(name="Opções:", value=texto_opcoes, inline=False)
    embed_pergunta.set_footer(text="⏳ Clica num dos botões abaixo para responder. Tens 30 segundos!")
    
    view_botoes = QuizBotoes(interaction, pergunta_atual, is_diaria=False)
    await interaction.response.send_message(embed=embed_pergunta, view=view_botoes, ephemeral=True)


@bot.tree.command(name="diaria", description="Responde à tua pergunta do dia e mantém o teu streak vivo!")
async def diaria_xbox(interaction: discord.Interaction):
    if not lista_perguntas:
        await interaction.response.send_message("❌ As perguntas não foram carregadas com sucesso.", ephemeral=True)
        return

    _, _, _, last_daily = get_or_create_user(interaction.user.id)
    hoje = str(datetime.now().date())
    
    if last_daily == hoje:
        await interaction.response.send_message("⏳ Já jogaste a tua diária hoje! Volta amanhã para manteres o teu streak.", ephemeral=True)
        return

    pergunta_atual = random.choice(lista_perguntas)
    texto_opcoes = "\n".join(pergunta_atual['opcoes'])
    
    embed_pergunta = discord.Embed(
        title="📅 Pergunta Diária Xbox 360!", 
        description=f"**{pergunta_atual['pergunta']}**\n\nNão erres para não perderes a tua ofensiva!", 
        color=0x107C10
    )
    embed_pergunta.add_field(name="Opções:", value=texto_opcoes, inline=False)
    embed_pergunta.set_footer(text="⏳ Clica num dos botões. Tens 30 segundos!")
    
    view_botoes = QuizBotoes(interaction, pergunta_atual, is_diaria=True)
    await interaction.response.send_message(embed=embed_pergunta, view=view_botoes, ephemeral=True)


@bot.tree.command(name="rank", description="Veja os líderes do bot!")
@app_commands.describe(tipo="Escolha qual rank visualizar")
@app_commands.choices(tipo=[
    app_commands.Choice(name="Quiz (Pontos Gerais)", value="quiz"),
    app_commands.Choice(name="Diária (Streaks Ativos)", value="diaria"),
    app_commands.Choice(name="Diária (Recordes)", value="max_diaria")
])
async def rank(interaction: discord.Interaction, tipo: app_commands.Choice[str]):
    coluna = ""
    titulo = ""
    sufixo = ""

    if tipo.value == "quiz":
        coluna = "quiz_points"
        titulo = "🏆 Rank: Pontos Gerais do Quiz"
        sufixo = "pts"
    elif tipo.value == "diaria":
        coluna = "daily_streak"
        titulo = "🔥 Rank: Streaks Ativos (Diária)"
        sufixo = "dias"
    elif tipo.value == "max_diaria":
        coluna = "max_streak"
        titulo = "👑 Rank: Recorde de Streaks (Hall da Fama)"
        sufixo = "dias"

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(f"SELECT user_id, {coluna} FROM users WHERE {coluna} > 0 ORDER BY {coluna} DESC LIMIT 10")
        resultados = c.fetchall()

    if not resultados:
        await interaction.response.send_message("Ainda não há ninguém neste rank. Sê o primeiro a pontuar!", ephemeral=True)
        return

    embed = discord.Embed(title=titulo, color=0xFFD700)
    texto_rank = ""
    
    for i, (user_id, valor) in enumerate(resultados):
        medalha = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"**{i+1}.**"
        texto_rank += f"{medalha} <@{user_id}> — **{valor} {sufixo}**\n"

    embed.description = texto_rank
    await interaction.response.send_message(embed=embed)


# ==========================================
# EVENTOS DO BOT
# ==========================================
@bot.event
async def on_message(message):
    await bot.process_commands(message)

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
    try:
        synced = await bot.tree.sync()
        logging.info(f"Árvore de comandos sincronizada: {len(synced)} comando(s) slash registado(s).")
    except Exception as e:
        logging.error(f"Erro ao sincronizar comandos slash: {e}")

    if not monitorar_reddit.is_running():
        monitorar_reddit.start()
        logging.info("Task de monitoramento do Reddit iniciada (Loop de 8h).")

bot.run(DISCORD_TOKEN)