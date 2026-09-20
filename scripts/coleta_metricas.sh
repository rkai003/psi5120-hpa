#!/usr/bin/env bash
# =============================================================================
# PSI5120 - Coleta de serie temporal do autoescalamento.
#
# REVISAO PARA O TRABALHO FINAL
# A versao empregada no trabalho intermediario obtinha o numero de replicas do
# campo status.currentReplicas do proprio HorizontalPodAutoscaler. Esse campo e
# atualizado apenas a cada ciclo de reconciliacao, tipicamente de quinze
# segundos, ao passo que o Deployment reflete a contagem de forma continua.
#
# A defasagem entre as duas fontes produziu amostras internamente
# inconsistentes, com numero de replicas prontas superior ao numero de replicas
# existentes, o que e impossivel. Como a mesma fonte defasada era usada para
# detectar o inicio da carga pela utilizacao de processador, a referencia
# temporal de todas as medidas ficava deslocada em ate um ciclo.
#
# Esta versao separa explicitamente tres origens:
#
#   replicas_deploy    contagem no Deployment, atualizada continuamente
#   replicas_pods      contagem direta de Pods, verificacao independente
#   replicas_hpa       contagem no status do HPA, preservada para evidenciar
#                      a defasagem entre a decisao e o estado observado
#
# A utilizacao de processador permanece sendo lida do HPA, pois e a grandeza
# sobre a qual o controlador decide, porem passa a ser interpretada como valor
# amostrado pelo controlador e nao como medida instantanea do sistema.
#
# USO
#   ./coleta_metricas.sh <arquivo_saida.csv> [duracao_segundos] [intervalo_s]
# =============================================================================

set -uo pipefail

NAMESPACE="${NAMESPACE:-hpa-demo}"
NOME_HPA="${NOME_HPA:-web-hpa}"
NOME_DEPLOY="${NOME_DEPLOY:-web}"
ROTULO_APP="${ROTULO_APP:-app=web}"

ARQUIVO="${1:-coleta.csv}"
DURACAO="${2:-900}"
INTERVALO="${3:-5}"

mkdir -p "$(dirname "$ARQUIVO")"

# -----------------------------------------------------------------------------
# Cabecalho do CSV.
#
#   t_rel              segundos desde o inicio da coleta
#   timestamp_utc      instante absoluto, base para correlacao com marcadores
#   replicas_desejadas valor calculado pelo HPA
#   replicas_hpa       replicas registradas no status do HPA, com defasagem
#   replicas_deploy    replicas registradas no Deployment, sem defasagem
#   replicas_pods      Pods contados diretamente, verificacao independente
#   replicas_prontas   Pods aprovados na sonda de prontidao
#   cpu_atual_pct      utilizacao amostrada pelo controlador
#   cpu_alvo_pct       alvo configurado
#   pods_pendentes     Pods sem no atribuido
# -----------------------------------------------------------------------------
echo "t_rel,timestamp_utc,replicas_desejadas,replicas_hpa,replicas_deploy,replicas_pods,replicas_prontas,cpu_atual_pct,cpu_alvo_pct,pods_pendentes" > "$ARQUIVO"

INICIO=$(date +%s)
echo "Coletando por ${DURACAO}s a cada ${INTERVALO}s em ${ARQUIVO}"

while true; do
    AGORA=$(date +%s)
    T_REL=$((AGORA - INICIO))
    [ "$T_REL" -ge "$DURACAO" ] && break

    TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    # -------------------------------------------------------------------------
    # Estado do Deployment. Fonte primaria da contagem de replicas, por ser
    # atualizada a cada alteracao e nao a cada ciclo do controlador.
    # -------------------------------------------------------------------------
    DEPLOY_JSON=$(kubectl get deployment "$NOME_DEPLOY" -n "$NAMESPACE" -o json 2>/dev/null)
    if [ -n "$DEPLOY_JSON" ]; then
        REP_DEPLOY=$(echo "$DEPLOY_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',{}).get('replicas',''))" 2>/dev/null)
        PRONTAS=$(echo "$DEPLOY_JSON"    | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',{}).get('readyReplicas',0) or 0)" 2>/dev/null)
    else
        REP_DEPLOY=""; PRONTAS=""
    fi

    # Contagem direta de Pods. Serve de verificacao independente do campo
    # anterior e permite identificar Pods em encerramento, que ainda existem
    # mas ja nao sao contabilizados pelo Deployment.
    REP_PODS=$(kubectl get pods -n "$NAMESPACE" -l "$ROTULO_APP" \
        --no-headers 2>/dev/null | wc -l)

    PENDENTES=$(kubectl get pods -n "$NAMESPACE" -l "$ROTULO_APP" \
        --field-selector=status.phase=Pending --no-headers 2>/dev/null | wc -l)

    # -------------------------------------------------------------------------
    # Estado do HPA. Fornece a decisao do controlador e a metrica sobre a qual
    # ela se baseia. Ambos sao atualizados por ciclo, e nao continuamente.
    # -------------------------------------------------------------------------
    HPA_JSON=$(kubectl get hpa "$NOME_HPA" -n "$NAMESPACE" -o json 2>/dev/null)
    if [ -n "$HPA_JSON" ]; then
        LEITURA=$(echo "$HPA_JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
st = d.get('status', {}) or {}
sp = d.get('spec', {}) or {}
desejadas = st.get('desiredReplicas', '')
hpa_rep = st.get('currentReplicas', '')
cpu = ''
for m in (st.get('currentMetrics') or []):
    r = (m or {}).get('resource') or {}
    if r.get('name') == 'cpu':
        cpu = (r.get('current') or {}).get('averageUtilization', '')
alvo = ''
for m in (sp.get('metrics') or []):
    r = (m or {}).get('resource') or {}
    if r.get('name') == 'cpu':
        alvo = (r.get('target') or {}).get('averageUtilization', '')
print('|'.join(str(x if x is not None else '') for x in (desejadas, hpa_rep, cpu, alvo)))
" 2>/dev/null)
        DESEJADAS=$(echo "$LEITURA" | cut -d'|' -f1)
        REP_HPA=$(echo "$LEITURA"   | cut -d'|' -f2)
        CPU_ATUAL=$(echo "$LEITURA" | cut -d'|' -f3)
        CPU_ALVO=$(echo "$LEITURA"  | cut -d'|' -f4)
    else
        DESEJADAS=""; REP_HPA=""; CPU_ATUAL=""; CPU_ALVO=""
    fi

    echo "${T_REL},${TS},${DESEJADAS},${REP_HPA},${REP_DEPLOY},${REP_PODS},${PRONTAS},${CPU_ATUAL},${CPU_ALVO},${PENDENTES}" >> "$ARQUIVO"

    printf "t=%4ss  deploy=%-3s pods=%-3s prontas=%-3s hpa=%-3s cpu=%-5s%% pend=%s\n" \
        "$T_REL" "${REP_DEPLOY:--}" "$REP_PODS" "${PRONTAS:--}" "${REP_HPA:--}" \
        "${CPU_ATUAL:--}" "$PENDENTES"

    sleep "$INTERVALO"
done

echo "Coleta concluida. Linhas gravadas: $(( $(wc -l < "$ARQUIVO") - 1 ))"