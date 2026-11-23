#!/usr/bin/env python3
"""Bot de jogos cooperativos de PC para Discord (feito para rodar no GitHub Actions).

Uso:
    python coopbot.py daily|epic|promo|lancamentos|gratis|votacao|all [--dry-run] [--force]

Webhooks vêm de variáveis de ambiente (GitHub Secrets):
    WEBHOOK_GRATIS, WEBHOOK_PROMO, WEBHOOK_LANCAMENTOS, WEBHOOK_VOTACAO, WEBHOOK_LOG
Opcional (reações numeradas na votação, webhooks não conseguem reagir):
    DISCORD_BOT_TOKEN
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).parent
STEAM_STORE = "https://store.steampowered.com"
CHEAPSHARK = "https://www.cheapshark.com/api/1.0"
EPIC_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"

# Categorias da Steam (IDs são estáveis; os nomes variam conforme o idioma).
CAT_COOP = {9, 38, 39}  # Co-op, Online Co-op, Shared/Split Screen Co-op
CAT_ONLINE_COOP = 38
CAT_CROSSPLAY = 27  # Cross-Platform Multiplayer
CAT_REMOTE = {44}  # Remote Play Together
CAT_LOCAL = {39}  # Shared/Split Screen Co-op

TAG_ONLINE = "🤝 Co-op online"
TAG_CROSS = "🔀 Crossplay"
TAG_LOCAL = "🛋️ Local/Remote Play Together"

# Escala de avaliações da Steam, da melhor para a pior: (nome em inglês, nome em pt-BR).
RATING_LEVELS = [
    ("Overwhelmingly Positive", "Extremamente positivas"),
    ("Very Positive", "Muito positivas"),
    ("Positive", "Positivas"),
    ("Mostly Positive", "Majoritariamente positivas"),
    ("Mixed", "Mistas"),
    ("Mostly Negative", "Majoritariamente negativas"),
    ("Negative", "Negativas"),
]
RATING_ALIASES = {
    "ligeiramente positivas": "Mostly Positive",  # nome usado no pedido original
    "ligeiramente positiva": "Mostly Positive",
}
for _en, _pt in RATING_LEVELS:
    RATING_ALIASES[_en.lower()] = _en
    RATING_ALIASES[_pt.lower()] = _en

DEFAULT_CONFIG = {
    "filtros": {
        "jogadores_min": 4,
        "jogadores_desconhecido": "aceitar",  # aceitar | rejeitar
        "jogadores_override": {},
        "avaliacao_minima": "Mostly Positive",
    },
    "promocoes": {
        "desconto_min": 50,
        "max_por_dia": 8,
        "max_avaliar": 60,
        "repetir_apos_dias": 30,
    },
    "lancamentos": {
        "dias_passados": 14,
        "dias_futuros": 21,
        "max_por_execucao": 6,
        "permitir_sem_avaliacao": False,
    },
    "votacao": {"opcoes": 5, "minimo": 3, "repetir_apos_dias": 60},
    "epic": {"pais": "BR", "idioma": "pt-BR"},
    "estado": {"retencao_dias": 365},
}


# --------------------------------------------------------------------------- util
def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path) -> dict:
    user = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = deep_merge(DEFAULT_CONFIG, user or {})
    cfg["filtros"]["avaliacao_minima"] = normalize_rating(cfg["filtros"]["avaliacao_minima"])
    return cfg


def normalize_rating(name: str) -> str:
    key = str(name).strip().lower()
    if key not in RATING_ALIASES:
        raise SystemExit(f"avaliacao_minima inválida: {name!r}. Use um de: "
                         + ", ".join(f"{en} / {pt}" for en, pt in RATING_LEVELS))
    return RATING_ALIASES[key]


def rating_index(label: str) -> int:
    return [en for en, _ in RATING_LEVELS].index(label)


def rating_pt(label: str) -> str:
    return dict(RATING_LEVELS)[label]


def rating_label(pct: float, count: int) -> str | None:
    """Aproxima as regras da Steam. Menos de 10 avaliações = sem nota."""
    if count < 10:
        return None
    if pct >= 95 and count >= 500:
        return "Overwhelmingly Positive"
    if pct >= 80 and count >= 50:
        return "Very Positive"
    if pct >= 80:
        return "Positive"
    if pct >= 70:
        return "Mostly Positive"
    if pct >= 40:
        return "Mixed"
    if pct >= 20:
        return "Mostly Negative"
    return "Negative"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def clip(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "coop-discord-bot/1.0"
    retry = Retry(total=4, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


HTTP = make_session()
VERBOSE = False


def vlog(*a):
    if VERBOSE:
        print("   ·", *a, file=sys.stderr)


def get_json(url: str, params: dict | None = None, timeout: int = 30):
    r = HTTP.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- estado
class State:
    def __init__(self, path: Path, dry_run: bool):
        self.path, self.dry_run = path, dry_run
        self.data = {"posted": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
            self.data.setdefault("posted", {})

    def seen(self, kind: str, key: str, cooldown_days: int | None = None) -> bool:
        ts = self.data["posted"].get(kind, {}).get(str(key))
        if ts is None:
            return False
        if cooldown_days is None:
            return True
        return now_utc() - datetime.fromisoformat(ts) < timedelta(days=cooldown_days)

    def mark(self, kind: str, key: str) -> None:
        self.data["posted"].setdefault(kind, {})[str(key)] = now_utc().isoformat(timespec="seconds")

    def save(self, retention_days: int) -> None:
        if self.dry_run:
            return
        cutoff = now_utc() - timedelta(days=retention_days)
        for kind, items in self.data["posted"].items():
            self.data["posted"][kind] = {k: v for k, v in items.items()
                                         if datetime.fromisoformat(v) >= cutoff}
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")


# --------------------------------------------------------------------------- Discord
class Discord:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run

    def post(self, env_name: str, content: str = "", embeds: list[dict] | None = None,
             wait: bool = False) -> list[dict]:
        """Envia a mensagem (dividindo em lotes de embeds). Retorna as respostas (com wait=True)."""
        embeds = embeds or []
        url = os.environ.get(env_name, "").strip()
        batches = chunk_embeds(embeds) or [[]]
        results = []
        for i, batch in enumerate(batches):
            payload = {"content": content if i == 0 else "", "embeds": batch,
                       "allowed_mentions": {"parse": []}}
            if self.dry_run:
                print_payload(env_name, payload)
                results.append({"id": "dry", "channel_id": "dry"})
                continue
            if not url:
                print(f"[aviso] {env_name} não definido; mensagem ignorada.", file=sys.stderr)
                continue
            results.append(self._send(url, payload, wait))
            time.sleep(1)
        return results

    @staticmethod
    def _send(url: str, payload: dict, wait: bool) -> dict:
        for _ in range(5):
            r = HTTP.post(url, params={"wait": "true"} if wait else None, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
                continue
            r.raise_for_status()
            return r.json() if r.content else {}
        raise RuntimeError("Discord: rate limit persistente")

    def react(self, channel_id: str, message_id: str, count: int) -> bool:
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if self.dry_run:
            print(f"[dry-run] reações 1️⃣..{count} na mensagem (requer DISCORD_BOT_TOKEN)")
            return True
        if not token:
            return False
        for n in range(1, count + 1):
            emoji = quote(f"{n}️⃣")
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me"
            for _ in range(4):
                r = HTTP.put(url, headers={"Authorization": f"Bot {token}"}, timeout=30)
                if r.status_code == 429:
                    time.sleep(float(r.json().get("retry_after", 1)) + 0.3)
                    continue
                r.raise_for_status()
                break
            time.sleep(0.6)
        return True


def chunk_embeds(embeds: list[dict]) -> list[list[dict]]:
    """Discord: no máximo 10 embeds e ~6000 caracteres por mensagem."""
    batches, cur, size = [], [], 0
    for e in embeds:
        n = len(json.dumps(e, ensure_ascii=False))
        if cur and (len(cur) >= 10 or size + n > 5500):
            batches.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        batches.append(cur)
    return batches


def print_payload(target: str, payload: dict) -> None:
    print(f"\n━━━ [dry-run] → {target} " + "━" * 30)
    if payload["content"]:
        print(payload["content"])
    for e in payload["embeds"]:
        print(f"  ▸ {e.get('title')}")
        if e.get("url"):
            print(f"    url:    {e['url']}")
        if e.get("description"):
            print("    " + e["description"].replace("\n", "\n    "))
        for f in e.get("fields", []):
            print(f"    [{f['name']}] {f['value']}")
        if e.get("image"):
            print(f"    capa:   {e['image']['url']}")
        if e.get("footer"):
            print(f"    rodapé: {e['footer']['text']}")


def embed(title: str, url: str | None, description: str, color: int, image: str | None = None,
          fields: list[tuple[str, str, bool]] | None = None, footer: str | None = None,
          timestamp: str | None = None) -> dict:
    e: dict = {"title": clip(title, 250), "description": clip(description, 4000), "color": color}
    if url:
        e["url"] = url
    if image:
        e["image"] = {"url": image}
    if fields:
        e["fields"] = [{"name": clip(n, 250), "value": clip(v, 1000), "inline": inl} for n, v, inl in fields if v]
    if footer:
        e["footer"] = {"text": footer}
    if timestamp:
        e["timestamp"] = timestamp
    return e


# --------------------------------------------------------------------------- Steam
@dataclass
class Game:
    appid: int
    name: str
    url: str
    image: str
    categories: set[int]
    players: int | None
    rating: str | None = None
    rating_pct: float | None = None
    rating_count: int = 0
    price_initial: str = ""
    price_final: str = ""
    discount: int = 0
    is_free: bool = False
    release_text: str = ""
    release_date: datetime | None = None
    coming_soon: bool = False
    historic_low: bool = False
    short_desc: str = ""

    @property
    def tags(self) -> list[str]:
        t = []
        if CAT_ONLINE_COOP in self.categories:
            t.append(TAG_ONLINE)
        if CAT_CROSSPLAY in self.categories:
            t.append(TAG_CROSS)
        if self.categories & (CAT_LOCAL | CAT_REMOTE):
            t.append(TAG_LOCAL)
        return t

    @property
    def is_coop(self) -> bool:
        return bool(self.categories & CAT_COOP)

    @property
    def rating_text(self) -> str:
        if not self.rating:
            return "Sem avaliações suficientes"
        return f"{rating_pt(self.rating)} ({self.rating_pct:.0f}% · {fmt_int(self.rating_count)})"


_APP_CACHE: dict[int, dict | None] = {}
_REVIEW_CACHE: dict[int, tuple[float, int] | None] = {}
_last_steam_call = 0.0


def steam_throttle() -> None:
    global _last_steam_call
    wait = 0.4 - (time.monotonic() - _last_steam_call)
    if wait > 0:
        time.sleep(wait)
    _last_steam_call = time.monotonic()


def steam_appdetails(appid: int) -> dict | None:
    """Uma chamada por jogo (a API recusa vários IDs quando se pede preço). Idioma EN para as
    categorias não virem traduzidas; cc=br para o preço em reais."""
    if appid in _APP_CACHE:
        return _APP_CACHE[appid]
    steam_throttle()
    try:
        j = get_json(f"{STEAM_STORE}/api/appdetails", {"appids": appid, "cc": "br", "l": "english"})
        entry = j.get(str(appid), {})
        data = entry.get("data") if entry.get("success") else None
    except (requests.RequestException, ValueError) as exc:
        vlog(f"appdetails {appid} falhou: {exc}")
        data = None
    _APP_CACHE[appid] = data
    return data


def steam_reviews(appid: int) -> tuple[float, int] | None:
    """(% positivas, total). Tenta /appreviews; se a Steam redirecionar (acontece), usa o
    histograma, que é um pouco menos exato mas devolve JSON."""
    if appid in _REVIEW_CACHE:
        return _REVIEW_CACHE[appid]
    result = None
    steam_throttle()
    try:
        r = HTTP.get(f"{STEAM_STORE}/appreviews/{appid}",
                     params={"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0},
                     timeout=30, allow_redirects=False)
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            q = r.json().get("query_summary", {})
            total = int(q.get("total_reviews", 0))
            if total:
                result = (100 * q.get("total_positive", 0) / total, total)
            else:
                result = (0.0, 0)
        else:
            j = get_json(f"{STEAM_STORE}/appreviewhistogram/{appid}", {"l": "english"})
            rollups = j.get("results", {}).get("rollups", [])
            up = sum(x.get("recommendations_up", 0) for x in rollups)
            down = sum(x.get("recommendations_down", 0) for x in rollups)
            result = (100 * up / (up + down), up + down) if up + down else (0.0, 0)
    except (requests.RequestException, ValueError) as exc:
        vlog(f"avaliações {appid} falharam: {exc}")
    _REVIEW_CACHE[appid] = result
    return result


_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
          "ten": 10, "twelve": 12, "sixteen": 16}
_NUM = r"(\d{1,2}|" + "|".join(_WORDS) + r")"
_PLAYERS_RE = re.compile(
    rf"\b(?:{_NUM}\s*(?:-|–|to)\s*)?{_NUM}[\s-]*(?:players?|jogadores?|friends|amigos)\b", re.I)


def guess_max_players(data: dict) -> int | None:
    """A Steam não expõe o nº de jogadores; extrai da descrição (ex.: 'up to 4 players')."""
    text = " ".join(str(data.get(k, "")) for k in ("short_description", "about_the_game"))
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    best = None
    for m in _PLAYERS_RE.finditer(text):
        raw = m.group(2).lower()
        n = _WORDS.get(raw) or int(raw)
        if 2 <= n <= 16:
            best = max(best or 0, n)
    return best


def parse_steam_date(text: str) -> datetime | None:
    for fmt in ("%d %b, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def build_game(appid: int, data: dict, cfg: dict) -> Game:
    cats = {c["id"] for c in data.get("categories", [])}
    po = data.get("price_overview") or {}
    rel = data.get("release_date", {})
    players = cfg["filtros"]["jogadores_override"].get(appid) or guess_max_players(data)
    return Game(
        appid=appid, name=html.unescape(data.get("name", str(appid))),
        url=f"{STEAM_STORE}/app/{appid}/", image=data.get("header_image", ""),
        categories=cats, players=players,
        price_initial=po.get("initial_formatted") or po.get("final_formatted", ""),
        price_final=po.get("final_formatted", ""), discount=po.get("discount_percent", 0),
        is_free=bool(data.get("is_free")), release_text=rel.get("date", ""),
        release_date=parse_steam_date(rel.get("date", "")), coming_soon=bool(rel.get("coming_soon")),
        short_desc=html.unescape(data.get("short_description", "")),
    )


def attach_rating(g: Game) -> None:
    rv = steam_reviews(g.appid)
    if rv:
        g.rating_pct, g.rating_count = rv
        g.rating = rating_label(*rv)


def passes_filters(g: Game, cfg: dict, *, require_rating: bool, need_coop: bool = True) -> str | None:
    """Devolve o motivo da rejeição, ou None se o jogo passa."""
    f = cfg["filtros"]
    if need_coop and not g.is_coop:
        return "não é co-op"
    if g.players is None:
        if f["jogadores_desconhecido"] == "rejeitar":
            return "nº de jogadores desconhecido"
    elif g.players < f["jogadores_min"]:
        return f"só {g.players} jogadores"
    if g.rating is None:
        if require_rating:
            return "sem avaliações suficientes"
    elif rating_index(g.rating) > rating_index(f["avaliacao_minima"]):
        return f"avaliação baixa ({g.rating})"
    return None


def evaluate(appid: int, cfg: dict, *, require_rating: bool, need_coop: bool = True) -> Game | None:
    data = steam_appdetails(appid)
    if not data or data.get("type") != "game":
        return None
    if not data.get("platforms", {}).get("windows", True):
        return None
    g = build_game(appid, data, cfg)
    if need_coop and not g.is_coop:
        vlog(f"{g.name}: não é co-op")
        return None
    attach_rating(g)
    why = passes_filters(g, cfg, require_rating=require_rating, need_coop=need_coop)
    if why:
        vlog(f"{g.name}: descartado — {why}")
        return None
    return g


@dataclass
class Row:
    appid: int
    title: str
    released: str
    discount: int
    price_final: int  # centavos


def steam_search(count: int = 50, start: int = 0, **extra) -> list[Row]:
    steam_throttle()
    params = {"category1": 998, "cc": "br", "l": "english", "count": count, "start": start,
              "infinite": 1, **extra}
    page = get_json(f"{STEAM_STORE}/search/results/", params)["results_html"]
    rows = []
    for chunk in re.split(r'(?=<a href="https://store\.steampowered\.com/app/)', page)[1:]:
        m = re.search(r'data-ds-appid="(\d+)"', chunk)
        if not m:
            continue
        title = re.search(r'<span class="title">(.*?)</span>', chunk, re.S)
        rel = re.search(r'search_released[^>]*>\s*(.*?)\s*<', chunk, re.S)
        disc = re.search(r'data-discount="(-?\d+)"', chunk)
        price = re.search(r'data-price-final="(\d+)"', chunk)
        rows.append(Row(int(m.group(1)), html.unescape(title.group(1)) if title else "",
                        rel.group(1) if rel else "", abs(int(disc.group(1))) if disc else 0,
                        int(price.group(1)) if price else 0))
    return rows


# --------------------------------------------------------------------------- CheapShark
def is_historic_low(appid: int) -> bool:
    """Compara o preço atual da Steam com o menor preço já registrado no CheapShark (ambos em USD)."""
    try:
        deals = get_json(f"{CHEAPSHARK}/deals", {"storeID": 1, "steamAppID": appid})
        deal = next((d for d in deals if str(d.get("steamAppID")) == str(appid)), None)
        if not deal:
            return False
        info = get_json(f"{CHEAPSHARK}/games", {"id": deal["gameID"]})
        low = float(info["cheapestPriceEver"]["price"])
        time.sleep(0.3)
        return float(deal["salePrice"]) <= low + 0.005
    except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
        vlog(f"CheapShark {appid} falhou: {exc}")
        return False


# --------------------------------------------------------------------------- embeds de jogos
def game_embed(g: Game, *, color: int, prefix: str = "", extra_fields=None, footer: str = "Steam") -> dict:
    if g.discount and g.price_initial:
        price = f"~~{g.price_initial}~~ → **{g.price_final}** (-{g.discount}%)"
    elif g.price_final:
        price = f"**{g.price_final}**"
    else:
        price = "**Free to Play**" if g.is_free else ("Preço ainda não divulgado" if g.coming_soon
                                                     else "Preço indisponível")
    if g.historic_low:
        price += "\n🔥 **Menor preço histórico!**"
    fields = [("Tags", " · ".join(g.tags) or "—", False),
              ("Avaliação", g.rating_text, True),
              ("Jogadores", f"até {g.players}" if g.players else "não informado", True)]
    fields += extra_fields or []
    title = f"{'🔥 ' if g.historic_low else ''}{prefix}{g.name}"
    return embed(title, g.url, price, color, g.image, fields, footer)


# --------------------------------------------------------------------------- jobs
def job_epic(cfg, state: State, dc: Discord, force: bool) -> None:
    now = now_utc()
    if not force and not (now.weekday() == 3 and now.hour >= 16):
        print("Epic: fora da janela (quinta ≥16h UTC / 13h Brasília). Use --force para ignorar.")
        return
    j = get_json(EPIC_URL, {"locale": cfg["epic"]["idioma"], "country": cfg["epic"]["pais"],
                            "allowCountries": cfg["epic"]["pais"]})
    elements = j["data"]["Catalog"]["searchStore"]["elements"]
    embeds, keys = [], []
    for e in elements:
        offers = [o for grp in ((e.get("promotions") or {}).get("promotionalOffers") or [])
                  for o in grp["promotionalOffers"]]
        free = [o for o in offers if o["discountSetting"]["discountPercentage"] == 0
                and datetime.fromisoformat(o["startDate"].replace("Z", "+00:00")) <= now
                < datetime.fromisoformat(o["endDate"].replace("Z", "+00:00"))]
        total = e["price"]["totalPrice"]
        if not free or total["originalPrice"] == 0:
            continue
        end = datetime.fromisoformat(free[0]["endDate"].replace("Z", "+00:00"))
        key = f"{e['id']}:{free[0]['endDate']}"
        if state.seen("epic", key):
            vlog(f"Epic: {e['title']} já postado")
            continue
        slug = next((m["pageSlug"] for m in (e.get("offerMappings") or []) + (e.get("catalogNs", {}).get("mappings") or [])
                     if m.get("pageSlug")), e.get("productSlug") or e.get("urlSlug"))
        url = f"https://store.epicgames.com/{cfg['epic']['idioma']}/p/{slug}" if slug else "https://store.epicgames.com/pt-BR/free-games"
        images = {k["type"]: k["url"] for k in e.get("keyImages", [])}
        img = images.get("OfferImageWide") or images.get("Thumbnail") or images.get("OfferImageTall")
        orig = html.unescape(total["fmtPrice"]["originalPrice"]).replace("\xa0", " ")
        desc = f"~~{orig}~~ → **GRÁTIS**\n⏳ Até <t:{int(end.timestamp())}:f> (<t:{int(end.timestamp())}:R>)"
        if e.get("description"):
            desc += f"\n\n{clip(e['description'], 250)}"
        embeds.append(embed(e["title"], url, desc, 0x2B2D31, img, footer="Epic Games Store"))
        keys.append(key)
    if not embeds:
        print("Epic: nada novo.")
        return
    dc.post("WEBHOOK_GRATIS", "🎁 **Grátis na Epic Games Store esta semana!** Resgate antes que acabe.", embeds)
    for k in keys:
        state.mark("epic", k)


def job_steam_free(cfg, state: State, dc: Discord) -> None:
    """Jogos pagos que ficaram 100% off por tempo limitado (não inclui free-to-play permanente)."""
    found: dict[int, Row] = {}
    for r in steam_search(count=50, specials=1, maxprice="free"):
        if r.discount == 100:
            found[r.appid] = r
    try:
        fc = get_json(f"{STEAM_STORE}/api/featuredcategories", {"cc": "br", "l": "english"})
        for it in fc.get("specials", {}).get("items", []):
            if it.get("discount_percent") == 100 and it.get("type", 0) == 0:
                found.setdefault(it["id"], Row(it["id"], it["name"], "", 100, 0))
    except (requests.RequestException, ValueError) as exc:
        vlog(f"featuredcategories falhou: {exc}")
    embeds, keys = [], []
    for appid, row in found.items():
        if state.seen("steam_free", appid, 30):
            continue
        data = steam_appdetails(appid)
        if not data or data.get("type") != "game":
            continue
        po = data.get("price_overview") or {}
        if po.get("discount_percent") != 100 or not po.get("initial"):
            continue
        g = build_game(appid, data, cfg)
        attach_rating(g)
        g.price_final = "GRÁTIS"
        embeds.append(game_embed(g, color=0x57F287, prefix="🎁 ", footer="Steam · grátis por tempo limitado"))
        keys.append(appid)
    if not embeds:
        print("Steam grátis: nada novo.")
        return
    dc.post("WEBHOOK_GRATIS", "🎮 **Grátis na Steam por tempo limitado!**", embeds)
    for k in keys:
        state.mark("steam_free", k)


def job_promo(cfg, state: State, dc: Discord) -> None:
    pc = cfg["promocoes"]
    seen_ids, rows = set(), []
    for cat in (CAT_ONLINE_COOP, 9, 39):  # mais populares primeiro dentro de cada categoria
        for r in steam_search(count=100, specials=1, category2=cat, sort_by="Reviews_DESC"):
            if r.appid not in seen_ids and r.discount >= pc["desconto_min"]:
                seen_ids.add(r.appid)
                rows.append(r)
    vlog(f"{len(rows)} candidatos com ≥{pc['desconto_min']}% de desconto")
    games: list[Game] = []
    evaluated = 0
    for r in rows:
        if len(games) >= pc["max_por_dia"] or evaluated >= pc["max_avaliar"]:
            break
        if state.seen("promo", r.appid, pc["repetir_apos_dias"]):
            continue
        evaluated += 1
        g = evaluate(r.appid, cfg, require_rating=True)
        if g and g.discount >= pc["desconto_min"]:
            g.historic_low = is_historic_low(g.appid)
            games.append(g)
    if not games:
        print("Promoções: nada novo hoje.")
        return
    games.sort(key=lambda g: (not g.historic_low, -g.discount))
    hoje = (now_utc() - timedelta(hours=3)).strftime("%d/%m")
    embeds = [game_embed(g, color=0xE74C3C if g.historic_low else 0xE67E22, footer="Steam · via CheapShark")
              for g in games]
    dc.post("WEBHOOK_PROMO", f"🛒 **Resumo diário de promoções co-op — {hoje}** ({len(games)} jogos)", embeds)
    for g in games:
        state.mark("promo", g.appid)


def job_lancamentos(cfg, state: State, dc: Discord) -> None:
    lc = cfg["lancamentos"]
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    past, future = today - timedelta(days=lc["dias_passados"]), today + timedelta(days=lc["dias_futuros"])
    cands: dict[int, tuple[Row, str]] = {}
    for cat in (CAT_ONLINE_COOP, 9, CAT_CROSSPLAY, 39):
        for kwargs in ({"sort_by": "Released_DESC"}, {"filter": "popularnew"}):
            for r in steam_search(count=50, category2=cat, **kwargs):
                d = parse_steam_date(r.released)
                if d and past <= d <= today:
                    cands.setdefault(r.appid, (r, "released"))
        for r in steam_search(count=50, category2=cat, filter="popularcomingsoon"):
            d = parse_steam_date(r.released)  # ignora datas vagas como "2026" ou "November 2026"
            if d and today < d <= future:
                cands.setdefault(r.appid, (r, "upcoming"))
    vlog(f"{len(cands)} candidatos a lançamento")
    out: list[tuple[datetime, str, Game]] = []
    for appid, (row, status) in cands.items():
        if len(out) >= lc["max_por_execucao"]:
            break
        if state.seen("lancamentos", f"{appid}:{status}"):
            continue
        g = evaluate(appid, cfg, require_rating=(status == "released" and not lc["permitir_sem_avaliacao"]),
                     need_coop=False)
        if not g or not (g.is_coop or CAT_CROSSPLAY in g.categories):
            continue
        out.append((g.release_date or today, status, g))
    if not out:
        print("Lançamentos: nada novo.")
        return
    out.sort(key=lambda t: t[0], reverse=True)
    embeds = []
    for date, status, g in out:
        label = ("🆕 Lançado em " if status == "released" else "📅 Lança em ") + date.strftime("%d/%m/%Y")
        embeds.append(game_embed(g, color=0x3498DB if status == "released" else 0x9B59B6,
                                 extra_fields=[("Lançamento", label, False)]))
    dc.post("WEBHOOK_LANCAMENTOS", "🚀 **Lançamentos co-op / crossplay**", embeds)
    for _, status, g in out:
        state.mark("lancamentos", f"{g.appid}:{status}")


def job_votacao(cfg, state: State, dc: Discord, force: bool) -> None:
    now = now_utc()
    if not force and now.weekday() != 4:
        print("Votação: só roda na sexta. Use --force para ignorar.")
        return
    vc = cfg["votacao"]
    iso = now.isocalendar()
    rng = random.Random(f"{iso.year}-{iso.week}")  # sugestões estáveis dentro da mesma semana
    pool: dict[int, Row] = {}
    for r in steam_search(count=100, specials=1, category2=CAT_ONLINE_COOP, sort_by="Reviews_DESC"):
        if r.discount >= cfg["promocoes"]["desconto_min"]:
            pool[r.appid] = r
    sale_ids = list(pool)
    rng.shuffle(sale_ids)
    for r in steam_search(count=100, start=rng.randrange(0, 150), category2=CAT_ONLINE_COOP,
                          sort_by="Reviews_DESC"):
        pool.setdefault(r.appid, r)
    others = [a for a in pool if a not in sale_ids]
    rng.shuffle(others)
    order = sale_ids[: vc["opcoes"]] + others  # dá preferência a jogos em promoção
    picks: list[Game] = []
    for appid in order:
        if len(picks) >= vc["opcoes"] or len(_APP_CACHE) > 120:
            break
        if state.seen("votacao", appid, vc["repetir_apos_dias"]):
            continue
        g = evaluate(appid, cfg, require_rating=True)
        if g:
            picks.append(g)
    if len(picks) < vc["minimo"]:
        raise RuntimeError(f"Votação: só {len(picks)} sugestões válidas (mínimo {vc['minimo']})")
    embeds = [game_embed(g, color=0xF1C40F, prefix=f"{i}️⃣ ", footer="Vote reagindo com o número")
              for i, g in enumerate(picks, 1)]
    res = dc.post("WEBHOOK_VOTACAO", "🗳️ **Votação da sexta!** O que vamos jogar? Reaja com o número do seu voto.",
                  embeds, wait=True)
    reacted = False
    if len(res) == 1:  # as reações vão na única mensagem (5 embeds cabem em uma)
        reacted = dc.react(res[0].get("channel_id", ""), res[0].get("id", ""), len(picks))
    if not reacted and not dc.dry_run:
        dc.post("WEBHOOK_LOG", "⚠️ Votação postada **sem reações automáticas**: defina o secret "
                "`DISCORD_BOT_TOKEN` (bot com permissão de adicionar reações no canal) ou reaja manualmente.")
    for g in picks:
        state.mark("votacao", g.appid)


# --------------------------------------------------------------------------- main
def log_error(dc: Discord, job: str, exc: BaseException) -> None:
    tb = "".join(traceback.format_exception(exc))[-1500:]
    msg = f"❌ **Erro no job `{job}`**\n```\n{tb}\n```"
    if dc.dry_run:
        print(msg, file=sys.stderr)
        return
    try:
        dc.post("WEBHOOK_LOG", clip(msg, 1990))
    except Exception as e2:  # o log nunca pode derrubar o job
        print(f"Falha ao enviar log: {e2}", file=sys.stderr)


def main() -> int:
    global VERBOSE
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job", choices=["daily", "epic", "promo", "lancamentos", "gratis", "votacao", "all"])
    ap.add_argument("--dry-run", action="store_true",
                    help="não envia nada ao Discord nem grava estado; imprime as mensagens (ignora os "
                         "dias/horários de epic e votação)")
    ap.add_argument("--force", action="store_true", help="ignora as janelas de dia/hora de epic e votação")
    ap.add_argument("-v", "--verbose", action="store_true", help="mostra por que cada jogo foi descartado")
    ap.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    ap.add_argument("--state", type=Path, default=ROOT / "state.json")
    args = ap.parse_args()
    VERBOSE = args.verbose or args.dry_run

    cfg = load_config(args.config)
    state = State(args.state, args.dry_run)
    dc = Discord(args.dry_run)
    force = args.force or args.dry_run

    plan = {
        "daily": ["gratis", "promo", "lancamentos"],
        "all": ["epic", "gratis", "promo", "lancamentos", "votacao"],
    }.get(args.job, [args.job])
    funcs = {
        "epic": lambda: job_epic(cfg, state, dc, force),
        "gratis": lambda: job_steam_free(cfg, state, dc),
        "promo": lambda: job_promo(cfg, state, dc),
        "lancamentos": lambda: job_lancamentos(cfg, state, dc),
        "votacao": lambda: job_votacao(cfg, state, dc, force),
    }
    failed = False
    for name in plan:
        print(f"▶ {name}")
        try:
            funcs[name]()
        except Exception as exc:  # um job com erro não impede os outros
            failed = True
            traceback.print_exc()
            log_error(dc, name, exc)
        finally:
            state.save(cfg["estado"]["retencao_dias"])  # grava após cada job: evita repetir posts
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
