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
import json
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
PRESET_SIZE = int(os.environ.get("PRESET_SIZE", "50"))   # top 50 de la semaine
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
# Qui détient quoi, wallet par wallet. Indispensable : le preset suit le top 50 de la
# semaine, et un trader qui SORT du classement emporte toutes ses positions avec lui.
# Comparer seulement les marchés agrégés faisait passer ce renouvellement pour des
# liquidations massives (151 fausses « sorties » mesurées sur un seul cycle).
db.execute("""CREATE TABLE IF NOT EXISTS holdings(
  addr TEXT, key TEXT, title TEXT, outcome TEXT, value REAL, ts INTEGER,
  PRIMARY KEY(addr, key))""")
db.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""")
# Tableau vivant : UN message que le bot réécrit à chaque cycle, pour qu'un salon
# « meilleur overlap du moment » montre l'état actuel sans avoir à remonter le fil.
db.execute("""CREATE TABLE IF NOT EXISTS board(
  channel_id INTEGER PRIMARY KEY, message_id INTEGER, updated INTEGER)""")
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
    label_out = ov.outcome_label(m["title"], m["outcome"])

    # Même lecture que les cartes du site : le pari et sa traduction, puis le contexte.
    desc = [f"## {m['title']}",
            f"**Le pari : `{label_out}`** = {ov.explain_outcome(m['title'], m['outcome'])}."]

    avg_entry = m.get("avgEntry") or 0
    line = (f"**{len(m['holders'])} traders** ont posé **{ov.fmt_usd(m['totalValue'])}** dessus "
            f"(entrée moyenne {round(avg_entry*100)}¢, prix actuel {round((m['price'] or 0)*100)}¢).")
    good = sum(1 for h in m["holders"] if (h["wallet"].quality or 0) > 0)
    if good == len(m["holders"]):
        line += " Tous sont gagnants sur leur historique récent."
    elif good == 0:
        line += " ⚠️ Aucun n'est gagnant sur son historique récent."
    else:
        line += f" {good}/{len(m['holders'])} sont gagnants sur leur historique récent."
    desc.append(line)

    if m.get("contested"):
        opp = [o for o in m["others"] if o["n"] > 0]
        opp_val = sum(o["value"] for o in opp)
        share = round(opp_val / max(1e-9, opp_val + m["totalValue"]) * 100)
        detail = ", ".join(
            f"{o['n']} trader{'s' if o['n'] > 1 else ''} parie{'nt' if o['n'] > 1 else ''} "
            f"**{ov.fmt_usd(o['value'])}** sur « {ov.outcome_label(m['title'], o['outcome'])} »"
            for o in opp)
        desc.append(f"> ⚔️ **Le smart money n'est pas d'accord.** {detail} — contre "
                    f"**{ov.fmt_usd(m['totalValue'])}** de ce côté, soit **{share}%** de "
                    f"l'argent suivi à contre-courant.")

    if avg_entry and m.get("price") and (m["price"] - avg_entry) > 0.08:
        desc.append(f"> ⚠️ **Tu arrives après eux.** Ils sont entrés à {round(avg_entry*100)}¢, "
                    f"le marché est à {round(m['price']*100)}¢.")

    e = discord.Embed(title=f"{prefix} · {label}"[:250],
                      description="\n\n".join(desc)[:4000],
                      url=ov.market_url(m), colour=colour)

    e.add_field(name="Proba estimée",
                value=f"**{round(m['myProba']*100)}%**\nmarché : {round(m['price']*100)}% ({pts:+d})",
                inline=True)
    stake = 100
    gain = stake * (1 - m["price"]) / m["price"] if 0 < (m["price"] or 0) < 1 else 0
    e.add_field(name="Ta mise de $100 → si gagné",
                value=f"**+{ov.fmt_usd(gain)}**", inline=True)
    e.add_field(name="Les wallets visent",
                value=f"**+{ov.fmt_usd(m.get('potentialGain') or 0)}**", inline=True)

    # Le détail wallet par wallet, comme le panneau dépliable du site.
    def who(holders, limit=4):
        rows = []
        for h in sorted(holders, key=lambda x: -x["value"])[:limit]:
            w = h["wallet"]
            invested = (h["shares"] or 0) * (h["avg"] or 0)
            perf = (h["value"] - invested) / invested if invested > 0 else 0
            pct = (h["value"] / w.portfolio * 100) if getattr(w, "portfolio", 0) else 0
            pct_txt = f"{pct:.0f}%" if pct >= 1 else (f"{pct:.1f}%" if pct >= 0.1 else "<0.1%")
            rows.append(
                f"**{w.name}**\n"
                f"💵 {ov.fmt_usd(invested)} à {round((h['avg'] or 0)*100)}¢ → "
                f"{ov.fmt_usd(h['value'])} ({'+' if perf >= 0 else '−'}{abs(round(perf*100))}%) "
                f"· {pct_txt} de son portefeuille\n"
                f"📊 {w.record_str()}")
        return "\n".join(rows)[:1000] or "—"

    e.add_field(name=f"✅ Pour « {label_out} » — {ov.fmt_usd(m['totalValue'])}",
                value=who(m["holders"]), inline=False)
    if m.get("contested"):
        for o in sorted((o for o in m["others"] if o["n"] > 0), key=lambda o: -o["value"])[:1]:
            e.add_field(
                name=f"⚔️ Contre — « {ov.outcome_label(m['title'], o['outcome'])} » — {ov.fmt_usd(o['value'])}",
                value=who(o["holders"]), inline=False)

    if m.get("daysLeft") is not None:
        d = m["daysLeft"]
        when = "résout aujourd'hui" if d <= 0 else ("résout demain" if d == 1 else f"résout dans {d} j")
    else:
        when = "échéance inconnue"
    foot = [when, f"top {PRESET_SIZE} traders de la semaine"]
    if m.get("hasTwins"):
        foot.append("⚠️ wallets très similaires — probablement la même personne")
    e.set_footer(text=" · ".join(foot))
    return e


# ---------------------------------------------------------------------------
# Surveillance
# ---------------------------------------------------------------------------
def board_embed(markets: list) -> discord.Embed:
    """Le classement du moment, lisible d'un coup d'œil, sans jargon."""
    picks = [m for m in markets if ov.verdict(m)[0] == "buy"]
    picks.sort(key=lambda m: -m["evTime"])
    e = discord.Embed(
        title="🏆 Meilleurs overlaps du moment",
        description=(
            "Les marchés où **plusieurs des 50 meilleurs traders de la semaine** ont "
            "misé du même côté, classés par intérêt. Mis à jour automatiquement.\n"
            "*Un « overlap » = un pari sur lequel plusieurs bons traders se retrouvent.*"),
        colour=GOLD)
    if not picks:
        e.add_field(name="Rien de convaincant pour l'instant",
                    value="Aucun marché ne dépasse le seuil. Le tableau se remplira "
                          "au prochain mouvement — c'est normal et voulu : pas de bruit.",
                    inline=False)
    for i, m in enumerate(picks[:5], 1):
        d = m.get("daysLeft")
        when = "aujourd'hui" if (d is not None and d <= 0) else (
               "demain" if d == 1 else (f"dans {d} j" if d is not None else "—"))
        label = ov.outcome_label(m["title"], m["outcome"])
        gain = 100 * (1 - m["price"]) / m["price"] if 0 < (m["price"] or 0) < 1 else 0
        warn = " ⚔️ *avis partagés*" if m.get("contested") else ""
        e.add_field(
            name=f"{i}. {m['title'][:80]}",
            value=(f"**Parier `{label}` à {round(m['price']*100)}¢** — "
                   f"{ov.explain_outcome(m['title'], m['outcome'])}\n"
                   f"👥 {len(m['holders'])} traders · 💰 {ov.fmt_usd(m['totalValue'])} engagés · "
                   f"🎯 proba estimée {round(m['myProba']*100)}% (marché {round(m['price']*100)}%)\n"
                   f"💵 100 $ misés → **+{ov.fmt_usd(gain)}** si ça passe · ⏳ {when}{warn}\n"
                   f"[Voir sur Polymarket]({ov.market_url(m)})")[:1020],
            inline=False)
    e.set_footer(text=f"Mis à jour toutes les {POLL_MINUTES} min · "
                      "informatif, pas un conseil financier · /guide pour tout comprendre")
    e.timestamp = discord.utils.utcnow()
    return e


async def refresh_boards(markets: list):
    """Réécrit le message du tableau au lieu d'en poster un nouveau."""
    for channel_id, message_id in db.execute("SELECT channel_id, message_id FROM board").fetchall():
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
        embed = board_embed(markets)
        try:
            msg = await ch.fetch_message(message_id)
            await msg.edit(embed=embed)
        except discord.NotFound:                      # message supprimé → on le recrée
            msg = await ch.send(embed=embed)
            db.execute("UPDATE board SET message_id=? WHERE channel_id=?", (msg.id, channel_id))
        except Exception as exc:
            print("board: mise à jour impossible —", exc)
            continue
        db.execute("UPDATE board SET updated=? WHERE channel_id=?", (int(time.time()), channel_id))
        db.commit()


def compute_deltas(prev_hold: dict, cur_hold: dict, current: dict,
                   prev_addrs: set | None = None, cur_addrs: set | None = None):
    """Vrais mouvements entre deux passages.

    Seuls les traders présents AVANT et MAINTENANT sont comparés : le preset suit le
    top 50 de la semaine, donc un trader qui quitte le classement emporte toutes ses
    positions. Les compter comme des ventes produisait des sorties fictives en masse.
    """
    # On compare les traders réellement ANALYSÉS aux deux passages, pas seulement ceux
    # qui avaient déjà une position commune : sinon un trader dont la position devient
    # tout juste partagée par un second wallet ne déclencherait jamais d'alerte.
    prev_set = prev_addrs if prev_addrs is not None else set(prev_hold)
    cur_set  = cur_addrs  if cur_addrs  is not None else set(cur_hold)
    common = prev_set & cur_set
    opened, closed = set(), {}
    for a in common:
        before, after = prev_hold.get(a, {}), cur_hold.get(a, {})
        for k in after.keys() - before.keys():
            opened.add(k)
        for k in before.keys() - after.keys():
            title, outcome, value = before[k]
            agg = closed.setdefault(k, [title, outcome, 0.0])
            agg[2] += value or 0
    entries = [current[k] for k in opened
               if k in current and ov.verdict(current[k])[0] == "buy"]
    # Une position encore tenue par d'autres traders suivis n'est pas une sortie du groupe.
    exits = [(k, t, o, v) for k, (t, o, v) in closed.items() if k not in current]
    return entries, exits


@tasks.loop(minutes=POLL_MINUTES)
async def watcher():
    subs = db.execute("SELECT channel_id, min_value FROM subs").fetchall()
    boards = db.execute("SELECT channel_id FROM board").fetchall()
    if not subs and not boards:
        return
    try:
        wallets, markets = await get_analysis(force=True)
    except Exception as exc:
        print("watcher: analyse impossible —", exc)
        return

    now = int(time.time())
    current = {m["key"]: m for m in markets}

    # État précédent, wallet par wallet.
    prev_hold: dict[str, dict[str, tuple]] = {}
    for addr, key, title, outcome, value in db.execute(
            "SELECT addr, key, title, outcome, value FROM holdings"):
        prev_hold.setdefault(addr, {})[key] = (title, outcome, value)

    # État courant, wallet par wallet.
    cur_hold: dict[str, dict[str, tuple]] = {}
    for m in markets:
        for h in m["holders"]:
            a = h["wallet"].addr.lower()
            cur_hold.setdefault(a, {})[m["key"]] = (m["title"], m["outcome"], h["value"])

    cur_addrs = {w.addr.lower() for w in wallets}
    try:
        prev_addrs = set(json.loads(meta_get("addrs") or "[]"))
    except ValueError:
        prev_addrs = set()

    first_run = meta_get("initialised") is None
    entries, exits = ([], []) if first_run else compute_deltas(
        prev_hold, cur_hold, current, prev_addrs, cur_addrs)

    db.execute("DELETE FROM seen")
    db.executemany("INSERT INTO seen VALUES(?,?,?,?,?)",
                   [(m["key"], m["title"], m["outcome"], m["totalValue"], now)
                    for m in markets])
    db.execute("DELETE FROM holdings")
    db.executemany("INSERT INTO holdings VALUES(?,?,?,?,?,?)",
                   [(a, k, t, o, v, now)
                    for a, ks in cur_hold.items() for k, (t, o, v) in ks.items()])
    meta_set("addrs", json.dumps(sorted(cur_addrs)))
    meta_set("initialised", "1")
    meta_set("last_run", now)
    db.commit()

    # Le tableau reflète l'état courant : il se met à jour même au tout premier passage,
    # où aucune alerte n'est envoyée.
    await refresh_boards(markets)

    if first_run:
        print(f"watcher: état initial enregistré ({len(markets)} marchés, "
              f"{len(cur_addrs)} wallets), aucune alerte envoyée.")
        return

    stamp = time.strftime("%H:%M:%S")
    churn = len(cur_addrs - prev_addrs) + len(prev_addrs - cur_addrs)
    print(f"watcher {stamp}: {len(markets)} marchés · {len(cur_addrs)} wallets "
          f"({churn} changements de classement ignorés) → "
          f"{len(entries)} entrée(s) ACHETER, {len(exits)} sortie(s)")

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

        big_exits = sorted((x for x in exits if (x[3] or 0) >= (min_value or 0)),
                           key=lambda x: -(x[3] or 0))
        if big_exits:
            txt = "\n".join(f"• **{x[1]}** — « {ov.outcome_label(x[1], x[2])} » "
                            f"(valait {ov.fmt_usd(x[3] or 0)})" for x in big_exits[:5])
            e = discord.Embed(
                title="🚪 Sorties — ils ont liquidé ces positions",
                description=txt[:3800], colour=RED)
            e.set_footer(text="Sorties de traders encore suivis — pas un simple "
                              "changement de classement. Un abandon vaut un achat.")
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
    # L'identifiant d'un bot EST l'Application ID : il peut donc afficher son propre
    # lien d'invitation, sans qu'on ait à retourner chercher quoi que ce soit.
    # Permissions 18432 = Send Messages (2048) + Embed Links (16384).
    print("Lien d'invitation : "
          f"https://discord.com/oauth2/authorize?client_id={bot.user.id}"
          "&permissions=18432&scope=bot%20applications.commands")
    print(f"Serveurs : {[g.name for g in bot.guilds] or 'aucun — utilise le lien ci-dessus'}")
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


@bot.slash_command(name="tableau",
                   description="Installer ici le tableau des meilleurs overlaps, tenu à jour",
                   guild_ids=GUILDS)
async def tableau(ctx):
    await ctx.defer()
    _, markets = await get_analysis()
    msg = await ctx.channel.send(embed=board_embed(markets))
    try:
        await msg.pin()
    except discord.Forbidden:
        pass                                          # pas la permission d'épingler
    db.execute("INSERT OR REPLACE INTO board VALUES(?,?,?)",
               (ctx.channel.id, msg.id, int(time.time())))
    db.commit()
    await ctx.respond(
        f"Tableau installé et épinglé. Il est **réécrit toutes les {POLL_MINUTES} min** "
        "au même endroit : ce salon montrera toujours l'état actuel, sans fil qui défile.\n"
        "Pense à `/guide` pour poster le mode d'emploi à épingler aussi.", ephemeral=True)


@bot.slash_command(name="guide", description="Poster le mode d'emploi du salon (à épingler)",
                   guild_ids=GUILDS)
async def guide(ctx):
    e = discord.Embed(
        title="📖 Comment lire ce salon",
        description="Ce salon suit les **50 meilleurs traders de la semaine** sur Polymarket "
                    "et repère les paris sur lesquels **plusieurs d'entre eux se retrouvent**.",
        colour=GOLD)
    e.add_field(
        name="1️⃣ C'est quoi un « overlap » ?",
        value="Un marché où **au moins 2 bons traders ont misé du même côté**. "
              "Seul, un trader peut se tromper ; quand plusieurs bons convergent, "
              "ça vaut le coup d'aller regarder.", inline=False)
    e.add_field(
        name="2️⃣ Les verdicts",
        value="🟢 **ACHETER** — les traders suivis voient l'issue plus probable que le marché\n"
              "⚪ **SUIVRE** — leur avis colle au prix, rien à gagner de spécial\n"
              "🔴 **ÉVITER** — ceux qui sont dessus perdent d'habitude, ou sont mal entrés\n"
              "⚔️ **avis partagés** — ils se contredisent entre eux : signal faible", inline=False)
    e.add_field(
        name="3️⃣ Les chiffres",
        value="**Prix en ¢** = la probabilité selon le marché (58¢ ≈ 58 % de chances). "
              "C'est aussi ton coût : 58¢ misés rapportent 1 $ si tu gagnes.\n"
              "**Proba estimée** = la même chose corrigée selon *qui* est positionné "
              "(leur palmarès, la part de portefeuille engagée, leur prix d'entrée).\n"
              "**100 $ → +X** = ce que rapporteraient 100 $ si le pari passe.", inline=False)
    e.add_field(
        name="4️⃣ Over / Under",
        value="Ce ne sont pas des équipes : c'est le **total de points du match**. "
              "« Over 8.5 » = 9 points ou plus, les deux équipes confondues. "
              "Un même match a plusieurs lignes (7.5, 8.5, 9.5…).", inline=False)
    e.add_field(
        name="5️⃣ À garder en tête",
        value="Ces traders **perdent aussi** des paris. Copier n'est pas gagner : ils entrent "
              "à un prix que tu n'auras plus, et peuvent sortir sans prévenir. "
              "**Rien ici n'est un conseil financier** — ne mise que ce que tu peux perdre.",
        inline=False)
    e.set_footer(text="/tableau installe le classement · /best affiche le top à la demande "
                      "· /wallet <adresse> analyse un portefeuille")
    await ctx.respond(embed=e)


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
