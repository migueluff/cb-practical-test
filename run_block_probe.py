#!/usr/bin/env python3
"""Teste por bloco: para cada bloco escolhido pelo modelo, tenta uma ODCR do
MESMO tamanho (tipo e nº de instâncias), cancela na hora e para no primeiro
sucesso. Sem sucesso, tenta de novo a cada OD_RETRY_MINUTES até o início do
bloco (+ BLOCK_EXTRA_WAIT_HOURS).

Sucesso em t significa: se a reserva tivesse sido mantida, o recurso on-demand
equivalente ao bloco estaria garantido a partir de t.

Métricas por bloco e modo (results/block_results.csv):
  wait_minutes                 tempo do início do teste até conseguir
  lead_hours_vs_block_start    início do bloco - momento em que conseguiu
                               (> 0: o on-demand ficou disponível antes do bloco)
  block_upfront_fee            custo do bloco
  od_cost_block_duration       preço on-demand x instâncias x duração do bloco
  od_hold_cost_until_start     custo de manter a ODCR de t até o início do bloco
                               (para garantir o recurso no mesmo horário do bloco)

Uso:
    python3 run_block_probe.py            # cria as tarefas (se preciso) e executa
    python3 run_block_probe.py --reset    # recria as tarefas a partir do decision.json
    python3 run_block_probe.py --summary  # só resumo + CSV
Para parar: Ctrl+C ou `touch STOP` (o estado é salvo; basta rodar de novo).
"""
import argparse
import csv
import json
import os
import random
import signal
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import config
import probe_odcr as P
from common import (all_probe_regions, cancel_reservation, cleanup_region, ec2_client,
                    log, utcnow)
from plan_ondemand import region_azs

HOUR = 3600.0


# ---------------------------------------------------------------------------
# Tarefas
# ---------------------------------------------------------------------------
def build_tasks(decision, now):
    tasks, cache = [], {}
    extra = timedelta(hours=config.BLOCK_EXTRA_WAIT_HOURS)
    if not decision["blocks"]:
        raise SystemExit("decision.json não tem blocos escolhidos; nada a testar.")
    for b in decision["blocks"]:
        itype = b["instance_type"]
        if itype not in config.PRICE_PER_INSTANCE_HOUR:
            raise SystemExit(f"Preço de {itype} ausente em PRICE_PER_INSTANCE_HOUR.")
        azs = region_azs(b["region"], itype, cache)
        start = datetime.fromisoformat(b["start"])
        for mode in config.BLOCK_MODES:
            if mode == "same_az":
                pools = [{"az": b["az"], "az_id": azs.get(b["az"])}]
            elif mode == "region":
                pools = [{"az": az, "az_id": az_id} for az, az_id in azs.items()]
            else:
                raise SystemExit(f"modo desconhecido em BLOCK_MODES: {mode}")
            tasks.append({
                "task_id": f"{b['block_id']}:{mode}", "block_id": b["block_id"], "mode": mode,
                "offering_id": b.get("offering_id"), "region": b["region"],
                "instance_type": itype, "instances": int(b["instance_count"]),
                "block_az": b["az"], "pools": pools,
                "block_start": b["start"], "block_end": b["end"],
                "block_upfront_fee": float(b["upfront_fee"]),
                "give_up_at": (start + extra).isoformat(),
                "next_attempt": now.isoformat(),
                "status": "pending", "attempts": 0, "first_attempt_at": None,
                "acquired_at": None, "acquired_az": None, "last_error": None,
            })
    return tasks


def load_tasks():
    return json.loads(Path(config.BLOCK_TASKS_FILE).read_text())


def save_tasks(tasks):
    p = Path(config.BLOCK_TASKS_FILE)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(tasks, indent=2))
    os.replace(tmp, p)


def log_attempt(task, rec):
    with open(config.BLOCK_ATTEMPTS_FILE, "a") as f:
        f.write(json.dumps({"task_id": task["task_id"], "attempt": task["attempts"], **rec}) + "\n")


# ---------------------------------------------------------------------------
# Tentativa
# ---------------------------------------------------------------------------
def attempt(task, budget, run_id):
    """Uma tentativa em todos os pools da tarefa. Atualiza o status da tarefa."""
    pools = list(task["pools"])
    random.shuffle(pools)
    task["attempts"] += 1
    for pl in pools:
        if P.should_stop():
            return
        expected = P.est_cost(task["instance_type"], task["instances"], 0)
        if budget["spent_estimate_usd"] + expected > config.BUDGET_USD_TOTAL:
            P.STOP["requested"], P.STOP["reason"] = True, "orçamento total atingido"
            return
        pool = {"region": task["region"], "az": pl["az"], "az_id": pl["az_id"],
                "instance_type": task["instance_type"]}
        rec = P.probe(pool, task["instances"], run_id, task["attempts"])
        log_attempt(task, rec)
        log(f"{task['task_id']:<28} tentativa {task['attempts']:<4} {pl['az']:<14} "
            f"{task['instance_type']} x{task['instances']} -> {rec['result']} "
            f"{rec['error_code'] or ''}")
        if rec["est_cost_usd"]:
            budget["spent_estimate_usd"] += rec["est_cost_usd"]
            budget["activated_reservations"] += 1
            P.save_budget(budget)
        if rec["result"] == "available":
            task.update(status="acquired", acquired_at=utcnow().isoformat(), acquired_az=pl["az"])
            return
        if rec["result"] == "quota":
            # quota é da conta: tentar de novo não muda nada, e não é falta de capacidade
            task.update(status="quota_blocked", last_error=rec["error_code"])
            log(f"  quota insuficiente para {task['instances']} x {task['instance_type']}; "
                f"peça aumento da quota e rode com --reset")
            return
        if rec["result"] in ("error", "unsupported"):
            task["last_error"] = rec["error_code"]
        time.sleep(config.SLEEP_BETWEEN_CALLS_SECONDS)


# ---------------------------------------------------------------------------
# Resultados
# ---------------------------------------------------------------------------
def metrics(t):
    price = config.PRICE_PER_INSTANCE_HOUR[t["instance_type"]]
    n = t["instances"]
    bs, be = datetime.fromisoformat(t["block_start"]), datetime.fromisoformat(t["block_end"])
    dur_h = (be - bs).total_seconds() / HOUR
    od_cost = price * n * dur_h
    m = {
        "task_id": t["task_id"], "block_id": t["block_id"], "mode": t["mode"],
        "instance_type": t["instance_type"], "instances": n, "block_az": t["block_az"],
        "status": t["status"], "attempts": t["attempts"],
        "first_attempt_at": t["first_attempt_at"], "acquired_at": t["acquired_at"],
        "acquired_az": t["acquired_az"], "block_start": t["block_start"],
        "block_end": t["block_end"], "block_duration_hours": round(dur_h, 2),
        "block_upfront_fee": round(t["block_upfront_fee"], 2),
        "od_cost_block_duration": round(od_cost, 2),
        "od_over_block_cost_ratio": round(od_cost / t["block_upfront_fee"], 3)
        if t["block_upfront_fee"] else None,
        "wait_minutes": None, "lead_hours_vs_block_start": None,
        "od_hold_cost_until_start": None,
    }
    if t["acquired_at"]:
        acq = datetime.fromisoformat(t["acquired_at"])
        first = datetime.fromisoformat(t["first_attempt_at"])
        lead_h = (bs - acq).total_seconds() / HOUR
        m["wait_minutes"] = round((acq - first).total_seconds() / 60, 1)
        m["lead_hours_vs_block_start"] = round(lead_h, 2)
        m["od_hold_cost_until_start"] = round(price * n * max(lead_h, 0), 2)
    return m


def summary(tasks):
    rows = [metrics(t) for t in tasks]
    with open(config.BLOCK_RESULTS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        if r["status"] == "acquired":
            when = (f"conseguiu após {r['wait_minutes']} min, "
                    f"{r['lead_hours_vs_block_start']:+.1f} h em relação ao início do bloco")
        else:
            when = r["status"]
        log(f"{r['task_id']:<28} {r['instance_type']} x{r['instances']}: {when} | "
            f"bloco ${r['block_upfront_fee']:,.2f} vs on-demand ${r['od_cost_block_duration']:,.2f}")
    log(f"resultados em {config.BLOCK_RESULTS_CSV}")


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="recria as tarefas do decision.json")
    ap.add_argument("--summary", action="store_true")
    args = ap.parse_args()
    Path(config.BLOCK_TASKS_FILE).parent.mkdir(parents=True, exist_ok=True)

    if args.reset or not Path(config.BLOCK_TASKS_FILE).exists():
        decision = json.loads(Path(config.DECISION_FILE).read_text())
        tasks = build_tasks(decision, utcnow())
        save_tasks(tasks)
        log(f"{len(tasks)} tarefa(s) criada(s) a partir de {config.DECISION_FILE}")
    tasks = load_tasks()
    if args.summary:
        summary(tasks)
        return 0

    signal.signal(signal.SIGINT, P.handle_signal)
    signal.signal(signal.SIGTERM, P.handle_signal)
    regions = sorted(set(all_probe_regions()) | {t["region"] for t in tasks})
    for r in regions:
        cleanup_region(r)
    budget, run_id, exit_code = P.load_budget(), uuid.uuid4().hex[:8], 0
    retry = timedelta(minutes=config.OD_RETRY_MINUTES)

    try:
        while not P.should_stop():
            now = utcnow()
            pending = [t for t in tasks if t["status"] == "pending"]
            if not pending:
                log("todas as tarefas terminaram")
                break
            due = sorted((t for t in pending if datetime.fromisoformat(t["next_attempt"]) <= now),
                         key=lambda t: t["give_up_at"])
            for t in due:
                if P.should_stop():
                    break
                t["first_attempt_at"] = t["first_attempt_at"] or utcnow().isoformat()
                attempt(t, budget, run_id)
                if t["status"] == "pending" and not P.should_stop():
                    nxt = utcnow() + retry
                    if nxt > datetime.fromisoformat(t["give_up_at"]):
                        t["status"] = "not_acquired_before_block_start"
                    else:
                        t["next_attempt"] = nxt.isoformat()
                save_tasks(tasks)
            P.interruptible_sleep(config.OD_TICK_SECONDS)
    except P.CriticalError as e:
        log(f"CRÍTICO: {e}")
        exit_code = 2
    finally:
        save_tasks(tasks)
        for cr_id, region in list(P.IN_FLIGHT.items()):
            if cancel_reservation(ec2_client(region), cr_id):
                P.IN_FLIGHT.pop(cr_id, None)
        failures = sum(cleanup_region(r)[1] for r in regions)
        if P.IN_FLIGHT or failures:
            log("CRÍTICO: reservas possivelmente ativas. Verifique o console EC2!")
            exit_code = 2
        summary(tasks)
        log(f"gasto estimado acumulado com sondagens: ${budget['spent_estimate_usd']:.2f}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())