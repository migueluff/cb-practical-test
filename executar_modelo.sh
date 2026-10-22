#!/bin/bash
#SBATCH --job-name=cb_model
#SBATCH --partition=cpu
#SBATCH --account=monitora
#SBATCH --nodes=1
#SBATCH --time=03:00:00
#SBATCH --output=/dfs_geral_ep/res/projetos_integrados/tc_custoshpc/miguel/cb_teste/CapacityBlocks/outputs/temporario/model_out_%j.txt
#SBATCH --error=/dfs_geral_ep/res/projetos_integrados/tc_custoshpc/miguel/cb_teste/CapacityBlocks/outputs/temporario/model_err_%j.txt

source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate petro_env
cd /dfs_geral_ep/res/projetos_integrados/tc_custoshpc/miguel/cb_teste/CapacityBlocks
python3 main.py /dfs_geral_ep/res/projetos_integrados/tc_custoshpc/miguel/cb_teste/CapacityBlocks/instances/echelon_cases/echelon_768_relaxado_relaxado_real.json --out-dir /dfs_geral_ep/res/projetos_integrados/tc_custoshpc/miguel/cb_teste/CapacityBlocks/outputs/temporario