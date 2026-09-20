#!/usr/bin/env bash
# =============================================================================
# PSI5120 - Trabalho Final
# Caracterizacao da distribuicao do atraso de deteccao.
#
# PROBLEMA IDENTIFICADO
# As varreduras de parametros do HPA apresentaram diferencas entre celulas que
# nao admitem explicacao fisica. Na varredura de carga oferecida, por exemplo, o
# ponto intermediario resultou em atraso de deteccao tres vezes superior ao
# observado nos dois extremos, com desvio de 23,6 s contra cerca de 3 s nos
# vizinhos.
#
# A explicacao proposta e o alinhamento de fase com o ciclo de coleta de
# metricas. O Metrics Server amostra em intervalos fixos, de modo que o atraso
# entre a aplicacao da carga e sua visibilidade ao autoescalador depende de onde
# o instante de inicio cai dentro desse ciclo. Uma carga iniciada imediatamente
# antes de uma coleta torna-se visivel em poucos segundos; iniciada logo apos,
# aguarda quase dois intervalos.
#
# Como cada ponto experimental possui duracao fixa, suas repeticoes tendem a
# cair sempre na mesma fase, o que produz variancia artificialmente baixa dentro
# de cada celula e diferencas aparentes entre celulas que refletem fase, e nao o
# fator sob estudo.
#
# PROCEDIMENTO
# Este experimento repete um unico ponto, mantendo todos os fatores fixos, e
# introduz atraso aleatorio entre o inicio da coleta e a aplicacao da carga. A
# aleatorizacao distribui as execucoes ao longo do ciclo de coleta e permite
# estimar a distribuicao do atraso de deteccao em vez de um valor pontual.
#
# HIPOTESE
# Com resolucao de coleta R, o atraso de deteccao deve distribuir-se
# aproximadamente de forma uniforme entre um piso, correspondente ao
# processamento do controlador, e esse piso acrescido de R.
#
# USO
#   ./experimento_fase.sh <rotulo> <repeticoes> [resolucao_segundos]
#
# EXEMPLO
#   ./experimento_fase.sh minikube 12 60
# =============================================================================

set -uo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
NAMESPACE="${NAMESPACE:-hpa-demo}"

ROTULO="${1:-minikube}"
N="${2:-12}"
RESOLUCAO="${3:-60}"

# Fatores mantidos fixos. Correspondem ao ponto de referencia das demais
# varreduras, de modo que os resultados sejam comparaveis a elas.
RPS=40
ALVO=50
JANELA=300

# A carga e mantida curta porque somente a fase de deteccao interessa aqui. A
# reducao nao e observada, o que reduz substancialmente o tempo total.
DUR_CARGA=90
POS=45

mkdir -p "${RAIZ}/dados"

echo "############################################################"
echo "# Caracterizacao do atraso de deteccao"
echo "#   repeticoes         ${N}"
echo "#   resolucao suposta  ${RESOLUCAO}s"
echo "#   fatores fixos      ${RPS} req/s, alvo ${ALVO}%, janela ${JANELA}s"
echo "############################################################"

for i in $(seq 1 "$N"); do
    # -------------------------------------------------------------------------
    # Atraso aleatorio no intervalo de um ciclo de coleta.
    #
    # A amostragem uniforme em [0, R) desloca o instante de inicio da carga ao
    # longo do ciclo, de modo que o conjunto de execucoes cubra todas as fases
    # possiveis em vez de repetir sempre a mesma.
    # -------------------------------------------------------------------------
    ATRASO=$(( RANDOM % RESOLUCAO ))
    PRE=$(( 30 + ATRASO ))

    echo
    echo "--- execucao ${i}/${N}, atraso de fase ${ATRASO}s (pre-carga ${PRE}s)"

    PRE_CARGA="$PRE" DURACAO_CARGA="$DUR_CARGA" POS_CARGA="$POS" \
        "${RAIZ}/experimentos/executar_experimento.sh" \
        "${ROTULO}-fase" "$RPS" "$ALVO" "$JANELA" "$i"

    # Registro do atraso sorteado, acrescentado ao arquivo de marcadores da
    # execucao. Permite verificar, na analise, se o atraso observado
    # correlaciona-se com a fase imposta.
    MARC="${RAIZ}/dados/exp_${ROTULO}-fase_rps${RPS}_alvo${ALVO}_jan${JANELA}_r${i}_marcadores.json"
    if [ -f "$MARC" ]; then
        python3 - "$MARC" "$ATRASO" "$RESOLUCAO" <<'PY'
import json, sys
caminho, atraso, resolucao = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
with open(caminho) as f:
    d = json.load(f)
d["fase_imposta_s"] = atraso
d["resolucao_suposta_s"] = resolucao
with open(caminho, "w") as f:
    json.dump(d, f, indent=2)
PY
    fi
done

echo
echo "############################################################"
echo "# Concluido. Execucoes gravadas com o rotulo ${ROTULO}-fase"
echo "############################################################"
