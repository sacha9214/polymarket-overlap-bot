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
  /watch-buys  ce salon reçoit les ENTRÉES du smart money
  /watch-exits ce salon reçoit les SORTIES
  /unwatch     désabonner ce salon
  /status      état de la surveillance

Textes affichés en anglais (public du serveur) ; commentaires en français.

Jeton : mets-le dans un fichier token.txt à côté de ce script (une ligne),
ou dans la variable d'environnement DISCORD_BOT_TOKEN. Ne le partage jamais.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import sqlite3
import sys
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

# Un seul bot à la fois. Deux instances simultanées se battent pour enregistrer les
# commandes (chacune efface celles de l'autre au démarrage) : elles disparaissent alors
# du menu Discord. Elles doublent aussi les alertes et la charge sur l'API.
_LOCK_PATH = Path(__file__).with_name("bot.lock")
_lock_file = None


def acquire_single_instance_lock():
    """Pris au LANCEMENT seulement : le module doit rester importable pour les tests."""
    global _lock_file
    _lock_file = open(_LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another instance is already running (bot.lock held). "
                 "Stop it first:  pkill -f bot.py")
    _lock_file.write(str(os.getpid()))
    _lock_file.flush()

DB_PATH = Path(__file__).with_name("overlap.db")
POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "2"))    # cycle court : cache l'historique
PRESET_SIZE = int(os.environ.get("PRESET_SIZE", "50"))   # top 50 de la semaine
DEFAULT_MIN_VALUE = 10_000     # n'alerte pas pour des miettes

GREEN, RED, GREY, GOLD = 0x22C55E, 0xEF4444, 0x8B93A1, 0xEAB308

# Droits demandés à l'invitation. /setup a besoin de créer des salons (16) et
# d'épingler (8192) ; le reste sert à lire, écrire et modifier ses propres messages.
# 16 + 1024 + 2048 + 8192 + 16384 + 65536
INVITE_PERMS = 93200

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.execute("""CREATE TABLE IF NOT EXISTS subs(
  channel_id INTEGER PRIMARY KEY, guild_id INTEGER,
  min_value REAL DEFAULT 10000, created INTEGER)""")
# Un salon s'abonne désormais à un TYPE d'alerte : « buys » ou « exits ». La clé porte
# donc sur (salon, type), et un même salon peut prendre les deux s'il le veut.
db.execute("""CREATE TABLE IF NOT EXISTS feeds(
  channel_id INTEGER, kind TEXT, guild_id INTEGER,
  min_value REAL DEFAULT 10000, created INTEGER,
  PRIMARY KEY(channel_id, kind))""")
# Reprise des abonnements de l'ancienne table : ils recevaient les deux flux.
if not db.execute("SELECT 1 FROM feeds LIMIT 1").fetchone():
    for ch_id, g_id, mv, cr in db.execute("SELECT channel_id, guild_id, min_value, created FROM subs"):
        for k in ("buys", "exits"):
            db.execute("INSERT OR IGNORE INTO feeds VALUES(?,?,?,?,?)", (ch_id, k, g_id, mv, cr))
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
# Le mode d'emploi est épinglé : relancer /guide doit RÉÉCRIRE ce message, pas en
# empiler un second (sinon l'ancienne version reste visible — cas vécu après la
# traduction : le guide français continuait de s'afficher).
db.execute("""CREATE TABLE IF NOT EXISTS guides(
  channel_id INTEGER PRIMARY KEY, message_id INTEGER, updated INTEGER)""")

# Salons retenus par IDENTIFIANT, pas par nom : un identifiant survit aux
# renommages, un nom non. Sans ça, renommer « buy-alerts » en « buy-alerts🚨 »
# fait que /setup ne le reconnaît plus et en recrée un doublon à côté.
# Wallets suivis en permanence, en plus du top 50 hebdomadaire. Un très bon
# trader peut être absent du palmarès de la semaine (RN1 est 4e all-time mais
# n'y figurait pas) : sans épinglage, on le perd de vue.
db.execute("""CREATE TABLE IF NOT EXISTS pinned(
  addr TEXT PRIMARY KEY, label TEXT, added INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS channels(
  guild_id INTEGER, key TEXT, channel_id INTEGER,
  PRIMARY KEY(guild_id, key))""")
db.commit()


def _norm(name):
    """Nom comparable : emojis, majuscules et ponctuation retirés."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def seed_channels_from_legacy(guild):
    """Récupère la correspondance depuis les tables déjà remplies.

    Les salons de ce serveur ont été renommés avant l'existence de la table
    `channels` : ni l'ID ni le nom ne permettent de les retrouver. Mais on sait
    déjà où vivent le guide, le tableau et les deux flux — c'est écrit dans
    `guides`, `board` et `feeds`. On s'en sert pour reconstituer la table.
    """
    known = {}
    r = db.execute("SELECT channel_id FROM guides").fetchone()
    if r: known["how-it-works"] = r[0]
    r = db.execute("SELECT channel_id FROM board").fetchone()
    if r: known["best-overlaps"] = r[0]
    for kind, key in (("buys", "buy-alerts"), ("exits", "exit-alerts")):
        r = db.execute("SELECT channel_id FROM feeds WHERE kind=?", (kind,)).fetchone()
        if r: known[key] = r[0]
    for key, cid in known.items():
        if guild.get_channel(cid) is not None:
            db.execute("INSERT OR IGNORE INTO channels VALUES(?,?,?)",
                       (guild.id, key, cid))
    db.commit()


async def ensure_channel(guild, cat, key, display, topic, overwrites):
    """Retrouve un salon par ID mémorisé, puis par nom normalisé, sinon le crée."""
    row = db.execute("SELECT channel_id FROM channels WHERE guild_id=? AND key=?",
                     (guild.id, key)).fetchone()
    if row:
        ch = guild.get_channel(row[0])
        if ch is not None:
            return ch, False

    target = _norm(key)
    for ch in cat.text_channels:
        if _norm(ch.name) == target:
            db.execute("INSERT OR REPLACE INTO channels VALUES(?,?,?)",
                       (guild.id, key, ch.id))
            db.commit()
            return ch, False

    ch = await guild.create_text_channel(display, category=cat, topic=topic,
                                         overwrites=overwrites)
    db.execute("INSERT OR REPLACE INTO channels VALUES(?,?,?)", (guild.id, key, ch.id))
    db.commit()
    return ch, True
db.commit()


# "flag" (défaut) : les market makers sont signalés, rien n'est retiré.
# "exclude"        : ils sortent du calcul d'overlap.
# "off"            : comportement d'avant le détecteur, à l'identique.
def mm_mode() -> str:
    return meta_get("mm_mode", "flag") or "flag"


def mm_badge(w) -> str:
    """Marqueur affiché à côté d'un trader dont les positions sont un stock."""
    if mm_mode() == "off" or not getattr(w, "is_market_maker", False):
        return ""
    return " 🤖"


def pinned_addrs() -> list[str]:
    return [r[0] for r in db.execute("SELECT addr FROM pinned").fetchall()]


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
        ws, ms = await ov.analyze_preset("week", PRESET_SIZE, pinned=pinned_addrs())
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
                f"**{w.name}**{mm_badge(w)}\n"
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
def build_guide_embed() -> discord.Embed:
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
    return e


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
            print("board: update failed —", exc)
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
    feeds = db.execute("SELECT 1 FROM feeds LIMIT 1").fetchone()
    boards = db.execute("SELECT 1 FROM board LIMIT 1").fetchone()
    if not feeds and not boards:
        return                                   # personne à prévenir : on n'appelle pas l'API
    try:
        wallets, markets = await get_analysis(force=True)
    except Exception as exc:
        print("watcher: analysis failed —", exc)
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
        print(f"watcher: baseline saved ({len(markets)} markets, "
              f"{len(cur_addrs)} wallets), no alert sent.")
        return

    stamp = time.strftime("%H:%M:%S")
    churn = len(cur_addrs - prev_addrs) + len(prev_addrs - cur_addrs)
    print(f"watcher {stamp}: {len(markets)} markets · {len(cur_addrs)} wallets "
          f"({churn} leaderboard changes ignored) → "
          f"{len(entries)} BUY entr{'y' if len(entries)==1 else 'ies'}, {len(exits)} exit(s)")

    # Flux « buys » : uniquement les nouvelles entrées.
    for channel_id, min_value in db.execute(
            "SELECT channel_id, min_value FROM feeds WHERE kind='buys'").fetchall():
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
        picks = [m for m in entries if m["totalValue"] >= (min_value or 0)]
        picks.sort(key=lambda m: -m["evTime"])
        for m in picks[:5]:                      # jamais plus de 5 alertes par cycle
            try:
                await ch.send(embed=market_embed(m, kind="new"))
            except Exception as exc:
                print("watcher: send failed —", exc)

    # Flux « exits » : uniquement les liquidations.
    for channel_id, min_value in db.execute(
            "SELECT channel_id, min_value FROM feeds WHERE kind='exits'").fetchall():
        ch = bot.get_channel(channel_id)
        if ch is None:
            continue
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
                print("watcher: send failed —", exc)


@watcher.before_loop
async def _before():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Connected: {bot.user} · watching every {POLL_MINUTES} min")
    # L'identifiant d'un bot EST l'Application ID : il peut donc afficher son propre
    # lien d'invitation, sans qu'on ait à retourner chercher quoi que ce soit.
    # Permissions 18432 = Send Messages (2048) + Embed Links (16384).
    print("Invite link: "
          f"https://discord.com/oauth2/authorize?client_id={bot.user.id}"
          f"&permissions={INVITE_PERMS}&scope=bot%20applications.commands")
    print(f"Servers: {[g.name for g in bot.guilds] or 'none — use the invite link above'}")

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
                # On affiche aussi les permissions exigées : une commande restreinte
                # reste invisible pour les membres sans le droit correspondant.
                def perms(c):
                    p = c.get("default_member_permissions")
                    return "" if p in (None, "0") else f" (restricted: {p})"
                print(f"Commands on \u201c{g.name}\u201d ({len(cmds)}): "
                      + ", ".join('/' + c["name"] + perms(c)
                                  for c in sorted(cmds, key=lambda c: c["name"])))
            glob = await bot.http.get_global_commands(bot.application_id)
            print(f"Global commands left: {len(glob)} (expect 0, otherwise duplicates)")
        except Exception as exc:
            print("Command sync failed —", exc)

    if not watcher.is_running():
        watcher.start()


# ---------------------------------------------------------------------------
# Commandes
# ---------------------------------------------------------------------------
# NB : les options sont déclarées en décorateur et pas en annotation de type.
# Sous Python 3.14 les annotations sont évaluées paresseusement (PEP 649) et
# py-cord les lisait comme du texte : toutes les options devenaient des chaînes
# obligatoires. Le décorateur ne dépend pas des annotations.
@bot.slash_command(
    name="marketmakers",
    description="How to handle market makers in the analysis",
    guild_ids=GUILDS,
)
@discord.default_permissions(manage_guild=True)
@discord.option("mode", str, description="off, flag or exclude",
                choices=["off", "flag", "exclude"], default="", required=False)
async def marketmakers(ctx, mode: str):
    await ctx.defer(ephemeral=True)
    mode = (mode or "").strip().lower()

    if mode not in {"off", "flag", "exclude"}:
        return await ctx.respond(
            f"Current mode: **{mm_mode()}**\n\n"
            "A market maker has no view — they quote both sides and collect the "
            "spread. Their positions are **inventory, not conviction**, so "
            "reading them as smart money is misleading.\n\n"
            "`off` — no detection (exactly how it worked before)\n"
            "`flag` — mark them 🤖 in alerts, change nothing else\n"
            "`exclude` — drop them from the overlap calculation\n\n"
            "Reference: RN1, #4 all-time, ticks every box — 773 trades/hour, "
            "$17 median fill, biggest position 1.5% of capital, both sides held "
            "on 38 events.",
            ephemeral=True,
        )

    meta_set("mm_mode", mode)
    txt = {
        "off": "🔕 Detection off — exactly the behaviour from before this feature.",
        "flag": "🤖 Market makers will be marked in alerts. Nothing is removed.",
        "exclude": "🚫 Market makers are dropped from the overlap calculation. "
                   "They stay visible via `/wallet`.",
    }[mode]
    await ctx.respond(f"{txt}\nTakes effect on the next cycle.", ephemeral=True)


@bot.slash_command(
    name="track",
    description="Always follow this trader, even outside the weekly top 50",
    guild_ids=GUILDS,
)
@discord.option("profile", str, description="Profile URL, username, or 0x address")
async def track(ctx, profile: str):
    await ctx.defer(ephemeral=True)
    u = await ov.resolve_profile(profile)
    if not u:
        return await ctx.respond(
            f"❌ Couldn't find **{profile}**.\n"
            "Give a profile URL (`https://polymarket.com/@name`), a username, or "
            "a `0x…` address. Usernames only resolve for traders who appear in a "
            "leaderboard — otherwise paste the address.",
            ephemeral=True,
        )
    addr = u["proxyWallet"].lower()
    name = u.get("userName") or addr[:6] + "…" + addr[-4:]
    db.execute("INSERT OR REPLACE INTO pinned VALUES(?,?,?)",
               (addr, name, int(time.time())))
    db.commit()

    pnl = u.get("pnl")
    # Le profil est résolu depuis le palmarès de la SEMAINE en premier : annoncer
    # « all-time » serait faux dès que le trader y figure (RN1 : 136 K\u00a0$ sur la
    # semaine, 12,8\u00a0M$ en cumulé).
    extra = f"\nLeaderboard P&L: **{ov.fmt_usd(pnl)}**" if pnl else ""
    await ctx.respond(
        f"✅ Now tracking **{name}** (`{addr[:10]}…`).{extra}\n"
        f"They'll be analysed every cycle alongside the weekly top {PRESET_SIZE}, "
        "whether or not they're in it.\nTakes effect on the next cycle.",
        ephemeral=True,
    )


@bot.slash_command(
    name="untrack", description="Stop following a pinned trader", guild_ids=GUILDS
)
@discord.option("profile", str, description="Username or 0x address to unpin")
async def untrack(ctx, profile: str):
    await ctx.defer(ephemeral=True)
    key = profile.strip().lower().lstrip("@")
    row = db.execute(
        "SELECT addr, label FROM pinned WHERE addr=? OR LOWER(label)=?", (key, key)
    ).fetchone()
    if not row:
        return await ctx.respond(f"❌ **{profile}** isn't pinned.", ephemeral=True)
    db.execute("DELETE FROM pinned WHERE addr=?", (row[0],))
    db.commit()
    await ctx.respond(f"🔕 Stopped tracking **{row[1]}**.", ephemeral=True)


@bot.slash_command(
    name="tracked", description="Traders pinned on top of the weekly top 50",
    guild_ids=GUILDS,
)
async def tracked(ctx):
    await ctx.defer(ephemeral=True)
    rows = db.execute("SELECT label, addr FROM pinned ORDER BY added").fetchall()
    if not rows:
        return await ctx.respond(
            "No pinned traders. The bot follows the weekly top "
            f"{PRESET_SIZE} only.\nAdd one with `/track <profile url>`.",
            ephemeral=True,
        )
    lines = [f"• **{l}** — [`{a[:10]}…`](https://polymarket.com/profile/{a})"
             for l, a in rows]
    await ctx.respond(
        f"**{len(rows)} pinned trader(s)**, followed every cycle on top of the "
        f"weekly top {PRESET_SIZE}:\n" + "\n".join(lines),
        ephemeral=True,
    )


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


def subscribe(ctx, kind: str, threshold: int) -> None:
    db.execute("INSERT OR REPLACE INTO feeds VALUES(?,?,?,?,?)",
               (ctx.channel.id, kind, ctx.guild.id if ctx.guild else 0,
                threshold, int(time.time())))
    db.commit()


@bot.slash_command(name="watch-buys",
                   description="Send BUY alerts to this channel (smart money entering)",
                   guild_ids=GUILDS)
@discord.default_permissions(manage_guild=True)
@discord.option("threshold", int, description="Only alert above this amount ($)",
                min_value=0, default=DEFAULT_MIN_VALUE, required=False)
async def watch_buys(ctx, threshold: int):
    await ctx.defer()
    subscribe(ctx, "buys", threshold)
    await ctx.respond(
        f"🟢 **Buy alerts enabled here.** You will get a card whenever tracked traders "
        f"**open** a position worth more than **{ov.fmt_usd(threshold)}** and the verdict is BUY.\n"
        f"Checked every {POLL_MINUTES} min · exits go to their own channel via `/watch-exits`.\n"
        f"_The first cycle is the baseline — alerts start from the next one._")


@bot.slash_command(name="watch-exits",
                   description="Send EXIT alerts to this channel (positions they closed)",
                   guild_ids=GUILDS)
@discord.default_permissions(manage_guild=True)
@discord.option("threshold", int, description="Only alert above this amount ($)",
                min_value=0, default=DEFAULT_MIN_VALUE, required=False)
async def watch_exits(ctx, threshold: int):
    await ctx.defer()
    subscribe(ctx, "exits", threshold)
    await ctx.respond(
        f"🔴 **Exit alerts enabled here.** You will be told when tracked traders **close** "
        f"a position that was worth more than **{ov.fmt_usd(threshold)}**.\n"
        f"Checked every {POLL_MINUTES} min · buys go to their own channel via `/watch-buys`.\n"
        f"_Only traders still in the tracked list count — a leaderboard reshuffle is not an exit._")


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
    e = build_guide_embed()

    # Réécrire le message existant s'il y en a un : sinon l'ancienne version reste
    # affichée dans le salon et deux guides cohabitent.
    row = db.execute("SELECT message_id FROM guides WHERE channel_id=?",
                     (ctx.channel.id,)).fetchone()
    if row:
        try:
            msg = await ctx.channel.fetch_message(row[0])
            await msg.edit(embed=e)
            db.execute("UPDATE guides SET updated=? WHERE channel_id=?",
                       (int(time.time()), ctx.channel.id))
            db.commit()
            return await ctx.respond("📖 Guide updated in place (pinned message).",
                                     ephemeral=True)
        except discord.NotFound:
            pass                                      # message supprimé → on le recrée

    msg = await ctx.channel.send(embed=e)
    try:
        await msg.pin()
    except discord.Forbidden:
        pass                                          # pas la permission d'épingler
    db.execute("INSERT OR REPLACE INTO guides VALUES(?,?,?)",
               (ctx.channel.id, msg.id, int(time.time())))
    db.commit()
    await ctx.respond("📖 Guide posted and pinned. Running /guide again will update "
                      "this same message instead of adding another one.", ephemeral=True)


@bot.slash_command(name="setup",
                   description="Create the full channel structure and wire everything up",
                   guild_ids=GUILDS)
@discord.default_permissions(manage_guild=True)
async def setup(ctx):
    await ctx.defer(ephemeral=True)
    g = ctx.guild
    if g is None:
        return await ctx.respond("Run this in a server, not in a DM.", ephemeral=True)
    me = g.me
    if not me.guild_permissions.manage_channels:
        return await ctx.respond(
            "I need the **Manage Channels** permission to build the structure.\n"
            "Either grant it to my role in Server Settings → Roles, or re-invite me with:\n"
            f"https://discord.com/oauth2/authorize?client_id={bot.user.id}"
            f"&permissions={INVITE_PERMS}&scope=bot%20applications.commands", ephemeral=True)

    # Salons en lecture seule : tout le monde lit, seul le bot écrit. Un flux d'alertes
    # où n'importe qui peut poster devient illisible en deux jours.
    read_only = {
        g.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=True),
        me: discord.PermissionOverwrite(send_messages=True, manage_messages=True),
    }

    # (clé logique, nom affiché à la création, sujet, lecture seule)
    # La clé identifie le salon en base et ne change jamais ; le nom affiché est
    # libre, tu peux le renommer sans que /setup fasse des doublons.
    plan = [
        ("how-it-works", "📖overlap-guide",
         "Read this first — what an overlap is and how to read the alerts", True),
        ("best-overlaps", "📈best-overlaps",
         "Live ranking of the best overlaps, rewritten automatically", True),
        ("buy-alerts", "🚨buy-alerts", "Smart money OPENING a position (verdict BUY)", True),
        ("exit-alerts", "🔻exit-alerts", "Smart money CLOSING a position — as telling as a buy", True),
        ("discussion", "💬overlap-discussion", "Talk about the calls here — this one is open to everyone", False),
    ]

    cat = discord.utils.get(g.categories, name="POLYMARKET OVERLAP")
    if cat is None:
        cat = await g.create_category("POLYMARKET OVERLAP")

    seed_channels_from_legacy(g)

    made, reused, chans = [], [], {}
    for key, display, topic, locked in plan:
        try:
            ch, created = await ensure_channel(
                g, cat, key, display, topic,
                # py-cord EXIGE un dict : `None` lève InvalidArgument et faisait
                # échouer toute la commande sur « discussion », le seul salon
                # ouvert — d'où un salon manquant sans message d'erreur visible.
                read_only if locked else {})
        except discord.HTTPException as e:
            return await ctx.respond(
                f"Could not create **{display}**: {e}\n"
                "Run `/setup` again once fixed — existing channels are reused.",
                ephemeral=True)
        (made if created else reused).append(ch)
        chans[key] = ch

    # Guide épinglé
    guide_ch = chans["how-it-works"]
    e = build_guide_embed()
    row = db.execute("SELECT message_id FROM guides WHERE channel_id=?", (guide_ch.id,)).fetchone()
    msg = None
    if row:
        try:
            msg = await guide_ch.fetch_message(row[0])
            await msg.edit(embed=e)
        except discord.NotFound:
            msg = None
    if msg is None:
        msg = await guide_ch.send(embed=e)
        try:
            await msg.pin()
        except discord.Forbidden:
            pass
    db.execute("INSERT OR REPLACE INTO guides VALUES(?,?,?)",
               (guide_ch.id, msg.id, int(time.time())))

    # Tableau vivant
    board_ch = chans["best-overlaps"]
    _, markets = await get_analysis()
    row = db.execute("SELECT message_id FROM board WHERE channel_id=?", (board_ch.id,)).fetchone()
    bmsg = None
    if row:
        try:
            bmsg = await board_ch.fetch_message(row[0])
            await bmsg.edit(embed=board_embed(markets))
        except discord.NotFound:
            bmsg = None
    if bmsg is None:
        bmsg = await board_ch.send(embed=board_embed(markets))
        try:
            await bmsg.pin()
        except discord.Forbidden:
            pass
    db.execute("INSERT OR REPLACE INTO board VALUES(?,?,?)",
               (board_ch.id, bmsg.id, int(time.time())))

    # Abonnements des deux flux
    for name, kind in (("buy-alerts", "buys"), ("exit-alerts", "exits")):
        db.execute("INSERT OR REPLACE INTO feeds VALUES(?,?,?,?,?)",
                   (chans[name].id, kind, g.id, DEFAULT_MIN_VALUE, int(time.time())))
    db.commit()

    lines = [f"**Setup complete.**",
             f"📖 {guide_ch.mention} — guide posted and pinned",
             f"🏆 {board_ch.mention} — live board, rewritten every {POLL_MINUTES} min",
             f"🟢 {chans['buy-alerts'].mention} — buy alerts above {ov.fmt_usd(DEFAULT_MIN_VALUE)}",
             f"🔴 {chans['exit-alerts'].mention} — exit alerts above {ov.fmt_usd(DEFAULT_MIN_VALUE)}",
             f"💬 {chans['discussion'].mention} — open to everyone"]
    if made:
        lines.append(f"\nCreated: {', '.join(c.mention for c in made)}")
    if reused:
        lines.append(f"Reused existing: {', '.join(c.mention for c in reused)}")
    lines.append("\nAlert channels are read-only for members (reactions still allowed). "
                 "Change a threshold anytime with `/watch-buys` or `/watch-exits` in that channel.")
    await ctx.respond("\n".join(lines), ephemeral=True)


@bot.slash_command(name="unwatch", description="Unsubscribe this channel", guild_ids=GUILDS)
@discord.default_permissions(manage_guild=True)
async def unwatch(ctx):
    await ctx.defer()
    kinds = [k for (k,) in db.execute("SELECT kind FROM feeds WHERE channel_id=?",
                                      (ctx.channel.id,))]
    db.execute("DELETE FROM feeds WHERE channel_id=?", (ctx.channel.id,))
    db.execute("DELETE FROM subs WHERE channel_id=?", (ctx.channel.id,))
    db.commit()
    if not kinds:
        return await ctx.respond("This channel had no alerts enabled.")
    label = " and ".join({"buys": "buy", "exits": "exit"}[k] for k in sorted(kinds))
    await ctx.respond(f"🔕 {label.capitalize()} alerts switched off for this channel.")


@bot.slash_command(name="status", description="Monitoring status", guild_ids=GUILDS)
async def status(ctx):
    await ctx.defer()
    buys = db.execute("SELECT channel_id FROM feeds WHERE kind='buys'").fetchall()
    exits = db.execute("SELECT channel_id FROM feeds WHERE kind='exits'").fetchall()
    tracked = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    last = meta_get("last_run")
    ago = f"{int((time.time()-int(last))//60)} min ago" if last else "never"
    await ctx.respond(
        f"**Monitoring**: {tracked} markets tracked\n"
        f"🟢 **Buy alerts** → {', '.join(f'<#{c}>' for (c,) in buys) or 'nowhere yet (`/watch-buys`)'}\n"
        f"🔴 **Exit alerts** → {', '.join(f'<#{c}>' for (c,) in exits) or 'nowhere yet (`/watch-exits`)'}\n"
        f"**Last check**: {ago} · every {POLL_MINUTES} min · "
        f"top {PRESET_SIZE} traders of the week")


if __name__ == "__main__":
    acquire_single_instance_lock()
    if not TOKEN:
        raise SystemExit(
            "No token found. Create an application at https://discord.com/developers/applications, "
            "put the bot token in a file named 'token.txt' next to this script "
            "(or in the DISCORD_BOT_TOKEN environment variable). Never share it with anyone.")
    bot.run(TOKEN)
