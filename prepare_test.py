#!/usr/bin/env python3
"""Prepara um teste a partir de um conjunto de jobs.

1. Lê os jobs (instância no formato do modelo, mesmo com campos em branco ou
   com placeholders como "x", ou uma lista de jobs).
2. Para cada grupo de jobs (rótulo de GPU) e cada tipo de instância candidato,
   deriva dos jobs quais ofertas consultar (quantidades, durações, janela).
3. Consulta DescribeCapacityBlockOfferings (gratuito) e guarda o snapshot.
4. Gera, por grupo, a instância no formato do modelo com os jobs de entrada e
   os capacity_blocks vindos da consulta:
     - no pacote:  results/bundles/<id>/instances/<grupo>.json (usado na importação)
     - uma cópia:  --out (padrão: <arquivo de entrada>_com_blocos.json)

Uso:
    python3 prepare_test.py --jobs jobs.json [--anchor-delay-hours 6] [--out saida.json]

Formato de entrada (a): instância do modelo
    {"solver": "GUROBI", "time_limit_s": x, "verbose": true, "Tmax": , "max_blocks": ,
     "jobs": [{"jid": 0, "m": 4, "p": 4, "gain": 15.16, "r": 0, "d": 534}, ...],
     "capacity_blocks": [...]}            # ignorado
  - só "jobs" é obrigatório; campos de topo inválidos são preenchidos;
  - r, d e p são períodos de --input-unit-minutes (padrão: MODEL_TIME_UNIT_MINUTES);
  - por padrão (DEADLINE_FROM_BLOCKS) o deadline de todos os jobs passa a ser o
    término do último bloco retornado na consulta; o "d" da entrada é ignorado
    e pode ficar em branco (use --deadline-from-input para manter o "d");
  - período 0 = momento da consulta, arredondado para baixo na grade de períodos;
    --anchor-delay-hours desloca r (e d) dos jobs para o futuro;
  - o grupo de GPU vem de --gpu-type (padrão JOBS_DEFAULT_GPU_TYPE).

Formato de entrada (b): lista de jobs
    [{"jid": 1, "gpu_type": "P5", "gpus": 16, "proc_hours": 10, "gain": 880.0,
      "submit_offset_hours": 0, "deadline_offset_hours": 48, "region": "us-east-1"}]
"""
import argparse
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

import config
from collect_offerings import THROTTLE_CODES, cached_query
from common import ec2_client, log, utcnow

UNIT = timedelta(minutes=config.MODEL_TIME_UNIT_MINUTES)


# ---------------------------------------------------------------------------
# Tempo discreto (arredondamento sempre conservador)
# ---------------------------------------------------------------------------
def parse_dt(v):
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def floor_to_unit(d):
    secs = UNIT.total_seconds()
    return datetime.fromtimestamp(math.floor(d.timestamp() / secs) * secs, tz=timezone.utc)


def ceil_to_unit(d):
    secs = UNIT.total_seconds()
    return datetime.fromtimestamp(math.ceil(d.timestamp() / secs) * secs, tz=timezone.utc)


def period_ceil(d, t0):    # início de bloco, liberação de job
    return math.ceil((d - t0) / UNIT - 1e-9)


def period_floor(d, t0):   # fim de bloco, deadline de job
    return math.floor((d - t0) / UNIT + 1e-9)


def from_period(k, t0):
    return t0 + k * UNIT


def query_horizon_end(ref):
    days = min(config.CB_QUERY_HORIZON_DAYS, config.CB_MAX_LOOKAHEAD_DAYS)
    return utcnow() + timedelta(days=days) if ref is None else min(
        ref + timedelta(days=days), utcnow() + timedelta(days=config.CB_MAX_LOOKAHEAD_DAYS))


# ---------------------------------------------------------------------------
# Leitura da entrada (tolerante a campos em branco / placeholders)
# ---------------------------------------------------------------------------
def _extract_array(text, key):
    """Extrai o array JSON de "key" de um texto que pode não ser JSON válido."""
    m = re.search(r'"%s"\s*:\s*\[' % re.escape(key), text)
    if not m:
        raise SystemExit(f'não encontrei "{key}" no arquivo de entrada')
    i = start = m.end() - 1
    depth, in_str, esc = 0, False, False
    while i < len(text):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
        i += 1
    raise SystemExit(f'array "{key}" não termina no arquivo de entrada')


def _scalar(text, key):
    """Valor de um campo simples de topo, se for um literal JSON válido."""
    m = re.search(r'"%s"\s*:\s*("(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null)\s*[,}\n]'
                  % re.escape(key), text)
    return json.loads(m.group(1)) if m else None


def _sanitize(text):
    """Troca valores em branco e placeholders (x, X, ?...) por null."""
    def repl(m):
        word = m.group(1)
        return m.group(0) if word in ("true", "false", "null") else ": null"
    text = re.sub(r":\s*(?=[,}\]])", ": null", text)                      # "Tmax": ,
    text = re.sub(r":\s*([A-Za-z_?][A-Za-z0-9_?]*)\s*(?=[,}\]\n])", repl, text)  # "M": x
    return text


TOP_KEYS = ("solver", "time_limit_s", "verbose", "max_gap", "max_blocks")


def read_input(path):
    """Retorna (jobs_brutos, topo, formato). topo só tem campos válidos (não nulos)."""
    text = Path(path).read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log("aviso: entrada não é JSON válido (campos em branco/placeholders); "
            "tratando esses campos como ausentes")
        try:
            data = json.loads(_sanitize(text))
        except json.JSONDecodeError:
            top = {k: v for k in TOP_KEYS if (v := _scalar(text, k)) is not None}
            return _extract_array(_sanitize(text), "jobs"), top, "model"
    if isinstance(data, list):
        return data, {}, "list"
    top = {k: data[k] for k in TOP_KEYS if data.get(k) is not None}
    return data["jobs"], top, "model"


def model_jobs_to_list(raw, unit_minutes, gpu_type):
    h = unit_minutes / 60.0
    required = ("jid", "m", "p", "gain", "r") + (() if config.DEADLINE_FROM_BLOCKS else ("d",))
    for j in raw:
        for k in required:
            if not isinstance(j.get(k), (int, float)):
                raise SystemExit(f"job {j.get('jid')}: campo '{k}' ausente ou inválido")
    return [{"jid": j["jid"], "gpu_type": gpu_type, "gpus": j["m"],
             "proc_hours": j["p"] * h, "gain": j["gain"],
             "submit_offset_hours": j["r"] * h,
             "deadline_offset_hours": j["d"] * h if isinstance(j.get("d"), (int, float)) else None}
            for j in raw]


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
def resolve_jobs(raw, anchor):
    jobs, seen = [], set()
    for j in raw:
        j = dict(j)
        if j["jid"] in seen:
            raise SystemExit(f"jid duplicado: {j['jid']}")
        seen.add(j["jid"])
        if j.get("instance_type"):
            group, types = j["instance_type"], [j["instance_type"]]
        else:
            group = j.get("gpu_type")
            types = config.GPU_TYPE_TO_INSTANCES.get(group)
            if not types:
                raise SystemExit(f"job {j['jid']}: gpu_type '{group}' fora de GPU_TYPE_TO_INSTANCES")
        missing = [t for t in types if t not in config.GPUS_PER_INSTANCE]
        if missing:
            raise SystemExit(f"GPUS_PER_INSTANCE sem {missing}")
        gpus = int(j["gpus"])
        # tipo serve ao job se o job cabe em uma instância, ou se o tipo é multi-nó
        types = [t for t in types if gpus <= config.GPUS_PER_INSTANCE[t]
                 or config.GPUS_PER_INSTANCE[t] >= config.MIN_GPUS_PER_INSTANCE_FOR_MULTI_NODE]
        if not types:
            raise SystemExit(f"job {j['jid']}: nenhum tipo candidato comporta {gpus} GPUs")
        if "submit" in j:
            sub = parse_dt(j["submit"])
            dl = parse_dt(j["deadline"]) if j.get("deadline") else None
        else:
            sub = anchor + timedelta(hours=float(j["submit_offset_hours"]))
            off = j.get("deadline_offset_hours")
            dl = anchor + timedelta(hours=float(off)) if off is not None else None
        deadline_input = dl.isoformat() if dl else None
        if config.DEADLINE_FROM_BLOCKS:
            # provisório: substituído pelo término do último bloco após a consulta
            dl = query_horizon_end(anchor)
        elif dl is None:
            raise SystemExit(f"job {j['jid']}: sem deadline (use DEADLINE_FROM_BLOCKS)")
        jobs.append({
            "jid": int(j["jid"]), "group": group, "instance_types": types,
            # nº de instâncias on-demand necessárias em cada tipo candidato
            "instances_by_type": {t: math.ceil(gpus / config.GPUS_PER_INSTANCE[t]) for t in types},
            "gpus": gpus, "proc_hours": float(j["proc_hours"]), "gain": float(j["gain"]),
            "submit": sub.isoformat(), "deadline": dl.isoformat(), "deadline_input": deadline_input,
            "region": j.get("region"),
        })
    return jobs


def recompute_gains(jobs):
    """gain = GPUs x horas x menor preço on-demand por GPU-hora entre os candidatos."""
    used = {}
    for j in jobs:
        rates = {t: config.PRICE_PER_INSTANCE_HOUR[t] / config.GPUS_PER_INSTANCE[t]
                 for t in j["instance_types"] if t in config.PRICE_PER_INSTANCE_HOUR}
        if not rates:
            raise SystemExit(f"--recompute-gain: nenhum preço para {j['instance_types']} "
                             f"(rode fetch_ondemand_prices.py ou preencha PRICE_PER_INSTANCE_HOUR)")
        t_min = min(rates, key=rates.get)
        used[t_min] = rates[t_min]
        j["gain_original"] = j["gain"]
        j["gain"] = round(j["gpus"] * j["proc_hours"] * rates[t_min], 2)   # m x p (inalterados) x preço
    for t, r in used.items():
        log(f"--recompute-gain: gain = GPUs x horas x ${r:.3f}/GPU-hora ({t} on-demand)")


# ---------------------------------------------------------------------------
# Grade de consultas derivada dos jobs
# ---------------------------------------------------------------------------
def query_grid(jobs_g, itype, now):
    """Combinações (quantidade, duração) úteis para os jobs de um grupo em um tipo.

    - quantidades: potências de 2 entre a menor necessidade de um job e a soma
      das necessidades de todos, até CB_MAX_INSTANCE_COUNT;
    - durações: dias inteiros entre o menor job e o tamanho da janela (ou
      CB_DURATION_DAYS), até CB_MAX_DURATION_DAYS; só durações que CABEM na janela;
    - poda por carga: descarta (quantidade, duração) cuja capacidade em GPU-horas
      passa de CB_MAX_CAPACITY_RATIO x a carga total dos jobs (blocos que ficariam
      quase todo ociosos). A menor duração de cada quantidade só é descartada se
      também passar do limite;
    - janela: da primeira submissão (ou agora) ao último deadline / horizonte.
    Retorna (combos, start, end, carga_gpu_horas, descartadas_pela_poda).
    """
    need = [j["instances_by_type"][itype] for j in jobs_g if itype in j["instances_by_type"]]
    if not need:
        return [], now, now, 0.0, 0
    lo = next((c for c in config.CB_ALLOWED_COUNTS if c >= min(need)), None)
    hi_need = next((c for c in config.CB_ALLOWED_COUNTS if c >= sum(need)),
                   config.CB_ALLOWED_COUNTS[-1])
    hi = min(hi_need, config.CB_MAX_INSTANCE_COUNT)
    counts = [c for c in config.CB_ALLOWED_COUNTS if lo is not None and lo <= c <= hi]

    start = max(now, min(parse_dt(j["submit"]) for j in jobs_g))
    end = min(max(parse_dt(j["deadline"]) for j in jobs_g),
              now + timedelta(days=config.CB_MAX_LOOKAHEAD_DAYS))
    span_h = (end - start).total_seconds() / 3600
    d_lo = max(1, math.ceil(min(j["proc_hours"] for j in jobs_g) / 24))
    d_fit = math.floor(span_h / 24 + 1e-9)                       # durações que cabem na janela
    d_hi = min(config.CB_MAX_DURATION_DAYS, d_fit)
    if config.CB_DURATION_DAYS:
        days = [d for d in config.CB_DURATION_DAYS if d_lo <= d <= d_hi]
    else:
        days = list(range(d_lo, d_hi + 1))
    days += [d for d in config.CB_EXTRA_DURATION_DAYS if d > d_hi and d <= d_fit]

    gpi = config.GPUS_PER_INSTANCE[itype]
    load = sum(j["gpus"] * j["proc_hours"] for j in jobs_g if itype in j["instances_by_type"])
    ratio = config.CB_MAX_CAPACITY_RATIO
    combos, pruned = [], 0
    for c in counts:
        for d in days:
            if ratio is not None and c * gpi * 24 * d > ratio * load:
                pruned += 1
                continue
            combos.append((c, 24 * d))
    return combos, start, end, load, pruned


def collect_type(itype, jobs_g, now, snapshot, stats):
    combos, start, end, load, pruned = query_grid(jobs_g, itype, now)
    by_count = {}
    for c, h in combos:
        by_count.setdefault(c, []).append(h // 24)
    log(f"  {itype}: carga dos jobs {load:.0f} GPU-h; combinações (instâncias: dias) {by_count}; "
        f"{pruned} podada(s) por excesso de capacidade; janela {start:%Y-%m-%d %H:%M} -> "
        f"{end:%Y-%m-%d %H:%M}; {len(combos) * len(config.CB_REGIONS)} consulta(s)")
    offers = {}
    if start >= end or not combos:
        if not combos:
            log("    nenhuma combinação para consultar (janela curta demais ou poda total)")
        return offers
    for region in config.CB_REGIONS:
        client = ec2_client(region)
        invalid_counts = set()
        for c, h in combos:
            if c in invalid_counts:
                continue
            q = {"region": region, "instance_type": itype, "count": c,
                 "duration_hours": h, "start": start.isoformat(), "end": end.isoformat()}
            try:
                res, from_cache = cached_query(client, region, itype, c, h, start, end, now)
                q["n_offerings"] = len(res)
                q["from_cache"] = from_cache
                stats["cache" if from_cache else "ok"] += 1
                for o in res:
                    o["Region"] = region
                    offers[o["CapacityBlockOfferingId"]] = o
            except (ClientError, BotoCoreError) as e:
                q["error"] = getattr(e, "response", {}).get("Error", {}).get("Code", str(e))
                msg = getattr(e, "response", {}).get("Error", {}).get("Message", "")
                log(f"    {region} {itype} x{c} {h // 24}d: ERRO {q['error']} {msg[:120]}")
                stats["error"] += 1
                if q["error"] == "InvalidParameterValue" and h == min(hh for cc, hh in combos if cc == c):
                    invalid_counts.add(c)   # falhou na menor duração: quantidade não suportada
                    log(f"    pulando as demais durações de {itype} x{c}")
            snapshot["queries"].append(q)
            if not q.get("from_cache"):
                time.sleep(config.CB_SLEEP_BETWEEN_CALLS)
    log(f"    {len(offers)} oferta(s)")
    return offers


# ---------------------------------------------------------------------------
# Instância no formato do modelo
# ---------------------------------------------------------------------------
def build_instance(offerings, jobs_g, t0, top, keep_all=False):
    """{solver, time_limit_s, verbose, Tmax, max_blocks, jobs, capacity_blocks}
    capacity_blocks: {bid, M, Sb, duration, cost}, M = InstanceCount x GPUs por instância.
    """
    mjobs = [{
        "jid": j["jid"], "m": j["gpus"],
        "p": math.ceil(j["proc_hours"] * 60 / config.MODEL_TIME_UNIT_MINUTES - 1e-9),
        "gain": j["gain"],
        "r": max(0, period_ceil(parse_dt(j["submit"]), t0)),
        "d": period_floor(parse_dt(j["deadline"]), t0),
    } for j in jobs_g]

    blocks, block_map, dropped = [], {}, []
    latest_start = max((j["d"] - j["p"] for j in mjobs), default=0)
    min_m = min((j["m"] for j in mjobs), default=0)
    for o in sorted(offerings, key=lambda o: (o["InstanceType"], str(o["StartDate"]),
                                              o["InstanceCount"], o["AvailabilityZone"])):
        s = max(0, period_ceil(parse_dt(o["StartDate"]), t0))
        e = period_floor(parse_dt(o["EndDate"]), t0)
        M = int(o["InstanceCount"]) * config.GPUS_PER_INSTANCE[o["InstanceType"]]
        reason = None
        if e <= s:
            reason = "sem_duracao_apos_arredondamento"   # termina antes do período 0 ou < 1 período
        elif not any(j["m"] <= M and max(j["r"], s) + j["p"] <= min(j["d"], e) for j in mjobs):
            if M < min_m:
                reason = "capacidade_menor_que_qualquer_job"
            elif s > latest_start:
                reason = "comeca_depois_do_ultimo_inicio_possivel"
            else:
                reason = "nenhum_job_cabe_na_janela_do_bloco"
        if reason and not keep_all:
            dropped.append({"offering_id": o.get("CapacityBlockOfferingId"),
                            "instance_type": o["InstanceType"], "instance_count": o["InstanceCount"],
                            "az": o.get("AvailabilityZone"), "start": str(o["StartDate"]),
                            "end": str(o["EndDate"]), "Sb": s, "end_period": e, "M": M,
                            "reason": reason})
            continue
        bid = f"cb{len(blocks) + 1}"
        blocks.append({"bid": bid, "M": M, "Sb": s, "duration": max(e - s, 0),
                       "cost": round(float(o["UpfrontFee"]), 2)})
        block_map[bid] = {k: o.get(k) for k in (
            "CapacityBlockOfferingId", "Region", "AvailabilityZone", "InstanceType",
            "InstanceCount", "StartDate", "EndDate", "UpfrontFee", "CurrencyCode",
            "CapacityBlockDurationHours")}
        real_h = (parse_dt(o["EndDate"]) - parse_dt(o["StartDate"])).total_seconds() / 3600
        model_h = max(e - s, 0) * config.MODEL_TIME_UNIT_MINUTES / 60
        block_map[bid]["real_duration_hours"] = round(real_h, 4)
        block_map[bid]["model_duration_hours"] = model_h
        block_map[bid]["rounding_lost_hours"] = round(real_h - model_h, 4)

    settings = {**config.MODEL_SETTINGS, **top}   # campos válidos da entrada prevalecem
    max_blocks = settings.get("max_blocks")
    if not isinstance(max_blocks, int) or max_blocks <= 0:
        max_blocks = len(blocks)
    instance = {
        "solver": settings.get("solver", "GUROBI"),
        "time_limit_s": settings.get("time_limit_s"),
        "verbose": settings.get("verbose", True),
        "Tmax": max([b["Sb"] + b["duration"] for b in blocks] + [j["d"] for j in mjobs] + [1]),
        "max_blocks": max_blocks,
    }
    if settings.get("max_gap") is not None:
        instance["max_gap"] = settings["max_gap"]
    instance["jobs"] = mjobs
    instance["capacity_blocks"] = blocks
    return instance, block_map, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--out", help="cópia da instância com os blocos "
                                  "(padrão: <entrada>_com_blocos.json)")
    ap.add_argument("--anchor-delay-hours", type=float, default=0.0,
                    help="desloca os jobs para o futuro (tempo para rodar o modelo)")
    ap.add_argument("--input-unit-minutes", type=float, default=config.MODEL_TIME_UNIT_MINUTES,
                    help="duração de 1 período na instância de entrada")
    ap.add_argument("--gpu-type", default=config.JOBS_DEFAULT_GPU_TYPE,
                    help="grupo (GPU_TYPE_TO_INSTANCES) dos jobs vindos de uma instância do modelo")
    ap.add_argument("--recompute-gain", action="store_true",
                    help="recalcula gain com o preço on-demand atual")
    ap.add_argument("--keep-all-offerings", action="store_true",
                    help="não descarta ofertas que não servem a nenhum job")
    ap.add_argument("--deadline-from-input", action="store_true",
                    help='usa o "d" da entrada em vez do término do último bloco')
    args = ap.parse_args()
    if args.deadline_from_input:
        config.DEADLINE_FROM_BLOCKS = False

    if args.input_unit_minutes != config.MODEL_TIME_UNIT_MINUTES:
        log(f"aviso: entrada em períodos de {args.input_unit_minutes} min, saída em "
            f"{config.MODEL_TIME_UNIT_MINUTES} min (MODEL_TIME_UNIT_MINUTES)")

    now = utcnow()
    raw, top, fmt = read_input(args.jobs)
    # período 0 = momento da consulta, arredondado para baixo na grade de períodos
    t0 = floor_to_unit(now)
    if fmt == "model":
        # r, d e p da entrada são preservados (deslocados só por --anchor-delay-hours)
        anchor = ceil_to_unit(t0 + timedelta(hours=args.anchor_delay_hours))
        raw = model_jobs_to_list(raw, args.input_unit_minutes, args.gpu_type)
    else:
        anchor = now + timedelta(hours=args.anchor_delay_hours)
    jobs = resolve_jobs(raw, anchor)
    if args.recompute_gain:
        recompute_gains(jobs)
    if anchor > t0:
        log(f"jobs deslocados em {round((anchor - t0) / UNIT)} período(s) por --anchor-delay-hours")
    log(f"{len(jobs)} job(s); período 0 = {t0:%Y-%m-%d %H:%M} UTC (momento da consulta); "
        f"1 período = {config.MODEL_TIME_UNIT_MINUTES} min; deadline = "
        + ("término do último bloco consultado" if config.DEADLINE_FROM_BLOCKS else "'d' da entrada"))

    bundle = Path(config.BUNDLES_DIR) / now.strftime("%Y%m%dT%H%M%SZ")
    (bundle / "instances").mkdir(parents=True, exist_ok=True)
    snapshot = {"collected_at": now.isoformat(), "queries": [], "offerings": []}
    manifest = {"created_at": now.isoformat(), "t0": t0.isoformat(),
                "time_unit_minutes": config.MODEL_TIME_UNIT_MINUTES,
                "anchor": anchor.isoformat(), "regions": config.CB_REGIONS,
                "input_file": str(args.jobs), "groups": {}}
    groups = sorted({j["group"] for j in jobs})
    copies = []
    stats = {"ok": 0, "cache": 0, "error": 0}

    for group in groups:
        jobs_g = [j for j in jobs if j["group"] == group]
        types = []
        for j in jobs_g:
            types += [t for t in j["instance_types"] if t not in types]
        log(f"grupo {group}: {len(jobs_g)} job(s), candidatos {types}")
        partial = {t: sum(1 for j in jobs_g if t not in j["instance_types"]) for t in types}
        partial = {t: n for t, n in partial.items() if n}
        if partial:
            log(f"  AVISO: tipos que não servem a todos os jobs do grupo {partial}; o modelo "
                f"não distingue compatibilidade por job e pode alocá-los mesmo assim")
        offers = {}
        for itype in types:
            offers.update(collect_type(itype, jobs_g, now, snapshot, stats))
        snapshot["offerings"].extend(offers.values())

        group_deadline = None
        if config.DEADLINE_FROM_BLOCKS:
            if offers:
                last_end = max(parse_dt(o["EndDate"]) for o in offers.values())
                d_period = period_floor(last_end, t0)
                group_deadline = from_period(d_period, t0)
                for j in jobs_g:
                    j["deadline"] = group_deadline.isoformat()
                log(f"  deadline dos jobs = término do último bloco retornado: período {d_period} "
                    f"({last_end:%Y-%m-%d %H:%M} UTC, arredondado para {group_deadline:%Y-%m-%d %H:%M})")
            else:
                log("  AVISO: nenhuma oferta retornada; deadline fica no horizonte da consulta")

        instance, block_map, dropped = build_instance(offers.values(), jobs_g, t0, top, args.keep_all_offerings)
        if instance["time_limit_s"] is None:
            instance["time_limit_s"] = config.MODEL_SETTINGS.get("time_limit_s", 120)
        inst_file = f"instances/{group}.json"
        (bundle / inst_file).write_text(json.dumps(instance, indent=2))

        # cópia no formato do modelo, ao lado da entrada (ou em --out)
        if args.out:
            out = Path(args.out)
            if len(groups) > 1:
                out = out.with_name(f"{out.stem}_{group}{out.suffix}")
        else:
            src = Path(args.jobs)
            suffix = f"_{group}" if len(groups) > 1 else ""
            out = src.with_name(f"{src.stem}_com_blocos{suffix}.json")
        out.write_text(json.dumps(instance, indent=2))
        copies.append(out)

        per_type = {}
        for b in block_map.values():
            per_type[b["InstanceType"]] = per_type.get(b["InstanceType"], 0) + 1
        manifest["groups"][group] = {
            "instance_file": inst_file, "copy_file": str(out), "instance_types": types,
            "n_jobs": len(jobs_g), "n_offerings": len(offers),
            "n_blocks": len(instance["capacity_blocks"]), "n_blocks_by_type": per_type,
            "deadline_mode": "last_block_end" if config.DEADLINE_FROM_BLOCKS else "input",
            "deadline": group_deadline.isoformat() if group_deadline else None,
            "n_dropped": len(dropped), "dropped": dropped, "block_map": block_map}

        gpu_periods = sum(j["m"] * j["p"] for j in instance["jobs"])
        jobs_rate = sum(j["gain"] for j in instance["jobs"]) / max(1, gpu_periods)
        cheapest = min((b["cost"] / (b["M"] * b["duration"]) for b in instance["capacity_blocks"]),
                       default=None)
        log(f"  {len(offers)} oferta(s) consultada(s) -> {len(instance['capacity_blocks'])} bloco(s) "
            f"na instância {per_type}; {len(dropped)} descartada(s); "
            f"Tmax={instance['Tmax']}; max_blocks={instance['max_blocks']}")
        lost = [b["rounding_lost_hours"] for b in block_map.values() if b["rounding_lost_hours"] > 1e-6]
        odd = sum(1 for b in block_map.values() if abs(b["real_duration_hours"] % 24) > 1e-6
                  and abs(b["real_duration_hours"] % 24 - 24) > 1e-6)
        if odd:
            log(f"    {odd} bloco(s) com duração real que não é múltiplo de 24 h")
        if lost:
            log(f"    arredondamento para períodos inteiros reduziu {len(lost)} bloco(s) "
                f"(perda média {sum(lost) / len(lost):.2f} h, máx {max(lost):.2f} h); "
                f"custo mantido integral; datas reais em manifest.json -> block_map")
        reasons = {}
        for d in dropped:
            reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
        for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            log(f"    descartadas por {r}: {n}")
        if dropped:
            log(f"    detalhes em manifest.json -> groups.{group}.dropped")
        log(f"  ganho médio dos jobs: ${jobs_rate:.3f}/GPU-período; bloco mais barato: "
            + (f"${cheapest:.3f}/GPU-período" if cheapest else "n/a"))
        if cheapest and cheapest > jobs_rate:
            log("  AVISO: todo bloco custa mais por GPU-período que o ganho médio dos jobs; "
                "o modelo tende a não reservar nada (considere --recompute-gain)")
        if not instance["capacity_blocks"]:
            log("  AVISO: nenhuma oferta útil encontrada; capacity_blocks está vazio")

    (bundle / "jobs_resolved.json").write_text(json.dumps(jobs, indent=2))
    (bundle / "offerings_snapshot.json").write_text(json.dumps(snapshot, indent=2, default=str))
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    (bundle / "solutions").mkdir(exist_ok=True)
    log(f"consultas: {stats['ok']} na API, {stats['cache']} do cache, {stats['error']} com erro")
    if stats["error"]:
        log(f"AVISO: {stats['error']} consulta(s) falharam; a instância está INCOMPLETA "
            f"(e o deadline pode estar subestimado). Rode de novo em alguns minutos: "
            f"o que já foi consultado vem do cache (validade {config.CB_CACHE_MAX_AGE_HOURS} h)")
    log(f"pacote pronto em {bundle}")
    for c in copies:
        log(f"instância com os blocos consultados: {c}")
    log(f"rode o modelo e salve a solução em {bundle}/solutions/<grupo>.json "
        f"(grupo(s): {', '.join(groups)})")
    first_start = min(parse_dt(j["submit"]) for j in jobs)
    log(f"importe antes de {first_start:%Y-%m-%d %H:%M} UTC (primeira submissão) "
        f"para não perder o início das janelas on-demand")


if __name__ == "__main__":
    main()