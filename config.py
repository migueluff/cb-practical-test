"""Configuração das sondagens de disponibilidade on-demand via ODCR.

Revise TODOS os valores antes de rodar, principalmente preços e orçamento.
"""

# ---------------------------------------------------------------------------
# Identificação: toda reserva criada recebe esta tag. A limpeza e a política
# IAM só atuam em reservas com ela, então suas outras reservas nunca são tocadas.
# ---------------------------------------------------------------------------
TAG_KEY = "probe-odcr"
TAG_VALUE = "true"

# ---------------------------------------------------------------------------
# O que sondar. "counts" = quantidades de instâncias a testar por pool.
# As AZs são descobertas automaticamente (onde o tipo é oferecido).
# ---------------------------------------------------------------------------
TARGETS = [
    {"region": "us-east-1", "instance_type": "p5.48xlarge", "counts": [1, 2, 4]},
    # {"region": "us-west-2", "instance_type": "p4d.24xlarge", "counts": [1, 2]},
]

# Restringir AZs (opcional). None = todas onde o tipo é oferecido.
# Exemplo: {"us-east-1": ["us-east-1a", "us-east-1d"]}
AZS = None

# ---------------------------------------------------------------------------
# Preço on-demand por instância-hora (USD). PREENCHA com o preço atual da sua
# região. Tipos sem preço aqui NÃO são sondados (trava de segurança).
# ---------------------------------------------------------------------------
PRICE_PER_INSTANCE_HOUR = {
    # "p5.48xlarge": 0.0,   # <- preencha
}

# Suposição conservadora de cobrança mínima por reserva ativada (segundos).
MIN_BILLED_SECONDS = 60

# ---------------------------------------------------------------------------
# Orçamento (estimado a partir dos preços acima)
# ---------------------------------------------------------------------------
BUDGET_USD_TOTAL = 50.0          # teto acumulado entre execuções (persistido)
BUDGET_USD_PER_ROUND = 10.0      # teto por rodada
MAX_SUCCESSES_PER_ROUND = 20     # máx. de reservas ativadas por rodada

# Se N instâncias estão disponíveis, qualquer quantidade menor também está
# (no mesmo instante). Sondando em ordem decrescente, economiza-se dinheiro:
# sucesso em N -> quantidades menores são registradas como "inferred_available".
INFER_MONOTONIC = True

# ---------------------------------------------------------------------------
# Tempo
# ---------------------------------------------------------------------------
ROUND_INTERVAL_SECONDS = 3600    # intervalo entre rodadas
SLEEP_BETWEEN_CALLS_SECONDS = 2  # espaçamento entre chamadas (rate limit)
PENDING_TIMEOUT_SECONDS = 60     # espera máxima se a reserva ficar "pending"

# Rede de segurança: a reserva é criada com data de término automática.
# A AWS encerra a reserva em até ~1h após essa data, então o pior caso de uma
# reserva "esquecida" fica limitado a aproximadamente este valor + 1h.
SAFETY_END_MINUTES = 60

CANCEL_MAX_ATTEMPTS = 8

# ---------------------------------------------------------------------------
# Arquivos
# ---------------------------------------------------------------------------
RESULTS_FILE = "results/odcr_probes.jsonl"
STATE_FILE = "results/budget_state.json"
STOP_FILE = "STOP"   # crie este arquivo para parar a execução com segurança

# ---------------------------------------------------------------------------
# Códigos de quota (vCPUs on-demand) por prefixo do tipo de instância.
# Confira os códigos no console Service Quotas da sua conta.
# ---------------------------------------------------------------------------
QUOTA_CODES = {
    "p": "L-417A185B",   # Running On-Demand P instances
}

# ===========================================================================
# Capacity Blocks: coleta de ofertas (DescribeCapacityBlockOfferings é gratuita)
# ===========================================================================
# Alvos da coleta avulsa (collect_offerings.py sem argumentos). O prepare_test.py
# NÃO usa esta lista: ele deriva as consultas dos jobs.
_P5_DAYS = list(range(1, 15))
CB_TARGETS = [
    {"region": "us-east-1", "instance_type": t, "counts": [1, 2, 4, 8, 16],
     "durations_hours": [24 * d for d in _P5_DAYS]}
    for t in ("p5.48xlarge",)
]
# Janela de início consultada a partir de agora. Horizontes longos implicam um
# experimento longo, porque as sondagens on-demand acontecem no início de cada job.
CB_HORIZON_DAYS = 7
CB_SLEEP_BETWEEN_CALLS = 2.0       # espaçamento entre consultas de ofertas (s)
# A API de ofertas tem um limite próprio (CapacityBlockDescribeLimitExceeded),
# além do throttling genérico. Nesses erros, espera e tenta de novo:
CB_THROTTLE_MAX_RETRIES = 8
CB_THROTTLE_BACKOFF_START_S = 30   # 30, 60, 120, ... até o teto abaixo
CB_THROTTLE_BACKOFF_MAX_S = 600
# Cache das consultas bem-sucedidas: rodar de novo não repete o que já foi
# consultado (dentro da validade). Ofertas mudam com o tempo; não use cache velho.
CB_CACHE_DIR = "results/offerings_cache"
CB_CACHE_MAX_AGE_HOURS = 6
OFFERINGS_DIR = "results/offerings"

# ===========================================================================
# Comparação on-demand a partir da decisão do modelo
# ===========================================================================
# Quando o "usuário on-demand" começa a tentar:
#   "submit"   -> no tempo de submissão do job
#   "cb_start" -> no início que o modelo atribuiu ao job no bloco (se escalonado)
OD_START_POLICY = "submit"
OD_RETRY_MINUTES = 15          # intervalo entre tentativas até o último início possível
# "same_az": mesma AZ do bloco escolhido (só para jobs escalonados pelo modelo)
# "region":  qualquer AZ da região onde o tipo é oferecido
OD_MODES = ["same_az", "region"]
OD_TICK_SECONDS = 30
DECISION_FILE = "results/decision.json"
OD_TASKS_FILE = "results/ondemand_tasks.json"
OD_ATTEMPTS_FILE = "results/ondemand_attempts.jsonl"

# ===========================================================================
# Exportação para o modelo (que roda em outro ambiente) e importação da resposta
# ===========================================================================
# 1 período do modelo, em minutos. Vale para jobs (r, d, p) E blocos (Sb, duration).
# Período 0 = momento da consulta, arredondado para baixo na grade de 10 min
# (ex.: 12:16 -> 12:10). Ofertas que começam/terminam em minutos múltiplos de 10
# (ex.: 11:30) ficam com Sb e duration exatos (24 h = 144 períodos).
MODEL_TIME_UNIT_MINUTES = 10
# Settings copiados para a instância exportada (o modelo pode ignorar/ajustar)
# Campos de topo da instância exportada (mesmo formato do seu modelo).
# max_blocks=None -> exportado como o nº de blocos (sem limite efetivo).
MODEL_SETTINGS = {"solver": "GUROBI", "time_limit_s": 120, "verbose": True,
                  "max_blocks": None}

# Jobs vindos de uma instância do modelo não têm tipo de GPU nem região:
JOBS_DEFAULT_GPU_TYPE = "P5"

# Rótulo de GPU dos jobs -> tipos de instância candidatos. Um job pode rodar em
# QUALQUER candidato do seu grupo: as ofertas de todos os candidatos entram na
# mesma instância do modelo (o modelo trata as GPUs do grupo como intercambiáveis).
# Jobs também podem trazer "instance_type" (um só candidato).
GPU_TYPE_TO_INSTANCES = {
    "P5":   ["p5.48xlarge"],          # só p5.48xlarge (família completa: P5_ALL)
    "P5_ALL": ["p5.4xlarge", "p5.48xlarge", "p5e.48xlarge", "p5en.48xlarge"],
    "H100": ["p5.4xlarge", "p5.48xlarge"],
    "H200": ["p5e.48xlarge", "p5en.48xlarge"],
    "A100": ["p4d.24xlarge"],
    "B200": ["p6-b200.48xlarge"],
}
# Um job que precisa de mais GPUs do que um tipo oferece só pode usar esse tipo
# espalhado em várias instâncias se o tipo tiver pelo menos este nº de GPUs
# (instâncias de 8 GPUs com EFA são feitas para multi-nó; um job de 8 GPUs em
# 8 x p5.4xlarge não é equivalente a uma p5.48xlarge).
MIN_GPUS_PER_INSTANCE_FOR_MULTI_NODE = 8

GPUS_PER_INSTANCE = {
    "p4d.24xlarge": 8, "p4de.24xlarge": 8,
    "p5.4xlarge": 1, "p5.48xlarge": 8, "p5e.48xlarge": 8, "p5en.48xlarge": 8,
    "p6-b200.48xlarge": 8,
}

CB_REGIONS = ["us-east-1"]        # regiões consultadas para os jobs
DEFAULT_REGION = "us-east-1"      # região on-demand de jobs sem bloco
CB_ALLOWED_COUNTS = [1, 2, 4, 8, 16, 32, 64]   # quantidades aceitas pela API
CB_MAX_INSTANCE_COUNT = 16        # teto de instâncias por bloco consultado
# Poda por carga: não consulta (quantidade, duração) cuja capacidade em GPU-horas
# passe de RATIO x a carga total dos jobs (GPUs x horas). None desliga a poda.
CB_MAX_CAPACITY_RATIO = 2.0
CB_MAX_DURATION_DAYS = 14         # durações de 1 a N dias (múltiplos de 24h)
# Lista fixa de durações (dias) para reduzir o nº de consultas; None = todos os
# dias de 1 a CB_MAX_DURATION_DAYS. Ex.: [1, 2, 3, 5, 7, 10, 14]
CB_DURATION_DAYS = None
# Durações extras acima de 14 dias: a AWS aceita múltiplos de 7 dias até 182.
# Só entram se couberem na janela.
CB_EXTRA_DURATION_DAYS = []       # ex.: [21, 28]
CB_MAX_LOOKAHEAD_DAYS = 56        # a API só oferece blocos até ~8 semanas à frente
# Deadline dos jobs = término do último bloco retornado na consulta (por grupo).
# O "d" da entrada é ignorado (pode até ficar em branco). Desligue com
# --deadline-from-input no prepare_test.py.
DEADLINE_FROM_BLOCKS = True
# Com DEADLINE_FROM_BLOCKS, a janela de consulta vai de agora até este horizonte
# (não depende mais do deadline). Limitado por CB_MAX_LOOKAHEAD_DAYS.
CB_QUERY_HORIZON_DAYS = 56   # com períodos de 10 min: 14 dias = 2016 períodos (Tmax).
                             # Curto demais pode não ter ofertas (elas costumam começar
                             # dias à frente); longo demais gera Tmax e modelo grandes.
BUNDLES_DIR = "results/bundles"

# ===========================================================================
# Teste por bloco (run_block_probe.py): ODCR do tamanho de cada bloco escolhido
# ===========================================================================
# "same_az": mesmo tipo e mesma AZ do bloco
# "region":  mesmo tipo, qualquer AZ da região onde o tipo é oferecido
BLOCK_MODES = ["same_az", "region"]
# Tentativas continuam até o início do bloco + esta folga (0 = para no início do bloco)
BLOCK_EXTRA_WAIT_HOURS = 0
BLOCK_TASKS_FILE = "results/block_tasks.json"
BLOCK_ATTEMPTS_FILE = "results/block_attempts.jsonl"
BLOCK_RESULTS_CSV = "results/block_results.csv"

# ===========================================================================
# Preços buscados por fetch_ondemand_prices.py (AWS Pricing API).
# Completam PRICE_PER_INSTANCE_HOUR; valores escritos à mão acima têm prioridade.
# ===========================================================================
ONDEMAND_PRICES_FILE = "results/ondemand_prices.json"
try:
    import json as _json
    with open(ONDEMAND_PRICES_FILE) as _f:
        for _t, _p in _json.load(_f).get("prices", {}).items():
            PRICE_PER_INSTANCE_HOUR.setdefault(_t, float(_p))
except (OSError, ValueError):
    pass