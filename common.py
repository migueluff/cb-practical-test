"""Funções compartilhadas entre a sondagem e a limpeza."""
import datetime as dt
import time

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

import config

# Retentativas automáticas com backoff para throttling e erros transitórios.
BOTO_CFG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

OPEN_STATES = ["active", "pending", "scheduled", "payment-pending", "assessing", "delayed"]
CLOSED_STATES = {"cancelled", "expired", "failed", "unsupported"}

_clients = {}


def ec2_client(region):
    if region not in _clients:
        _clients[region] = boto3.client("ec2", region_name=region, config=BOTO_CFG)
    return _clients[region]


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def log(msg):
    print(f"[{utcnow().isoformat(timespec='seconds')}] {msg}", flush=True)


def get_state(client, cr_id):
    """Estado atual da reserva, ou None se ela não existe."""
    try:
        r = client.describe_capacity_reservations(CapacityReservationIds=[cr_id])
        items = r.get("CapacityReservations", [])
        return items[0]["State"] if items else None
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidCapacityReservationId.NotFound":
            return None
        raise


def cancel_reservation(client, cr_id):
    """Cancela com retentativas e CONFIRMA o estado final.

    Retorna True somente quando a reserva está comprovadamente encerrada.
    """
    for attempt in range(config.CANCEL_MAX_ATTEMPTS):
        try:
            client.cancel_capacity_reservation(CapacityReservationId=cr_id)
        except (ClientError, BotoCoreError) as e:
            log(f"  aviso: cancel {cr_id} tentativa {attempt + 1}: {e}")
        try:
            state = get_state(client, cr_id)
            if state is None or state in CLOSED_STATES:
                return True
        except (ClientError, BotoCoreError) as e:
            log(f"  aviso: describe {cr_id}: {e}")
        time.sleep(min(2 ** attempt, 30))
    return False


def list_open_probe_reservations(client):
    """Reservas abertas que tenham a tag da sondagem (e só elas)."""
    filters = [
        {"Name": f"tag:{config.TAG_KEY}", "Values": [config.TAG_VALUE]},
        {"Name": "state", "Values": OPEN_STATES},
    ]
    found = []
    paginator = client.get_paginator("describe_capacity_reservations")
    for page in paginator.paginate(Filters=filters):
        found.extend(page.get("CapacityReservations", []))
    return found


def cleanup_region(region):
    """Cancela toda reserva aberta com a tag da sondagem. Retorna (achadas, falhas)."""
    client = ec2_client(region)
    try:
        leftovers = list_open_probe_reservations(client)
    except (ClientError, BotoCoreError) as e:
        log(f"ERRO ao listar reservas em {region}: {e}")
        return 0, 1
    failed = 0
    for cr in leftovers:
        cr_id = cr["CapacityReservationId"]
        log(f"limpeza: cancelando {cr_id} ({region}, {cr['InstanceType']}, "
            f"x{cr['TotalInstanceCount']}, estado={cr['State']})")
        if not cancel_reservation(client, cr_id):
            log(f"CRÍTICO: não foi possível cancelar {cr_id} em {region}. Cancele manualmente!")
            failed += 1
    return len(leftovers), failed


def all_probe_regions():
    """Todas as regiões em que alguma sondagem pode criar reservas.

    União de: TARGETS, CB_TARGETS, CB_REGIONS, DEFAULT_REGION e as regiões das
    tarefas on-demand e por bloco já planejadas (se o arquivo existir). A limpeza usa esta
    lista para não deixar nenhuma região de fora.
    """
    import json
    from pathlib import Path
    regions = {t["region"] for t in getattr(config, "TARGETS", [])}
    regions |= {t["region"] for t in getattr(config, "CB_TARGETS", [])}
    regions |= set(getattr(config, "CB_REGIONS", []))
    if getattr(config, "DEFAULT_REGION", None):
        regions.add(config.DEFAULT_REGION)
    for name in ("OD_TASKS_FILE", "BLOCK_TASKS_FILE"):
        tasks_file = Path(getattr(config, name, "") or "")
        if tasks_file.is_file():
            try:
                regions |= {t["region"] for t in json.loads(tasks_file.read_text())}
            except (ValueError, KeyError, TypeError):
                log(f"aviso: não consegui ler regiões de {tasks_file}")
    return sorted(regions)