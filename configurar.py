#!/usr/bin/env python3
"""
configurar.py — lo usa el formulario "Configurar radar" de GitHub Actions.

Lee estas variables (vacío = no cambiar; "-" = borrar):
  EMPRESAS     nombres o links, separados por coma:  "Gorilla Logic, https://empresa.wd5.myworkdayjobs.com/Careers"
  UBICACIONES  países/ciudades:                      "Venezuela, Caracas"
  REMOTO       latam | global | no
  PALABRAS     palabras del título:                  "data, analyst, sql"

Actualiza perfil.txt y companies.txt, y deja un resumen en la corrida (y en Telegram, si está configurado).
También funciona en la terminal:  EMPRESAS="Gorilla Logic" python configurar.py
"""
from __future__ import annotations

import html
import os
import re
import sys
from datetime import date
from urllib.parse import urlparse

import requests

import job_radar as jr

PERFIL = jr.PERFIL_PATH
COMPANIES = jr.HERE / "companies.txt"


def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def update_perfil(changes: dict[str, str]) -> None:
    """Cambia solo las claves indicadas y conserva comentarios y el resto del archivo."""
    lines = PERFIL.read_text(encoding="utf-8").splitlines() if PERFIL.exists() else []
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"\s*([a-z_]+)\s*=", line, re.I)
        if m and m.group(1).lower() in changes:
            key = m.group(1).lower()
            lines[i] = f"{key} = {changes[key]}"
            done.add(key)
    lines += [f"{k} = {v}" for k, v in changes.items() if k not in done]
    PERFIL.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─────────────────── encontrar el portal de una empresa por su nombre ───────────────────

def slugs(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", jr.fold(re.split(r"[/(]", name)[0]))
    cands = ["".join(words), "-".join(words), "".join(w.capitalize() for w in words)]
    return list(dict.fromkeys(c for c in cands if len(c) >= 2))


def same_company(a: str, b: str) -> bool:
    na, nb = (re.sub(r"[^a-z0-9]", "", jr.fold(x)) for x in (a, b))
    return bool(na and nb) and (na in nb or nb in na)


def _try(url: str, params: dict | None = None):
    try:
        return jr.get_json(url, params)
    except (requests.RequestException, ValueError):
        return None


def probe(name: str) -> jr.Target | None:
    """Prueba los portales donde la dirección sale del nombre de la empresa."""
    for slug in slugs(name):
        s = slug.lower()
        meta = _try(f"https://boards-api.greenhouse.io/v1/boards/{s}")
        if isinstance(meta, dict) and same_company(name, meta.get("name", "")):
            return jr.detect_ats(f"https://job-boards.greenhouse.io/{s}", name)
        d = _try(f"https://api.ashbyhq.com/posting-api/job-board/{s}")
        if isinstance(d, dict) and d.get("jobs"):
            return jr.detect_ats(f"https://jobs.ashbyhq.com/{s}", name)
        d = _try(f"https://api.lever.co/v0/postings/{s}", {"mode": "json", "limit": 1})
        if isinstance(d, list) and d:
            return jr.detect_ats(f"https://jobs.lever.co/{s}", name)
        d = _try(f"https://apply.workable.com/api/v1/widget/accounts/{s}")
        if isinstance(d, dict) and (same_company(name, d.get("name", "")) or d.get("jobs")):
            return jr.detect_ats(f"https://apply.workable.com/{s}", name)
    for slug in slugs(name):   # SmartRecruiters responde aunque la empresa no exista: verificamos el nombre
        d = _try(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", {"limit": 1})
        first = ((d or {}).get("content") or [{}])[0]
        if (d or {}).get("totalFound") and same_company(name, (first.get("company") or {}).get("name", "")):
            return jr.detect_ats(f"https://jobs.smartrecruiters.com/{slug}", name)
    return None


def resolve(item: str) -> tuple[jr.Target | None, str]:
    name, forced, url, flags = jr.parse_line(item) if "|" in item else ("", "", item, "")
    looks_like_link = url.lower().startswith("http") or re.match(r"^[\w-]+(\.[\w-]+)+/", url)
    if not looks_like_link:
        t = probe(url)
        return (t, "encontrada por nombre") if t else (None, "no la encontré por nombre")
    if not url.lower().startswith("http"):
        url = "https://" + url
    t = jr.detect_ats(url, name, forced, flags)
    if t:
        if not name:
            t.name = re.sub(r"[-_]+", " ", t.name.split(".")[0]).title()
        return t, "link"
    try:   # página de empleos propia: buscamos adentro un link a un portal conocido
        page = jr.get_text(url).replace("\\/", "/")
    except requests.RequestException:
        return None, "no pude abrir el link"
    for ats, rx in jr.ATS_PATTERNS:
        if ats == "amazon":
            continue
        for m in rx.finditer(page):
            t = jr.detect_ats(m.group(0), name or urlparse(url).netloc.split(".")[-2].title())
            if t:
                return t, "encontrada en la página"
    return None, "ese portal no es compatible"


def key(t: jr.Target) -> str:
    return f"{t.ats}:{t.extra.get('host', '')}:{t.board}".lower()


def check(t: jr.Target) -> str:
    _, jobs, err, _ = jr.run_one(t)
    if err:
        return f"⚠ no respondió ({err})"
    ok = sum(1 for j in jobs if jr.location_ok(j.location) and jr.title_ok(j.title))
    return f"{len(jobs)} vacantes publicadas · {ok} pasan tus filtros hoy"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    jr.PAUSE = (0.2, 0.5)

    changes = {}
    for var, field in (("UBICACIONES", "ubicaciones"), ("PALABRAS", "incluir")):
        if env(var):
            changes[field] = "" if env(var) == "-" else env(var)
    if fold_remoto := jr.fold(env("REMOTO")):
        if fold_remoto in ("latam", "global", "no"):
            changes["remoto"] = fold_remoto
    if changes:
        update_perfil(changes)
    perfil = jr.load_perfil(PERFIL)
    jr.configure(perfil)

    existing = jr.load_targets(COMPANIES) if COMPANIES.exists() else []
    known = {key(t) for t in existing}
    items = [x.strip() for x in re.split(r"[,;\n]", env("EMPRESAS")) if x.strip()]
    rows, new_lines = [], []
    for item in items:
        print(f"→ {item}")
        t, how = resolve(item)
        if not t:
            rows.append(("✗", item, how))
        elif key(t) in known:
            rows.append(("=", t.name, "ya estaba en la lista"))
        else:
            known.add(key(t))
            new_lines.append(t.to_line())
            rows.append(("✓", t.name, f"{t.ats}, {how} · {check(t)}"))
    if new_lines:
        text = COMPANIES.read_text(encoding="utf-8") if COMPANIES.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        COMPANIES.write_text(text + f"\n# ── Agregadas el {date.today()} ──\n" + "\n".join(new_lines) + "\n",
                             encoding="utf-8")

    # ── resumen ──
    remoto_txt = {"latam": "remoto LATAM", "global": "remoto LATAM + global", "no": "sin remoto"}.get(
        jr.fold(perfil.get("remoto", "")), perfil.get("remoto", ""))
    n_inc = len(jr.split_list(perfil.get("incluir", "")))
    lines = ["## Radar configurado", "",
             f"- **Ubicaciones:** {perfil.get('ubicaciones') or '(ninguna)'} · **{remoto_txt}**",
             f"- **Palabras del título:** {n_inc or 'cualquier título'}{' palabras' if n_inc else ''} "
             f"(se editan en `perfil.txt`)",
             f"- **Empresas en la lista:** {len(known)}", ""]
    if rows:
        lines += ["| | Empresa | Resultado |", "|---|---|---|"] + [f"| {a} | {b} | {c} |" for a, b, c in rows]
    if any(r[0] == "✗" for r in rows):
        lines += ["", "**Para las ✗:** abrí una vacante de esa empresa, tocá *Apply* y pegá ese link en el "
                      "formulario. Si aun así no es compatible, el radar no puede leer ese portal."]
    summary = "\n".join(lines)
    print("\n" + summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary + "\n")

    cfg = jr.load_config()
    if cfg.get("telegram_token") and cfg.get("telegram_chat_id"):
        e = lambda s: html.escape(str(s))  # noqa: E731
        msg = [f"⚙️ <b>Radar configurado</b>\n📍 {e(perfil.get('ubicaciones') or '(sin ubicaciones)')} · {e(remoto_txt)}"
               f"\n🏢 {len(known)} empresas en la lista"]
        msg += [f"{a} {e(b)} — {e(c)}" for a, b, c in rows]
        if new_lines:
            msg.append("En unos minutos corre el radar y te manda las vacantes.")
        try:
            jr.tg_send(cfg, "\n\n".join(msg))
        except Exception as ex:  # noqa: BLE001
            print("⚠ No pude avisar por Telegram:", ex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
