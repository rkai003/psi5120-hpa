#!/usr/bin/env python3
"""
PSI5120 - Atividade Pratica 1
Analise das series temporais coletadas durante os testes de autoescalamento.

PROPOSITO
O enunciado pede analise comparativa do tempo de reacao do HPA entre a
implantacao local e a implantacao em nuvem. Este script transforma os arquivos
CSV produzidos por coleta_metricas.sh em grandezas objetivas e em graficos
utilizaveis no artigo.

METRICAS CALCULADAS
    t_primeira_replica    segundos entre o inicio da carga e a criacao da
                          primeira replica adicional, ou seja, a latencia de
                          deteccao do controlador
    t_primeira_pronta     segundos ate essa replica passar na readiness probe e
                          comecar a receber trafego, o que inclui o tempo de
                          partida do container
    t_pico                segundos ate atingir o numero maximo de replicas
    replicas_pico         maior numero de replicas observado
    t_inicio_reducao      segundos entre o fim da carga e a primeira reducao,
                          grandeza dominada pela janela de estabilizacao
    cpu_pico_pct          maior utilizacao de CPU registrada

O inicio da carga e detectado automaticamente como o primeiro instante em que a
utilizacao de CPU ultrapassa o alvo configurado. Esse criterio evita depender de
anotacao manual do horario em que o gerador foi aplicado.

USO
    python3 analisar.py dados/minikube.csv dados/eks.csv

    python3 analisar.py dados/minikube.csv          # analise de um unico ambiente

SAIDA
    Tabela de metricas no terminal, em formato pronto para transcricao no artigo.
    Arquivos PNG em artigo/figuras/ quando matplotlib estiver disponivel.
"""

import csv
import os
import sys
from typing import Optional


# ---------------------------------------------------------------------------
# Leitura e limpeza
# ---------------------------------------------------------------------------
def ler_serie(caminho: str) -> list:
    """
    Le o CSV e converte os campos numericos.

    Linhas em que o Metrics Server ainda nao havia reportado dados possuem
    campos vazios. Elas sao preservadas com valor None em vez de descartadas,
    para que a base de tempo permaneca continua.
    """
    with open(caminho, newline="", encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))

    serie = []
    for ln in linhas:
        def num(campo: str) -> Optional[float]:
            v = (ln.get(campo) or "").strip()
            if v == "":
                return None
            try:
                return float(v)
            except ValueError:
                return None

        serie.append({
            "t": num("t_rel"),
            "ts": ln.get("timestamp_utc", ""),
            "desejadas": num("replicas_desejadas"),
            "atuais": num("replicas_atuais"),
            "prontas": num("replicas_prontas"),
            "cpu": num("cpu_atual_pct"),
            "alvo": num("cpu_alvo_pct"),
            "pendentes": num("pods_pendentes"),
        })

    return [p for p in serie if p["t"] is not None]


# ---------------------------------------------------------------------------
# Deteccao dos eventos de interesse
# ---------------------------------------------------------------------------
def detectar_inicio_carga(serie: list) -> Optional[float]:
    """
    Primeiro instante em que a utilizacao de CPU ultrapassa o alvo do HPA.

    Usar o proprio alvo como limiar torna o criterio independente da
    intensidade da carga aplicada e comparavel entre ambientes.
    """
    for p in serie:
        if p["cpu"] is not None and p["alvo"] is not None and p["cpu"] > p["alvo"]:
            return p["t"]
    return None


def detectar_fim_carga(serie: list) -> Optional[float]:
    """
    Ultimo instante com utilizacao acima do alvo.

    Corresponde, na pratica, ao momento em que o gerador de carga foi removido.
    """
    ultimo = None
    for p in serie:
        if p["cpu"] is not None and p["alvo"] is not None and p["cpu"] > p["alvo"]:
            ultimo = p["t"]
    return ultimo


def calcular_metricas(serie: list, rotulo: str) -> dict:
    """Extrai as grandezas usadas na comparacao entre ambientes."""
    m = {"ambiente": rotulo}

    if not serie:
        return m

    t_ini = detectar_inicio_carga(serie)
    t_fim = detectar_fim_carga(serie)
    m["t_inicio_carga"] = t_ini
    m["t_fim_carga"] = t_fim

    # Baseline de replicas antes da carga.
    base = None
    for p in serie:
        if p["atuais"] is not None:
            base = p["atuais"]
            break
    m["replicas_iniciais"] = base

    # Primeira replica adicional apos o inicio da carga.
    if t_ini is not None and base is not None:
        for p in serie:
            if p["t"] >= t_ini and p["atuais"] is not None and p["atuais"] > base:
                m["t_primeira_replica"] = p["t"] - t_ini
                break

        # Primeira replica adicional efetivamente pronta para receber trafego.
        for p in serie:
            if p["t"] >= t_ini and p["prontas"] is not None and p["prontas"] > base:
                m["t_primeira_pronta"] = p["t"] - t_ini
                break

    # Pico de replicas e instante em que foi atingido.
    pico, t_pico = None, None
    for p in serie:
        if p["atuais"] is not None and (pico is None or p["atuais"] > pico):
            pico, t_pico = p["atuais"], p["t"]
    m["replicas_pico"] = pico
    if t_ini is not None and t_pico is not None:
        m["t_pico"] = t_pico - t_ini

    # Inicio da reducao apos o fim da carga.
    if t_fim is not None and pico is not None:
        for p in serie:
            if p["t"] > t_fim and p["atuais"] is not None and p["atuais"] < pico:
                m["t_inicio_reducao"] = p["t"] - t_fim
                break

    # Pico de utilizacao de CPU.
    cpus = [p["cpu"] for p in serie if p["cpu"] is not None]
    m["cpu_pico_pct"] = max(cpus) if cpus else None

    # Pods pendentes, indicando insuficiencia de capacidade do no.
    pend = [p["pendentes"] for p in serie if p["pendentes"] is not None]
    m["pendentes_max"] = max(pend) if pend else 0

    return m


# ---------------------------------------------------------------------------
# Apresentacao
# ---------------------------------------------------------------------------
def imprimir_metricas(lista: list) -> None:
    """Imprime a tabela comparativa no terminal."""
    campos = [
        ("replicas_iniciais", "Replicas iniciais", ""),
        ("t_primeira_replica", "Ate a 1a replica adicional", "s"),
        ("t_primeira_pronta", "Ate a 1a replica pronta", "s"),
        ("t_pico", "Ate o pico de replicas", "s"),
        ("replicas_pico", "Replicas no pico", ""),
        ("cpu_pico_pct", "Pico de utilizacao de CPU", "%"),
        ("t_inicio_reducao", "Ate iniciar a reducao", "s"),
        ("pendentes_max", "Maximo de Pods pendentes", ""),
    ]

    largura = 32
    print("\n" + "=" * (largura + 18 * len(lista)))
    print("METRICAS DE AUTOESCALAMENTO")
    print("=" * (largura + 18 * len(lista)))

    cabecalho = "Grandeza".ljust(largura)
    for m in lista:
        cabecalho += m["ambiente"].rjust(18)
    print(cabecalho)
    print("-" * (largura + 18 * len(lista)))

    for chave, rotulo, unidade in campos:
        linha = rotulo.ljust(largura)
        for m in lista:
            v = m.get(chave)
            if v is None:
                txt = "n/d"
            elif isinstance(v, float) and v == int(v):
                txt = f"{int(v)}{unidade}"
            else:
                txt = f"{v}{unidade}"
            linha += txt.rjust(18)
        print(linha)

    print("=" * (largura + 18 * len(lista)) + "\n")


def gerar_graficos(series: dict, destino: str = "artigo/figuras") -> None:
    """
    Gera os graficos usados no artigo.

    A dependencia de matplotlib e opcional. Caso a biblioteca nao esteja
    instalada, o script informa e segue, pois as metricas numericas ja foram
    calculadas e sao suficientes para a analise.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")  # backend sem interface grafica
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib nao encontrado. Graficos nao gerados.")
        print("Instale com: pip install matplotlib")
        return

    os.makedirs(destino, exist_ok=True)

    # -- Grafico 1: evolucao do numero de replicas -------------------------
    fig, ax = plt.subplots(figsize=(7, 3.6))
    for rotulo, serie in series.items():
        t = [p["t"] for p in serie if p["atuais"] is not None]
        r = [p["atuais"] for p in serie if p["atuais"] is not None]
        ax.plot(t, r, label=rotulo, linewidth=1.6)
    ax.set_xlabel("Tempo desde o inicio da coleta (s)")
    ax.set_ylabel("Replicas")
    ax.set_title("Evolucao do numero de replicas")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(f"{destino}/replicas.png", dpi=160)
    plt.close(fig)

    # -- Grafico 2: utilizacao de CPU --------------------------------------
    fig, ax = plt.subplots(figsize=(7, 3.6))
    for rotulo, serie in series.items():
        t = [p["t"] for p in serie if p["cpu"] is not None]
        c = [p["cpu"] for p in serie if p["cpu"] is not None]
        ax.plot(t, c, label=rotulo, linewidth=1.4)
    # Linha do alvo configurado, para leitura direta do gatilho de escala.
    alvos = [p["alvo"] for s in series.values() for p in s if p["alvo"] is not None]
    if alvos:
        ax.axhline(alvos[0], linestyle="--", linewidth=1.0,
                   label=f"Alvo ({int(alvos[0])}%)")
    ax.set_xlabel("Tempo desde o inicio da coleta (s)")
    ax.set_ylabel("Utilizacao de CPU (% de requests)")
    ax.set_title("Utilizacao de CPU observada pelo HPA")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(f"{destino}/cpu.png", dpi=160)
    plt.close(fig)

    print(f"Graficos gravados em {destino}/")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    series, metricas = {}, []

    for caminho in sys.argv[1:]:
        if not os.path.exists(caminho):
            print(f"arquivo nao encontrado: {caminho}")
            continue
        # O rotulo do ambiente vem do nome do arquivo, por exemplo
        # dados/minikube.csv resulta em "minikube".
        rotulo = os.path.splitext(os.path.basename(caminho))[0]
        serie = ler_serie(caminho)
        series[rotulo] = serie
        metricas.append(calcular_metricas(serie, rotulo))
        print(f"lido {caminho}: {len(serie)} amostras")

    if metricas:
        imprimir_metricas(metricas)
        gerar_graficos(series)


if __name__ == "__main__":
    main()
