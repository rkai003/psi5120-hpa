#!/usr/bin/env python3
"""
PSI5120 - Trabalho Final
Analise agregada da matriz experimental.

PROPOSITO
O trabalho intermediario reportou uma unica execucao por ambiente e declarou,
entre suas limitacoes, a ausencia de caracterizacao estatistica. Este script
processa o conjunto completo de execucoes, agrupa as repeticoes de cada ponto
experimental e reporta media e desvio padrao, o que permite distinguir
diferencas sistematicas de flutuacao entre execucoes.

ENTRADA
Arquivos nomeados segundo a convencao adotada pelo orquestrador:

    exp_<ambiente>_rps<taxa>_alvo<alvo>_jan<janela>_r<repeticao>.csv

Os parametros do ponto experimental sao extraidos do proprio nome, de modo que
nenhum registro auxiliar precisa ser mantido em paralelo.

USO
    python3 analisar_matriz.py dados/exp_minikube_*.csv
    python3 analisar_matriz.py dados/exp_*.csv --figuras artigo-final/figuras
"""

import argparse
import csv
import glob
import json
import os
import re
import statistics
import sys
from datetime import datetime
from typing import Optional


PADRAO = re.compile(
    r"exp_(?P<ambiente>[^_]+)_rps(?P<rps>\d+)_alvo(?P<alvo>\d+)"
    r"_jan(?P<janela>\d+)_r(?P<rep>\d+)\.csv$"
)

# Custo de processador por requisicao, em segundos. Usado para converter a taxa
# oferecida em nucleos, mantendo coerencia com o gerador.
CUSTO_S = 0.050


def identificar(caminho: str) -> Optional[dict]:
    """Extrai os parametros do ponto experimental a partir do nome do arquivo."""
    m = PADRAO.search(os.path.basename(caminho))
    if not m:
        return None
    d = m.groupdict()
    # A varredura de resolucao do pipeline de metricas codifica o parametro no
    # proprio rotulo do ambiente, no formato <nome>-res<valor>, o que evita
    # alterar a convencao de nomes dos arquivos.
    mres = re.search(r"-res(\d+)$", d["ambiente"])
    resolucao = int(mres.group(1)) if mres else None
    base_amb = re.sub(r"-res\d+$", "", d["ambiente"])
    return {
        "ambiente": d["ambiente"],
        "ambiente_base": base_amb,
        "resolucao": resolucao,
        "rps": int(d["rps"]),
        "alvo": int(d["alvo"]),
        "janela": int(d["janela"]),
        "rep": int(d["rep"]),
        "nucleos": round(int(d["rps"]) * CUSTO_S, 2),
        "caminho": caminho,
    }


def ler(caminho: str) -> list:
    """Le a serie temporal e converte os campos numericos."""
    serie = []
    with open(caminho, newline="", encoding="utf-8") as f:
        for ln in csv.DictReader(f):
            def num(c):
                v = (ln.get(c) or "").strip()
                try:
                    return float(v) if v else None
                except ValueError:
                    return None
            # A coluna replicas_deploy e a fonte primaria por refletir o
            # estado do Deployment sem a defasagem do ciclo do controlador.
            # As alternativas mantem compatibilidade com coletas anteriores a
            # revisao da instrumentacao.
            atuais = num("replicas_deploy")
            if atuais is None:
                atuais = num("replicas_pods")
            if atuais is None:
                atuais = num("replicas_atuais")
            p = {
                "t": num("t_rel"),
                "ts": (ln.get("timestamp_utc") or "").strip(),
                "atuais": atuais,
                "hpa": num("replicas_hpa"),
                "prontas": num("replicas_prontas"),
                "cpu": num("cpu_atual_pct"),
                "alvo": num("cpu_alvo_pct"),
                "pend": num("pods_pendentes"),
            }
            if p["t"] is not None:
                serie.append(p)
    return serie


def _para_t_rel(serie: list, instante_utc: str):
    """
    Converte um instante absoluto no tempo relativo da coleta.

    A conversao usa a primeira amostra com timestamp valido como origem, de
    modo que marcadores registrados pelo orquestrador possam ser projetados na
    mesma base de tempo da serie.
    """
    if not instante_utc:
        return None
    try:
        alvo = datetime.strptime(instante_utc, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    for p in serie:
        if p.get("ts"):
            try:
                origem = datetime.strptime(p["ts"], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            return (alvo - origem).total_seconds() + p["t"]
    return None


def metricas(serie: list, marcadores: Optional[dict] = None) -> dict:
    """
    Calcula as grandezas de uma execucao.

    REFERENCIA TEMPORAL
    Quando ha marcadores registrados pelo orquestrador, o inicio e o fim da
    carga sao obtidos deles. Essa e a referencia correta, por corresponder ao
    instante em que a carga foi efetivamente aplicada.

    Na ausencia de marcadores, recorre-se a deteccao por limiar de utilizacao,
    criterio empregado antes da revisao da instrumentacao. Essa alternativa
    introduz deslocamento de ate um ciclo de reconciliacao, uma vez que a
    utilizacao registrada pelo HPA e atualizada apenas a cada ciclo, e por isso
    e mantida somente para compatibilidade com coletas anteriores.
    """
    m = {}
    if not serie:
        return m

    t_ini = t_fim = None
    if marcadores:
        t_ini = _para_t_rel(serie, marcadores.get("carga_inicio_utc", ""))
        t_fim = _para_t_rel(serie, marcadores.get("carga_fim_utc", ""))
        m["referencia"] = "marcadores"

    if t_ini is None:
        m["referencia"] = "limiar"
        t_ini = next((p["t"] for p in serie
                      if p["cpu"] is not None and p["alvo"] is not None
                      and p["cpu"] > p["alvo"]), None)
        t_fim = None
        for p in serie:
            if p["cpu"] is not None and p["alvo"] is not None and p["cpu"] > p["alvo"]:
                t_fim = p["t"]

    # Linha de base tomada na ultima amostra anterior ao inicio da carga, e nao
    # na primeira amostra da serie, para refletir o estado imediatamente antes
    # do estimulo.
    base = None
    for p in serie:
        if p["atuais"] is None:
            continue
        if t_ini is not None and p["t"] > t_ini:
            break
        base = p["atuais"]
    if base is None:
        base = next((p["atuais"] for p in serie if p["atuais"] is not None), None)

    if t_ini is not None and base is not None:
        t_cria = None
        for p in serie:
            if p["t"] >= t_ini and p["atuais"] is not None and p["atuais"] > base:
                m["t_1a_replica"] = p["t"] - t_ini
                t_cria = p["t"]
                break
        # A prontidao so e buscada apos a criacao, para que oscilacoes
        # transitorias anteriores ao escalamento nao sejam interpretadas como
        # replica nova.
        if t_cria is not None:
            for p in serie:
                if p["t"] >= t_cria and p["prontas"] is not None and p["prontas"] > base:
                    m["t_1a_pronta"] = p["t"] - t_ini
                    break

    pico, t_pico = None, None
    for p in serie:
        if p["atuais"] is not None and (pico is None or p["atuais"] > pico):
            pico, t_pico = p["atuais"], p["t"]
    m["replicas_pico"] = pico
    if t_ini is not None and t_pico is not None:
        m["t_pico"] = t_pico - t_ini

    if t_fim is not None and pico is not None:
        for p in serie:
            if p["t"] > t_fim and p["atuais"] is not None and p["atuais"] < pico:
                m["t_reducao"] = p["t"] - t_fim
                break

    cpus = [p["cpu"] for p in serie if p["cpu"] is not None]
    m["cpu_pico"] = max(cpus) if cpus else None

    # Utilizacao em regime, calculada sobre o intervalo em que o sistema ja
    # atingiu o pico de replicas e ainda esta sob carga. Media mais estavel que
    # o valor de pico, que e sensivel a instante de amostragem.
    if t_ini is not None and t_fim is not None and pico is not None:
        regime = [p["cpu"] for p in serie
                  if p["cpu"] is not None and p["atuais"] == pico
                  and t_ini <= p["t"] <= t_fim]
        if regime:
            m["cpu_regime"] = round(statistics.mean(regime), 1)

    pend = [p["pend"] for p in serie if p["pend"] is not None]
    m["pendentes_max"] = max(pend) if pend else 0
    return m


def agregar(execucoes: list) -> dict:
    """Calcula media e desvio padrao de cada grandeza entre repeticoes."""
    chaves = ["t_1a_replica", "t_1a_pronta", "t_pico", "replicas_pico",
              "cpu_pico", "cpu_regime", "t_reducao", "pendentes_max"]
    saida = {"n": len(execucoes)}
    for k in chaves:
        vals = [e[k] for e in execucoes if e.get(k) is not None]
        if not vals:
            saida[k] = (None, None)
            continue
        media = statistics.mean(vals)
        # O desvio amostral exige ao menos duas observacoes.
        desvio = statistics.stdev(vals) if len(vals) > 1 else 0.0
        saida[k] = (round(media, 1), round(desvio, 1))
    return saida


def formatar(par: tuple, unidade: str = "") -> str:
    """Apresenta media e desvio no formato usual de relato experimental."""
    media, desvio = par
    if media is None:
        return "n/d"
    if desvio is None or desvio == 0:
        return f"{media:g}{unidade}"
    return f"{media:g} ± {desvio:g}{unidade}"


def tabela(titulo: str, fator: str, grupos: dict, unidade_fator: str = "") -> None:
    """Imprime uma varredura como tabela comparativa."""
    print()
    print("=" * 78)
    print(titulo)
    print("=" * 78)

    linhas = [
        ("t_1a_replica", "Ate a 1a replica", "s"),
        ("t_1a_pronta", "Ate a 1a pronta", "s"),
        ("t_pico", "Ate o pico", "s"),
        ("replicas_pico", "Replicas no pico", ""),
        ("cpu_regime", "Utilizacao em regime", "%"),
        ("t_reducao", "Ate iniciar reducao", "s"),
        ("pendentes_max", "Pods pendentes", ""),
    ]

    niveis = sorted(grupos.keys())
    larg = 26
    cab = f"{fator:<{larg}}"
    for n in niveis:
        cab += f"{str(n) + unidade_fator:>17}"
    print(cab)
    print(f"{'repeticoes':<{larg}}" + "".join(
        f"{grupos[n]['n']:>17}" for n in niveis))
    print("-" * 78)

    for chave, rotulo, un in linhas:
        linha = f"{rotulo:<{larg}}"
        for n in niveis:
            linha += f"{formatar(grupos[n].get(chave, (None, None)), un):>17}"
        print(linha)
    print("=" * 78)


def graficos(varreduras: dict, destino: str) -> None:
    """Gera os graficos de sensibilidade de cada varredura."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\nmatplotlib nao encontrado. Graficos nao gerados.")
        return

    os.makedirs(destino, exist_ok=True)

    config = [
        ("alvo", "Alvo de utilizacao (%)", "sensibilidade_alvo"),
        ("nucleos", "Carga oferecida (nucleos)", "sensibilidade_carga"),
        ("janela", "Janela de estabilizacao (s)", "sensibilidade_janela"),
        ("resolucao", "Resolucao do Metrics Server (s)", "sensibilidade_resolucao"),
    ]

    for fator, rotulo_x, nome in config:
        grupos = varreduras.get(fator)
        if not grupos or len(grupos) < 2:
            continue

        niveis = sorted(grupos.keys())
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.5, 3.4))

        # Painel esquerdo: tempos de reacao.
        for chave, rot in [("t_1a_replica", "ate a 1a replica"),
                           ("t_pico", "ate o pico")]:
            y = [grupos[n][chave][0] for n in niveis]
            e = [grupos[n][chave][1] for n in niveis]
            if all(v is not None for v in y):
                ax1.errorbar(niveis, y, yerr=e, marker="o", capsize=3,
                             linewidth=1.5, label=rot)
        ax1.set_xlabel(rotulo_x)
        ax1.set_ylabel("Tempo (s)")
        ax1.grid(True, alpha=0.3)
        ax1.legend(fontsize=8)

        # Painel direito: replicas e utilizacao em regime.
        y = [grupos[n]["replicas_pico"][0] for n in niveis]
        e = [grupos[n]["replicas_pico"][1] for n in niveis]
        if all(v is not None for v in y):
            ax2.errorbar(niveis, y, yerr=e, marker="s", capsize=3,
                         linewidth=1.5, color="tab:green", label="replicas no pico")
        ax2.set_xlabel(rotulo_x)
        ax2.set_ylabel("Replicas")
        ax2.grid(True, alpha=0.3)
        ax2.legend(fontsize=8)

        fig.tight_layout()
        fig.savefig(f"{destino}/{nome}.png", dpi=160)
        plt.close(fig)

    print(f"\nGraficos gravados em {destino}/")


def main() -> None:
    p = argparse.ArgumentParser(description="Analise agregada da matriz experimental")
    p.add_argument("arquivos", nargs="+", help="arquivos CSV das execucoes")
    p.add_argument("--figuras", default="artigo-final/figuras")
    p.add_argument("--incluir-r0", action="store_true",
                   help="inclui execucoes de repeticao zero, usadas em teste")
    args = p.parse_args()

    caminhos = []
    for a in args.arquivos:
        caminhos.extend(glob.glob(a)) if any(c in a for c in "*?[") else caminhos.append(a)

    execucoes = []
    for c in sorted(set(caminhos)):
        ident = identificar(c)
        if not ident:
            print(f"ignorado, nome fora do padrao: {c}")
            continue
        # A repeticao zero corresponde a ensaios de verificacao do aparato,
        # conduzidos com duracao reduzida, e nao integra a matriz.
        if ident["rep"] == 0 and not args.incluir_r0:
            continue
        marc = None
        caminho_marc = c.replace(".csv", "_marcadores.json")
        if os.path.exists(caminho_marc):
            try:
                marc = json.load(open(caminho_marc))
            except (json.JSONDecodeError, OSError):
                marc = None
        ident.update(metricas(ler(c), marc))
        execucoes.append(ident)

    if not execucoes:
        print("nenhuma execucao valida encontrada")
        sys.exit(1)

    ambientes = sorted({e["ambiente"] for e in execucoes})
    print(f"{len(execucoes)} execucoes lidas, ambientes: {', '.join(ambientes)}")

    # Transparencia quanto a referencia temporal empregada em cada execucao.
    refs = {}
    for e in execucoes:
        refs[e.get("referencia", "desconhecida")] = refs.get(e.get("referencia", "desconhecida"), 0) + 1
    detalhe = ", ".join(f"{k}: {v}" for k, v in sorted(refs.items()))
    print(f"referencia temporal -> {detalhe}")
    if refs.get("limiar"):
        print("AVISO: execucoes sem marcadores usam deteccao por limiar, sujeita a")
        print("       deslocamento de ate um ciclo de reconciliacao.")

    # Valores de referencia, mantidos fixos quando outro fator e variado.
    REF = {"rps": 40, "alvo": 50, "janela": 300}

    varreduras = {}

    for fator, chave, fixos in [
        ("alvo", "alvo", ["rps", "janela"]),
        ("nucleos", "rps", ["alvo", "janela"]),
        ("janela", "janela", ["rps", "alvo"]),
    ]:
        grupos = {}
        for e in execucoes:
            # Execucoes da varredura de resolucao operam com o pipeline de
            # metricas alterado e nao podem integrar as demais varreduras, ainda
            # que compartilhem os valores de referencia dos fatores do HPA.
            # Inclui-las misturaria condicoes distintas na mesma coluna e
            # inflaria artificialmente o desvio observado.
            if e.get("resolucao") is not None:
                continue
            # Seleciona apenas execucoes em que os demais fatores estao no
            # valor de referencia, isolando o efeito do fator sob estudo.
            if all(e[f] == REF[f] for f in fixos):
                nivel = e["nucleos"] if fator == "nucleos" else e[chave]
                grupos.setdefault(nivel, []).append(e)
        if grupos:
            varreduras[fator] = {k: agregar(v) for k, v in grupos.items()}

    if "alvo" in varreduras:
        tabela("VARREDURA A - sensibilidade ao alvo de utilizacao",
               "Alvo de utilizacao", varreduras["alvo"], "%")
    if "nucleos" in varreduras:
        tabela("VARREDURA B - sensibilidade a carga oferecida",
               "Carga oferecida", varreduras["nucleos"], " nucleos")
    if "janela" in varreduras:
        tabela("VARREDURA C - sensibilidade a janela de estabilizacao",
               "Janela de reducao", varreduras["janela"], "s")

    # Varredura D. Agrupa por resolucao do pipeline de metricas, extraida do
    # rotulo do ambiente, mantendo os demais fatores no valor de referencia.
    grupos_res = {}
    for e in execucoes:
        if e.get("resolucao") is None:
            continue
        if all(e[f] == REF[f] for f in ("rps", "alvo", "janela")):
            grupos_res.setdefault(e["resolucao"], []).append(e)
    if grupos_res:
        varreduras["resolucao"] = {k: agregar(v) for k, v in grupos_res.items()}
        tabela("VARREDURA D - sensibilidade a resolucao do pipeline de metricas",
               "Resolucao de coleta", varreduras["resolucao"], "s")

    graficos(varreduras, args.figuras)


if __name__ == "__main__":
    main()