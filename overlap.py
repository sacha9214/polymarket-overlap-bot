"""
Moteur Polymarket Overlap — port fidèle du modèle du site.

Aucune dépendance Discord ici : ce module se teste seul (`python3 overlap.py`),
ce qui permet de vérifier les chiffres sans jamais lancer le bot.

Source des données : APIs publiques Polymarket (aucune clé nécessaire).
"""

from __future__ import annotations

import asyncio
import datetime
import math
import re
import time
from dataclasses import dataclass, field

import aiohttp

API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "polymarket-overlap-bot/1.0"}

# L'API renvoie au maximum 500 événements d'historique par wallet.
ACTIVITY_LIMIT = 500


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def fmt_usd(v: float) -> str:
    v = v or 0
    if abs(v) >= 1e6:
        return f"${v/1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def _parse_date(value: str | None) -> datetime.datetime | None:
    """L'API renvoie tantôt « 2026-11-03T00:00:00Z », tantôt « 2026-11-03 » tout court.
    Sans fuseau, la comparaison avec un datetime aware lève une exception : la date
    nue était donc rejetée pour TOUS les marchés, ce qui annulait silencieusement la
    pondération par échéance du classement."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def days_left(end: str | None) -> float | None:
    dt = _parse_date(end)
    if dt is None:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    return math.ceil((dt - now).total_seconds() / 86400)


def outcome_label(title: str, outcome: str) -> str:
    """« Over » seul ne dit rien : la ligne O/U vit dans le titre du marché."""
    o = (outcome or "").strip()
    m = re.search(r"O/U\s*([\d.]+)", title or "", re.I)
    if m and re.fullmatch(r"over|under", o, re.I):
        return f"{o} {m.group(1)}"
    return o


def explain_outcome(title: str, outcome: str) -> str:
    """« Over » n'est pas une équipe : c'est un pari sur le TOTAL de points du match."""
    t, o = title or "", (outcome or "").strip()
    m = re.search(r"O/U\s*([\d.]+)", t, re.I) or re.search(r"(?:Over/Under|Total)\s*([\d.]+)", t, re.I)
    line = m.group(1) if m else None
    if re.fullmatch(r"over", o, re.I):
        return (f"match total **above {line}** ({math.ceil(float(line))} or more, both teams combined)"
                if line else "match total above the line")
    if re.fullmatch(r"under", o, re.I):
        return (f"match total **below {line}** ({math.floor(float(line))} or fewer, both teams combined)"
                if line else "match total below the line")
    if re.search(r"spread|handicap", t, re.I):
        sp = re.search(r"([+-]?\d+(?:[.,]\d+)?)\s*\)?\s*$", t) or re.search(r"([+-]\d+(?:[.,]\d+)?)", t)
        return (f"{o} wins with a {sp.group(1)} handicap applied to their score"
                if sp else f"{o} wins once the handicap is applied")
    if re.fullmatch(r"yes", o, re.I):
        return "the question in the title comes true"
    if re.fullmatch(r"no", o, re.I):
        return "the question in the title does not come true"
    return f"{o} wins"


# ---------------------------------------------------------------------------
# Réseau
# ---------------------------------------------------------------------------
class Client:
    def __init__(self, session: aiohttp.ClientSession, gap: float = 0.12):
        self.s = session
        self.gap = gap          # espacement minimal entre deux requêtes
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def get(self, url: str):
        async with self._lock:                      # sérialisé : l'API n'aime pas les rafales
            wait = self.gap - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
        async with self.s.get(url, headers=UA, timeout=aiohttp.ClientTimeout(total=25)) as r:
            r.raise_for_status()
            return await r.json()

    async def get_or_none(self, url: str):
        try:
            return await self.get(url)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Palmarès réel d'un wallet
# ---------------------------------------------------------------------------
@dataclass
class TrackRecord:
    trades: int = 0
    wins: int = 0
    realized: float = 0.0
    win_rate: float | None = None
    days: int | None = None
    last_buy: dict = field(default_factory=dict)


def _end_ts(value: str | None) -> float | None:
    """Date de résolution d'un marché en timestamp UTC, ou None si absente/illisible."""
    dt = _parse_date(value)
    return dt.timestamp() if dt else None


def track_record(activity: list, positions: list) -> TrackRecord:
    """
    La plus-value latente n'est PAS un palmarès. On reconstruit les paris réellement
    clos : achat → (revente | remboursement). Les parts perdantes ne sont jamais
    remboursées et dorment à 0 $ dans le portefeuille : sans rattrapage on ne
    compterait que les gagnants (100 % de réussite factice).
    """
    by_asset: dict[str, dict] = {}
    oldest = math.inf
    for e in activity or []:
        if not e.get("conditionId"):
            continue
        ts = e.get("timestamp") or 0
        if ts:
            oldest = min(oldest, ts)
        key = f"{e['conditionId']}|{e.get('outcome')}"
        a = by_asset.setdefault(key, {"bought": 0.0, "cost": 0.0, "out": 0.0,
                                      "proceeds": 0.0, "last_buy": 0})
        if e.get("type") == "TRADE" and e.get("side") == "BUY":
            a["bought"] += e.get("size") or 0
            a["cost"] += e.get("usdcSize") or 0
            a["last_buy"] = max(a["last_buy"], ts)
        elif e.get("type") == "TRADE" and e.get("side") == "SELL":
            a["out"] += e.get("size") or 0
            a["proceeds"] += e.get("usdcSize") or 0
        elif e.get("type") == "REDEEM":
            a["out"] += e.get("size") or 0
            a["proceeds"] += e.get("usdcSize") or 0

    tr = TrackRecord()
    settled: set[str] = set()          # paris RÉSOLUS pendant la fenêtre, gagnés ou perdus
    for key, a in by_asset.items():
        if a["last_buy"]:
            tr.last_buy[key] = a["last_buy"]
        # Aller-retour complet observé : on connaît le coût, donc le gain réel.
        if a["bought"] > 0 and a["out"] >= a["bought"] * 0.98:
            pnl = a["proceeds"] - a["cost"]
            tr.trades += 1
            tr.wins += 1 if pnl > 0 else 0
            tr.realized += pnl
            settled.add(key)

    # Les gains ne sont visibles que dans la fenêtre de /activity (plafonnée à 500
    # événements — parfois quelques HEURES chez un gros trader), alors que les positions
    # perdantes s'accumulent indéfiniment dans le portefeuille. Compter toutes les pertes
    # face aux seuls gains récents écrasait le palmarès : mesuré sur wr0ngw4yb3tt0r,
    # 488 pertes retenues dont 7 seulement dans la fenêtre → « 3 % de réussite, −11 M$ »
    # pour un trader à +407 K$ sur la semaine. On ne garde donc que les pertes datées
    # de la même période.
    for p in positions or []:
        if p.get("redeemable") is not True:
            continue
        won = (p.get("currentValue") or 0) >= 0.01
        if not won and (p.get("initialValue") or 0) <= 1:
            continue                                   # poussière, pas un vrai pari
        if oldest is not math.inf:
            end = _end_ts(p.get("endDate"))
            if end is None or end < oldest:
                continue                               # résolu hors fenêtre observée
        key = f"{p.get('conditionId')}|{p.get('outcome')}"
        if key in settled:
            continue                                   # déjà compté via l'historique
        settled.add(key)
        tr.trades += 1
        tr.wins += 1 if won else 0
        tr.realized += p.get("cashPnl") or 0

    # Un pari GAGNÉ aujourd'hui mais acheté AVANT la fenêtre n'a pas d'achat observable :
    # il échouait au test de l'aller-retour et n'était jamais compté, alors que son
    # équivalent perdant l'était (il dort dans le portefeuille). D'où des « 0 % de
    # réussite » chez des traders parmi les plus rentables de la semaine. On rattrape
    # ces victoires par leurs remboursements, sans toucher au PnL (le coût est inconnu).
    for e in activity or []:
        if e.get("type") != "REDEEM" or (e.get("usdcSize") or 0) <= 1:
            continue
        key = f"{e.get('conditionId')}|{e.get('outcome')}"
        if key in settled:
            continue
        settled.add(key)
        tr.trades += 1
        tr.wins += 1

    tr.win_rate = (tr.wins / tr.trades) if tr.trades else None
    tr.days = None if oldest is math.inf else max(1, round((time.time() - oldest) / 86400))
    return tr


def wallet_quality(tr: TrackRecord | None, lb: dict | None) -> float:
    """Qualité dans [-1,1].

    L'argent vient du classement (PnL de la semaine, complet et fiable) ; le taux de
    réussite vient des paris résolus pendant la fenêtre observée. Utiliser le PnL
    reconstruit à la place donnait des résultats absurdes : son coût n'est connu que
    pour les allers-retours entièrement visibles, soit quelques heures d'historique
    chez les traders les plus actifs.
    """
    lb_pnl = (lb or {}).get("pnl")
    profit = None
    if lb_pnl is not None:
        sign = 1 if lb_pnl > 0 else (-1 if lb_pnl < 0 else 0)
        profit = clamp(sign * min(1, math.log10(1 + abs(lb_pnl)) / 6), -1, 1)
    elif tr and tr.trades >= 5:
        profit = math.tanh(tr.realized / 20000)

    if tr and tr.trades >= 5 and tr.win_rate is not None:
        skill = (tr.win_rate - 0.5) * 2
        conf = min(1, tr.trades / 15)
        if profit is None:
            return clamp(skill * conf, -1, 1)
        return clamp(profit * (1 - 0.4 * conf) + skill * 0.4 * conf, -1, 1)
    return clamp(profit, -1, 1) if profit is not None else 0.0


@dataclass
class Wallet:
    addr: str
    name: str
    positions: list
    portfolio: float
    open_pnl: float
    tr: TrackRecord | None
    lb: dict | None
    quality: float
    group: int = -1

    @property
    def last_buy(self) -> dict:
        return self.tr.last_buy if self.tr else {}

    def record_str(self) -> str:
        # Le PnL de la semaine vient du classement (fiable) ; le taux de réussite du
        # sous-ensemble de paris résolus pendant la fenêtre d'historique observable.
        if self.tr and self.tr.trades >= 5:
            base = f"{round(self.tr.win_rate*100)}% win rate over {self.tr.trades} settled bets"
            if self.lb and self.lb.get("pnl") is not None:
                pnl = self.lb["pnl"]
                return f"{'+' if pnl>=0 else '−'}{fmt_usd(abs(pnl))} this week · {base}"
            return base
        if self.lb:
            pnl = self.lb.get("pnl") or 0
            return f"{'+' if pnl>=0 else '−'}{fmt_usd(abs(pnl))} (leaderboard)"
        return "no track record available"


# Le palmarès et le pseudo d'un wallet bougent lentement ; ses POSITIONS changent à
# chaque trade. Recharger l'historique complet à chaque cycle doublait les requêtes
# pour rien — à 2 min d'intervalle, cela dépasserait 3 000 appels/heure sur une API
# publique. On garde donc l'historique en cache et on ne rafraîchit que les positions.
_SLOW_TTL = 1800          # 30 min : bien plus court que la vitesse d'évolution d'un palmarès
_slow_cache: dict[str, tuple[float, str, list | None]] = {}


async def load_wallet(c: Client, addr: str, lb: dict | None) -> Wallet:
    positions = await c.get(f"{API}/positions?user={addr}&limit=500&sizeThreshold=1")

    cached = _slow_cache.get(addr.lower())
    if cached and time.time() - cached[0] < _SLOW_TTL:
        name, activity = cached[1], cached[2]
    else:
        name = (lb or {}).get("userName") or ""
        if not name or re.match(r"^0x[0-9a-fA-F]{40}", name):
            prof = await c.get_or_none(f"{GAMMA}/public-profile?address={addr}")
            name = ""
            if prof:
                n = prof.get("name") or ""
                name = n if n and not re.match(r"^0x[0-9a-fA-F]{40}", n) else (prof.get("pseudonym") or "")
        if not name:
            name = addr[:6] + "…" + addr[-4:]
        activity = await c.get_or_none(f"{API}/activity?user={addr}&limit={ACTIVITY_LIMIT}")
        _slow_cache[addr.lower()] = (time.time(), name, activity)

    # Le palmarès est recalculé à chaque fois : les positions perdantes, elles, viennent
    # des données fraîches (c'est ce qui décide gagné/perdu).
    tr = track_record(activity, positions) if isinstance(activity, list) else None
    return Wallet(
        addr=addr, name=name, positions=positions,
        portfolio=sum(p.get("currentValue") or 0 for p in positions),
        open_pnl=sum(p.get("cashPnl") or 0 for p in positions),
        tr=tr, lb=lb, quality=wallet_quality(tr, lb),
    )


def detect_twins(ws: list[Wallet]) -> None:
    """Deux portefeuilles quasi identiques = probablement la même personne : une seule voix."""
    sets = [{f"{p.get('conditionId')}|{p.get('outcome')}" for p in w.positions} for w in ws]
    for i, w in enumerate(ws):
        w.group = i
    for i in range(len(ws)):
        for j in range(i + 1, len(ws)):
            a, b = sets[i], sets[j]
            if len(a) < 5 or len(b) < 5:
                continue
            inter = len(a & b)
            if inter / (len(a) + len(b) - inter) >= 0.6:
                ws[j].group = ws[i].group


# ---------------------------------------------------------------------------
# Modèle
# ---------------------------------------------------------------------------
def side_signal(holders: list[dict], price: float | None):
    per_group: dict = {}
    for h in holders:
        g = h["wallet"].group
        per_group[g] = per_group.get(g, 0) + 1
    s = 0.0
    for h in holders:
        w = h["wallet"]
        # conviction = part du portefeuille engagée (10 % = maximum).
        conviction = (min(1, (h["value"] / w.portfolio) / 0.10) if w.portfolio > 0
                      else min(1, math.sqrt(max(0, h["value"])) / 100))
        timing = (clamp((price - h["avg"]) / 0.3, -0.5, 0.5) + 1
                  if price is not None and h["avg"] > 0 else 1)
        s += w.quality * conviction * timing / per_group[w.group]
    return s, any(n > 1 for n in per_group.values())


def compute_my_proba(m: dict):
    """
    Le désaccord DÉTRUIT de l'information, il ne l'inverse pas : on ramène le signal
    vers zéro à mesure que l'argent suivi se répartit des deux côtés.
    L'ajustement est plafonné par l'incertitude du marché (12 pts à 50¢, ~3 pts à 7¢).
    """
    pro_s, has_twins = side_signal(m["holders"], m.get("price"))
    my_val = sum(h["value"] for h in m["holders"])
    opp_val = sum(o["value"] for o in m.get("others", []))
    agreement = my_val / (my_val + opp_val) if (my_val + opp_val) > 0 else 1
    confidence = max(0.0, 2 * agreement - 1)
    signal = math.tanh(pro_s) * confidence
    p = m.get("price")
    p = 0.5 if p is None else p
    room = 4 * p * (1 - p)
    my_proba = clamp(p + 0.12 * signal * room, 0.01, 0.99)
    return my_proba, my_proba - p, has_twins


def compute_overlaps(ws: list[Wallet]) -> list[dict]:
    markets: dict[str, dict] = {}
    for w in ws:
        for p in w.positions:
            if not p.get("conditionId") or (p.get("currentValue") or 0) < 1:
                continue
            key = f"{p['conditionId']}|{p.get('outcome')}"
            m = markets.setdefault(key, {
                "key": key, "conditionId": p["conditionId"], "title": p.get("title"),
                "outcome": p.get("outcome"), "slug": p.get("slug"),
                "eventSlug": p.get("eventSlug"), "endDate": p.get("endDate"),
                "price": p.get("curPrice"), "holders": [],
            })
            m["holders"].append({
                "wallet": w, "shares": p.get("size") or 0, "avg": p.get("avgPrice") or 0,
                "value": p.get("currentValue") or 0,
                "last_buy": w.last_buy.get(key, 0),
            })

    by_cid: dict[str, list] = {}
    for m in markets.values():
        by_cid.setdefault(m["conditionId"], []).append(m)
    for m in markets.values():
        m["others"] = [
            {"outcome": o["outcome"], "n": len(o["holders"]),
             "value": sum(h["value"] for h in o["holders"]), "holders": o["holders"]}
            for o in by_cid[m["conditionId"]] if o["outcome"] != m["outcome"]
        ]
    return [m for m in markets.values() if len(m["holders"]) >= 2]


def metrics(m: dict) -> dict:
    hs = m["holders"]
    total_value = sum(h["value"] for h in hs)
    total_shares = sum(h["shares"] for h in hs)
    price = m.get("price")
    m["totalValue"] = total_value
    m["potentialGain"] = total_shares * (1 - price) if price is not None else 0
    m["avgEntry"] = (sum(h["avg"] * h["value"] for h in hs) / total_value) if total_value else 0
    m["freshest"] = max((h["last_buy"] or 0) for h in hs) if hs else 0
    m["myProba"], m["edge"], m["hasTwins"] = compute_my_proba(m)
    ev = ((m["myProba"] / price - 1) * min(1, total_value / 5000)
          if price and 0.01 < price < 0.99 else -1)
    d = days_left(m.get("endDate"))
    m["ev"] = ev
    m["evTime"] = ev / max(0.5, (d if d and d > 0 else 90) / 30)
    m["daysLeft"] = d
    # Un désaccord signalé dès 1 $ en face se déclenchait presque toujours et ne voulait
    # plus rien dire. On exige que le camp adverse pèse vraiment : au moins 10 % de
    # l'argent de ce côté (et plus que de la poussière).
    m["contested"] = any(o["value"] >= max(100, 0.10 * (m.get("totalValue") or 0))
                         for o in m.get("others", []))
    return m


def verdict(m: dict) -> tuple[str, str, int]:
    """(classe, libellé, écart en points) — mêmes seuils que le site."""
    pts = round(m["edge"] * 100)
    price = m.get("price")
    if price is None or price <= 0.01 or price >= 0.99:
        return "hold", "WATCH", pts
    if pts >= 2:
        return "buy", f"BUY \u201c{outcome_label(m['title'], m['outcome'])}\u201d @ {round(price*100)}\u00a2", pts
    if pts <= -2:
        return "avoid", "AVOID", pts
    return "hold", "WATCH", pts


def market_url(m: dict) -> str:
    ev, sl = m.get("eventSlug"), m.get("slug")
    if ev and sl and ev != sl:
        return f"https://polymarket.com/event/{ev}/{sl}"
    return f"https://polymarket.com/event/{ev or sl}" if (ev or sl) else "https://polymarket.com"


# ---------------------------------------------------------------------------
# Pipeline complet
# ---------------------------------------------------------------------------
async def leaderboard(c: Client, period: str = "week", limit: int = 50) -> list[dict]:
    return await c.get(f"{API}/v1/leaderboard?limit={limit}&timePeriod={period}")


async def analyze(addresses: list[str], lb_by_addr: dict | None = None,
                  concurrency: int = 4) -> tuple[list[Wallet], list[dict]]:
    lb_by_addr = lb_by_addr or {}
    async with aiohttp.ClientSession() as s:
        c = Client(s)
        sem = asyncio.Semaphore(concurrency)

        async def one(a):
            async with sem:
                try:
                    return await load_wallet(c, a, lb_by_addr.get(a.lower()))
                except Exception:
                    return None

        ws = [w for w in await asyncio.gather(*(one(a) for a in addresses)) if w]

    detect_twins(ws)
    markets = [metrics(m) for m in compute_overlaps(ws)]
    markets.sort(key=lambda m: (m["evTime"], m["totalValue"]), reverse=True)
    return ws, markets


async def analyze_preset(period: str = "week", limit: int = 50):
    async with aiohttp.ClientSession() as s:
        lb = await leaderboard(Client(s), period, limit)
    by_addr = {u["proxyWallet"].lower(): u for u in lb}
    return await analyze([u["proxyWallet"] for u in lb], by_addr)


# ---------------------------------------------------------------------------
# Test manuel : python3 overlap.py
# ---------------------------------------------------------------------------
async def _demo():
    t0 = time.time()
    ws, markets = await analyze_preset("week", 25)
    buys = [m for m in markets if verdict(m)[0] == "buy"]
    print(f"{len(ws)} wallets · {len(markets)} marchés en overlap · "
          f"{len(buys)} ACHETER · {time.time()-t0:.1f}s\n")
    for m in buys[:5]:
        cls, label, pts = verdict(m)
        opp = sum(o["n"] for o in m["others"])
        print(f"  {label}")
        print(f"    {m['title'][:66]}")
        print(f"    proba {round(m['myProba']*100)}% vs marché {round(m['price']*100)}% "
              f"({pts:+d}) · {len(m['holders'])} traders · {fmt_usd(m['totalValue'])}"
              + (f" · ⚔️ {opp} en face" if opp else "")
              + (f" · résout dans {m['daysLeft']}j" if m['daysLeft'] is not None else ""))
        print(f"    {market_url(m)}\n")


if __name__ == "__main__":
    asyncio.run(_demo())
