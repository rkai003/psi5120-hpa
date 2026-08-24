#!/usr/bin/env bash
# =============================================================================
# PSI5120 - Atividade Pratica 1
# Coleta de serie temporal do autoescalamento.
#
# PROPOSITO
# O enunciado pede analise comparativa do tempo de reacao do HPA entre os dois
# ambientes. Capturas de tela mostram que o escalamento ocorreu, mas nao
# permitem medir quando ocorreu nem comparar dois ambientes com precisao.
#
# Este script amostra o estado do autoescalador em intervalos regulares e grava
# os valores em CSV, o que torna possivel calcular o intervalo entre o inicio
# da carga e a criacao da primeira replica adicional, o tempo ate a
# estabilizacao e o comportamento durante a reducao.
#
# USO
#   ./coleta_metricas.sh <arquivo_saida.csv> [duracao_segundos] [intervalo_s]
#
# EXEMPLO
#   ./coleta_metricas.sh dados/minikube.csv 900 5
#
# O script encerra sozinho ao fim da duracao, ou pode ser interrompido com
# Ctrl+C sem perda dos dados ja gravados.
# =============================================================================

set -uo pipefail

NAMESPACE="${NAMESPACE:-hpa-demo}"
NOME_HPA="${NOME_HPA:-web-hpa}"
NOME_DEPLOY="${NOME_DEPLOY:-web}"

ARQUIVO="${1:-coleta.csv}"
DURACAO="${2:-900}"      # duracao total da coleta, em segundos
INTERVALO="${3:-5}"      # periodo de amostragem, em segundos

# Cria o diretorio de destino caso ainda nao exista.
mkdir -p "$(dirname "$ARQUIVO")"

# -----------------------------------------------------------------------------
# Cabecalho do CSV.
#
#   t_rel               segundos decorridos desde o inicio da coleta
#   timestamp_utc       instante absoluto, para correlacionar com eventos
#   replicas_desejadas  valor que o HPA calculou
#   replicas_atuais     replicas existentes no Deployment
#   replicas_prontas    replicas que passaram na readiness probe
#   cpu_atual_pct       utilizacao media observada, em porcentagem de requests
#   cpu_alvo_pct        alvo configurado no HPA
#   pods_pendentes      replicas sem no atribuido, indicando falta de capacidade
# -----------------------------------------------------------------------------
echo "t_rel,timestamp_utc,replicas_desejadas,replicas_atuais,replicas_prontas,cpu_atual_pct,cpu_alvo_pct,pods_pendentes" > "$ARQUIVO"

INICIO=$(date +%s)
echo "Coletando por ${DURACAO}s a cada ${INTERVALO}s. Saida: ${ARQUIVO}"
echo "Interrompa com Ctrl+C se necessario; os dados ja gravados sao preservados."

while true; do
    AGORA=$(date +%s)
    T_REL=$((AGORA - INICIO))
    [ "$T_REL" -ge "$DURACAO" ] && break

    TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # -------------------------------------------------------------------------
    # Estado do HPA.
    #
    # A leitura usa jsonpath em vez do formato tabular porque a saida tabular
    # do kubectl pode mudar entre versoes, e os dois clusters do trabalho
    # executam versoes diferentes do Kubernetes.
    #
    # currentCPUUtilizationPercentage aparece vazio enquanto o Metrics Server
    # ainda nao reportou dados. Nesse caso registra-se o valor vazio, que a
    # analise interpreta como ausencia de metrica.
    # -------------------------------------------------------------------------
    HPA_JSON=$(kubectl get hpa "$NOME_HPA" -n "$NAMESPACE" -o json 2>/dev/null)

    if [ -z "$HPA_JSON" ]; then
        # HPA ainda nao existe ou o cluster nao respondeu. Registra linha vazia
        # para preservar a continuidade temporal da serie.
        echo "${T_REL},${TS},,,,,," >> "$ARQUIVO"
        sleep "$INTERVALO"
        continue
    fi

    DESEJADAS=$(echo "$HPA_JSON" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status',{}).get('desiredReplicas',''))" 2>/dev/null)
    ATUAIS=$(echo "$HPA_JSON"    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status',{}).get('currentReplicas',''))" 2>/dev/null)
    CPU_ATUAL=$(echo "$HPA_JSON" | python3 -c "
import sys,json
d=json.load(sys.stdin)
# Em autoscaling/v2 a metrica fica em status.currentMetrics.
v=''
for m in (d.get('status',{}).get('currentMetrics') or []):
    r=(m or {}).get('resource') or {}
    if r.get('name')=='cpu':
        v=(r.get('current') or {}).get('averageUtilization','')
print(v if v is not None else '')
" 2>/dev/null)
    CPU_ALVO=$(echo "$HPA_JSON" | python3 -c "
import sys,json
d=json.load(sys.stdin)
v=''
for m in (d.get('spec',{}).get('metrics') or []):
    r=(m or {}).get('resource') or {}
    if r.get('name')=='cpu':
        v=(r.get('target') or {}).get('averageUtilization','')
print(v if v is not None else '')
" 2>/dev/null)

    # Replicas prontas, lidas do Deployment. O HPA informa quantas existem,
    # nao quantas ja recebem trafego.
    PRONTAS=$(kubectl get deployment "$NOME_DEPLOY" -n "$NAMESPACE" \
        -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
    PRONTAS=${PRONTAS:-0}

    # Pods sem no atribuido. Valor maior que zero indica que o cluster nao tem
    # capacidade para acomodar as replicas solicitadas, situacao que evidencia
    # que escalar Pods nao equivale a escalar nos.
    PENDENTES=$(kubectl get pods -n "$NAMESPACE" -l app=web \
        --field-selector=status.phase=Pending --no-headers 2>/dev/null | wc -l)

    echo "${T_REL},${TS},${DESEJADAS},${ATUAIS},${PRONTAS},${CPU_ATUAL},${CPU_ALVO},${PENDENTES}" >> "$ARQUIVO"

    # Eco resumido no terminal, util para acompanhar a execucao ao vivo.
    printf "t=%4ss  desejadas=%-3s prontas=%-3s cpu=%-5s%% alvo=%s%% pendentes=%s\n" \
        "$T_REL" "${DESEJADAS:--}" "$PRONTAS" "${CPU_ATUAL:--}" "${CPU_ALVO:--}" "$PENDENTES"

    sleep "$INTERVALO"
done

echo "Coleta concluida. Linhas gravadas: $(( $(wc -l < "$ARQUIVO") - 1 ))"
