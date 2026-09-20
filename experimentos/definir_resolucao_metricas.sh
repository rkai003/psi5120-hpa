#!/usr/bin/env bash
# =============================================================================
# PSI5120 - Trabalho Final
# Ajuste da resolucao de coleta do Metrics Server.
#
# MOTIVACAO
# A investigacao do tempo de reacao revelou que o complemento Metrics Server
# instalado pelo Minikube opera com --metric-resolution=60s, e nao com os 15s
# adotados como padrao pelo projeto. Como o calculo de utilizacao de processador
# parte de contadores cumulativos do kubelet, sao necessarias duas amostras
# consecutivas para produzir um valor, o que introduz atraso de ate duas vezes a
# resolucao antes que qualquer variacao de carga se torne visivel ao
# autoescalador.
#
# Este utilitario permite variar esse parametro de forma controlada, de modo que
# sua contribuicao ao tempo de reacao total possa ser medida em vez de suposta.
#
# USO
#   ./definir_resolucao_metricas.sh 15s
#   ./definir_resolucao_metricas.sh 60s
#
# O script aguarda a reimplantacao e a disponibilidade da metrica antes de
# retornar, garantindo que experimentos subsequentes partam de estado estavel.
# =============================================================================

set -uo pipefail

RESOLUCAO="${1:?informe a resolucao, por exemplo 15s ou 60s}"
NAMESPACE_SISTEMA="${NAMESPACE_SISTEMA:-kube-system}"

echo "Ajustando Metrics Server para --metric-resolution=${RESOLUCAO}"

# -----------------------------------------------------------------------------
# Construcao do patch.
#
# A posicao do argumento na lista varia entre versoes e entre formas de
# instalacao, de modo que substituir por indice fixo seria fragil. O script le a
# lista atual, substitui apenas o argumento de resolucao, preservando os demais,
# e reescreve a lista completa.
# -----------------------------------------------------------------------------
ARGS_ATUAIS=$(kubectl -n "$NAMESPACE_SISTEMA" get deployment metrics-server \
    -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null)

if [ -z "$ARGS_ATUAIS" ]; then
    echo "ERRO: nao foi possivel ler a configuracao do Metrics Server"
    exit 1
fi

NOVOS_ARGS=$(python3 - "$ARGS_ATUAIS" "$RESOLUCAO" <<'PY'
import json, sys

args = json.loads(sys.argv[1])
resolucao = sys.argv[2]

saida = [a for a in args if not a.startswith("--metric-resolution")]
saida.append(f"--metric-resolution={resolucao}")
print(json.dumps(saida))
PY
)

kubectl -n "$NAMESPACE_SISTEMA" patch deployment metrics-server --type=json \
    -p="[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":${NOVOS_ARGS}}]" \
    >/dev/null

echo "       aguardando reimplantacao"
kubectl -n "$NAMESPACE_SISTEMA" rollout status deployment/metrics-server --timeout=180s >/dev/null 2>&1

# -----------------------------------------------------------------------------
# Espera pela disponibilidade da metrica.
#
# Apos a reimplantacao, o novo Pod precisa coletar ao menos duas amostras antes
# de reportar utilizacao. Iniciar um experimento antes disso produziria tempo de
# reacao contaminado pela partida do proprio coletor.
# -----------------------------------------------------------------------------
echo "       aguardando metrica ficar disponivel"
for _ in $(seq 1 36); do
    if kubectl top nodes >/dev/null 2>&1; then
        break
    fi
    sleep 5
done

CONFIRMACAO=$(kubectl -n "$NAMESPACE_SISTEMA" get deployment metrics-server \
    -o jsonpath='{.spec.template.spec.containers[0].args}' | tr ',' '\n' | grep metric-resolution)

echo "       configuracao efetiva: ${CONFIRMACAO}"

# Margem adicional para que o coletor acumule amostras suficientes com a nova
# resolucao antes do primeiro experimento.
sleep 30
echo "       pronto"
