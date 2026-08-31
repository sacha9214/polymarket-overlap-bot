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
    mm_score: int = 0          # 0 à 4 — voir market_maker_score()
    mm_reasons: list = field(default_factory=list)
    # Part de son capital par secteur. Mesure : 35 wallets sur 46 sont
    # mono-secteur a plus de 80 %, souvent a 100 %. Un specialiste macro qui
    # apparait dans un match de foot n y exprime aucune competence.
    sectors: dict = field(default_factory=dict)

    def exposure(self, theme: str) -> float:
        """Part du capital de ce wallet engagee dans ce secteur, dans [0,1]."""
        return self.sectors.get(theme, 0.0)

    @property
    def is_market_maker(self) -> bool:
        return self.mm_score >= MM_THRESHOLD

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
    mm_score, mm_reasons = market_maker_score(
        positions, activity if isinstance(activity, list) else None
    )
    by_sector: dict = {}
    for p in positions or []:
        by_sector[theme_of(p.get("title"), p.get("eventSlug"))] = (
            by_sector.get(theme_of(p.get("title"), p.get("eventSlug")), 0.0)
            + (p.get("initialValue") or 0)
        )
    tot_sec = sum(by_sector.values()) or 1.0
    sectors = {k: v / tot_sec for k, v in by_sector.items()}
    return Wallet(
        addr=addr, name=name, positions=positions,
        portfolio=sum(p.get("currentValue") or 0 for p in positions),
        open_pnl=sum(p.get("cashPnl") or 0 for p in positions),
        tr=tr, lb=lb, quality=wallet_quality(tr, lb),
        mm_score=mm_score, mm_reasons=mm_reasons, sectors=sectors,
    )


# ---------------------------------------------------------------------------
# Détection des market makers
# ---------------------------------------------------------------------------
# Un market maker n'a pas d'avis : il cote des deux côtés et encaisse le spread.
# Ses positions sont un STOCK, pas une conviction. Les traiter comme un signal
# revient à croire qu'il « parie » alors qu'il absorbe simplement le flux des
# autres — et comme il détient des centaines de positions, il pèse lourd dans
# le calcul d'overlap tout en n'exprimant rien.
#
# Étalonné sur RN1 (4e all-time, 12,8 M$ de gains) : 773 trades/heure, taille
# médiane de trade 17 $, 52 marchés touchés en 36 minutes, plus grosse position
# à 1,5 % du capital, et les deux côtés tenus sur 38 événements.

MM_THRESHOLD = 3                # nombre de critères à cocher pour être signalé
MM_MAX_TRADE_USD = 100.0        # fills minuscules alors que les positions sont grosses
MM_MIN_MARKETS_PER_HOUR = 10.0  # il quote partout à la fois
MM_MAX_TOP_SHARE = 0.05         # aucune position ne dépasse 5 % du capital
MM_MIN_BOTH_SIDES = 3           # tient les deux côtés d'un même événement


def market_maker_score(positions: list, activity: list | None) -> tuple[int, list[str]]:
    """Combien de signatures de market making ce portefeuille présente-t-il ?

    Chaque critère est mesurable sans rien deviner. Un trader directionnel n'en
    coche normalement aucun ; RN1 les coche tous les quatre.
    """
    reasons: list[str] = []
    positions = positions or []
    if len(positions) < 20:
        return 0, reasons        # trop peu de matière pour conclure

    # 1. Des fills minuscules pour des positions grosses = exécution algorithmique.
    if activity:
        sizes = sorted(a.get("usdcSize") or 0 for a in activity)
        if sizes:
            med_trade = sizes[len(sizes) // 2]
            med_pos = sorted((p.get("initialValue") or 0) for p in positions)[len(positions) // 2]
            if med_trade < MM_MAX_TRADE_USD and med_pos > med_trade * 20:
                reasons.append(f"fills de {med_trade:.0f} $ pour des positions de {med_pos:,.0f} $")

    # 2. Beaucoup de marchés distincts par heure = il cote, il ne choisit pas.
    if activity and len(activity) > 50:
        ts = [a.get("timestamp") or 0 for a in activity]
        span_h = (max(ts) - min(ts)) / 3600 if max(ts) > min(ts) else 0
        if span_h > 0.05:
            n_markets = len({a.get("eventSlug") for a in activity})
            per_hour = n_markets / span_h
            if per_hour >= MM_MIN_MARKETS_PER_HOUR:
                reasons.append(f"{per_hour:.0f} marchés touchés par heure")

    # 3. Aucune conviction : la plus grosse position reste marginale.
    vals = sorted(((p.get("initialValue") or 0) for p in positions), reverse=True)
    total = sum(vals)
    if total > 0 and vals[0] / total <= MM_MAX_TOP_SHARE:
        reasons.append(f"plus grosse position à {100*vals[0]/total:.1f} % du capital")

    # 4. Les deux côtés du même événement : impossible pour qui a un avis.
    by_event: dict = {}
    for p in positions:
        by_event.setdefault(p.get("eventSlug"), set()).add(p.get("outcome"))
    both = sum(1 for o in by_event.values()
               if {"Over", "Under"} <= o or {"Yes", "No"} <= o)
    if both >= MM_MIN_BOTH_SIDES:
        reasons.append(f"deux côtés tenus sur {both} événements")

    return len(reasons), reasons


async def load_one(addr_or_profile: str) -> Wallet | None:
    """Charge un seul trader, en dehors de tout palmarès.

    Le pipeline habituel ne connaît que le top 50 : suivre quelqu'un dans son
    propre salon ne doit pas l'obliger à entrer dans l'analyse d'overlap.
    """
    u = await resolve_profile(addr_or_profile)
    if not u:
        return None
    addr = u["proxyWallet"]
    async with aiohttp.ClientSession() as s:
        return await load_wallet(Client(s), addr, u)


def style_summary(w: Wallet) -> dict:
    """Chiffres qui décrivent COMMENT il trade, pas ce qu'il gagne."""
    pos = w.positions or []
    vals = sorted(((p.get("initialValue") or 0) for p in pos), reverse=True)
    total = sum(vals) or 1.0
    prices = sorted((p.get("avgPrice") or 0) for p in pos if p.get("avgPrice"))
    themes: dict = {}
    for p in pos:
        k = theme_of(p.get("title"), p.get("eventSlug"))
        themes[k] = themes.get(k, 0) + (p.get("initialValue") or 0)
    return {
        "n": len(pos),
        "capital": total,
        "top_share": (vals[0] / total) if vals else 0.0,
        "median_entry": prices[len(prices) // 2] if prices else 0.0,
        "themes": sorted(themes.items(), key=lambda x: -x[1])[:3],
    }


# Secteurs. Un wallet peut etre excellent sur le foot et desastreux sur la
# geopolitique : une note globale melangerait les deux et masquerait les deux.
THEMES = (
    # Les slugs de ligue (mls-, epl-, nba-…) sont le signal le plus fiable :
    # un titre peut etre ambigu, un slug de match ne l'est jamais.
    ("sport", (" vs ", "vs.", "vs-", "nba", "nfl", "soccer", "football", "tennis",
               "ufc", "mlb", "nhl", "match", "cup", "league", "premier", "liga",
               "serie a", "bundesliga", "ligue 1", "o/u", "esports", "cs2",
               "dota", "lol:", "mls-", "epl-", "bl1-", "bl2-", "el1-", "clf-",
               "kbo:", "npb", "atp", "wta", "f1", "golf", "masters", "open")),
    ("crypto", ("bitcoin", "ethereum", "btc", "eth", "solana", "crypto", "xrp",
                "dogecoin", "token")),
    ("politics", ("election", "president", "senate", "trump", "governor",
                  "primary", "congress", "parliament", "vote", "poll", "cabinet")),
    ("world", ("ukraine", "russia", "israel", "gaza", "china", "nato", "war",
               "ceasefire", "iran", "hamas", "strike", "invade")),
    ("macro", ("fed", "inflation", "gdp", "rate", "recession", "cpi", "unemployment")),
    ("weather", ("temperature", "hurricane", "rain", "snow", "storm", "weather")),
    ("culture", ("netflix", "oscar", "grammy", "box office", "movie", "album",
                 "spotify", "rotten tomatoes")),
)


# Motifs propres aux paris sportifs, que les mots-cles seuls ratent :
# « Will Tottenham win on 2026-08-22? », « Spread: Indiana Fever (-3.5) ».
# Mesure sur 3 547 titres reels : ils representaient a eux seuls la moitie
# des marches classes "other" a tort.
_SPORT_PATTERNS = re.compile(
    r"win on \d{4}-\d{2}-\d{2}"          # « win on 2026-08-20 »
    r"|^spread:"                            # « Spread: LV (-1.5) »
    r"|\bfc\b|\bsc\b|\bcf\b|\bac\b"  # clubs
    r"|ballon d'or|world cup|champions",
    re.I,
)


def theme_of(title: str, event_slug: str = "") -> str:
    """Secteur d'un marche, devine depuis son titre et son slug."""
    t = f"{title or ''} {event_slug or ''}".lower()
    for name, words in THEMES:
        if any(w in t for w in words):
            return name
    if _SPORT_PATTERNS.search(t):
        return "sport"
    return "other"


# Un overlap ne vaut pas la meme chose partout. Mesure sur les portefeuilles
# reels : 34 wallets jouent au sport et 20,1 % des marches sportifs ont un
# overlap ; en macro ils ne sont que 13 mais 25,4 % des marches en ont un —
# ils se tassent tous sur la meme decision de la Fed. Comparer a un absolu
# serait donc faux : on compare a la moyenne DU SECTEUR.
def sector_baselines(ws: list["Wallet"]) -> dict:
    """Nombre moyen de wallets suivis par marche, secteur par secteur."""
    counts: dict = {}
    for w in ws:
        for p in w.positions or []:
            if (p.get("currentValue") or 0) < 1:
                continue
            th = theme_of(p.get("title"), p.get("eventSlug"))
            key = f"{p.get('conditionId')}|{p.get('outcome')}"
            counts.setdefault(th, {}).setdefault(key, set()).add(w.group)
    out = {}
    for th, markets in counts.items():
        if len(markets) >= 5:          # sous 5 marches, la moyenne ne veut rien dire
            out[th] = sum(len(v) for v in markets.values()) / len(markets)
    return out


def surprise_factor(n_groups: int, theme: str | None, baselines: dict) -> float:
    """A quel point cet overlap depasse la normale de son secteur.

    Borne entre 0.6 et 2.0 : cela module le classement sans jamais l ecraser,
    et un secteur mal echantillonne ne peut pas produire un facteur delirant.
    """
    if not SECTOR_ADJUST:
        return 1.0
    base = baselines.get(theme or "")
    if not base or base <= 0:
        return 1.0
    return clamp(n_groups / base, 0.6, 2.0)


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
# Un specialiste totalement absent d un secteur ne doit pas y peser, mais on ne
# l annule pas non plus : sa presence exceptionnelle hors de son terrain reste
# une information. Plancher a 0.25.
SECTOR_FLOOR = 0.25
# Au-dela, une exposition de 40 % au secteur vaut deja pleine confiance : exiger
# 100 % ecraserait les rares wallets reellement polyvalents.
SECTOR_FULL = 0.40
# Interrupteur : a False, aucune des deux corrections sectorielles ne
# s applique et le comportement revient exactement a celui d avant.
SECTOR_ADJUST = True


def sector_weight(w: "Wallet", theme: str | None) -> float:
    """Poids d un wallet sur un secteur donne, dans [SECTOR_FLOOR, 1]."""
    if not theme or not SECTOR_ADJUST:
        return 1.0
    e = w.exposure(theme)
    return SECTOR_FLOOR + (1 - SECTOR_FLOOR) * min(1.0, e / SECTOR_FULL)


def side_signal(holders: list[dict], price: float | None, theme: str | None = None):
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
        s += (w.quality * conviction * timing * sector_weight(w, theme)
              / per_group[w.group])
    return s, any(n > 1 for n in per_group.values())


def compute_my_proba(m: dict):
    """
    Le désaccord DÉTRUIT de l'information, il ne l'inverse pas : on ramène le signal
    vers zéro à mesure que l'argent suivi se répartit des deux côtés.
    L'ajustement est plafonné par l'incertitude du marché (12 pts à 50¢, ~3 pts à 7¢).
    """
    theme = theme_of(m.get("title"), m.get("eventSlug"))
    pro_s, has_twins = side_signal(m["holders"], m.get("price"), theme)
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


def metrics(m: dict, baselines: dict | None = None) -> dict:
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
    # La surprise module le CLASSEMENT, pas la probabilite : elle dit qu un
    # overlap est inhabituel pour ce secteur, pas que l issue est plus probable.
    m["theme"] = theme_of(m.get("title"), m.get("eventSlug"))
    groups = len({h["wallet"].group for h in hs})
    m["surprise"] = surprise_factor(groups, m["theme"], baselines or {})
    m["evTime"] = ev / max(0.5, (d if d and d > 0 else 90) / 30) * m["surprise"]
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
                  concurrency: int = 4, exclude_mm: bool = False) -> tuple[list[Wallet], list[dict]]:
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
    # `exclude_mm` retire les market makers du CALCUL d'overlap. Ils restent dans
    # `ws` pour rester consultables via /wallet : on les écarte du signal, on ne
    # les efface pas.
    pool = [w for w in ws if not w.is_market_maker] if exclude_mm else ws
    baselines = sector_baselines(pool)
    markets = [metrics(m, baselines) for m in compute_overlaps(pool)]
    markets.sort(key=lambda m: (m["evTime"], m["totalValue"]), reverse=True)
    return ws, markets


async def resolve_profile(text: str) -> dict | None:
    """Retrouve un trader depuis une URL, un pseudo ou une adresse.

    La recherche publique de Gamma ne renvoie rien sur les pseudos de traders
    (testé : `public-search?q=rn1` → 0 résultat). En revanche les classements
    exposent `userName`, donc on y cherche — d'abord la semaine, puis l'historique
    complet, ce qui couvre aussi les gros traders absents du palmarès courant.
    """
    t = (text or "").strip()
    t = re.sub(r"^https?://(www\.)?polymarket\.com/", "", t, flags=re.I)
    t = t.lstrip("@/").split("/")[0].split("?")[0].strip()
    if not t:
        return None

    if re.fullmatch(r"0x[0-9a-fA-F]{40}", t):
        addr = t.lower()
        async with aiohttp.ClientSession() as s:
            c = Client(s)
            for period in ("week", "all"):
                for u in (await leaderboard(c, period, 50)) or []:
                    if (u.get("proxyWallet") or "").lower() == addr:
                        return u
        return {"proxyWallet": addr, "userName": ""}

    async with aiohttp.ClientSession() as s:
        c = Client(s)
        for period in ("week", "all"):
            for u in (await leaderboard(c, period, 50)) or []:
                if (u.get("userName") or "").lower() == t.lower():
                    return u
    return None


async def analyze_preset(period: str = "week", limit: int = 50,
                         pinned: list[str] | None = None,
                         exclude_mm: bool = False):
    """Palmarès de la période, plus d'éventuels wallets suivis en permanence."""
    pinned = [a.lower() for a in (pinned or []) if a]
    async with aiohttp.ClientSession() as s:
        c = Client(s)
        lb = await leaderboard(c, period, limit) or []
        by_addr = {u["proxyWallet"].lower(): u for u in lb}

        missing = [a for a in pinned if a not in by_addr]
        if missing:
            # Un épinglé absent du palmarès de la période n'a aucune donnée de
            # gains, et `wallet_quality` le noterait alors au plus bas — ce qui
            # le ferait disparaître du classement alors qu'on l'a justement
            # demandé. On va chercher sa fiche dans le classement historique.
            for u in (await leaderboard(c, "all", 50)) or []:
                a = (u.get("proxyWallet") or "").lower()
                if a in missing:
                    by_addr[a] = u

    addrs = [u["proxyWallet"] for u in lb]
    known = {a.lower() for a in addrs}
    addrs += [a for a in pinned if a not in known]
    return await analyze(addrs, by_addr, exclude_mm=exclude_mm)


# ---------------------------------------------------------------------------
# Test manuel : python3 overlap.py
# ---------------------------------------------------------------------------
async def _demo():
    t0 = time.time()
    ws, markets = await analyze_preset("week", 25)
    buys = [m for m in markets if verdict(m)[0] == "buy"]
    print(f"{len(ws)} wallets · {len(markets)} overlap markets · "
          f"{len(buys)} BUY · {time.time()-t0:.1f}s\n")
    for m in buys[:5]:
        cls, label, pts = verdict(m)
        opp = sum(o["n"] for o in m["others"])
        print(f"  {label}")
        print(f"    {m['title'][:66]}")
        print(f"    est. {round(m['myProba']*100)}% vs market {round(m['price']*100)}% "
              f"({pts:+d}) · {len(m['holders'])} traders · {fmt_usd(m['totalValue'])}"
              + (f" · ⚔️ {opp} against" if opp else "")
              + (f" · resolves in {m['daysLeft']}d" if m['daysLeft'] is not None else ""))
        print(f"    {market_url(m)}\n")


if __name__ == "__main__":
    asyncio.run(_demo())
