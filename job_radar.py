#!/usr/bin/env python3
"""
job_radar.py — radar de vacantes directo desde los portales de empleo de las empresas,
con aviso por Telegram.

USO
  python job_radar.py                    corrida normal (avisa por Telegram si hay nuevas)
  python job_radar.py --telegram-setup   conecta tu bot de Telegram (una sola vez)
  python job_radar.py --test-telegram    manda un mensaje de prueba
  python job_radar.py --show-all         incluye vacantes ya vistas (no avisa por Telegram)
  python job_radar.py --no-title-filter  sin filtrar títulos (para ajustar filtros)
  python job_radar.py --only "Intel"     corre solo empresas cuyo nombre contenga ese texto

perfil.txt     — dónde buscar (país/ciudades, remoto) y qué palabras del título cuentan.
companies.txt  — una fuente por línea:
  Nombre | URL                    el portal se detecta por el dominio
  Nombre | tipo | URL             tipo forzado: successfactors, radancy, phenom, jibe, icims, hiringroom
  Nombre | tipo | URL | local     "local" = si el portal no dice la ubicación, asumir que es la tuya
  # al inicio de la línea          la fuente queda silenciada
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urljoin, urlparse, urlencode, parse_qsl, urlunparse

import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
PERFIL_PATH = HERE / "perfil.txt"

# ═══════════════════ PERFIL POR DEFECTO (lo normal es editar perfil.txt) ═══════════════════
# Si perfil.txt no existe o le falta una clave, se usa esto.

DEFAULT_PERFIL = {
    "ubicaciones": "",
    "remoto": "latam",
    "incluir": ("data, analyst, analytics, sql, bi, business intelligence, etl, database, reporting, "
                "insight*, quant*, risk, fraud, python, power bi, tableau, snowflake, databricks, "
                "machine learning, revenue operations, business systems, financial systems, "
                "governance, automation, rpa, pricing, research, fp&a"),
    "excluir": ("intern, internship, practicante, pasant*, student, estudiante, trainee, apprentice, "
                "graduate program, launch program, data center, data centre, data entry, annotat*, "
                "labeling, labelling, transcri*, customer support, customer service, customer care, "
                "support analyst, help desk, helpdesk, service desk, soc, payroll, accounts payable, "
                "accounts receivable, collections, director, vice president, vp, head of"),
}

# Países conocidos: nombres en español/inglés y códigos ISO, porque cada portal escribe la
# ubicación a su manera ("Venezuela", "VE", "VEN", "Caracas, VE"...).
COUNTRIES = [
    # (nombres, ISO2, ISO3)
    (["argentina"], "AR", "ARG"), (["bolivia"], "BO", "BOL"), (["brasil", "brazil"], "BR", "BRA"),
    (["chile"], "CL", "CHL"), (["colombia"], "CO", "COL"), (["costa rica"], "CR", "CRI"),
    (["ecuador"], "EC", "ECU"), (["el salvador"], "SV", "SLV"), (["guatemala"], "GT", "GTM"),
    (["honduras"], "HN", "HND"), (["mexico"], "MX", "MEX"), (["nicaragua"], "NI", "NIC"),
    (["panama"], "PA", "PAN"), (["paraguay"], "PY", "PRY"), (["peru"], "PE", "PER"),
    (["puerto rico"], "PR", "PRI"), (["republica dominicana", "dominican republic"], "DO", "DOM"),
    (["uruguay"], "UY", "URY"), (["venezuela"], "VE", "VEN"), (["espana", "spain"], "ES", "ESP"),
]
REMOTE_LATAM_WORDS = ["latam", "lat am", "latin america*", "latinoamerica", "america latina",
                      "south america", "sudamerica", "americas"]
REMOTE_GLOBAL_WORDS = ["worldwide", "anywhere", "global", "any location", "work from anywhere"]

REQUEST_TIMEOUT = 30
MAX_PER_COMPANY = 500        # tope de vacantes revisadas por empresa (paginación)
PAUSE = (0.6, 1.4)           # pausa aleatoria entre requests a la MISMA empresa
WORKERS = 8                  # empresas consultadas en paralelo (cada una a su propio servidor)
TIME_BUDGET_MIN = 14         # tope de la corrida: lo que no alcance queda para la próxima
                             # (GitHub Actions corta a los 20 min; así siempre llega el aviso)

# ═══════════════════════════════════ MODELO ═══════════════════════════════════


@dataclass
class Target:
    name: str
    ats: str
    board: str
    extra: dict = field(default_factory=dict)

    def to_line(self) -> str:
        """Línea canónica para companies.txt."""
        e = self.extra
        flag = " | local" if e.get("assume_local") else ""
        if self.ats == "workday":
            line = e["web_base"]
        elif self.ats == "greenhouse":
            line = f"https://job-boards.{'eu.' if e.get('eu') else ''}greenhouse.io/{self.board}"
        elif self.ats == "lever":
            line = f"https://jobs.{'eu.' if e.get('eu') else ''}lever.co/{self.board}"
        elif self.ats == "ashby":
            line = f"https://jobs.ashbyhq.com/{self.board}"
        elif self.ats == "smartrecruiters":
            line = f"https://jobs.smartrecruiters.com/{self.board}"
        elif self.ats == "workable":
            line = f"https://apply.workable.com/{self.board}"
        elif self.ats == "oracle":
            fac = f"?selectedLocationsFacet={e['location_facet']}" if e.get("location_facet") else ""
            line = f"https://{e['host']}/hcmUI/CandidateExperience/en/sites/{self.board}/jobs{fac}"
        elif self.ats == "eightfold":
            line = f"https://{e['host']}/careers?domain={e['domain']}"
        elif self.ats == "amazon":
            line = "https://www.amazon.jobs/en/search"
        else:
            return f"{self.name} | {self.ats} | {e.get('url') or 'https://' + e.get('host', '')}{flag}"
        if flag:
            return f"{self.name} | {self.ats} | {line}{flag}"
        return f"{self.name} | {line}"


@dataclass
class Job:
    ats: str
    board: str
    company: str
    job_id: str
    title: str
    location: str
    url: str
    posted: str = ""
    salary: str = ""

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.board}:{self.job_id}"


# ═══════════════════════════════ HTTP ═══════════════════════════════
# Una sesión por hilo: requests.Session no garantiza ser segura entre hilos.

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 job-radar/3.0",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.session = s
    return s


def pause() -> None:
    time.sleep(random.uniform(*PAUSE))


def get_json(url: str, params: dict | None = None):
    r = _session().get(url, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def post_json(url: str, payload: dict):
    r = _session().post(url, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_text(url: str, params: dict | None = None) -> str:
    r = _session().get(url, params=params, timeout=REQUEST_TIMEOUT, headers={"Accept": "text/html,*/*"})
    r.raise_for_status()
    return r.text


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def with_param(url: str, key: str, value) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[key] = str(value)
    return urlunparse(p._replace(query=urlencode(q)))


def epoch_to_date(v) -> str:
    try:
        v = float(v)
        if v > 1e11:          # milisegundos
            v /= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def as_loc(x) -> str:
    """Normaliza una ubicación que puede venir como texto o como objeto."""
    if isinstance(x, dict):
        return x.get("name") or x.get("location") or ", ".join(
            str(x[k]) for k in ("city", "state", "region", "country") if x.get(k))
    return str(x or "")


# ═══════════════════════════ ADAPTADORES POR ATS ═══════════════════════════

def fetch_greenhouse(t: Target) -> Iterator[Job]:
    hosts = ["https://boards-api.greenhouse.io"]
    if t.extra.get("eu"):
        hosts.insert(0, "https://boards-api.eu.greenhouse.io")
    data, last = None, None
    for h in hosts:
        try:
            data = get_json(f"{h}/v1/boards/{t.board}/jobs")
            break
        except requests.HTTPError as e:
            last = e
    if data is None:
        raise last
    for j in data.get("jobs", []):
        yield Job("greenhouse", t.board, t.name, str(j["id"]), j.get("title", ""),
                  (j.get("location") or {}).get("name", ""), j.get("absolute_url", ""),
                  (j.get("first_published") or j.get("updated_at") or "")[:10])


def fetch_lever(t: Target) -> Iterator[Job]:
    base = "https://api.eu.lever.co" if t.extra.get("eu") else "https://api.lever.co"
    for p in get_json(f"{base}/v0/postings/{t.board}", params={"mode": "json"}):
        cats = p.get("categories") or {}
        locs = list(cats.get("allLocations") or [cats.get("location", "")])  # copia, no alias
        country = COUNTRY_BY_ISO2.get((p.get("country") or "").upper())   # Lever da el país como "VE"
        if country:
            locs.append(country)
        yield Job("lever", t.board, t.name, p["id"], p.get("text", ""),
                  " | ".join(filter(None, locs)), p.get("hostedUrl", ""), epoch_to_date(p.get("createdAt")))


def fetch_ashby(t: Target) -> Iterator[Job]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{t.board}",
                    params={"includeCompensation": "true"})
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry", "")
        yield Job("ashby", t.board, t.name, j["id"], j.get("title", ""),
                  " | ".join(filter(None, locs + [country])),
                  j.get("jobUrl") or f"https://jobs.ashbyhq.com/{t.board}/{j['id']}",
                  (j.get("publishedAt") or "")[:10],
                  (j.get("compensation") or {}).get("compensationTierSummary") or "")


def fetch_smartrecruiters(t: Target) -> Iterator[Job]:
    offset = 0
    while offset < MAX_PER_COMPANY:
        data = get_json(f"https://api.smartrecruiters.com/v1/companies/{t.board}/postings",
                        params={"limit": 100, "offset": offset})
        content = data.get("content", [])
        for p in content:
            loc = p.get("location") or {}
            country = (loc.get("country") or "").strip()
            country = COUNTRY_BY_ISO2.get(country.upper(), country)         # "ve" → "Venezuela"
            parts = [loc.get("city"), loc.get("region"), country, "Remote" if loc.get("remote") else ""]
            yield Job("smartrecruiters", t.board, t.name, str(p["id"]), p.get("name", ""),
                      ", ".join(filter(None, parts)),
                      f"https://jobs.smartrecruiters.com/{t.board}/{p['id']}",
                      (p.get("releasedDate") or "")[:10])
        offset += len(content)
        if not content or offset >= data.get("totalFound", 0):
            break
        pause()


def fetch_workable(t: Target) -> Iterator[Job]:
    data = get_json(f"https://apply.workable.com/api/v1/widget/accounts/{t.board}")
    for j in data.get("jobs", []):
        locs = [as_loc(l) for l in j.get("locations") or [] if not l.get("hidden")]
        if not locs:
            locs = [", ".join(filter(None, [j.get("city"), j.get("state"), j.get("country")]))]
        if j.get("telecommuting"):
            locs.append("Remote")
        sc = j.get("shortcode") or j.get("code") or j.get("title")
        yield Job("workable", t.board, t.name, str(sc), j.get("title", ""),
                  " | ".join(filter(None, locs)),
                  j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{t.board}/j/{sc}/",
                  str(j.get("published_on") or j.get("created_at") or "")[:10])


def _find_location_facets(facets: list) -> tuple[dict[str, list[str]], list[str]]:
    """Busca en el árbol de filtros de Workday los valores que coinciden con tu perfil."""
    hits: list[tuple[str, str, str]] = []     # (parámetro, id, nombre)

    def walk(items, parent):
        for f in items:
            param = f.get("facetParameter") or parent
            for v in f.get("values", []):
                if "values" in v:
                    walk([v], param)
                elif v.get("id") and location_ok(v.get("descriptor", "")):
                    hits.append((param, v["id"], v.get("descriptor", "")))

    walk(facets, None)
    country = [h for h in hits if "country" in (h[0] or "").lower()]
    if not country and hits:   # sin filtro de país: UNO solo (varios se combinan con AND)
        best = max({h[0] for h in hits}, key=lambda k: sum(h[0] == k for h in hits))
        country = [h for h in hits if h[0] == best]
    applied: dict[str, list[str]] = {}
    for param, fid, _ in country:
        applied.setdefault(param, []).append(fid)
    return applied, [h[2] for h in country]


def fetch_workday(t: Target) -> Iterator[Job]:
    host, tenant, site, web_base = t.extra["host"], t.extra["tenant"], t.board, t.extra["web_base"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

    def page(applied: dict, offset: int, search: str) -> dict:
        return post_json(api, {"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": search})

    first = page({}, 0, "")
    applied, labels = _find_location_facets(first.get("facets", []))
    search = "" if applied else SEARCH_TERM      # sin filtro útil: búsqueda por texto (o nada)

    offset, total = 0, None
    while offset < MAX_PER_COMPANY:
        pause()
        data = page(applied, offset, search)
        if total is None:
            total = data.get("total", 0)  # Workday solo da 'total' confiable en la 1ª página
        posts = data.get("jobPostings", [])
        for p in posts:
            title, loc = p.get("title", ""), p.get("locationsText", "")
            if applied:
                loc += " | " + " / ".join(labels)   # el filtro ya garantiza la ubicación
            elif re.match(r"\d+ (Locations|ubicaciones)", loc, re.I) and title_ok(title):
                loc = _workday_detail_locations(host, tenant, site, p.get("externalPath", "")) or loc
            bullets = p.get("bulletFields") or []
            yield Job("workday", f"{tenant}/{site}", t.name,
                      bullets[0] if bullets else p.get("externalPath", ""),
                      title, loc, f"{web_base}{p.get('externalPath', '')}", p.get("postedOn", ""))
        offset += len(posts)
        if not posts or offset >= (total or 0):
            break


def _workday_detail_locations(host: str, tenant: str, site: str, path: str) -> str:
    try:
        pause()
        info = get_json(f"https://{host}/wday/cxs/{tenant}/{site}{path}").get("jobPostingInfo", {})
    except (requests.RequestException, ValueError):
        return ""
    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
    country = (info.get("country") or {}).get("descriptor", "")
    return " | ".join(filter(None, locs + [country]))


def fetch_oracle(t: Target) -> Iterator[Job]:
    """Oracle Recruiting Cloud (HCM). Ej.: Akamai, Emerson, Oracle."""
    host, site = t.extra["host"], t.board
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

    def call(offset: int, limit: int, sel: str) -> dict:
        finder = (f"findReqs;siteNumber={site},facetsList=LOCATIONS,limit={limit},offset={offset},"
                  f"sortBy=POSTING_DATES_DESC" + (f",selectedLocationsFacet={sel}" if sel else ""))
        data = get_json(f"{base}?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}")
        return (data.get("items") or [{}])[0]

    sel = str(t.extra.get("location_facet") or "")
    if not sel:
        facets = call(0, 1, "").get("locationsFacet") or []
        cands = [f for f in facets if location_ok(str(f.get("Name", "")))]
        cands.sort(key=lambda f: -int(f.get("TotalCount") or 0))      # el más amplio (país antes que ciudad)
        sel = str(cands[0]["Id"]) if cands else ""
        t.extra["facet_name"] = str(cands[0].get("Name", "")) if cands else ""

    offset, total = 0, None
    while offset < MAX_PER_COMPANY:
        pause()
        it = call(offset, 25, sel)
        reqs = it.get("requisitionList") or []
        if total is None:
            total = it.get("TotalJobsCount") or 0
        for r in reqs:
            locs = [r.get("PrimaryLocation", "")] + [s.get("Name", "") for s in r.get("secondaryLocations") or []]
            locs.append(COUNTRY_BY_ISO2.get((r.get("PrimaryLocationCountry") or "").upper(), ""))
            if sel:
                locs.append(t.extra.get("facet_name") or ASSUMED)
            yield Job("oracle", f"{host}/{site}", t.name, str(r.get("Id")), r.get("Title", ""),
                      " | ".join(filter(None, locs)),
                      f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r.get('Id')}",
                      (r.get("PostedDate") or "")[:10])
        offset += len(reqs)
        if not reqs or offset >= total:
            break


def fetch_amazon(t: Target) -> Iterator[Job]:
    offset = 0
    while offset < MAX_PER_COMPANY:
        params = {"result_limit": 100, "offset": offset, "sort": "recent"}
        if AMAZON_COUNTRY:
            params.update(country=AMAZON_COUNTRY, loc_query=SEARCH_TERM)
        data = get_json("https://www.amazon.jobs/en/search.json", params=params)
        jobs = data.get("jobs", [])
        for j in jobs:
            loc = j.get("normalized_location") or j.get("location") or ""
            loc += " | " + COUNTRY_BY_ISO3.get((j.get("country_code") or "").upper(), "")
            yield Job("amazon", "amazon", t.name, str(j.get("id_icims") or j.get("id")),
                      j.get("title", ""), loc, "https://www.amazon.jobs" + (j.get("job_path") or ""),
                      j.get("posted_date", ""))
        offset += len(jobs)
        if not jobs or offset >= (data.get("hits") or 0):
            break
        pause()


def fetch_eightfold(t: Target) -> Iterator[Job]:
    """Eightfold (Microsoft, Bayer...). Pide tu ubicación principal; 10 por página."""
    host, domain = t.extra["host"], t.extra["domain"]
    origin = f"https://{host}"
    mode, start, seen, total = "pcsx", 0, set(), None
    for _ in range(25):
        if mode == "pcsx":
            try:
                raw = get_json(f"{origin}/api/pcsx/search", {"domain": domain, "query": "", "location": SEARCH_TERM,
                                                             "start": start, "sort_by": "timestamp"})
            except requests.HTTPError:
                if start:
                    raise
                mode = "v2"
                continue
        else:
            raw = get_json(f"{origin}/api/apply/v2/jobs", {"domain": domain, "start": start, "num": 10,
                                                          "location": SEARCH_TERM, "sort_by": "relevance"})
        body = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        positions = body.get("positions") or []
        if total is None:
            total = body.get("count") or 0
        fresh = 0
        for p in positions:
            pid = str(p.get("id") or p.get("ats_job_id") or p.get("atsJobId") or "")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            fresh += 1
            locs = [as_loc(x) for x in (p.get("locations") or p.get("standardizedLocations") or [])]
            if not locs:
                locs = [as_loc(p.get("location"))]
            link = p.get("canonicalPositionUrl") or p.get("positionUrl") or f"/careers/job/{pid}?domain={domain}"
            yield Job("eightfold", domain, t.name, pid,
                      p.get("name") or p.get("posting_name") or p.get("title") or "",
                      " | ".join(dict.fromkeys(filter(None, locs))), urljoin(origin, link),
                      epoch_to_date(p.get("t_create") or p.get("postedTs") or p.get("creationTs")))
        start += len(positions)
        if not positions or not fresh or start >= total:
            break
        pause()


def fetch_jibe(t: Target) -> Iterator[Job]:
    """iCIMS Jibe (careers.empresa.com/jobs/12345). Ej.: Schneider Electric. Beta."""
    origin = f"https://{t.extra['host']}"
    seen, total = set(), None
    for page in range(1, 11):
        data = get_json(f"{origin}/api/jobs", {"location": SEARCH_TERM, "page": page, "limit": 100,
                                               "sortBy": "posted_date", "descending": "true", "internal": "false"})
        jobs = data.get("jobs") or []
        if total is None:
            total = data.get("totalCount") or data.get("count") or 0
        for j in jobs:
            d = j.get("data") or j
            jid = str(d.get("req_id") or d.get("slug") or d.get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            loc = d.get("full_location") or d.get("location_name") or ", ".join(
                filter(None, [d.get("city"), d.get("state"), d.get("country")]))
            yield Job("jibe", t.extra["host"], t.name, jid, d.get("title", ""), loc,
                      f"{origin}/jobs/{d.get('slug') or jid}?lang=en-us", str(d.get("posted_date") or "")[:10])
        if not jobs or len(seen) >= total:
            break
        pause()


def _sf_parse(page: str) -> list[tuple[str, str, str, str]]:
    """SuccessFactors RMK: filas con <a class="jobTitle-link"> y <span class="jobLocation">."""
    rows, seen = [], set()
    for m in re.finditer(r'<a\b([^>]*class="[^"]*jobTitle-link[^"]*"[^>]*)>(.*?)</a>', page, re.S | re.I):
        href = re.search(r'href="([^"]+)"', m.group(1))
        if not href:
            continue
        jid = re.search(r"/(\d{4,})/?(?:[?#]|$)", href.group(1))
        jid = jid.group(1) if jid else href.group(1)
        if jid in seen:
            continue
        seen.add(jid)
        lm = re.search(r'class="[^"]*jobLocation[^"]*"[^>]*>(.*?)</span>', page[m.end():m.end() + 3000], re.S | re.I)
        rows.append((jid, strip_tags(m.group(2)), strip_tags(lm.group(1)) if lm else "", html.unescape(href.group(1))))
    return rows


def fetch_successfactors(t: Target) -> Iterator[Job]:
    base = t.extra["base"]   # ej. https://careers.ey.com/ey  (algunos portales llevan ruta)
    seen: set[str] = set()
    start = 0
    while start < MAX_PER_COMPANY:
        page = get_text(f"{base}/search/", params={"q": "", "locationsearch": SEARCH_TERM, "startrow": start})
        fresh = [r for r in _sf_parse(page) if r[0] not in seen]
        if not fresh:
            break
        for jid, title, loc, href in fresh:
            seen.add(jid)
            yield Job("successfactors", t.extra["host"], t.name, jid, title, loc or (f"{SEARCH_TERM} (búsqueda)" if SEARCH_TERM else ""),
                      urljoin(base + "/", href))
        start += 25
        pause()


def _radancy_parse(page: str) -> list[tuple[str, str, str, str]]:
    rows, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*href="((?:/[a-z]{2}(?:-[a-z]{2})?)?/job/[^"]+?/(\d+)/(\d+))"[^>]*>(.*?)</a>',
                         page, re.S | re.I):
        href, jid, inner = m.group(1), m.group(3), m.group(4)
        if jid in seen:
            continue
        seen.add(jid)
        h = re.search(r"<h[23][^>]*>(.*?)</h[23]>", inner, re.S | re.I)
        loc = re.search(r'class="[^"]*location[^"]*"[^>]*>(.*?)</span>', inner, re.S | re.I)
        rows.append((jid, strip_tags(h.group(1) if h else inner), strip_tags(loc.group(1)) if loc else "", href))
    return rows


def fetch_radancy(t: Target) -> Iterator[Job]:
    """Radancy/TalentBrew: usá la URL de la página de tu país en ese portal (…/location/…)."""
    url = t.extra["url"]
    seen: set[str] = set()
    for page_no in range(1, 21):
        page = get_text(url if page_no == 1 else with_param(url, "p", page_no))
        fresh = [r for r in _radancy_parse(page) if r[0] not in seen]
        if not fresh:
            break
        for jid, title, loc, href in fresh:
            seen.add(jid)
            yield Job("radancy", urlparse(url).netloc, t.name, jid, title,
                      loc or ASSUMED, urljoin(url, href))
        pause()


def _walk_job_lists(obj) -> Iterator[dict]:
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj) and \
                any("title" in x and ("jobId" in x or "jobSeqNo" in x or "reqId" in x) for x in obj):
            yield from obj
        else:
            for x in obj:
                yield from _walk_job_lists(x)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_job_lists(v)


def fetch_phenom(t: Target) -> Iterator[Job]:
    """Phenom: lee las vacantes que la página trae embebidas (phApp.ddo). Beta."""
    url = t.extra["url"]
    page = get_text(url)
    m = re.search(r"phApp\.ddo\s*=\s*(\{.*?\})\s*;\s*phApp\.", page, re.S)
    if not m:
        raise ValueError("Phenom: la página no trae vacantes embebidas (probá otra URL de búsqueda)")
    data = json.loads(m.group(1))
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    locale = "/".join(urlparse(url).path.strip("/").split("/")[:2])
    seen = set()
    for j in _walk_job_lists(data):
        jid = str(j.get("jobSeqNo") or j.get("jobId") or j.get("reqId"))
        if jid in seen:
            continue
        seen.add(jid)
        loc = j.get("location") or j.get("cityStateCountry") or ", ".join(
            filter(None, [j.get("city"), j.get("state"), j.get("country")]))
        link = f"{origin}/{locale}/job/{jid}" if locale else (j.get("applyUrl") or url)
        yield Job("phenom", urlparse(url).netloc, t.name, jid, j.get("title", ""), str(loc), link,
                  str(j.get("postedDate") or "")[:10])


def _icims_parse(page: str) -> list[tuple[str, str, str, str]]:
    rows, seen = [], set()
    for m in re.finditer(r'<a\b([^>]*href="([^"]*?/jobs/(\d+)/([^/"?]+)/job)[^"]*"[^>]*)>(.*?)</a>', page, re.S | re.I):
        attrs, href, jid, slug, inner = m.groups()
        if jid in seen:
            continue
        seen.add(jid)
        tm = re.search(r'title="\s*\d+\s*-\s*([^"]+)"', attrs)
        title = html.unescape(tm.group(1)).strip() if tm else strip_tags(inner) or slug.replace("-", " ").title()
        after = page[m.end():m.end() + 2500]
        lm = re.search(r'Location[^<]*</(?:span|dt|div)>\s*(?:<[^>]+>\s*)*([^<]{2,120})<', after, re.I)
        rows.append((jid, title, strip_tags(lm.group(1)) if lm else "", html.unescape(href)))
    return rows


def fetch_icims(t: Target) -> Iterator[Job]:
    """iCIMS clásico (careers-empresa.icims.com). Beta."""
    origin = f"https://{t.extra['host']}"
    seen: set[str] = set()
    for pr in range(0, 20):
        page = get_text(f"{origin}/jobs/search", {"ss": 1, "in_iframe": 1, "pr": pr})
        fresh = [r for r in _icims_parse(page) if r[0] not in seen]
        if not fresh:
            break
        for jid, title, loc, href in fresh:
            seen.add(jid)
            yield Job("icims", t.extra["host"], t.name, jid, title, loc, urljoin(origin + "/", href.split("?")[0]))
        pause()


def fetch_hiringroom(t: Target) -> Iterator[Job]:
    """HiringRoom (empresa.hiringroom.com). Beta: no publica ubicación → usar la marca | local."""
    origin = f"https://{t.extra['host']}"
    page = get_text(f"{origin}/jobs")
    seen = set()
    for m in re.finditer(r'<a\b[^>]*href="([^"]*?/jobs/get_vacancy/([0-9a-f]{24})[^"]*)"[^>]*>(.*?)</a>', page, re.S | re.I):
        href, jid, inner = m.groups()
        title = strip_tags(inner)
        if jid in seen or not title:
            continue
        seen.add(jid)
        yield Job("hiringroom", t.extra["host"], t.name, jid, title, "", urljoin(origin + "/", html.unescape(href)))


FETCHERS: dict[str, Callable[[Target], Iterator[Job]]] = {
    "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters, "workable": fetch_workable, "workday": fetch_workday,
    "oracle": fetch_oracle, "amazon": fetch_amazon, "eightfold": fetch_eightfold, "jibe": fetch_jibe,
    "successfactors": fetch_successfactors, "radancy": fetch_radancy, "phenom": fetch_phenom,
    "icims": fetch_icims, "hiringroom": fetch_hiringroom,
}

# ═══════════════════════ DETECTAR EL ATS DESDE UNA URL ═══════════════════════

ATS_PATTERNS = [
    ("workday", re.compile(
        r"(?P<host>(?P<tenant>[\w-]+)\.wd\d+\.myworkdayjobs\.com)/"
        r"(?:wday/cxs/[\w-]+/)?(?:[a-z]{2}(?:-[A-Z]{2})?/)?(?P<site>[\w-]+)")),
    ("workday-site", re.compile(
        r"(?P<host>wd\d+\.myworkdaysite\.com)/(?:[a-z]{2}(?:-[A-Z]{2})?/)?recruiting/(?P<tenant>[\w-]+)/(?P<site>[\w-]+)")),
    ("greenhouse", re.compile(
        r"(?:boards|job-boards)\.(?P<eu>eu\.)?greenhouse\.io/(?:embed/job_(?:board|app)(?:/js)?\?for=)?(?P<board>[\w-]+)")),
    ("greenhouse", re.compile(r"boards-api\.(?P<eu>eu\.)?greenhouse\.io/v1/boards/(?P<board>[\w-]+)")),
    ("lever", re.compile(r"(?:jobs|api)\.(?P<eu>eu\.)?lever\.co/(?:v0/postings/)?(?P<board>[\w.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/(?P<board>[\w.%-]+)")),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/(?P<board>[\w-]+)")),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/(?P<board>[\w-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v\d/(?:widget/)?accounts/)?(?P<board>[\w-]+)")),
    ("oracle", re.compile(
        r"(?P<host>[\w.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/(?P<site>[\w-]+)")),
    ("eightfold", re.compile(r"(?P<host>[\w.-]+)/careers(?:/job/\d+[^?\s]*)?\?(?:[^\s]*&)?domain=(?P<domain>[\w.-]+)")),
    ("amazon", re.compile(r"amazon\.jobs")),
]
NOT_A_BOARD = {"embed", "v1", "jobs", "job", "api", "en", "es", "search", "j"}
HTML_ATS = ("successfactors", "radancy", "phenom", "jibe", "icims", "hiringroom")


def detect_ats(url: str, name: str = "", forced: str = "", flags: str = "") -> Target | None:
    url, forced = url.strip(), forced.strip().lower()
    assume_local = bool({"local", "cr"} & {f.strip().lower() for f in flags.split(",")})
    t = _detect(url, name, forced)
    if t and assume_local:
        t.extra["assume_local"] = True
    return t


def _detect(url: str, name: str, forced: str) -> Target | None:
    if forced in HTML_ATS:
        p = urlparse(url if "://" in url else "https://" + url)
        path = re.sub(r"/search/?$", "", p.path.rstrip("/"))
        return Target(name or p.netloc, forced, p.netloc,
                      {"host": p.netloc, "url": url, "base": f"{p.scheme}://{p.netloc}{path}"})
    for ats, rx in ATS_PATTERNS:
        if forced and not ats.startswith(forced):
            continue
        m = rx.search(url)
        if not m:
            continue
        g = m.groupdict()
        if ats in ("workday", "workday-site"):
            web_base = (f"https://{g['host']}/{g['site']}" if ats == "workday"
                        else f"https://{g['host']}/recruiting/{g['tenant']}/{g['site']}")
            if g["site"].lower() in NOT_A_BOARD:
                continue
            return Target(name or g["tenant"], "workday", g["site"],
                          {"host": g["host"], "tenant": g["tenant"], "web_base": web_base})
        if ats == "oracle":
            fac = re.search(r"selectedLocationsFacet=(\d+)", url)
            return Target(name or g["host"], "oracle", g["site"],
                          {"host": g["host"], "location_facet": fac.group(1) if fac else ""})
        if ats == "eightfold":
            host = g["host"].split("//")[-1]
            return Target(name or g["domain"], "eightfold", g["domain"], {"host": host, "domain": g["domain"]})
        if ats == "amazon":
            return Target(name or "Amazon", "amazon", "amazon", {})
        if g["board"].lower() in NOT_A_BOARD:
            continue
        return Target(name or g["board"], ats, g["board"], {"eu": bool(g.get("eu"))})
    return None


def parse_line(line: str) -> tuple[str, str, str, str]:
    """→ (nombre, ats_forzado, url, flags). Acepta 1 a 4 columnas separadas por |."""
    parts = [p.strip() for p in line.split("|")]
    if len(parts) == 1:
        return "", "", parts[0], ""
    if len(parts) == 2:
        return parts[0], "", parts[1], ""
    if parts[1].lower().startswith("http"):           # Nombre | URL | flags
        return parts[0], "", parts[1], parts[2]
    return parts[0], parts[1], parts[2], (parts[3] if len(parts) > 3 else "")


def load_targets(path: Path) -> list[Target]:
    targets = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, forced, url, flags = parse_line(line)
        t = detect_ats(url, name, forced, flags)
        if t:
            targets.append(t)
        else:
            print(f"  ⚠ línea {n}: no reconozco el ATS de {url!r}")
    return targets


# ═══════════════════════════════ FILTROS ═══════════════════════════════

# Todo se compara "doblado": minúsculas y sin tildes ("Panamá" = "panama").
NEVER, ALWAYS = re.compile(r"(?!)"), re.compile("")
COUNTRY_BY_ALIAS = {name: (names, iso2, iso3) for names, iso2, iso3 in COUNTRIES for name in names}
COUNTRY_BY_ISO2 = {iso2: names[0].title() for names, iso2, _ in COUNTRIES}
COUNTRY_BY_ISO3 = {iso3: names[0].title() for names, _, iso3 in COUNTRIES}


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def split_list(value: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;\n]", value or "") if x.strip()]


def word_rx(words: list[str]) -> re.Pattern:
    """Palabras simples → regex. 'bi' no encuentra 'Bilingual'; 'insight*' encuentra 'insights'."""
    pats = []
    for w in words:
        w = fold(w).strip()
        if not w:
            continue
        star = w.endswith("*")
        body = re.escape(w.rstrip("*")).replace(r"\ ", r"[\s\-_/]*")   # "power bi" = "PowerBI" = "Power-BI"
        pats.append(r"(?<![a-z0-9])" + body + (r"[a-z0-9]*" if star else r"(?![a-z0-9])"))
    return re.compile("|".join(pats)) if pats else NEVER


def load_perfil(path: Path = PERFIL_PATH) -> dict:
    perfil = dict(DEFAULT_PERFIL)
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                perfil[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return perfil


def configure(perfil: dict) -> None:
    """Arma los filtros a partir del perfil. Se llama al importar y desde configurar.py."""
    global RX_TITLE_IN, RX_TITLE_OUT, RX_LOCATION, RX_ISO2, RX_REMOTE, SEARCH_TERM, AMAZON_COUNTRY, PERFIL
    PERFIL = perfil
    places = split_list(perfil.get("ubicaciones", ""))
    terms, iso2s, iso3s = [], [], []
    for place in places:
        known = COUNTRY_BY_ALIAS.get(fold(place))
        if known:
            terms += known[0]
            iso2s.append(known[1])
            iso3s.append(known[2])
        else:
            terms.append(place)
    RX_LOCATION = word_rx(terms + [c.lower() for c in iso3s])          # "Venezuela", "VEN - Caracas"
    RX_ISO2 = re.compile(r",\s*(?:%s)\b(?![\w-])" % "|".join(iso2s)) if iso2s else NEVER   # "Caracas, VE"
    mode = fold(perfil.get("remoto", "latam")).strip()
    RX_REMOTE = (word_rx(REMOTE_LATAM_WORDS + (REMOTE_GLOBAL_WORDS if mode == "global" else []))
                 if mode in ("latam", "global") else NEVER)
    SEARCH_TERM = places[0] if places else ""      # para portales que piden un texto de ubicación
    AMAZON_COUNTRY = iso3s[0] if iso3s else ""
    include = split_list(perfil.get("incluir", ""))
    RX_TITLE_IN = word_rx(include) if include else ALWAYS                # vacío = cualquier título
    RX_TITLE_OUT = word_rx(split_list(perfil.get("excluir", "")))


def title_ok(title: str) -> bool:
    t = fold(title)
    return bool(RX_TITLE_IN.search(t)) and not RX_TITLE_OUT.search(t)


def location_ok(location: str) -> bool:
    loc = str(location or "")
    if loc == ASSUMED:
        return True
    f = fold(loc)
    return bool(RX_LOCATION.search(f) or RX_ISO2.search(loc) or RX_REMOTE.search(f))


ASSUMED = "(ubicación asumida)"
configure(load_perfil())


# ═══════════════════════ MEMORIA (SQLite) ═══════════════════════
# notified = 0 → ya la vimos pero el aviso todavía no salió (p. ej. la corrida se cortó o
# Telegram falló). Se reintenta en la próxima corrida: ninguna vacante se pierde en silencio.

def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS seen (
        key TEXT PRIMARY KEY, ats TEXT, company TEXT, title TEXT, location TEXT, url TEXT, first_seen TEXT)""")
    cols = {r[1] for r in db.execute("PRAGMA table_info(seen)")}
    if "notified" not in cols:      # migración: lo que ya existía se considera avisado
        db.execute("ALTER TABLE seen ADD COLUMN notified INTEGER NOT NULL DEFAULT 1")
    if "salary" not in cols:
        db.execute("ALTER TABLE seen ADD COLUMN salary TEXT")
    db.commit()
    return db


def remember(db: sqlite3.Connection, j: Job) -> bool:
    cur = db.execute(
        "INSERT OR IGNORE INTO seen (key, ats, company, title, location, url, first_seen, notified, salary) "
        "VALUES (?,?,?,?,?,?,?,0,?)",
        (j.key, j.ats, j.company, j.title, j.location, j.url, datetime.now().isoformat(timespec="seconds"), j.salary))
    return cur.rowcount == 1


def pending_notifications(db: sqlite3.Connection) -> list[Job]:
    rows = db.execute("SELECT key, ats, company, title, location, url, first_seen, salary FROM seen "
                      "WHERE notified = 0 ORDER BY company, title").fetchall()
    return [Job(r[1], "", r[2], r[0], r[3], r[4] or "", r[5], r[6][:10], r[7] or "") for r in rows]


def mark_notified(db: sqlite3.Connection, jobs: list[Job]) -> None:
    db.executemany("UPDATE seen SET notified = 1 WHERE key = ?", [(j.job_id,) for j in jobs])
    db.commit()


# ═══════════════════════════════ TELEGRAM ═══════════════════════════════

def load_config() -> dict:
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    if os.environ.get("TELEGRAM_TOKEN"):       # en GitHub Actions llegan como Secrets
        cfg["telegram_token"] = os.environ["TELEGRAM_TOKEN"].strip()
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"].strip()
    return cfg


def tg_call(token: str, method: str, **params):
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def tg_send(cfg: dict, text: str) -> None:
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        if len(cur) + len(block) + 2 > 3900:
            chunks.append(cur)
            cur = ""
        cur += (block + "\n\n")
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        tg_call(cfg["telegram_token"], "sendMessage", chat_id=cfg["telegram_chat_id"], text=c.strip(),
                parse_mode="HTML", disable_web_page_preview=True)
        time.sleep(0.5)


def telegram_setup() -> int:
    print("\n1) En Telegram, abrí @BotFather → /newbot → elegí nombre → copiá el token.")
    token = input("2) Pegá aquí el token y presioná Enter: ").strip()
    try:
        me = tg_call(token, "getMe")
    except Exception as e:  # noqa: BLE001
        print("   ✗ Token inválido:", e)
        return 1
    print(f"3) Ahora abrí https://t.me/{me['username']} , tocá INICIAR (o mandale /start). Esperando hasta 3 min...")
    chat_id, deadline = None, time.time() + 180
    while time.time() < deadline and chat_id is None:
        for u in tg_call(token, "getUpdates", timeout=25):
            msg = u.get("message") or u.get("edited_message") or {}
            if msg.get("chat"):
                chat_id = msg["chat"]["id"]
    if chat_id is None:
        print("   ✗ No recibí tu mensaje. Corré de nuevo --telegram-setup y mandale /start al bot.")
        return 1
    cfg = load_config()
    cfg.update({"telegram_token": token, "telegram_chat_id": chat_id})
    cfg.setdefault("avisar_sin_nuevas", False)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tg_send(cfg, "✅ <b>Job Radar conectado.</b> Te voy a avisar aquí cuando aparezcan vacantes nuevas.")
    print(f"   ✓ Listo. Configuración guardada en {CONFIG_PATH.name}. Revisá Telegram.")
    return 0


def telegram_report(cfg: dict, new: list[Job], failures: list[str], n_sources: int,
                    first_run: bool = False, skipped: list[str] | None = None) -> bool:
    """Devuelve True si el mensaje salió (o no había nada que mandar)."""
    if not cfg.get("telegram_token") or not cfg.get("telegram_chat_id"):
        return False
    new = sorted(new, key=lambda j: (j.company, j.title))
    if not new and not cfg.get("avisar_sin_nuevas"):
        return True
    e = lambda s: html.escape(str(s), quote=True)  # noqa: E731
    if first_run:
        head = f"🛰 <b>Primera corrida: {len(new)} vacantes abiertas</b> que pasan tus filtros"
    else:
        head = f"🛰 <b>{len(new)} vacante{'s' if len(new) != 1 else ''} nueva{'s' if len(new) != 1 else ''}</b>"
    parts = [f"{head} · {n_sources} fuentes revisadas"]
    for j in new:
        line = f"<b>{e(j.company)}</b> — <a href=\"{e(j.url)}\">{e(j.title)}</a>\n📍 {e(j.location[:90])}"
        if j.salary:
            line += f"  💰 {e(j.salary)}"
        parts.append(line)
    if failures:
        parts.append(f"⚠ {len(failures)} fuentes con error (detalle en el log de la corrida)")
    if skipped:
        parts.append(f"⏱ {len(skipped)} fuentes quedaron para la próxima corrida por tiempo")
    tg_send(cfg, "\n\n".join(parts))
    return True


# ═══════════════════════════════ MAIN ═══════════════════════════════

def run_one(t: Target) -> tuple[Target, list[Job], str, float]:
    """Corre en un hilo: descarga una fuente. Nunca lanza excepción."""
    t0 = time.time()
    try:
        jobs = list(FETCHERS[t.ats](t))
        if t.extra.get("assume_local"):
            for j in jobs:
                if not j.location:
                    j.location = ASSUMED
        return t, jobs, "", time.time() - t0
    except requests.HTTPError as ex:
        code = ex.response.status_code if ex.response is not None else "?"
        return t, [], f"HTTP {code}", time.time() - t0
    except Exception as ex:  # noqa: BLE001 — una fuente rota no tumba la corrida
        return t, [], f"{type(ex).__name__}: {str(ex)[:100]}", time.time() - t0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="Radar de vacantes directo desde los ATS.")
    ap.add_argument("--companies", type=Path, default=HERE / "companies.txt")
    ap.add_argument("--db", type=Path, default=HERE / "radar.db")
    ap.add_argument("--out", type=Path, default=HERE / "salida")
    ap.add_argument("--show-all", action="store_true")
    ap.add_argument("--no-title-filter", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--telegram-setup", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--budget-min", type=float, default=TIME_BUDGET_MIN)
    args = ap.parse_args(argv)

    if args.telegram_setup:
        return telegram_setup()
    cfg = load_config()
    if args.test_telegram:
        if not cfg.get("telegram_token"):
            print("Primero corré: python job_radar.py --telegram-setup")
            return 1
        tg_send(cfg, "🧪 Prueba del Job Radar: si ves esto, Telegram funciona.")
        print("Mensaje de prueba enviado.")
        return 0

    targets = [t for t in load_targets(args.companies) if args.only.lower() in t.name.lower()]
    if not targets:
        msg = ("Todavía no hay empresas para revisar. Agregalas con Actions → 'Configurar radar' "
               "→ Run workflow, o editando companies.txt.")
        print(("::warning::" if os.environ.get("GITHUB_ACTIONS") else "") + msg)
        return 0
    if RX_LOCATION is NEVER and RX_REMOTE is NEVER:
        print("⚠ perfil.txt no tiene ubicaciones y remoto = no: ninguna vacante va a pasar el filtro.")
    random.shuffle(targets)   # si el tiempo no alcanza, no siempre se quedan las mismas por fuera

    db = open_db(args.db)
    first_run = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    results: list[tuple[bool, Job]] = []
    failures: list[str] = []
    started = time.time()
    deadline = started + args.budget_min * 60

    print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] Revisando {len(targets)} fuentes ({WORKERS} en paralelo)...\n")
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    futures = {pool.submit(run_one, t): t for t in targets}
    done_targets: set[int] = set()
    try:
        for fut in as_completed(futures, timeout=max(1.0, deadline - time.time())):
            t, jobs, err, secs = fut.result()
            done_targets.add(id(t))
            if err:
                failures.append(f"{t.name} ({t.ats}): {err}")
                print(f"  ✗ {t.name[:30]:<30} {t.ats:<15} {err}")
                continue
            matched = [j for j in jobs if location_ok(j.location) and (args.no_title_filter or title_ok(j.title))]
            new_here = 0
            for j in matched:
                is_new = remember(db, j)
                new_here += is_new
                if is_new or args.show_all:
                    results.append((is_new, j))
            db.commit()
            print(f"  {t.name[:30]:<30} {t.ats:<15} {len(jobs):>4} revisadas · "
                  f"{len(matched):>3} pasan filtros · {new_here:>3} nuevas · {secs:4.0f}s")
    except FuturesTimeout:
        pass
    skipped = [t.name for t in targets if id(t) not in done_targets]
    pool.shutdown(wait=False, cancel_futures=True)

    print(f"\nDuración: {time.time() - started:.0f}s")
    if skipped:
        print(f"⏱ Sin tiempo para {len(skipped)} fuentes (van en la próxima corrida): {', '.join(skipped)}")
    n_new = sum(n for n, _ in results)
    if not results:
        print("Nada nuevo desde la última corrida.")
    else:
        print(f"{'═' * 70}\n {n_new} NUEVAS · {len(results) - n_new} ya vistas\n{'═' * 70}")
        for is_new, j in sorted(results, key=lambda r: (not r[0], r[1].company, r[1].title)):
            extra = f"  $ {j.salary}" if j.salary else ""
            print(f" {'★' if is_new else ' '} [{j.company}] {j.title}\n     {j.location} · {j.posted}{extra}\n     {j.url}")
        args.out.mkdir(exist_ok=True)
        csv_path = args.out / f"vacantes_{datetime.now():%Y-%m-%d_%H%M}.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["nueva", "empresa", "titulo", "ubicacion", "salario", "publicada", "url", "ats"])
            for is_new, j in results:
                w.writerow(["sí" if is_new else "", j.company, j.title, j.location, j.salary, j.posted, j.url, j.ats])
        print(f"\nCSV: {csv_path}")

    if failures:
        print("\nFallaron:")
        for msg in failures:
            print("  ✗", msg)

    if not args.no_telegram and not args.show_all and not args.only:
        pending = pending_notifications(db)   # incluye avisos que quedaron pendientes de antes
        try:
            if telegram_report(cfg, pending, failures, len(targets), first_run, skipped):
                mark_notified(db, pending)
            elif not cfg.get("telegram_token"):
                mark_notified(db, pending)    # sin Telegram configurado, la consola es el aviso
        except Exception as ex:  # noqa: BLE001
            print("\n⚠ No pude enviar a Telegram (se reintenta en la próxima corrida):", ex)
    db.close()
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Salida inmediata: si el tope de tiempo dejó alguna consulta lenta corriendo en otro hilo,
    # no la esperamos (todo lo importante ya quedó guardado en radar.db y enviado a Telegram).
    os._exit(code)
