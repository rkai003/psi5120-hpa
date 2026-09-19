#!/usr/bin/env bash
# =============================================================================
# PSI5120 - Trabalho Final
# Orquestrador da matriz experimental.
#
# PROPOSITO
# O trabalho intermediario reportou resultados de execucao unica por ambiente e
# declarou, entre suas limitacoes, a ausencia de caracterizacao estatistica.
# Repetir manualmente cada ponto experimental seria inviavel e introduziria
# variacao de procedimento entre execucoes.
#
# Este script executa um ponto experimental completo sem supervisao, garantindo
# que todas as repeticoes sigam exatamente o mesmo procedimento: restaura o
# estado inicial, aguarda estabilizacao, inicia a coleta, aplica a carga
# calibrada, aguarda a reducao e encerra.
#
# USO
#   ./executar_experimento.sh <rotulo> <rps> <alvo_cpu> <janela_reducao> [repeticao]
#
# EXEMPLO
#   ./executar_experimento.sh minikube 40 50 300 1
#
# SAIDA
#   dados/exp_<rotulo>_rps<rps>_alvo<alvo>_jan<janela>_r<repeticao>.csv
#   dados/exp_<...>_gerador.json   relatorio do gerador, com aderencia
# =============================================================================

set -uo pipefail

NAMESPACE="${NAMESPACE:-hpa-demo}"
RAIZ="${RAIZ:-$(cd "$(dirname "$0")/.." && pwd)}"

ROTULO="${1:?informe o rotulo do ambiente, por exemplo minikube ou eks}"
RPS="${2:?informe a taxa de requisicoes por segundo}"
ALVO_CPU="${3:-50}"
JANELA="${4:-300}"
REPETICAO="${5:-1}"

# Duracao da fase de carga. Precisa exceder o tempo de subida ate o teto de
# replicas com margem para observar o regime estavel.
DURACAO_CARGA="${DURACAO_CARGA:-300}"

# Tempo de observacao apos a remocao da carga. Deve exceder a janela de
# estabilizacao para que o inicio da reducao seja observado.
POS_CARGA="${POS_CARGA:-$((JANELA + 180))}"

# Tempo de repouso registrado antes da aplicacao da carga, usado como linha de
# base pela deteccao automatica do inicio da carga.
PRE_CARGA="${PRE_CARGA:-45}"

BASE="exp_${ROTULO}_rps${RPS}_alvo${ALVO_CPU}_jan${JANELA}_r${REPETICAO}"
CSV="${RAIZ}/dados/${BASE}.csv"
JSON_GERADOR="${RAIZ}/dados/${BASE}_gerador.json"

mkdir -p "${RAIZ}/dados"

echo "============================================================"
echo "Ponto experimental: ${BASE}"
echo "  taxa alvo            ${RPS} req/s"
echo "  carga oferecida      $(python3 -c "print(f'{${RPS}*0.05:.2f}')") nucleos"
echo "  alvo de utilizacao   ${ALVO_CPU}%"
echo "  janela de reducao    ${JANELA}s"
echo "============================================================"

# -----------------------------------------------------------------------------
# Etapa 1. Restauracao do estado inicial.
#
# Remove geradores remanescentes de execucoes anteriores e reduz o Deployment a
# uma replica. Sem essa restauracao, o ponto experimental partiria de um estado
# indeterminado e o tempo de reacao medido nao seria comparavel entre
# repeticoes.
# -----------------------------------------------------------------------------
echo "[1/6] restaurando estado inicial"
kubectl delete pod gerador-rps -n "$NAMESPACE" --ignore-not-found --wait=true >/dev/null 2>&1
kubectl delete hpa web-hpa -n "$NAMESPACE" --ignore-not-found >/dev/null 2>&1
kubectl scale deployment/web -n "$NAMESPACE" --replicas=1 >/dev/null 2>&1
kubectl rollout status deployment/web -n "$NAMESPACE" --timeout=120s >/dev/null 2>&1

# -----------------------------------------------------------------------------
# Etapa 2. Aplicacao do autoescalador com os parametros do ponto experimental.
#
# O manifesto base e transformado em tempo de execucao para refletir o alvo de
# utilizacao e a janela de estabilizacao sob estudo. A transformacao ocorre em
# arquivo temporario, de modo que o manifesto versionado permanece inalterado.
# -----------------------------------------------------------------------------
echo "[2/6] aplicando HPA com alvo=${ALVO_CPU}% e janela=${JANELA}s"
TMP_HPA="$(mktemp)"
sed -e "s/averageUtilization: .*/averageUtilization: ${ALVO_CPU}/" \
    -e "s/stabilizationWindowSeconds: 300/stabilizationWindowSeconds: ${JANELA}/" \
    "${RAIZ}/k8s/30-hpa.yaml" > "$TMP_HPA"
kubectl apply -f "$TMP_HPA" >/dev/null
rm -f "$TMP_HPA"

# Aguarda o autoescalador reportar metrica. Enquanto o valor permanece
# desconhecido, nenhuma decisao de escala e tomada, e iniciar a carga nesse
# estado deslocaria a medida do tempo de reacao.
echo "       aguardando metrica ficar disponivel"
for _ in $(seq 1 24); do
    ALVOS=$(kubectl get hpa web-hpa -n "$NAMESPACE" \
        -o jsonpath='{.status.currentMetrics[0].resource.current.averageUtilization}' 2>/dev/null)
    [ -n "$ALVOS" ] && break
    sleep 5
done
[ -z "${ALVOS:-}" ] && echo "       AVISO: metrica ainda indisponivel, prosseguindo"

# -----------------------------------------------------------------------------
# Etapa 3. Coleta em segundo plano.
#
# A coleta cobre todo o ciclo, do repouso a reducao completa, em arquivo unico.
# Isso evita a emenda de coletas separadas que foi necessaria no trabalho
# intermediario.
# -----------------------------------------------------------------------------
DURACAO_TOTAL=$((PRE_CARGA + DURACAO_CARGA + POS_CARGA))
echo "[3/6] iniciando coleta por ${DURACAO_TOTAL}s"
"${RAIZ}/scripts/coleta_metricas.sh" "$CSV" "$DURACAO_TOTAL" 5 > /dev/null 2>&1 &
PID_COLETA=$!

echo "       registrando linha de base por ${PRE_CARGA}s"
sleep "$PRE_CARGA"

# -----------------------------------------------------------------------------
# Etapa 4. Aplicacao da carga calibrada.
# -----------------------------------------------------------------------------
echo "[4/6] aplicando carga de ${RPS} req/s por ${DURACAO_CARGA}s"
kubectl create configmap gerador-rps -n "$NAMESPACE" \
    --from-file=gerador_rps.py="${RAIZ}/experimentos/gerador_rps.py" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null

TMP_GER="$(mktemp)"
sed -e "s/value: \"40\"/value: \"${RPS}\"/" \
    -e "s/value: \"300\"/value: \"${DURACAO_CARGA}\"/" \
    "${RAIZ}/k8s/41-gerador-rps.yaml" > "$TMP_GER"
kubectl apply -f "$TMP_GER" >/dev/null
rm -f "$TMP_GER"

sleep "$DURACAO_CARGA"

# -----------------------------------------------------------------------------
# Etapa 5. Coleta do relatorio do gerador e remocao da carga.
#
# O relatorio final do gerador informa a taxa efetivamente alcancada. Execucoes
# cuja aderencia a taxa alvo seja insuficiente devem ser descartadas, pois nesse
# caso o gargalo foi o proprio gerador e nao o sistema sob teste.
# -----------------------------------------------------------------------------
echo "[5/6] coletando relatorio do gerador"

# -----------------------------------------------------------------------------
# Espera pelo encerramento efetivo do Pod gerador.
#
# Um intervalo fixo seria insuficiente, uma vez que o instante de termino
# depende do tempo de agendamento do Pod e do periodo em que o gerador aguarda
# a conclusao das requisicoes pendentes antes de emitir o relatorio final. Ler
# os registros antes disso retorna saida incompleta, sem a linha que informa a
# aderencia a taxa alvo.
#
# A espera observa a fase do Pod e admite limite maximo, de modo que uma falha
# do gerador nao interrompa a execucao da matriz.
# -----------------------------------------------------------------------------
for _ in $(seq 1 24); do
    FASE=$(kubectl get pod gerador-rps -n "$NAMESPACE" \
        -o jsonpath='{.status.phase}' 2>/dev/null)
    case "$FASE" in
        Succeeded|Failed) break ;;
        "") break ;;
    esac
    sleep 5
done
[ "${FASE:-}" = "Running" ] && echo "       AVISO: gerador ainda em execucao apos o limite de espera"

kubectl logs gerador-rps -n "$NAMESPACE" 2>/dev/null > "$JSON_GERADOR" || true

if [ -s "$JSON_GERADOR" ]; then
    python3 - "$JSON_GERADOR" <<'PY'
import json, sys

# -----------------------------------------------------------------------------
# Avaliacao da carga aplicada.
#
# A aderencia acumulada inclui a fase inicial, em que o servico opera com uma
# unica replica e e incapaz de atender a taxa alvo por razao fisica. Avaliar a
# validade da carga por esse valor levaria a descartar execucoes corretas.
#
# O criterio adotado e a aderencia em regime, calculada sobre a segunda metade
# dos relatorios de progresso, quando o escalamento ja se completou. Valores
# abaixo de 0,95 nesse periodo indicam que o gerador, e nao o sistema sob teste,
# limitou a carga.
# -----------------------------------------------------------------------------
registros = []
for linha in open(sys.argv[1]):
    linha = linha.strip()
    if not linha.startswith("{"):
        continue
    try:
        registros.append(json.loads(linha))
    except json.JSONDecodeError:
        pass

progresso = [r for r in registros if r.get("evento") in ("progresso", "final")]
if not progresso:
    print("       AVISO: nenhum relatorio de progresso obtido")
    sys.exit(0)

final = progresso[-1]
print(f"       taxa alvo {final.get('rps_alvo')} req/s, "
      f"acumulada {final.get('rps_acumulado')} req/s")

metade = progresso[len(progresso) // 2:]
janelas = [r.get("aderencia_janela") for r in metade
           if r.get("aderencia_janela") is not None]

if janelas:
    regime = sum(janelas) / len(janelas)
    print(f"       aderencia em regime {regime:.3f} "
          f"(media de {len(janelas)} janelas)")
    if regime < 0.95:
        print("       AVISO: gerador nao sustentou a carga. Candidata a descarte.")
else:
    ac = final.get("aderencia_acumulada")
    print(f"       aderencia acumulada {ac} (sem janelas suficientes)")

print(f"       latencia p50 {final.get('latencia_p50_ms')} ms, "
      f"p95 {final.get('latencia_p95_ms')} ms")
PY
else
    echo "       AVISO: relatorio do gerador nao obtido"
fi

kubectl delete pod gerador-rps -n "$NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1

# -----------------------------------------------------------------------------
# Etapa 6. Observacao da reducao.
# -----------------------------------------------------------------------------
echo "[6/6] observando reducao por ${POS_CARGA}s"
wait "$PID_COLETA" 2>/dev/null || true

AMOSTRAS=$(( $(wc -l < "$CSV") - 1 ))
echo "Concluido. ${AMOSTRAS} amostras em ${CSV}"
echo