"""
Polymarket Overlap — bot Discord (py-cord)

Ce que le site ne peut pas faire : te prévenir. Le bot surveille les positions des
meilleurs traders et poste dans un salon quand le smart money ENTRE sur un marché
ou en SORT — sans que tu aies à ouvrir quoi que ce soit.

Commandes :
  /best        les meilleures entrées du moment
  /board       installe le tableau vivant dans le salon
  /guide       poste le mode d'emploi (à épingler)
  /wallet      les paris et le palmarès d'une adresse
  /watch       abonner ce salon aux alertes
  /unwatch     désabonner ce salon
  /status      état de la surveillance

Textes affichés en anglais (public du serveur) ; commentaires en français.

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


# auto_sync_commands=False : sinon py-cord enregistre aussi les commandes en GLOBAL,
# elles cohabitent avec les copies par serveur et Discord les affiche EN DOUBLE.
# On synchronise nous-mêmes, uniquement sur les serveurs du bot (effet immédiat).
bot = discord.Bot(intents=discord.Intents.default(), auto_sync_commands=False)

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

    prefix = {"new": "🆕 Smart money just entered", "pick": "🎯"}.get(kind, "")
    label_out = ov.outcome_label(m["title"], m["outcome"])

    # Même lecture que les cartes du site : le pari et sa traduction, puis le contexte.
    desc = [f"## {m['title']}",
            f"**The bet: `{label_out}`** = {ov.explain_outcome(m['title'], m['outcome'])}."]

    avg_entry = m.get("avgEntry") or 0
    line = (f"**{len(m['holders'])} traders** put **{ov.fmt_usd(m['totalValue'])}** on it "
            f"(avg entry {round(avg_entry*100)}¢, now {round((m['price'] or 0)*100)}¢).")
    good = sum(1 for h in m["holders"] if (h["wallet"].quality or 0) > 0)
    if good == len(m["holders"]):
        line += " All of them are profitable over their recent history."
    elif good == 0:
        line += " ⚠️ None of them is profitable over their recent history."
    else:
        line += f" {good}/{len(m['holders'])} are profitable over their recent history."
    desc.append(line)

    if m.get("contested"):
        opp = [o for o in m["others"] if o["n"] > 0]
        opp_val = sum(o["value"] for o in opp)
        share = round(opp_val / max(1e-9, opp_val + m["totalValue"]) * 100)
        detail = ", ".join(
            f"{o['n']} trader{'s' if o['n'] > 1 else ''} bet{'' if o['n'] > 1 else 's'} "
            f"**{ov.fmt_usd(o['value'])}** on \u201c{ov.outcome_label(m['title'], o['outcome'])}\u201d"
            for o in opp)
        desc.append(f"> ⚔️ **The smart money disagrees.** {detail} — against "
                    f"**{ov.fmt_usd(m['totalValue'])}** on this side, i.e. **{share}%** of the "
                    f"tracked money betting the other way.")

    if avg_entry and m.get("price") and (m["price"] - avg_entry) > 0.08:
        desc.append(f"> ⚠️ **You are late.** They entered at {round(avg_entry*100)}¢, "
                    f"the market is now at {round(m['price']*100)}¢.")

    e = discord.Embed(title=f"{prefix} · {label}"[:250],
                      description="\n\n".join(desc)[:4000],
                      url=ov.market_url(m), colour=colour)

    e.add_field(name="Estimated probability",
                value=f"**{round(m['myProba']*100)}%**\nmarket: {round(m['price']*100)}% ({pts:+d})",
                inline=True)
    stake = 100
    gain = stake * (1 - m["price"]) / m["price"] if 0 < (m["price"] or 0) < 1 else 0
    e.add_field(name="Your $100 → if it wins",
                value=f"**+{ov.fmt_usd(gain)}**", inline=True)
    e.add_field(name="They are aiming for",
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
                f"💵 {ov.fmt_usd(invested)} at {round((h['avg'] or 0)*100)}¢ → "
                f"{ov.fmt_usd(h['value'])} ({'+' if perf >= 0 else '−'}{abs(round(perf*100))}%) "
                f"· {pct_txt} of their portfolio\n"
                f"📊 {w.record_str()}")
        return "\n".join(rows)[:1000] or "—"

    e.add_field(name=f"✅ For \u201c{label_out}\u201d — {ov.fmt_usd(m['totalValue'])}",
                value=who(m["holders"]), inline=False)
    if m.get("contested"):
        for o in sorted((o for o in m["others"] if o["n"] > 0), key=lambda o: -o["value"])[:1]:
            e.add_field(
                name=f"⚔️ Against — \u201c{ov.outcome_label(m['title'], o['outcome'])}\u201d — {ov.fmt_usd(o['value'])}",
                value=who(o["holders"]), inline=False)

    if m.get("daysLeft") is not None:
        d = m["daysLeft"]
        when = "resolves today" if d <= 0 else ("resolves tomorrow" if d == 1 else f"resolves in {d}d")
    else:
        when = "no resolution date"
    foot = [when, f"top {PRESET_SIZE} traders of the week"]
    if m.get("hasTwins"):
        foot.append("⚠️ near-identical wallets — probably the same person")
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
        title="🏆 Best overlaps right now",
        description=(
            "Markets where **several of the week's 50 best traders** have bet on the "
            "same side, ranked by how interesting they look. Updated automatically.\n"
            "*An \u201coverlap\u201d = a bet that several good traders landed on together.*"),
        colour=GOLD)
    if not picks:
        e.add_field(name="Nothing convincing right now",
                    value="No market clears the bar. The board will fill up on the next "
                          "move — this is by design: no noise for the sake of noise.",
                    inline=False)
    for i, m in enumerate(picks[:5], 1):
        d = m.get("daysLeft")
        when = "today" if (d is not None and d <= 0) else (
               "tomorrow" if d == 1 else (f"in {d}d" if d is not None else "—"))
        label = ov.outcome_label(m["title"], m["outcome"])
        gain = 100 * (1 - m["price"]) / m["price"] if 0 < (m["price"] or 0) < 1 else 0
        warn = " ⚔️ *they disagree*" if m.get("contested") else ""
        e.add_field(
            name=f"{i}. {m['title'][:80]}",
            value=(f"**Bet `{label}` at {round(m['price']*100)}¢** — "
                   f"{ov.explain_outcome(m['title'], m['outcome'])}\n"
                   f"👥 {len(m['holders'])} traders · 💰 {ov.fmt_usd(m['totalValue'])} at stake · "
                   f"🎯 estimated {round(m['myProba']*100)}% (market {round(m['price']*100)}%)\n"
                   f"💵 $100 → **+{ov.fmt_usd(gain)}** if it lands · ⏳ {when}{warn}\n"
                   f"[View on Polymarket]({ov.market_url(m)})")[:1020],
            inline=False)
    e.set_footer(text=f"Updated every {POLL_MINUTES} min · "
                      "information only, not financial advice · /guide explains everything")
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
            txt = "\n".join(f"• **{x[1]}** — \u201c{ov.outcome_label(x[1], x[2])}\u201d "
                            f"(was worth {ov.fmt_usd(x[3] or 0)})" for x in big_exits[:5])
            e = discord.Embed(
                title="🚪 Exits — they closed these positions",
                description=txt[:3800], colour=RED)
            e.set_footer(text="Exits by traders still tracked — not a leaderboard reshuffle. "
                              "Walking away is as telling as buying in.")
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

    # Sans DISCORD_GUILD_ID, les commandes sont enregistrées globalement et Discord met
    # jusqu'à UNE HEURE à les propager : une nouvelle commande semble alors « ne pas
    # exister ». On les resynchronise sur chaque serveur où le bot se trouve, où la
    # prise en compte est immédiate — aucune configuration à faire.
    if bot.guilds:
        try:
            # On efface les globales AVANT de resynchroniser, sinon les copies déjà
            # publiées continuent d'apparaître en double dans le menu.
            await bot.http.bulk_upsert_global_commands(bot.application_id, [])
            await bot.sync_commands(guild_ids=[g.id for g in bot.guilds], force=True)
            for g in bot.guilds:
                cmds = await bot.http.get_guild_commands(bot.application_id, g.id)
                print(f"Commandes sur « {g.name} » ({len(cmds)}) : "
                      + ", ".join('/' + c["name"] for c in sorted(cmds, key=lambda c: c["name"])))
            glob = await bot.http.get_global_commands(bot.application_id)
            print(f"Commandes globales restantes : {len(glob)} (0 attendu, sinon doublons)")
        except Exception as exc:
            print("Synchronisation des commandes impossible —", exc)

    if not watcher.is_running():
        watcher.start()


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------
# NB : les options sont déclarées en décorateur et pas en annotation de type.
# Sous Python 3.14 les annotations sont évaluées paresseusement (PEP 649) et
# py-cord les lisait comme du texte : toutes les options devenaient des chaînes
# obligatoires. Le décorateur ne dépend pas des annotations.
@bot.slash_command(name="best", description="The best entries right now", guild_ids=GUILDS)
@discord.option("count", int, description="How many to show (1-5)",
                min_value=1, max_value=5, default=3, required=False)
async def best(ctx, count: int):
    await ctx.defer()
    try:
        _, markets = await get_analysis()
    except Exception as exc:
        return await ctx.respond(f"Could not reach the Polymarket API: {exc}")
    picks = [m for m in markets if ov.verdict(m)[0] == "buy"][:count]
    if not picks:
        return await ctx.respond("Nothing convincing right now — the smart money agrees with the market "
                                 "everywhere. That is information too.")
    await ctx.respond(embeds=[market_embed(m) for m in picks])


@bot.slash_command(name="wallet", description="Bets and track record of an address", guild_ids=GUILDS)
@discord.option("address", str, description="Polymarket address (0x…)")
async def wallet(ctx, address: str):
    a = address.strip()
    if not (a.startswith("0x") and len(a) == 42):
        return await ctx.respond("Invalid address: it must be the 42-character 0x… "
                                 "(the one in your Polymarket profile URL).", ephemeral=True)
    await ctx.defer()
    try:
        ws, _ = await ov.analyze([a])
    except Exception as exc:
        return await ctx.respond(f"Could not reach the Polymarket API: {exc}")
    if not ws:
        return await ctx.respond("Address not found, or it holds no position.")
    w = ws[0]
    openp = [p for p in w.positions if p.get("redeemable") is not True
             and (p.get("currentValue") or 0) >= 0.01]
    unclaimed = [p for p in w.positions if p.get("redeemable") is True
                 and (p.get("currentValue") or 0) >= 0.01]

    e = discord.Embed(title=f"👤 {w.name}",
                      url=f"https://polymarket.com/profile/{w.addr}",
                      colour=GREEN if w.open_pnl >= 0 else RED)
    e.add_field(name="Open positions",
                value=f"{ov.fmt_usd(w.portfolio)} · {len(openp)} bets", inline=True)
    e.add_field(name="Unrealised P&L",
                value=f"{'+' if w.open_pnl>=0 else '−'}{ov.fmt_usd(abs(w.open_pnl))}", inline=True)
    e.add_field(name="Real track record", value=w.record_str(), inline=False)
    if unclaimed:
        e.add_field(name="💰 Unclaimed winnings",
                    value=f"**{ov.fmt_usd(sum(p['currentValue'] for p in unclaimed))}** across "
                          f"{len(unclaimed)} winning bets — the money only lands once claimed.",
                    inline=False)
    top = sorted(openp, key=lambda p: -(p.get("currentValue") or 0))[:5]
    if top:
        e.add_field(name="Biggest open positions", value="\n".join(
            f"• **{(p.get('title') or '')[:60]}** — "
            f"{ov.outcome_label(p.get('title'), p.get('outcome'))} · "
            f"{ov.fmt_usd(p.get('currentValue') or 0)} (entered at {round((p.get('avgPrice') or 0)*100)}¢)"
            for p in top)[:1000], inline=False)
    if w.tr and w.tr.days:
        e.set_footer(text=f"Track record over the last {ov.ACTIVITY_LIMIT} events "
                          f"(~{w.tr.days}d) — the API does not go back further.")
    await ctx.respond(embed=e)


@bot.slash_command(name="watch", description="Subscribe this channel to alerts", guild_ids=GUILDS)
@discord.default_permissions(manage_guild=True)
@discord.option("threshold", int, description="Only alert above this amount ($)",
                min_value=0, default=DEFAULT_MIN_VALUE, required=False)
async def watch(ctx, threshold: int):
    await ctx.defer()
    db.execute("INSERT OR REPLACE INTO subs VALUES(?,?,?,?)",
               (ctx.channel.id, ctx.guild.id if ctx.guild else 0, threshold, int(time.time())))
    db.commit()
    await ctx.respond(
        f"✅ This channel will get alerts: smart-money entries above "
        f"**{ov.fmt_usd(threshold)}**, and their exits. Checked every {POLL_MINUTES} min.\n"
        f"_The first cycle is the baseline — alerts start from the next one._")


@bot.slash_command(name="board",
                   description="Install the live board of best overlaps in this channel",
                   guild_ids=GUILDS)
async def board(ctx):
    await ctx.defer()
    _, markets = await get_analysis()
    msg = await ctx.channel.send(embed=board_embed(markets))
    try:
        await msg.pin()
    except discord.Forbidden:
        pass                                          # pas la permission d'épingler (silencieux)
    db.execute("INSERT OR REPLACE INTO board VALUES(?,?,?)",
               (ctx.channel.id, msg.id, int(time.time())))
    db.commit()
    await ctx.respond(
        f"Board installed and pinned. It is **rewritten every {POLL_MINUTES} min** in place, "
        "so this channel always shows the current state — no feed to scroll through.\n"
        "Run `/guide` to post the how-to-read message and pin it too.", ephemeral=True)


@bot.slash_command(name="guide", description="Post the how-to-read guide for this channel (pin it)",
                   guild_ids=GUILDS)
async def guide(ctx):
    await ctx.defer()
    e = discord.Embed(
        title="📖 How to read this channel",
        description="This channel tracks the **50 best Polymarket traders of the week** and "
                    "flags the bets that **several of them land on together**.",
        colour=GOLD)
    e.add_field(
        name="1️⃣ What is an \u201coverlap\u201d?",
        value="A market where **at least 2 good traders bet on the same side**. "
              "One trader alone can be wrong; when several good ones converge, "
              "it is worth a look.", inline=False)
    e.add_field(
        name="2️⃣ The verdicts",
        value="🟢 **BUY** — the tracked traders see this outcome as likelier than the market does\n"
              "⚪ **WATCH** — their view matches the price, nothing special to gain\n"
              "🔴 **AVOID** — those holding it usually lose, or entered badly\n"
              "⚔️ **they disagree** — tracked traders contradict each other: weak signal", inline=False)
    e.add_field(
        name="3️⃣ The numbers",
        value="**Price in ¢** = the market's probability (58¢ ≈ 58% chance). "
              "It is also your cost: 58¢ staked pays $1 if you win.\n"
              "**Estimated probability** = the same thing adjusted for *who* is positioned "
              "(their track record, how much of their portfolio they put in, their entry price).\n"
              "**$100 → +X** = what $100 would return if the bet lands.", inline=False)
    e.add_field(
        name="4️⃣ Over / Under",
        value="These are not teams: it is the **match point total**. "
              "\u201cOver 8.5\u201d = 9 points or more, both teams combined. "
              "A single match has several lines (7.5, 8.5, 9.5…).", inline=False)
    e.add_field(
        name="5️⃣ Keep in mind",
        value="These traders **lose bets too**. Copying is not winning: they enter at a price "
              "you will not get, and they can exit without warning. "
              "**Nothing here is financial advice** — only stake what you can afford to lose.",
        inline=False)
    e.set_footer(text="/board installs the live ranking · /best shows the top on demand "
                      "· /wallet <address> analyses a portfolio")
    await ctx.respond(embed=e)


@bot.slash_command(name="unwatch", description="Unsubscribe this channel", guild_ids=GUILDS)
@discord.default_permissions(manage_guild=True)
async def unwatch(ctx):
    await ctx.defer()
    db.execute("DELETE FROM subs WHERE channel_id=?", (ctx.channel.id,))
    db.commit()
    await ctx.respond("🔕 Alerts switched off for this channel.")


@bot.slash_command(name="status", description="Monitoring status", guild_ids=GUILDS)
async def status(ctx):
    await ctx.defer()
    n = db.execute("SELECT COUNT(*) FROM subs").fetchone()[0]
    tracked = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    last = meta_get("last_run")
    ago = f"{int((time.time()-int(last))//60)} min ago" if last else "never"
    await ctx.respond(
        f"**Monitoring**: {n} subscribed channel(s) · {tracked} markets tracked\n"
        f"**Last check**: {ago} · every {POLL_MINUTES} min · "
        f"top {PRESET_SIZE} traders of the week")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "Aucun jeton trouvé. Crée une application sur https://discord.com/developers/applications, "
            "copie le jeton du bot dans un fichier 'token.txt' à côté de ce script "
            "(ou dans la variable d'environnement DISCORD_BOT_TOKEN). Ne le partage avec personne.")
    bot.run(TOKEN)
