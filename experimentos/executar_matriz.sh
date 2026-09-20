#!/usr/bin/env bash
# =============================================================================
# PSI5120 - Trabalho Final
# Execucao da matriz experimental completa.
#
# ESTRUTURA DA MATRIZ
# Tres varreduras independentes, cada uma isolando um fator e mantendo os
# demais no valor de referencia adotado no trabalho intermediario, que e taxa
# de 40 requisicoes por segundo, alvo de 50 por cento e janela de 300 segundos.
#
#   Varredura A   alvo de utilizacao        30, 50 e 70 por cento
#   Varredura B   carga oferecida           20, 40 e 80 requisicoes por segundo
#   Varredura C   janela de estabilizacao   60, 180 e 300 segundos
#
# Cada ponto e repetido tres vezes, o que permite estimar a variabilidade entre
# execucoes e sustentar afirmacoes sobre diferencas sistematicas.
#
# DURACAO DIFERENCIADA POR VARREDURA
# As varreduras A e B investigam o comportamento durante o crescimento, de modo
# que a fase posterior a carga e reduzida ao minimo necessario para confirmar o
# retorno ao repouso. A varredura C investiga justamente a reducao, e por isso
# mantem observacao integral apos a remocao da carga.
#
# USO
#   ./executar_matriz.sh <rotulo_do_ambiente> [varredura]
#
# EXEMPLOS
#   ./executar_matriz.sh minikube          executa as tres varreduras
#   ./executar_matriz.sh minikube A        executa apenas a varredura A
#   ./executar_matriz.sh eks B             executa apenas a varredura B
# =============================================================================

set -uo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
EXEC="${RAIZ}/experimentos/executar_experimento.sh"

ROTULO="${1:?informe o rotulo do ambiente, por exemplo minikube ou eks}"
VARREDURA="${2:-TODAS}"
REPETICOES="${REPETICOES:-3}"

# Valores de referencia, herdados da configuracao do trabalho intermediario.
REF_RPS=40
REF_ALVO=50
REF_JANELA=300

INICIO_GERAL=$(date +%s)

registrar_cabecalho() {
    echo
    echo "############################################################"
    echo "# $1"
    echo "############################################################"
}

# -----------------------------------------------------------------------------
# Varredura A. Sensibilidade ao alvo de utilizacao.
#
# O trabalho intermediario adotou 50 por cento sem justificar a escolha. Alvos
# menores tendem a produzir escalamento mais agressivo e maior numero de
# replicas em regime, ao custo de capacidade ociosa.
# -----------------------------------------------------------------------------
varredura_a() {
    registrar_cabecalho "Varredura A: alvo de utilizacao"
    for alvo in 30 50 70; do
        for r in $(seq 1 "$REPETICOES"); do
            DURACAO_CARGA=240 POS_CARGA=120 \
                "$EXEC" "$ROTULO" "$REF_RPS" "$alvo" "$REF_JANELA" "$r"
        done
    done
}

# -----------------------------------------------------------------------------
# Varredura B. Sensibilidade a carga oferecida.
#
# Com custo de 50 ms por requisicao, as taxas correspondem a 1, 2 e 4 nucleos
# de carga oferecida. A varredura verifica se o tempo de reacao depende da
# intensidade da carga ou apenas do ciclo de reconciliacao do controlador.
# -----------------------------------------------------------------------------
varredura_b() {
    registrar_cabecalho "Varredura B: carga oferecida"
    for rps in 20 40 80; do
        for r in $(seq 1 "$REPETICOES"); do
            DURACAO_CARGA=240 POS_CARGA=120 \
                "$EXEC" "$ROTULO" "$rps" "$REF_ALVO" "$REF_JANELA" "$r"
        done
    done
}

# -----------------------------------------------------------------------------
# Varredura C. Sensibilidade a janela de estabilizacao de reducao.
#
# Unica varredura que exige observacao integral da fase posterior a carga, uma
# vez que o objeto de estudo e precisamente o intervalo ate o inicio da
# reducao.
# -----------------------------------------------------------------------------
varredura_c() {
    registrar_cabecalho "Varredura C: janela de estabilizacao"
    for janela in 60 180 300; do
        for r in $(seq 1 "$REPETICOES"); do
            DURACAO_CARGA=180 POS_CARGA=$((janela + 150)) \
                "$EXEC" "$ROTULO" "$REF_RPS" "$REF_ALVO" "$janela" "$r"
        done
    done
}

# -----------------------------------------------------------------------------
# Varredura D. Sensibilidade a resolucao do pipeline de metricas.
#
# Investigacao preliminar indicou que o intervalo entre a aplicacao da carga e a
# primeira decisao do autoescalador e dominado pelo tempo que a metrica leva
# para tornar-se visivel, e nao pelo ciclo de reconciliacao do controlador. O
# Metrics Server calcula utilizacao a partir de contadores cumulativos e exige
# duas amostras consecutivas, de modo que a resolucao de coleta impoe um piso ao
# tempo de reacao.
#
# Esta varredura mede essa contribuicao diretamente. O rotulo do ambiente
# incorpora a resolucao empregada, de forma que a analise possa agrupar as
# execucoes sem alterar a convencao de nomes dos arquivos.
#
# Ao final, a resolucao e restaurada ao valor originalmente instalado pelo
# complemento, evitando que execucoes posteriores herdem configuracao alterada.
# -----------------------------------------------------------------------------
varredura_d() {
    registrar_cabecalho "Varredura D: resolucao do pipeline de metricas"
    local resolucao_original="60s"

    for res in 15 30 60; do
        "${RAIZ}/experimentos/definir_resolucao_metricas.sh" "${res}s"
        for r in $(seq 1 "$REPETICOES"); do
            DURACAO_CARGA=240 POS_CARGA=120 \
                "$EXEC" "${ROTULO}-res${res}" "$REF_RPS" "$REF_ALVO" "$REF_JANELA" "$r"
        done
    done

    echo "Restaurando resolucao original (${resolucao_original})"
    "${RAIZ}/experimentos/definir_resolucao_metricas.sh" "$resolucao_original"
}

case "$VARREDURA" in
    A) varredura_a ;;
    B) varredura_b ;;
    C) varredura_c ;;
    D) varredura_d ;;
    TODAS)
        varredura_a
        varredura_b
        varredura_c
        varredura_d
        ;;
    *)
        echo "varredura desconhecida: ${VARREDURA}. Use A, B, C, D ou TODAS."
        exit 1
        ;;
esac

TOTAL=$(( $(date +%s) - INICIO_GERAL ))
echo
echo "============================================================"
echo "Matriz concluida em $((TOTAL / 60)) minutos"
echo "Arquivos gerados:"
ls -1 "${RAIZ}/dados/exp_${ROTULO}_"*.csv 2>/dev/null | wc -l
echo "============================================================"