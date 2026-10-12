#!/usr/bin/env python3
"""Snapshot das ofertas de Capacity Blocks (gratuito; nada é comprado).

Também fornece a função `query`, usada pelo prepare_test.py.

Registra as consultas que retornaram zero ofertas, porque "não havia bloco"
também é um dado de disponibilidade.

Uso:
    python3 collect_offerings.py                       # alvos de CB_TARGETS (config.py)
    python3 collect_offerings.py --instance-types p5.48xlarge p5e.48xlarge \\
        --regions us-east-1 us-east-2 --counts 1 2 4 8 --durations-days 1 2 7 \\
        --horizon-days 14 [--out arquivo.json]

Saída (results/offerings/<timestamp>.json, ou --out):
    {"collected_at", "horizon_start", "horizon_end",
     "queries":   [{region, instance_type, count, duration_hours, n_offerings | error}],
     "offerings": [ofertas da API + "Region"],
     "summary":   {tipo: {"n_offerings", "min_price_per_instance_hour"}}}
"""
import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

import config
from common import ec2_client, log, utcnow


THROTTLE_CODES = {"CapacityBlockDescribeLimitExceeded", "RequestLimitExceeded",
                  "Throttling", "ThrottlingException"}


def _describe_with_retry(client, **kw):
    """Uma chamada, com espera crescente nos erros de limite da API."""
    wait = config.CB_THROTTLE_BACKOFF_START_S
    for attempt in range(config.CB_THROTTLE_MAX_RETRIES + 1):
        try:
            return client.describe_capacity_block_offerings(**kw)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code not in THROTTLE_CODES or attempt == config.CB_THROTTLE_MAX_RETRIES:
                raise
            log(f"    limite da API ({code}); aguardando {wait}s "
                f"(tentativa {attempt + 1}/{config.CB_THROTTLE_MAX_RETRIES})")
            time.sleep(wait)
            wait = min(wait * 2, config.CB_THROTTLE_BACKOFF_MAX_S)


def query(client, itype, count, hours, start, end):
    """Todas as ofertas (com paginação) para tipo, quantidade, duração e janela."""
    out, token = [], None
    while True:
        kw = dict(InstanceType=itype, InstanceCount=count, CapacityDurationHours=hours,
                  StartDateRange=start, EndDateRange=end)
        if token:
            kw["NextToken"] = token
        r = _describe_with_retry(client, **kw)
        out.extend(r.get("CapacityBlockOfferings", []))
        token = r.get("NextToken")
        if not token:
            return out


# ---------------------------------------------------------------------------
# Cache de consultas bem-sucedidas
# ---------------------------------------------------------------------------
def _cache_path(region, itype, count, hours):
    return Path(config.CB_CACHE_DIR) / f"{region}_{itype}_{count}_{hours}.json"


def _to_dt(v, tz):
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=tz)


def cache_get(region, itype, count, hours, now, start, end):
    """Ofertas em cache, só se forem recentes E a janela em cache cobrir a pedida."""
    p = _cache_path(region, itype, count, hours)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        age_h = (now - datetime.fromisoformat(data["collected_at"])).total_seconds() / 3600
        if age_h > config.CB_CACHE_MAX_AGE_HOURS:
            return None
        c_start = _to_dt(data["window_start"], now.tzinfo)
        c_end = _to_dt(data["window_end"], now.tzinfo)
        # a janela em cache precisa conter a pedida (início pode ter avançado com o tempo)
        if c_end < end or c_start > start:
            return None
        return data["offerings"]
    except (ValueError, KeyError):
        return None


def cache_put(region, itype, count, hours, now, offerings, start, end):
    p = _cache_path(region, itype, count, hours)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"collected_at": now.isoformat(), "window_start": start.isoformat(),
                             "window_end": end.isoformat(), "offerings": offerings}, default=str))


def cached_query(client, region, itype, count, hours, start, end, now):
    """(ofertas, veio_do_cache). Ofertas do cache que começam antes de `start`
    ou terminam depois de `end` são filtradas."""
    cached = cache_get(region, itype, count, hours, now, start, end)
    if cached is not None:
        return [o for o in cached if _to_dt(o["StartDate"], start.tzinfo) >= start
                and _to_dt(o["EndDate"], start.tzinfo) <= end], True
    res = query(client, itype, count, hours, start, end)
    cache_put(region, itype, count, hours, now, res, start, end)
    return res, False


def build_targets(args):
    """Alvos a partir dos argumentos; sem argumentos, usa CB_TARGETS."""
    if not args.instance_types:
        return config.CB_TARGETS
    regions = args.regions or config.CB_REGIONS
    counts = args.counts or [1, 2, 4, 8]
    days = args.durations_days or [1, 2, 3]
    return [{"region": r, "instance_type": t, "counts": counts,
             "durations_hours": [24 * d for d in days]}
            for r in regions for t in args.instance_types]


def _price_per_instance_hour(o):
    try:
        start = datetime.fromisoformat(str(o["StartDate"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(o["EndDate"]).replace("Z", "+00:00"))
        hours = (end - start).total_seconds() / 3600
        return float(o["UpfrontFee"]) / (int(o["InstanceCount"]) * hours)
    except (KeyError, ValueError, ZeroDivisionError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance-types", nargs="*")
    ap.add_argument("--regions", nargs="*")
    ap.add_argument("--counts", nargs="*", type=int)
    ap.add_argument("--durations-days", nargs="*", type=int)
    ap.add_argument("--horizon-days", type=float, default=config.CB_HORIZON_DAYS,
                    help="janela de início a partir de agora (a API vai até ~8 semanas)")
    ap.add_argument("--out")
    args = ap.parse_args()

    now = utcnow()
    end = now + timedelta(days=min(args.horizon_days, config.CB_MAX_LOOKAHEAD_DAYS))
    targets = build_targets(args)
    n_calls = sum(len(t["counts"]) * len(t["durations_hours"]) for t in targets)
    log(f"{len(targets)} alvo(s), até {n_calls} consulta(s); janela "
        f"{now:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC")

    snap = {"collected_at": now.isoformat(), "horizon_start": now.isoformat(),
            "horizon_end": end.isoformat(), "queries": [], "offerings": [], "summary": {}}
    seen = set()

    for t in targets:
        region, itype = t["region"], t["instance_type"]
        client = ec2_client(region)
        for count in t["counts"]:
            for i, hours in enumerate(t["durations_hours"]):
                q = {"region": region, "instance_type": itype, "count": count,
                     "duration_hours": hours}
                try:
                    offs, from_cache = cached_query(client, region, itype, count, hours,
                                                    now, end, now)
                    q["n_offerings"] = len(offs)
                    q["from_cache"] = from_cache
                    for o in offs:
                        if o["CapacityBlockOfferingId"] in seen:
                            continue
                        seen.add(o["CapacityBlockOfferingId"])
                        o["Region"] = region
                        snap["offerings"].append(o)
                    log(f"{region} {itype} x{count} {hours // 24}d: {len(offs)} oferta(s)"
                        + (" (cache)" if from_cache else ""))
                    if from_cache:
                        continue
                except (ClientError, BotoCoreError) as e:
                    q["error"] = getattr(e, "response", {}).get("Error", {}).get("Code", str(e))
                    log(f"{region} {itype} x{count} {hours // 24}d: ERRO {q['error']}")
                    snap["queries"].append(q)
                    if q["error"] == "InvalidParameterValue" and i == 0:
                        # provavelmente quantidade não suportada para o tipo/região
                        log(f"  pulando as demais durações de {itype} x{count} em {region}")
                        break
                    time.sleep(config.CB_SLEEP_BETWEEN_CALLS)
                    continue
                snap["queries"].append(q)
                time.sleep(config.CB_SLEEP_BETWEEN_CALLS)

    for o in snap["offerings"]:
        s = snap["summary"].setdefault(o["InstanceType"],
                                       {"n_offerings": 0, "min_price_per_instance_hour": None})
        s["n_offerings"] += 1
        p = _price_per_instance_hour(o)
        if p is not None and (s["min_price_per_instance_hour"] is None
                              or p < s["min_price_per_instance_hour"]):
            s["min_price_per_instance_hour"] = round(p, 4)

    out = Path(args.out or f"{config.OFFERINGS_DIR}/{now.strftime('%Y%m%dT%H%M%SZ')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=2, default=str))
    for itype, s in sorted(snap["summary"].items()):
        price = s["min_price_per_instance_hour"]
        log(f"{itype}: {s['n_offerings']} oferta(s); menor preço "
            + (f"${price:.2f}/instância-hora" if price is not None else "n/a"))
    errors = sum(1 for q in snap["queries"] if "error" in q)
    log(f"{len(snap['offerings'])} oferta(s) salvas em {out} ({errors} consulta(s) com erro)")
    if errors:
        log("rode de novo para completar: as consultas bem-sucedidas vêm do cache")
    print(out)


if __name__ == "__main__":
    main()