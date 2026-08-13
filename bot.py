"""
Polymarket Overlap — bot Discord (py-cord)

Ce que le site ne peut pas faire : te prévenir. Le bot surveille les positions des
meilleurs traders et poste dans un salon quand le smart money ENTRE sur un marché
ou en SORT — sans que tu aies à ouvrir quoi que ce soit.

Commandes :
  /best        les meilleures entrées du moment
  /wallet      les paris et le palmarès d'une adresse
  /watch       abonner ce salon aux alertes
  /unwatch     désabonner ce salon
  /status      état de la surveillance

Jeton : mets-le dans un fichier token.txt à côté de ce script (une ligne),
ou dans la variable d'environnement DISCORD_BOT_TOKEN. Ne le partage jamais.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path

import discord
from discord.ext import tasks

import overlap as ov

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN:
    _f = Path(__file__).with_name("token.txt")
    if _f.exists():
        TOKEN = _f.read_text(encoding="utf-8").strip()

# Sync instantané des commandes si tu renseignes ton serveur, sinon global (~1 h).
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0") or 0)
GUILDS = [GUILD_ID] if GUILD_ID else None

DB_PATH = Path(__file__).with_name("overlap.db")
POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "20"))
PRESET_SIZE = int(os.environ.get("PRESET_SIZE", "40"))
DEFAULT_MIN_VALUE = 10_000     # n'alerte pas pour des miettes

GREEN, RED, GREY, GOLD = 0x22C55E, 0xEF4444, 0x8B93A1, 0xEAB308

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.execute("""CREATE TABLE IF NOT EXISTS subs(
  channel_id INTEGER PRIMARY KEY, guild_id INTEGER,
  min_value REAL DEFAULT 10000, created INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS seen(
  key TEXT PRIMARY KEY, title TEXT, outcome TEXT, value REAL, ts INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""")
db.commit()


def meta_get(k, default=None):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def meta_set(k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    db.commit()


bot = discord.Bot(intents=discord.Intents.default())

# Un seul cycle d'analyse à la fois : les commandes réutilisent le cache récent.
_lock = asyncio.Lock()
_cache: dict = {"ts": 0, "wallets": [], "markets": []}
CACHE_TTL = 300


async def get_analysis(force: bool = False):
    async with _lock:
        if not force and time.time() - _cache["ts"] < CACHE_TTL and _cache["markets"]:
            return _cache["wallets"], _cache["markets"]
        ws, ms = await ov.analyze_preset("week", PRESET_SIZE)
        _cache.update(ts=time.time(), wallets=ws, markets=ms)
        return ws, ms


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------
def market_embed(m: dict, kind: str = "pick") -> discord.Embed:
    cls, label, pts = ov.verdict(m)
    colour = {"buy": GREEN, "avoid": RED}.get(cls, GREY)
    if kind == "new":
        colour = GOLD

    prefix = {"new": "🆕 Le smart money vient d'entrer", "pick": "🎯"}.get(kind, "")
    e = discord.Embed(
        title=f"{prefix} · {label}"[:250],
        description=f"**{m['title']}**",
        url=ov.market_url(m), colour=colour,
    )
    e.add_field(name="Proba estimée",
                value=f"**{round(m['myProba']*100)}%** vs marché {round(m['price']*100)}% "
                      f"({pts:+d} pts)", inline=True)
    e.add_field(name="Argent engagé",
                value=f"{ov.fmt_usd(m['totalValue'])} · {len(m['holders'])} traders", inline=True)
    if m.get("daysLeft") is not None:
        d = m["daysLeft"]
        e.add_field(name="Échéance",
                    value="aujourd'hui" if d <= 0 else ("demain" if d == 1 else f"dans {d} j"),
                    inline=True)

    top = sorted(m["holders"], key=lambda h: -h["value"])[:3]
    e.add_field(
        name="Qui est dessus",
        value="\n".join(
            f"• **{h['wallet'].name}** — {ov.fmt_usd(h['value'])} "
            f"(entré à {round(h['avg']*100)}¢) · {h['wallet'].record_str()}" for h in top
        )[:1000] or "—", inline=False)

    if m.get("contested"):
        opp = [o for o in m["others"] if o["n"] > 0]
        e.add_field(
            name="⚔️ Désaccord",
            value=" · ".join(f"{o['n']} sur « {ov.outcome_label(m['title'], o['outcome'])} » "
                             f"({ov.fmt_usd(o['value'])})" for o in opp)[:1000], inline=False)
    if m.get("hasTwins"):
        e.set_footer(text="⚠️ Des wallets suivis se ressemblent beaucoup — probablement la même personne.")
    return e


# ---------------------------------------------------------------------------
# Surveillance
# ---------------------------------------------------------------------------
@tasks.loop(minutes=POLL_MINUTES)
async def watcher():
    subs = db.execute("SELECT channel_id, min_value FROM subs").fetchall()
    if not subs:
        return
    try:
        _, markets = await get_analysis(force=True)
    except Exception as exc:
        print("watcher: analyse impossible —", exc)
        return

    now = int(time.time())
    prev = {r[0]: r for r in db.execute("SELECT key, title, outcome, value FROM seen")}
    current = {m["key"]: m for m in markets}

    # Premier passage : on enregistre l'état sans rien annoncer, sinon 200 alertes d'un coup.
    first_run = meta_get("initialised") is None

    entries, exits = [], []
    if not first_run:
        entries = [m for k, m in current.items()
                   if k not in prev and ov.verdict(m)[0] == "buy"]
        exits = [prev[k] for k in prev if k not in current]

    db.execute("DELETE FROM seen")
    db.executemany("INSERT INTO seen VALUES(?,?,?,?,?)",
                   [(m["key"], m["title"], m["outcome"], m["totalValue"], now)
                    for m in markets])
    meta_set("initialised", "1")
    meta_set("last_run", now)
    db.commit()

    if first_run:
        print(f"watcher: état initial enregistré ({len(markets)} marchés), aucune alerte envoyée.")
        return

    for channel_id, min_value in subs:
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
        picks = [m for m in entries if m["totalValue"] >= (min_value or 0)]
        picks.sort(key=lambda m: -m["evTime"])
        for m in picks[:5]:                      # jamais plus de 5 alertes par cycle
            try:
                await ch.send(embed=market_embed(m, kind="new"))
            except Exception as exc:
                print("watcher: envoi impossible —", exc)

        big_exits = [x for x in exits if (x[3] or 0) >= (min_value or 0)]
        if big_exits:
            txt = "\n".join(f"• **{x[1]}** — « {ov.outcome_label(x[1], x[2])} » "
                            f"(valait {ov.fmt_usd(x[3] or 0)})" for x in big_exits[:5])
            e = discord.Embed(
                title="🚪 Sorties — ils ont liquidé ces positions",
                description=txt[:3800], colour=RED)
            e.set_footer(text="Une sortie est un signal au moins aussi fort qu'une entrée.")
            try:
                await ch.send(embed=e)
            except Exception as exc:
                print("watcher: envoi impossible —", exc)


@watcher.before_loop
async def _before():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Connecté : {bot.user} · surveillance toutes les {POLL_MINUTES} min")
    if not watcher.is_running():
        watcher.start()


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------
# NB : les options sont déclarées en décorateur et pas en annotation de type.
# Sous Python 3.14 les annotations sont évaluées paresseusement (PEP 649) et
# py-cord les lisait comme du texte : toutes les options devenaient des chaînes
# obligatoires. Le décorateur ne dépend pas des annotations.
@bot.slash_command(name="best", description="Les meilleures entrées du moment", guild_ids=GUILDS)
@discord.option("nombre", int, description="Combien en afficher (1-5)",
                min_value=1, max_value=5, default=3, required=False)
async def best(ctx, nombre: int):
    await ctx.defer()
    try:
        _, markets = await get_analysis()
    except Exception as exc:
        return await ctx.respond(f"Impossible de joindre l'API Polymarket : {exc}")
    picks = [m for m in markets if ov.verdict(m)[0] == "buy"][:nombre]
    if not picks:
        return await ctx.respond("Aucune entrée convaincante en ce moment — le smart money "
                                 "est aligné avec le marché partout. C'est une info aussi.")
    await ctx.respond(embeds=[market_embed(m) for m in picks])


@bot.slash_command(name="wallet", description="Les paris et le palmarès d'une adresse", guild_ids=GUILDS)
@discord.option("adresse", str, description="Adresse Polymarket (0x…)")
async def wallet(ctx, adresse: str):
    a = adresse.strip()
    if not (a.startswith("0x") and len(a) == 42):
        return await ctx.respond("Adresse invalide : il faut le 0x… de 42 caractères "
                                 "(celui de l'URL du profil Polymarket).", ephemeral=True)
    await ctx.defer()
    try:
        ws, _ = await ov.analyze([a])
    except Exception as exc:
        return await ctx.respond(f"Impossible de joindre l'API Polymarket : {exc}")
    if not ws:
        return await ctx.respond("Adresse introuvable ou sans position.")
    w = ws[0]
    openp = [p for p in w.positions if p.get("redeemable") is not True
             and (p.get("currentValue") or 0) >= 0.01]
    unclaimed = [p for p in w.positions if p.get("redeemable") is True
                 and (p.get("currentValue") or 0) >= 0.01]

    e = discord.Embed(title=f"👤 {w.name}",
                      url=f"https://polymarket.com/profile/{w.addr}",
                      colour=GREEN if w.open_pnl >= 0 else RED)
    e.add_field(name="Positions ouvertes",
                value=f"{ov.fmt_usd(w.portfolio)} · {len(openp)} paris", inline=True)
    e.add_field(name="Plus/moins-value",
                value=f"{'+' if w.open_pnl>=0 else '−'}{ov.fmt_usd(abs(w.open_pnl))}", inline=True)
    e.add_field(name="Palmarès réel", value=w.record_str(), inline=False)
    if unclaimed:
        e.add_field(name="💰 Gains non réclamés",
                    value=f"**{ov.fmt_usd(sum(p['currentValue'] for p in unclaimed))}** sur "
                          f"{len(unclaimed)} paris gagnés — l'argent n'arrive qu'après réclamation.",
                    inline=False)
    top = sorted(openp, key=lambda p: -(p.get("currentValue") or 0))[:5]
    if top:
        e.add_field(name="Ses plus grosses positions", value="\n".join(
            f"• **{(p.get('title') or '')[:60]}** — "
            f"{ov.outcome_label(p.get('title'), p.get('outcome'))} · "
            f"{ov.fmt_usd(p.get('currentValue') or 0)} (entré à {round((p.get('avgPrice') or 0)*100)}¢)"
            for p in top)[:1000], inline=False)
    if w.tr and w.tr.days:
        e.set_footer(text=f"Palmarès calculé sur les {ov.ACTIVITY_LIMIT} derniers événements "
                          f"(~{w.tr.days} j) — l'API ne remonte pas plus loin.")
    await ctx.respond(embed=e)


@bot.slash_command(name="watch", description="Abonner ce salon aux alertes", guild_ids=GUILDS)
@discord.default_permissions(manage_guild=True)
@discord.option("seuil", int, description="Ne prévenir qu'au-dessus de ce montant ($)",
                min_value=0, default=DEFAULT_MIN_VALUE, required=False)
async def watch(ctx, seuil: int):
    db.execute("INSERT OR REPLACE INTO subs VALUES(?,?,?,?)",
               (ctx.channel.id, ctx.guild.id if ctx.guild else 0, seuil, int(time.time())))
    db.commit()
    await ctx.respond(
        f"✅ Ce salon recevra les alertes : entrées du smart money au-dessus de "
        f"**{ov.fmt_usd(seuil)}**, et leurs sorties. Vérification toutes les {POLL_MINUTES} min.\n"
        f"_Le premier cycle sert de référence : les alertes commencent au suivant._")


@bot.slash_command(name="unwatch", description="Désabonner ce salon", guild_ids=GUILDS)
@discord.default_permissions(manage_guild=True)
async def unwatch(ctx):
    db.execute("DELETE FROM subs WHERE channel_id=?", (ctx.channel.id,))
    db.commit()
    await ctx.respond("🔕 Alertes coupées pour ce salon.")


@bot.slash_command(name="status", description="État de la surveillance", guild_ids=GUILDS)
async def status(ctx):
    n = db.execute("SELECT COUNT(*) FROM subs").fetchone()[0]
    tracked = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    last = meta_get("last_run")
    ago = f"il y a {int((time.time()-int(last))//60)} min" if last else "jamais"
    await ctx.respond(
        f"**Surveillance** : {n} salon(s) abonné(s) · {tracked} marchés suivis\n"
        f"**Dernier passage** : {ago} · fréquence {POLL_MINUTES} min · "
        f"top {PRESET_SIZE} traders de la semaine")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "Aucun jeton trouvé. Crée une application sur https://discord.com/developers/applications, "
            "copie le jeton du bot dans un fichier 'token.txt' à côté de ce script "
            "(ou dans la variable d'environnement DISCORD_BOT_TOKEN). Ne le partage avec personne.")
    bot.run(TOKEN)
