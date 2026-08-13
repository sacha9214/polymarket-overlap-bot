"""
Alertes Polymarket Overlap via un simple webhook Discord — sans créer de bot.

Un webhook ne sait qu'envoyer des messages, ce qui suffit pour les alertes
(entrées et sorties du smart money). Les commandes /best, /wallet… demandent
en revanche une vraie application bot : c'est bot.py qu'il faut alors lancer.

Mise en route (30 secondes, aucun jeton de bot) :
  1. Discord → Paramètres du serveur → Intégrations → Webhooks → Nouveau webhook
  2. Choisis le salon, puis « Copier l'URL du webhook »
  3. Colle cette URL dans un fichier webhook.txt à côté de ce script
  4. python3 webhook_alerts.py

L'URL du webhook permet de publier dans ce salon : garde-la pour toi.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

import aiohttp

import overlap as ov

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
if not WEBHOOK:
    _f = Path(__file__).with_name("webhook.txt")
    if _f.exists():
        WEBHOOK = _f.read_text(encoding="utf-8").strip()

DB_PATH = Path(__file__).with_name("overlap.db")
POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "20"))
PRESET_SIZE = int(os.environ.get("PRESET_SIZE", "40"))
MIN_VALUE = float(os.environ.get("MIN_VALUE", "10000"))   # n'alerte pas pour des miettes
MAX_PER_CYCLE = 5

GREEN, RED, GOLD = 0x22C55E, 0xEF4444, 0xEAB308

db = sqlite3.connect(DB_PATH)
db.execute("""CREATE TABLE IF NOT EXISTS seen(
  key TEXT PRIMARY KEY, title TEXT, outcome TEXT, value REAL, ts INTEGER)""")
db.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""")
db.commit()


def meta_get(k):
    r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def meta_set(k, v):
    db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    db.commit()


def entry_embed(m: dict) -> dict:
    """Même contenu que l'embed du bot, en JSON brut pour le webhook."""
    _cls, label, pts = ov.verdict(m)
    fields = [
        {"name": "Proba estimée",
         "value": f"**{round(m['myProba']*100)}%** vs marché {round(m['price']*100)}% ({pts:+d} pts)",
         "inline": True},
        {"name": "Argent engagé",
         "value": f"{ov.fmt_usd(m['totalValue'])} · {len(m['holders'])} traders",
         "inline": True},
    ]
    if m.get("daysLeft") is not None:
        d = m["daysLeft"]
        fields.append({"name": "Échéance",
                       "value": "aujourd'hui" if d <= 0 else ("demain" if d == 1 else f"dans {d} j"),
                       "inline": True})

    top = sorted(m["holders"], key=lambda h: -h["value"])[:3]
    fields.append({"name": "Qui est dessus", "inline": False, "value": "\n".join(
        f"• **{h['wallet'].name}** — {ov.fmt_usd(h['value'])} "
        f"(entré à {round(h['avg']*100)}¢) · {h['wallet'].record_str()}" for h in top)[:1000] or "—"})

    if m.get("contested"):
        opp = [o for o in m["others"] if o["n"] > 0]
        fields.append({"name": "⚔️ Désaccord", "inline": False, "value": " · ".join(
            f"{o['n']} sur « {ov.outcome_label(m['title'], o['outcome'])} » ({ov.fmt_usd(o['value'])})"
            for o in opp)[:1000]})

    e = {"title": f"🆕 Le smart money vient d'entrer · {label}"[:250],
         "description": f"**{m['title']}**"[:2000],
         "url": ov.market_url(m), "color": GOLD, "fields": fields}
    if m.get("hasTwins"):
        e["footer"] = {"text": "⚠️ Des wallets suivis se ressemblent beaucoup — probablement la même personne."}
    return e


def exits_embed(rows) -> dict:
    txt = "\n".join(f"• **{r[1]}** — « {ov.outcome_label(r[1], r[2])} » "
                    f"(valait {ov.fmt_usd(r[3] or 0)})" for r in rows[:MAX_PER_CYCLE])
    return {"title": "🚪 Sorties — ils ont liquidé ces positions",
            "description": txt[:3800], "color": RED,
            "footer": {"text": "Une sortie est un signal au moins aussi fort qu'une entrée."}}


async def post(session: aiohttp.ClientSession, embeds: list[dict]):
    # Discord accepte jusqu'à 10 embeds par message.
    for i in range(0, len(embeds), 10):
        async with session.post(WEBHOOK, json={"embeds": embeds[i:i+10]}) as r:
            if r.status == 429:                       # trop de messages : on respecte le délai
                retry = (await r.json()).get("retry_after", 5)
                await asyncio.sleep(float(retry) + 0.5)
                async with session.post(WEBHOOK, json={"embeds": embeds[i:i+10]}) as r2:
                    r2.raise_for_status()
            elif r.status >= 400:
                raise RuntimeError(f"webhook refusé ({r.status}) : {await r.text()}")


async def cycle(session: aiohttp.ClientSession):
    _, markets = await ov.analyze_preset("week", PRESET_SIZE)
    now = int(time.time())
    prev = {r[0]: r for r in db.execute("SELECT key, title, outcome, value FROM seen")}
    current = {m["key"]: m for m in markets}

    # Premier passage : photo de référence, sinon 200 alertes d'un coup.
    first_run = meta_get("initialised") is None
    entries = [] if first_run else [m for k, m in current.items()
                                    if k not in prev and ov.verdict(m)[0] == "buy"]
    exits = [] if first_run else [prev[k] for k in prev if k not in current]

    db.execute("DELETE FROM seen")
    db.executemany("INSERT INTO seen VALUES(?,?,?,?,?)",
                   [(m["key"], m["title"], m["outcome"], m["totalValue"], now) for m in markets])
    meta_set("initialised", "1")
    meta_set("last_run", now)

    if first_run:
        print(f"[{time.strftime('%H:%M')}] état initial enregistré "
              f"({len(markets)} marchés) — aucune alerte envoyée.")
        return

    picks = sorted([m for m in entries if m["totalValue"] >= MIN_VALUE],
                   key=lambda m: -m["evTime"])[:MAX_PER_CYCLE]
    big_exits = [x for x in exits if (x[3] or 0) >= MIN_VALUE]

    embeds = [entry_embed(m) for m in picks]
    if big_exits:
        embeds.append(exits_embed(big_exits))
    if embeds:
        await post(session, embeds)
    print(f"[{time.strftime('%H:%M')}] {len(markets)} marchés · "
          f"{len(picks)} entrée(s), {len(big_exits)} sortie(s) → {len(embeds)} message(s)")


async def main():
    if not WEBHOOK.startswith("https://discord.com/api/webhooks/"):
        sys.exit("Aucune URL de webhook valide. Colle-la dans un fichier webhook.txt "
                 "à côté de ce script (Discord → Paramètres du serveur → Intégrations → Webhooks), "
                 "ou dans la variable d'environnement DISCORD_WEBHOOK_URL.")
    print(f"Surveillance du top {PRESET_SIZE} · vérification toutes les {POLL_MINUTES} min · "
          f"seuil {ov.fmt_usd(MIN_VALUE)}")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await cycle(session)
            except Exception as exc:
                print("cycle en échec :", exc)
            await asyncio.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    asyncio.run(main())
