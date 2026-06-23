import os
import json
import logging
import asyncio
import discord
import feedparser
import aiohttp
import random
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
# SISTEMA DO REDDIT (SEM API OFICIAL)
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
    def __init__(self, lancar_interaction: discord.Interaction, pergunta_atual):
        super().__init__(timeout=30.0) # Tempo limite de 30 segundos
        self.lancar_interaction = lancar_interaction
        self.pergunta_atual = pergunta_atual
        self.resposta_certa = pergunta_atual['resposta_correta']
        self.foi_respondido = False

    async def responder(self, interaction: discord.Interaction, opcao_escolhida: str):
        if self.foi_respondido: return
        self.foi_respondido = True
        self.stop() # Finaliza a escuta da view

        # Verifica acerto ou erro
        if opcao_escolhida == self.resposta_certa:
            embed_resultado = discord.Embed(
                title="🏆 Acertaste em cheio!", 
                color=discord.Color.blue()
            )
        else:
            embed_resultado = discord.Embed(
                title="❌ Erraste...", 
                description=f"A resposta correta era a **{self.resposta_certa}**.",
                color=discord.Color.red()
            )
        
        embed_resultado.add_field(name="💡 Curiosidade:", value=self.pergunta_atual['curiosidade'], inline=False)
        
        # Desativa e colore os botões para mostrar o feedback visual
        for item in self.children:
            item.disabled = True
            if item.label == self.resposta_certa:
                item.style = discord.ButtonStyle.success # Verde para a correta
            elif item.label == opcao_escolhida:
                item.style = discord.ButtonStyle.danger # Vermelho para a errada se falhou

        # Atualiza a mensagem efêmera original
        await interaction.response.edit_message(embed=embed_resultado, view=self)

    # Definição dos 4 botões interativos
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
        """Garante que se o tempo esgotar, os botões fiquem desativados no card efêmero."""
        if not self.foi_respondido:
            for item in self.children:
                item.disabled = True
            
            embed_timeout = discord.Embed(
                title="⏰ O tempo esgotou!", 
                description=f"Demoraste muito tempo. A resposta correta era **{self.resposta_certa}**.",
                color=discord.Color.orange()
            )
            embed_timeout.add_field(name="💡 Curiosidade:", value=self.pergunta_atual['curiosidade'], inline=False)
            
            try:
                # Edita a mensagem efêmera original após o tempo esgotar
                await self.lancar_interaction.edit_original_response(embed=embed_timeout, view=self)
            except:
                pass


# ==========================================
# COMANDO DO QUIZ (EFÊMERO COM BOTÕES)
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
    
    # Cria os botões anexados à pergunta
    view_botoes = QuizBotoes(interaction, pergunta_atual)
    
    # Envia a mensagem com 'ephemeral=True' para que APENAS quem digitou veja
    await interaction.response.send_message(embed=embed_pergunta, view=view_botoes, ephemeral=True)


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