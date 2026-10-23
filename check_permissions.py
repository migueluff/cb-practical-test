#!/usr/bin/env python3
"""Verifica se as credenciais têm as permissões do experimento, sem gastar nada.

Toda chamada é feita com DryRun=True: a AWS valida a autorização e devolve
DryRunOperation (autorizado) ou UnauthorizedOperation (negado) SEM executar a
operação. Nenhuma reserva é criada, nenhuma oferta é comprada, custo zero.

O que o DryRun NÃO responde:
  - se existe capacidade on-demand (é justamente o que o experimento mede);
  - se a quota da conta cabe o pedido (use --quota, que só a consulta);
  - condições IAM que dependem das tags do recurso já existente
    (no CancelCapacityReservation; em RequestTag, da criação, o DryRun vale).

Uso:
    python3 check_permissions.py
    python3 check_permissions.py --regions us-east-1 us-east-2 \\
        --instance-type p5.48xlarge --instances 2 --quota
"""
import argparse
import uuid
from datetime import timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

import config
import probe_odcr as P
from common import BOTO_CFG, OPEN_STATES, ec2_client, log, utcnow

DRY_RUN_OK = "DryRunOperation"
DENIED = "UnauthorizedOperation"


def _check(fn):
    """Classifica uma chamada feita com DryRun.

    Devolve (status, detalhe), com status em "ok", "negado" ou "?"
    (a chamada falhou antes da checagem de autorização).
    """
    try:
        fn()
    except ClientError as e:
        err = e.response["Error"]
        code, msg = err["Code"], err.get("Message", "")[:200]
        if code == DRY_RUN_OK:
            return "ok", ""
        if code == DENIED:
            return "negado", msg
        return "?", f"{code}: {msg}"
    except BotoCoreError as e:
        return "?", f"{type(e).__name__}: {e}"
    return "ok", "DryRun ignorado pela API (a chamada passou de verdade)"


def _pick_az(region, itype):
    """Uma AZ da região onde `itype` é oferecido, para montar a chamada de criação.

    Degrada para a primeira AZ da região, e depois para "<região>a", porque aqui
    só interessa que a chamada seja bem formada.
    """
    client = ec2_client(region)
    try:
        offered = {o["Location"] for page in client.get_paginator(
                       "describe_instance_type_offerings").paginate(
                       LocationType="availability-zone",
                       Filters=[{"Name": "instance-type", "Values": [itype]}])
                   for o in page["InstanceTypeOfferings"]}
        zones = client.describe_availability_zones(
            Filters=[{"Name": "zone-type", "Values": ["availability-zone"]}])["AvailabilityZones"]
        for z in zones:
            if z["ZoneName"] in offered:
                return z["ZoneName"], z["ZoneId"]
        if zones:
            log(f"  aviso: {itype} não é oferecido em nenhuma AZ de {region}")
            return zones[0]["ZoneName"], zones[0]["ZoneId"]
    except (ClientError, BotoCoreError) as e:
        log(f"  aviso: não consegui listar AZs de {region} ({e}); usando {region}a")
    return f"{region}a", None


def checks(region, itype, instances, horizon_days):
    """As chamadas que o experimento faz, cada uma com DryRun. (rótulo, fn, obrigatória)"""
    client = ec2_client(region)
    az, az_id = _pick_az(region, itype)
    pool = {"region": region, "az": az, "az_id": az_id, "instance_type": itype}
    start = utcnow() + timedelta(days=1)
    end = start + timedelta(days=horizon_days)
    tag_filters = [{"Name": f"tag:{config.TAG_KEY}", "Values": [config.TAG_VALUE]},
                   {"Name": "state", "Values": OPEN_STATES}]
    return [
        ("DescribeCapacityBlockOfferings (prepare_test)",
         lambda: client.describe_capacity_block_offerings(
             InstanceType=itype, InstanceCount=instances, CapacityDurationHours=24,
             StartDateRange=start, EndDateRange=end, DryRun=True), True),
        ("DescribeInstanceTypeOfferings (plan_ondemand)",
         lambda: client.describe_instance_type_offerings(
             LocationType="availability-zone",
             Filters=[{"Name": "instance-type", "Values": [itype]}], DryRun=True), True),
        ("DescribeAvailabilityZones (plan_ondemand)",
         lambda: client.describe_availability_zones(
             Filters=[{"Name": "zone-type", "Values": ["availability-zone"]}],
             DryRun=True), True),
        ("DescribeCapacityReservations (limpeza)",
         lambda: client.describe_capacity_reservations(Filters=tag_filters, DryRun=True), True),
        (f"CreateCapacityReservation {itype} x{instances} em {az} (+tags)",
         lambda: P._create(client, pool, instances, "dryrun",
                           f"probe-dryrun-{uuid.uuid4().hex[:16]}", dry_run=True), True),
        ("CancelCapacityReservation (limpeza)",
         lambda: client.cancel_capacity_reservation(
             CapacityReservationId="cr-0123456789abcdef0", DryRun=True), True),
        ("PurchaseCapacityBlock (opcional: o pipeline não compra blocos)",
         lambda: client.purchase_capacity_block(
             CapacityBlockOfferingId="cb-0123456789abcdef0",
             InstancePlatform="Linux/UNIX", DryRun=True), False),
    ]


def show_quota(region, itype):
    """Mostra a quota de vCPUs on-demand da família (o DryRun não a verifica)."""
    code = next((c for prefix, c in sorted(config.QUOTA_CODES.items(), key=lambda kv: -len(kv[0]))
                 if itype.startswith(prefix)), None)
    if not code:
        log(f"{region}: nenhum código de quota em QUOTA_CODES casa com {itype}")
        return
    try:
        sq = boto3.client("service-quotas", region_name=region, config=BOTO_CFG)
        q = sq.get_service_quota(ServiceCode="ec2", QuotaCode=code)["Quota"]
        log(f"{region}: quota {code} ({q['QuotaName']}) = {q['Value']:.0f} vCPUs")
        log("  compare com vCPUs do tipo x nº de instâncias do maior bloco escolhido")
    except (ClientError, BotoCoreError) as e:
        log(f"{region}: não consegui ler a quota {code} ({e}); veja no console Service Quotas")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", nargs="+", default=None,
                    help=f"padrão: CB_REGIONS + DEFAULT_REGION ({config.CB_REGIONS})")
    ap.add_argument("--instance-type", default=None,
                    help="padrão: o primeiro tipo com preço em PRICE_PER_INSTANCE_HOUR")
    ap.add_argument("--instances", type=int, default=1,
                    help="nº de instâncias da chamada de criação (padrão: 1)")
    ap.add_argument("--quota", action="store_true", help="também consulta a quota de vCPUs")
    args = ap.parse_args()

    regions = args.regions or sorted(set(config.CB_REGIONS) | {config.DEFAULT_REGION})
    itype = args.instance_type or next(iter(config.PRICE_PER_INSTANCE_HOUR), None)
    if not itype:
        raise SystemExit("Nenhum tipo em PRICE_PER_INSTANCE_HOUR; passe --instance-type.")

    try:
        who = boto3.client("sts", config=BOTO_CFG).get_caller_identity()
        log(f"identidade: {who['Arn']} (conta {who['Account']})")
    except (ClientError, BotoCoreError, NoCredentialsError) as e:
        raise SystemExit(f"Credenciais indisponíveis: {e}")
    log("todas as chamadas abaixo usam DryRun: nada é criado nem comprado")

    denied = indeterminate = 0
    for region in regions:
        log(f"--- {region} ---")
        for label, fn, required in checks(region, itype, args.instances,
                                          config.CB_HORIZON_DAYS):
            status, detail = _check(fn)
            mark = {"ok": "OK    ", "negado": "NEGADO", "?": "?     "}[status]
            log(f"  [{mark}] {label}" + (f"\n           {detail}" if detail else ""))
            if status == "negado" and required:
                denied += 1
            elif status == "?" and required:
                indeterminate += 1
        if args.quota:
            show_quota(region, itype)

    if denied:
        log(f"{denied} permissão(ões) obrigatória(s) NEGADA(S): ajuste a política IAM")
        return 1
    if indeterminate:
        log(f"{indeterminate} verificação(ões) indeterminada(s) (nenhuma negada); "
            f"veja as mensagens acima")
        return 0
    log("todas as permissões obrigatórias estão OK (capacidade e quota NÃO foram testadas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
