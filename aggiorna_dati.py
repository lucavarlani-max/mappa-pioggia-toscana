#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
aggiorna_dati.py — Scarica i dati pluviometrici REALI del Mugello dal
Servizio Idrologico Regionale (SIR) della Toscana e genera:
  - stations.json : dati compatti (cumulati per periodo + serie giornaliera)
  - dati.js       : lo stesso contenuto come  window.DATI_PIOGGIA = {...};
                    (usato da index.html; funziona aprendo il file col doppio click)

Fonte dati: http://www.sir.toscana.it/  (archivio, aggregazione giornaliera) — licenza CC-BY-SA.
Nessuna chiave necessaria. Richiede solo la libreria 'requests':  pip install requests

Uso:
  python aggiorna_dati.py                 # finestra ultimi 45 giorni
  python aggiorna_dati.py --giorni 90     # finestra piu' ampia (date libere piu' lunghe)
"""

import json, os, sys, time, argparse, datetime as dt
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ImportError:
    sys.exit("Manca la libreria 'requests'. Installala con:  pip install requests")

BASE = "http://www.sir.toscana.it"
STATIONS_URL = BASE + "/archivio/dati.php?D=json_stations"

# Copertura: tutta la Toscana (tutti i pluviometri attivi nell'anno corrente).
# Per limitare a una o piu' province, valorizza PROVINCE (es. {"FI","PO","PT"});
# lascia vuoto per l'intera regione.
PROVINCE = set()
# Per limitare a specifici comuni (es. il solo Mugello), valorizza COMUNI;
# lascia vuoto per non filtrare per comune.
COMUNI = set()
PLUVIO_KEYS = [
    "PLUVIOMETRIA - Aggregazione a 24 ore (9-9)",
    "PLUVIOMETRIA - Aggregazione a 24 ore (0-24)",
]
PERIODI = [1, 7, 10, 15, 20, 30]
# Sicurezza: se il numero di stazioni valide scaricate e' inferiore a questa
# soglia (es. SIR irraggiungibile), NON sovrascrive i dati buoni esistenti.
MIN_STAZIONI_OK = 30


def log(*a):
    print(*a, flush=True)


def get_json(url, tries=3, timeout=40):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2)
    raise last


def abs_url(sd):
    if sd.startswith("http"):
        return sd
    return BASE + "/" + sd.lstrip("/")


def channel_years(ch):
    """Elenco anni disponibili per un canale di misura."""
    a = ch.get("Anni")
    if isinstance(a, list) and a and isinstance(a[0], dict):
        return {str(v) for v in a[0].values()}
    return set()


def pick_source(cons, year):
    """Restituisce (chiave, SorgenteDati) del primo canale pluviometrico
    che risulta attivo nell'anno indicato."""
    if not isinstance(cons, dict):
        return None
    for k in PLUVIO_KEYS:
        ch = cons.get(k)
        if isinstance(ch, dict) and ch.get("SorgenteDati") and str(year) in channel_years(ch):
            return k, ch["SorgenteDati"]
    return None


def build_station_list():
    log("Scarico l'elenco stazioni SIR...")
    gj = get_json(STATIONS_URL, timeout=90)
    feats = gj.get("features", [])
    year = dt.date.today().year
    out = []
    for f in feats:
        p = f.get("properties", {})
        comune = (p.get("Comune") or "").strip()
        if COMUNI and comune not in COMUNI:
            continue
        if PROVINCE and p.get("Provincia") not in PROVINCE:
            continue
        src = pick_source(p.get("Consistenza"), year)
        if not src:
            continue
        geom = f.get("geometry", {}).get("coordinates")
        if not geom:
            continue
        lon, lat = geom[0], geom[1]
        out.append({
            "cod": p.get("Codice"),
            "nome": p.get("Nome"),
            "com": (p.get("Comune") or "").strip(),
            "prov": p.get("Provincia"),
            "q": int(round(float(p.get("Quota mslm") or 0))),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "sd": abs_url(src[1]),
        })
    # dedup per nome
    seen, ded = set(), []
    for s in out:
        if s["nome"] in seen:
            continue
        seen.add(s["nome"])
        ded.append(s)
    log(f"  stazioni pluviometriche nell'area: {len(ded)}")
    return ded


def fetch_series(st, cutoff):
    try:
        d = get_json(st["sd"], timeout=40)
        rows = d.get("properties", {}).get("SerieDati", []) or []
        daily = {}
        for r in rows:
            day = (r.get("Data") or "")[:10]
            if day < cutoff:
                continue
            v = r.get("Valore")
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = None
            daily[day] = v
        return st["cod"], daily
    except Exception as e:
        log(f"  ! errore su {st['nome']}: {e}")
        return st["cod"], {}


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--giorni", type=int, default=45, help="ampiezza finestra dati (default 45)")
    ap.add_argument("--min-copertura", type=int, default=20,
                    help="giorni minimi con dato negli ultimi 30 per includere la stazione")
    ap.add_argument("--min-stazioni", type=int, default=MIN_STAZIONI_OK,
                    help="sotto questa soglia di stazioni valide NON sovrascrive i dati esistenti")
    args = ap.parse_args()

    today = dt.date.today()
    end = today - dt.timedelta(days=1)          # ieri: si esclude la giornata odierna
    start = end - dt.timedelta(days=args.giorni - 1)
    cutoff = start.isoformat()
    axis = [d.isoformat() for d in daterange(start, end)]

    stations = build_station_list()

    log("Scarico le serie giornaliere (puo' richiedere qualche minuto)...")
    series = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        for cod, daily in ex.map(lambda s: fetch_series(s, cutoff), stations):
            series[cod] = daily

    # finestra copertura ultimi 30 gg
    win30 = set(axis[-30:])

    def period_sum(daily, days):
        s, any_ = 0.0, False
        for k in range(days):
            day = (end - dt.timedelta(days=k)).isoformat()
            v = daily.get(day)
            if v is not None:
                s += v
                any_ = True
        return round(s, 1) if any_ else 0.0

    good = []
    for st in stations:
        daily = series.get(st["cod"], {})
        cov = sum(1 for d in win30 if daily.get(d) is not None)
        if cov < args.min_copertura:
            continue
        v = [daily.get(d) for d in axis]
        mm = {str(p): period_sum(daily, p) for p in PERIODI}
        good.append({
            "nome": st["nome"], "com": st["com"], "prov": st["prov"],
            "q": st["q"], "lat": st["lat"], "lon": st["lon"],
            "mm": mm, "v": v,
        })

    good.sort(key=lambda s: (s["prov"], s["nome"]))
    if COMUNI:
        cop = "Comuni: " + ", ".join(sorted(COMUNI))
    elif PROVINCE:
        cop = "Province: " + ", ".join(sorted(PROVINCE))
    else:
        cop = "Tutta la Toscana"
    payload = {
        "generato": dt.datetime.now().isoformat(timespec="seconds"),
        "fonte": "Servizio Idrologico Regionale (SIR) - Regione Toscana, archivio dati (aggregazione giornaliera)",
        "fonte_url": BASE + "/",
        "aggregazione": "Precipitazione giornaliera cumulata (mm). I dati escludono la giornata odierna.",
        "ultimo_giorno": end.isoformat(),
        "copertura": cop,
        "n_stazioni": len(good),
        "periodi": PERIODI,
        "date": axis,
        "stazioni": good,
    }

    # --- Guardia di sicurezza: non sovrascrivere dati buoni con un risultato vuoto/parziale ---
    if len(good) < args.min_stazioni:
        log(f"\n[ATTENZIONE] Solo {len(good)} stazioni valide (soglia {args.min_stazioni}).")
        log("Il SIR potrebbe essere irraggiungibile o lento. I dati esistenti NON sono stati toccati.")
        log("Riprova piu' tardi. (Nessuna modifica a stations.json / dati.js)")
        sys.exit(2)

    js = "window.DATI_PIOGGIA = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n"
    sj = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def save_atomic(path, content):
        # backup del file buono precedente, poi scrittura atomica su file temporaneo
        if os.path.exists(path):
            try:
                os.replace(path, path + ".bak")
            except OSError:
                pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)

    save_atomic("stations.json", sj)
    save_atomic("dati.js", js)

    log(f"\nFatto. {len(good)} stazioni con dati recenti.")
    log(f"Periodo: {axis[0]} -> {axis[-1]}")
    log("File aggiornati: stations.json, dati.js  (backup: *.bak)")
    log("Apri index.html per vedere la mappa.")


if __name__ == "__main__":
    main()
