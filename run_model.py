#!/usr/bin/env python3
"""Roda o modelo completo do CapacityBlocks (main.py) sobre as instâncias de um pacote.

Faz o mesmo que `python main.py <instância>`: solve_from_json e, em seguida, a
passada de earliest start (mesmos blocos e jobs, inícios o mais cedo possível).
A diferença é que a solução é gravada em JSON direto em
<pacote>/solutions/<grupo>.json, que o import_solution.py lê, sem depender do
Graphviz (no main.py, se a renderização falhar, o allocation_report.txt não é escrito).

Uso:
    python3 run_model.py --bundle results/bundles/<id> [--capacityblocks ../CapacityBlocks]
                         [--solver GUROBI|CBC] [--time-limit 1800] [--max-gap 0.01]
                         [--no-earliest-start]
"""
import argparse
import json
import sys
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--capacityblocks", default=str(Path(__file__).resolve().parent.parent / "CapacityBlocks"),
                    help="pasta do repositório CapacityBlocks (com main.py)")
    ap.add_argument("--solver", help="sobrescreve o solver da instância (GUROBI, CBC)")
    ap.add_argument("--time-limit", type=int, help="sobrescreve time_limit_s da instância")
    ap.add_argument("--max-gap", type=float)
    ap.add_argument("--no-earliest-start", action="store_true")
    args = ap.parse_args()

    cb_dir = Path(args.capacityblocks).resolve()
    if not (cb_dir / "main.py").exists():
        raise SystemExit(f"main.py não encontrado em {cb_dir}")
    sys.path.insert(0, str(cb_dir))
    import main as cb                       # noqa: E402  (CapacityBlocks/main.py)
    from src.models import load_instance    # noqa: E402

    bundle = Path(args.bundle)
    manifest = json.loads((bundle / "manifest.json").read_text())
    (bundle / "solutions").mkdir(exist_ok=True)

    for group, info in manifest["groups"].items():
        inst = json.loads((bundle / info["instance_file"]).read_text())
        if not inst["capacity_blocks"]:
            print(f"{group}: instância sem blocos; pulando")
            continue
        # cópia com os ajustes de solver/tempo (o import só compara blocos e jobs)
        if args.solver:
            inst["solver"] = args.solver
        if args.time_limit is not None:
            inst["time_limit_s"] = args.time_limit
        out_dir = bundle / "model_out" / group
        out_dir.mkdir(parents=True, exist_ok=True)
        inst_path = out_dir / "instance.json"
        inst_path.write_text(json.dumps(inst, indent=2))
        print(f"{group}: {len(inst['jobs'])} job(s), {len(inst['capacity_blocks'])} bloco(s), "
              f"Tmax={inst['Tmax']}, solver={inst['solver']}, limite={inst['time_limit_s']} s")

        t = time.perf_counter()
        sol = cb.solve_from_json(str(inst_path), str(out_dir), max_gap=args.max_gap)
        solver_time = time.perf_counter() - t
        if sol is None:
            print(f"{group}: o modelo não encontrou solução viável")
            continue
        gap = sol.get("gap")
        if not args.no_earliest_start:
            Tmax, jobs, blocks, settings = load_instance(str(inst_path))
            if args.max_gap is not None:
                settings["max_gap"] = args.max_gap
            try:
                earliest = cb.solve_earliest_start(sol, Tmax, jobs, blocks, settings)
            except Exception as e:  # mesmo comportamento do main.py
                print(f"{group}: earliest start falhou ({e}); usando a solução original")
                earliest = None
            if earliest is not None:
                sol = earliest

        status = getattr(sol["status"], "name", str(sol["status"]))
        out = {"status": status, "objective": sol["objective"], "gap": gap,
               "solver_time_s": round(solver_time, 2), "instance_path": str(inst_path),
               "blocks": sol["blocks"], "jobs": sol["jobs"]}
        sol_path = bundle / "solutions" / f"{group}.json"
        sol_path.write_text(json.dumps(out, indent=2, default=str))
        print(f"{group}: {status}, objetivo={sol['objective']:.2f}, gap={gap}, "
              f"{len(sol['blocks'])} bloco(s), {len(sol['jobs'])} job(s) -> {sol_path}")

        try:   # relatórios texto do CapacityBlocks (opcionais)
            from utils.report import write_solution_reports
            write_solution_reports(sol, out_dir, str(inst_path), solver_time)
        except Exception as e:
            print(f"{group}: aviso: relatório texto não gerado ({e})")

    print(f"agora: python3 import_solution.py --bundle {bundle}")


if __name__ == "__main__":
    main()
