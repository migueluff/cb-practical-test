"""Uma sondagem de disponibilidade on-demand via ODCR (On-Demand Capacity Reservation).

probe() cria uma reserva do tamanho pedido em uma AZ e, se ela ficar ativa,
cancela na hora. Reserva ativa = havia capacidade on-demand naquele instante.
A cobrança é por segundo enquanto a reserva está ativa (o mesmo preço on-demand),
então um sucesso custa ~ preço x instâncias x MIN_BILLED_SECONDS.

Usado pelo run_block_probe.py. Estado global (STOP, IN_FLIGHT) é compartilhado
com quem chama para permitir parada segura e cancelamento no `finally`.
"""
import json
import time
import uuid
from datetime import timedelta
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

import config
from common import cancel_reservation, ec2_client, get_state, log, utcnow

# Falta de capacidade: é o dado que queremos medir
CAPACITY_CODES = {"InsufficientInstanceCapacity", "InsufficientCapacity",
                  "InsufficientReservedInstanceCapacity"}
# Limites da conta: tentar de novo não adianta
QUOTA_CODES = {"VcpuLimitExceeded", "InstanceLimitExceeded", "ReservationCapacityExceeded",
               "MaxSpotInstanceCountExceeded", "CapacityReservationLimitExceeded"}
# Tipo/AZ/plataforma não aceitos para ODCR
UNSUPPORTED_CODES = {"Unsupported", "UnsupportedOperation", "InvalidParameterValue",
                     "InvalidParameterCombination"}

STOP = {"requested": False, "reason": None}
IN_FLIGHT = {}   # cr_id -> região, reservas criadas e ainda não confirmadas como canceladas


class CriticalError(Exception):
    """Reserva que não pôde ser cancelada: interrompe tudo."""


# ---------------------------------------------------------------------------
# Parada segura
# ---------------------------------------------------------------------------
def handle_signal(signum, _frame):
    STOP["requested"], STOP["reason"] = True, f"sinal {signum}"
    log(f"parada solicitada (sinal {signum}); terminando a tentativa atual...")


def should_stop():
    if Path(config.STOP_FILE).exists() and not STOP["requested"]:
        STOP["requested"], STOP["reason"] = True, f"arquivo {config.STOP_FILE}"
        log(f"parada solicitada (arquivo {config.STOP_FILE})")
    return STOP["requested"]


def interruptible_sleep(seconds):
    end = time.monotonic() + seconds
    while not should_stop() and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


# ---------------------------------------------------------------------------
# Orçamento (estimado; persistido entre execuções)
# ---------------------------------------------------------------------------
def est_cost(instance_type, instances, active_seconds):
    price = config.PRICE_PER_INSTANCE_HOUR[instance_type]
    return price * instances * max(active_seconds, config.MIN_BILLED_SECONDS) / 3600


def load_budget():
    p = Path(config.STATE_FILE)
    if p.exists():
        return json.loads(p.read_text())
    return {"spent_estimate_usd": 0.0, "activated_reservations": 0}


def save_budget(budget):
    p = Path(config.STATE_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(budget, indent=2))
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Sondagem
# ---------------------------------------------------------------------------
def _create(client, pool, instances, run_id, token):
    end = utcnow() + timedelta(minutes=config.SAFETY_END_MINUTES)
    kw = dict(
        InstanceType=pool["instance_type"], InstancePlatform="Linux/UNIX",
        InstanceCount=instances, Tenancy="default",
        # targeted: instâncias já rodando na conta não são absorvidas pela reserva
        InstanceMatchCriteria="targeted",
        EndDateType="limited", EndDate=end,
        ClientToken=token,
        TagSpecifications=[{"ResourceType": "capacity-reservation", "Tags": [
            {"Key": config.TAG_KEY, "Value": config.TAG_VALUE},
            {"Key": "probe-run", "Value": run_id}]}],
    )
    if pool.get("az_id"):
        kw["AvailabilityZoneId"] = pool["az_id"]
    else:
        kw["AvailabilityZone"] = pool["az"]
    # ClientToken torna a chamada idempotente: se a resposta se perder e o boto
    # repetir, a AWS devolve a MESMA reserva em vez de criar outra.
    return client.create_capacity_reservation(**kw)["CapacityReservation"]


def probe(pool, instances, run_id, attempt_no):
    """Tenta reservar `instances` x tipo na AZ do pool. Retorna o registro da tentativa.

    result: available | unavailable | quota | unsupported | pending_timeout | error
    """
    client = ec2_client(pool["region"])
    rec = {"ts": utcnow().isoformat(), "run_id": run_id, "attempt_no": attempt_no,
           "region": pool["region"], "az": pool["az"], "az_id": pool.get("az_id"),
           "instance_type": pool["instance_type"], "instances": instances,
           "result": None, "error_code": None, "error_message": None,
           "reservation_id": None, "active_seconds": None, "est_cost_usd": 0.0}
    token = f"probe-{run_id}-{uuid.uuid4().hex[:16]}"
    t_create = time.monotonic()
    try:
        cr = _create(client, pool, instances, run_id, token)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        rec["error_code"], rec["error_message"] = code, e.response["Error"].get("Message", "")[:300]
        rec["result"] = ("unavailable" if code in CAPACITY_CODES else
                         "quota" if code in QUOTA_CODES else
                         "unsupported" if code in UNSUPPORTED_CODES else "error")
        return rec
    except BotoCoreError as e:
        # sem resposta: a reserva PODE ter sido criada; a limpeza por tag no fim cobre isso
        rec["result"], rec["error_code"], rec["error_message"] = "error", type(e).__name__, str(e)[:300]
        return rec

    cr_id = cr["CapacityReservationId"]
    IN_FLIGHT[cr_id] = pool["region"]
    rec["reservation_id"] = cr_id
    state = cr["State"]
    deadline = time.monotonic() + config.PENDING_TIMEOUT_SECONDS
    while state in ("pending", "assessing", "scheduled", "delayed") and time.monotonic() < deadline:
        time.sleep(2)
        try:
            state = get_state(client, cr_id) or "cancelled"
        except (ClientError, BotoCoreError) as e:
            log(f"  aviso: describe {cr_id}: {e}")
    rec["state_seen"] = state

    if not cancel_reservation(client, cr_id):
        raise CriticalError(f"não foi possível cancelar {cr_id} em {pool['region']}. "
                            f"CANCELE MANUALMENTE no console EC2!")
    IN_FLIGHT.pop(cr_id, None)
    active_s = time.monotonic() - t_create
    rec["active_seconds"] = round(active_s, 1)

    if state == "active":
        rec["result"] = "available"
        rec["est_cost_usd"] = round(est_cost(pool["instance_type"], instances, active_s), 4)
    elif state in ("pending", "assessing", "scheduled", "delayed"):
        rec["result"] = "pending_timeout"   # conservador: não conta como disponível
    else:
        rec["result"] = "unavailable"       # failed / cancelado pela AWS
    return rec
