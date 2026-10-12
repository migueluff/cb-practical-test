#!/usr/bin/env python3
"""Importa a resposta do modelo (rodado em outro ambiente) e gera decision.json.

Formatos aceitos para a solução:
  a) allocation_report.txt gerado pelo modelo (seções "[Capacity Blocks Alocados]"
     e "[Jobs Alocados]");
  b) JSON com o dicionário de solve_from_json / solve_earliest_start
     (json.dump(solution, f, default=str)), usando "blocks" [{bid}] e
     "jobs" [{jid, block, start, end}].

Onde a solução é procurada (nesta ordem):
  --solution <arquivo>                       (pacote com um único grupo)
  <pacote>/solutions/<grupo>.json | .txt
  <pacote>/solutions/allocation_report.txt   (pacote com um único grupo)

A solução é verificada contra a instância do pacote: blocos existentes e
IGUAIS (janela, M e custo do relatório = instância), janelas e duração dos
jobs e capacidade por período. Isso impede importar um relatório gerado a
partir da instância de outra execução.

Uso:
    python3 import_solution.py --bundle results/bundles/<id> [--solution allocation_report.txt]
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import config
from datetime import timedelta

from prepare_test import parse_dt


_BLOCK_RE = re.compile(r"^-\s*(\S+?):\s*janela=\[(\d+)\.\.(\d+)\]\s*M=(\d+)\s*cost=([\d.]+)")
_JOB_RE = re.compile(r"^-\s*J(\d+)\s+em\s+(\S+?):\s*\[(\d+)\.\.(\d+)\]")


def parse_report(text):
    """Lê allocation_report.txt e devolve uma solução no formato do solve_from_json."""
    sol = {"blocks": [], "jobs": [], "source": "allocation_report"}
    m = re.search(r"^Status:\s*(.+)$", text, re.M)
    sol["status"] = m.group(1).strip() if m else None
    m = re.search(r"^Objective:\s*([-\d.eE+]+)", text, re.M)
    sol["objective"] = float(m.group(1)) if m else None
    m = re.search(r"^Instance:\s*(.+)$", text, re.M)
    sol["instance_path"] = m.group(1).strip() if m else None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
            continue
        if section == "[Capacity Blocks Alocados]":
            b = _BLOCK_RE.match(line)
            if b:
                sol["blocks"].append({"bid": b.group(1), "start": int(b.group(2)),
                                      "end": int(b.group(3)), "M": int(b.group(4)),
                                      "cost": float(b.group(5))})
        elif section == "[Jobs Alocados]":
            j = _JOB_RE.match(line)
            if j:
                sol["jobs"].append({"jid": int(j.group(1)), "block": j.group(2),
                                    "start": int(j.group(3)), "end": int(j.group(4))})
    if not sol["blocks"] and "[Capacity Blocks Alocados]" not in text:
        raise SystemExit("arquivo não parece um allocation_report.txt (seções não encontradas)")
    return sol


def load_solution(path):
    text = Path(path).read_text()
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return parse_report(text)


def find_solution(bundle, group, n_groups, cli_path):
    if cli_path:
        if n_groups > 1:
            raise SystemExit("--solution só pode ser usado com pacote de um único grupo")
        return Path(cli_path)
    sdir = bundle / "solutions"
    for name in (f"{group}.json", f"{group}.txt", f"{group}_allocation_report.txt"):
        if (sdir / name).exists():
            return sdir / name
    if n_groups == 1 and (sdir / "allocation_report.txt").exists():
        return sdir / "allocation_report.txt"
    return None


def check_blocks_match(sol, inst):
    """Blocos informados pela solução precisam ser os MESMOS da instância do pacote."""
    blocks = {b["bid"]: b for b in inst["capacity_blocks"]}
    errs = []
    for b in sol.get("blocks", []):
        ib = blocks.get(b["bid"])
        if ib is None:
            continue   # reportado por verify
        if "start" in b and (b["start"] != ib["Sb"] or b["end"] != ib["Sb"] + ib["duration"]):
            errs.append(f"bloco {b['bid']}: janela [{b['start']}..{b['end']}] na solução, "
                        f"[{ib['Sb']}..{ib['Sb'] + ib['duration']}] no pacote")
        if "M" in b and b["M"] != ib["M"]:
            errs.append(f"bloco {b['bid']}: M={b['M']} na solução, M={ib['M']} no pacote")
        if "cost" in b and abs(float(b["cost"]) - float(ib["cost"])) > 0.01:
            errs.append(f"bloco {b['bid']}: cost={b['cost']} na solução, cost={ib['cost']} no pacote")
    if errs:
        errs.append("a solução parece ter sido gerada a partir da instância de OUTRA execução "
                    "do prepare_test.py")
    return errs


def verify(sol, inst):
    jobs = {j["jid"]: j for j in inst["jobs"]}
    blocks = {b["bid"]: {"M": b["M"], "start": b["Sb"], "end": b["Sb"] + b["duration"]}
              for b in inst["capacity_blocks"]}
    reserved = {b["bid"] for b in sol.get("blocks", [])}
    errs, seen = [], set()
    usage = defaultdict(lambda: defaultdict(int))

    for bid in reserved:
        if bid not in blocks:
            errs.append(f"bloco reservado {bid} não existe na instância")
    for sj in sol.get("jobs", []):
        jid, bid = int(sj["jid"]), sj["block"]
        s, e = int(sj["start"]), int(sj["end"])
        j, b = jobs.get(jid), blocks.get(bid)
        if j is None or b is None:
            errs.append(f"job {jid} ou bloco {bid} inexistente")
            continue
        if jid in seen:
            errs.append(f"job {jid} escalonado mais de uma vez")
        seen.add(jid)
        if bid not in reserved:
            errs.append(f"job {jid} em bloco não reservado {bid}")
        if e - s != j["p"]:
            errs.append(f"job {jid}: duração {e - s} != p={j['p']}")
        if s < j["r"] or e > j["d"]:
            errs.append(f"job {jid}: [{s},{e}] fora da janela [{j['r']},{j['d']}]")
        if s < b["start"] or e > b["end"]:
            errs.append(f"job {jid}: [{s},{e}] fora do bloco {bid} [{b['start']},{b['end']}]")
        for t in range(s, e):
            usage[bid][t] += j["m"]
    for bid, u in usage.items():
        peak = max(u.values())
        if bid in blocks and peak > blocks[bid]["M"]:
            errs.append(f"bloco {bid}: pico de {peak} GPUs > capacidade {blocks[bid]['M']}")
    return errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--solution", help="arquivo da solução (allocation_report.txt ou JSON)")
    ap.add_argument("--allow-invalid", action="store_true",
                    help="importa mesmo se a verificação encontrar problemas")
    args = ap.parse_args()

    bundle = Path(args.bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    jobs = json.loads((bundle / "jobs_resolved.json").read_text())
    t0 = parse_dt(manifest["t0"])
    unit = timedelta(minutes=manifest["time_unit_minutes"])   # unidade usada na exportação

    def from_period(k, t0):
        return t0 + k * unit

    decision = {"decision_time": None, "bundle": str(bundle),
                "offerings_snapshot": str(bundle / "offerings_snapshot.json"),
                "model": {"t0": manifest["t0"], "time_unit_minutes": manifest["time_unit_minutes"],
                          "per_group": {}},
                "blocks": [], "jobs": []}
    scheduled = {}

    for group, info in manifest["groups"].items():
        sol_path = find_solution(bundle, group, len(manifest["groups"]), args.solution)
        summary = {"n_jobs": info["n_jobs"], "n_blocks_offered": info["n_blocks"]}
        if sol_path is None or not sol_path.exists():
            print(f"{group}: sem solução em {sol_path}; jobs desse grupo ficam sem bloco")
            summary["status"] = "missing"
            decision["model"]["per_group"][group] = summary
            continue
        sol = load_solution(sol_path)
        print(f"{group}: solução lida de {sol_path}"
              + (f" (relatório da instância {sol['instance_path']})" if sol.get("instance_path") else ""))
        inst = json.loads((bundle / info["instance_file"]).read_text())
        errs = check_blocks_match(sol, inst) + verify(sol, inst)
        if errs:
            print(f"{group}: {len(errs)} problema(s) na solução:")
            for e in errs[:20]:
                print(f"  - {e}")
            if not args.allow_invalid:
                raise SystemExit("Solução inválida; corrija ou use --allow-invalid.")
        summary.update(status=str(sol.get("status")), objective=sol.get("objective"),
                       gap=sol.get("gap"), n_blocks=len(sol.get("blocks", [])),
                       n_scheduled=len(sol.get("jobs", [])), verification_errors=len(errs))
        decision["model"]["per_group"][group] = summary

        for b in sol.get("blocks", []):
            o = info["block_map"][b["bid"]]
            decision["blocks"].append({
                "block_id": f"{group}:{b['bid']}", "offering_id": o["CapacityBlockOfferingId"],
                "region": o["Region"], "az": o["AvailabilityZone"],
                "instance_type": o["InstanceType"], "instance_count": int(o["InstanceCount"]),
                "start": parse_dt(o["StartDate"]).isoformat(),
                "end": parse_dt(o["EndDate"]).isoformat(),
                "upfront_fee": float(o["UpfrontFee"]),
            })
        for sj in sol.get("jobs", []):
            scheduled[int(sj["jid"])] = {
                "block_id": f"{group}:{sj['block']}",
                "start": from_period(int(sj["start"]), t0).isoformat(),
                "end": from_period(int(sj["end"]), t0).isoformat(),
            }

    blocks_by_id = {b["block_id"]: b for b in decision["blocks"]}
    for j in jobs:
        s = scheduled.get(j["jid"])
        blk = blocks_by_id[s["block_id"]] if s else None
        if blk:   # tipo e nº de instâncias do bloco onde o modelo colocou o job
            s["instance_type"] = blk["instance_type"]
            s["instances"] = j["instances_by_type"][blk["instance_type"]]
        decision["jobs"].append({
            "job_id": str(j["jid"]), "group": j["group"],
            "region": blk["region"] if blk else (j.get("region") or config.DEFAULT_REGION),
            # candidatos on-demand: qualquer tipo do grupo, com o nº de instâncias de cada um
            "candidates": [{"instance_type": t, "instances": n}
                           for t, n in j["instances_by_type"].items()],
            "gpus": j["gpus"], "submit": j["submit"], "deadline": j["deadline"],
            "proc_hours": j["proc_hours"], "gain": j["gain"], "scheduled": s,
        })

    from common import utcnow
    decision["decision_time"] = utcnow().isoformat()
    out = Path(config.DECISION_FILE)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(decision, indent=2, default=str))
    n_s = sum(1 for j in decision["jobs"] if j["scheduled"])
    cost = sum(b["upfront_fee"] for b in decision["blocks"])
    print(f"{len(decision['blocks'])} bloco(s) (custo ${cost:,.2f}), "
          f"{n_s}/{len(jobs)} job(s) escalonado(s) -> {out}")
    late = [j for j in jobs if parse_dt(j["submit"]) < parse_dt(decision["decision_time"])]
    if late:
        print(f"aviso: {len(late)} job(s) já foram submetidos antes da importação; "
              f"as tentativas on-demand deles começam agora")


if __name__ == "__main__":
    main()