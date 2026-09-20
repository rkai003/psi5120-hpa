#!/usr/bin/env python3
"""
PSI5120 - Trabalho Final
Analise da distribuicao do atraso de deteccao.

PROPOSITO
Verificar a hipotese de que o atraso entre a aplicacao da carga e a primeira
decisao do autoescalador nao e uma constante, e sim uma variavel aleatoria cuja
dispersao decorre do alinhamento de fase com o ciclo de coleta de metricas.

HIPOTESE
Com resolucao de coleta R, o atraso deve distribuir-se aproximadamente de forma
uniforme entre um piso, correspondente ao processamento do controlador e a
propagacao da decisao, e esse piso acrescido de R.

CONSEQUENCIA PRATICA
Se confirmada, reportar o atraso de deteccao como valor unico e insuficiente. A
grandeza relevante para dimensionamento passa a ser o limite superior, e nao a
media, uma vez que e ele que determina o pior caso de exposicao a sobrecarga.

USO
    python3 analisar_fase.py "dados/exp_minikube-fase_*.csv"
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


def ler_serie(caminho: str) -> list:
    """Le a serie temporal, preferindo a contagem sem defasagem do Deployment."""
    serie = []
    with open(caminho, newline="", encoding="utf-8") as f:
        for ln in csv.DictReader(f):
            def num(c):
                v = (ln.get(c) or "").strip()
                try:
                    return float(v) if v else None
                except ValueError:
                    return None
            atuais = num("replicas_deploy")
            if atuais is None:
                atuais = num("replicas_atuais")
            t = num("t_rel")
            if t is not None:
                serie.append({
                    "t": t,
                    "ts": (ln.get("timestamp_utc") or "").strip(),
                    "atuais": atuais,
                    "cpu": num("cpu_atual_pct"),
                })
    return serie


def atraso_deteccao(serie: list, marcadores: dict):
    """
    Intervalo entre a aplicacao da carga e a criacao da primeira replica
    adicional, medido a partir do marcador temporal registrado pelo
    orquestrador.
    """
    inicio = marcadores.get("carga_inicio_utc")
    if not inicio:
        return None, None
    try:
        t_carga = datetime.strptime(inicio, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None, None

    # Projeta o instante absoluto na base de tempo relativa da coleta.
    t_ini = None
    for p in serie:
        if p["ts"]:
            try:
                origem = datetime.strptime(p["ts"], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            t_ini = (t_carga - origem).total_seconds() + p["t"]
            break
    if t_ini is None:
        return None, None

    # Linha de base tomada imediatamente antes da carga.
    base = None
    for p in serie:
        if p["atuais"] is None:
            continue
        if p["t"] > t_ini:
            break
        base = p["atuais"]
    if base is None:
        return None, None

    # Primeira replica adicional e, separadamente, o instante em que a metrica
    # passou a refletir a carga. A diferenca entre os dois isola a parcela do
    # controlador da parcela do pipeline de metricas.
    t_replica = t_metrica = None
    for p in serie:
        if p["t"] < t_ini:
            continue
        if t_metrica is None and p["cpu"] is not None and p["cpu"] > 50:
            t_metrica = p["t"] - t_ini
        if t_replica is None and p["atuais"] is not None and p["atuais"] > base:
            t_replica = p["t"] - t_ini
        if t_replica is not None and t_metrica is not None:
            break

    return t_replica, t_metrica


def histograma(valores: list, largura: int = 10, colunas: int = 44) -> None:
    """Histograma textual, suficiente para inspecao rapida no terminal."""
    if not valores:
        return
    menor = int(min(valores) // largura) * largura
    maior = int(max(valores) // largura + 1) * largura
    faixas = {}
    for v in valores:
        k = int(v // largura) * largura
        faixas[k] = faixas.get(k, 0) + 1
    pico = max(faixas.values())
    print()
    print("Distribuicao do atraso de deteccao")
    for k in range(menor, maior, largura):
        n = faixas.get(k, 0)
        barra = "#" * int(round(n / pico * colunas)) if pico else ""
        print(f"  [{k:>3}, {k + largura:>3})  {n:>2}  {barra}")


def main() -> None:
    p = argparse.ArgumentParser(description="Distribuicao do atraso de deteccao")
    p.add_argument("arquivos", nargs="+")
    args = p.parse_args()

    caminhos = []
    for a in args.arquivos:
        caminhos.extend(glob.glob(a)) if any(c in a for c in "*?[") else caminhos.append(a)

    registros = []
    for c in sorted(set(caminhos)):
        if c.endswith("_marcadores.json") or c.endswith("_gerador.json"):
            continue
        marc_path = c.replace(".csv", "_marcadores.json")
        if not os.path.exists(marc_path):
            continue
        try:
            marc = json.load(open(marc_path))
        except (json.JSONDecodeError, OSError):
            continue
        serie = ler_serie(c)
        t_rep, t_met = atraso_deteccao(serie, marc)
        if t_rep is None:
            continue
        registros.append({
            "arquivo": os.path.basename(c),
            "fase": marc.get("fase_imposta_s"),
            "atraso_replica": t_rep,
            "atraso_metrica": t_met,
        })

    if not registros:
        print("nenhuma execucao valida encontrada")
        sys.exit(1)

    atrasos = [r["atraso_replica"] for r in registros]
    resolucao = None
    for c in sorted(set(caminhos)):
        mp = c.replace(".csv", "_marcadores.json")
        if os.path.exists(mp):
            try:
                resolucao = json.load(open(mp)).get("resolucao_suposta_s")
            except (json.JSONDecodeError, OSError):
                pass
            if resolucao:
                break

    print(f"{len(registros)} execucoes analisadas")
    print()
    print("fase imposta   ate a metrica   ate a replica")
    for r in sorted(registros, key=lambda x: (x["fase"] is None, x["fase"])):
        fase = "n/d" if r["fase"] is None else f"{r['fase']:>3}s"
        met = "n/d" if r["atraso_metrica"] is None else f"{r['atraso_metrica']:>6.0f}s"
        print(f"{fase:>12}   {met:>13}   {r['atraso_replica']:>12.0f}s")

    print()
    print("=" * 58)
    print("ATRASO DE DETECCAO")
    print("=" * 58)
    print(f"  minimo            {min(atrasos):>8.1f} s")
    print(f"  maximo            {max(atrasos):>8.1f} s")
    print(f"  media             {statistics.mean(atrasos):>8.1f} s")
    print(f"  mediana           {statistics.median(atrasos):>8.1f} s")
    if len(atrasos) > 1:
        print(f"  desvio padrao     {statistics.stdev(atrasos):>8.1f} s")
    amplitude = max(atrasos) - min(atrasos)
    print(f"  amplitude         {amplitude:>8.1f} s")

    if resolucao:
        print()
        print(f"  resolucao de coleta declarada    {resolucao} s")
        print(f"  amplitude observada              {amplitude:.0f} s")
        # A hipotese preve amplitude da ordem de uma resolucao. Admite-se
        # tolerancia ampla, uma vez que a cobertura das fases depende do numero
        # de execucoes e da aleatorizacao.
        if 0.6 * resolucao <= amplitude <= 1.6 * resolucao:
            print("  A amplitude e compativel com um ciclo de coleta, o que")
            print("  sustenta a hipotese de dispersao por alinhamento de fase.")
        else:
            print("  A amplitude difere de um ciclo de coleta. A dispersao pode")
            print("  ter outra origem, ou a cobertura de fases foi insuficiente.")

    histograma(atrasos)

    # Decomposicao entre as duas parcelas, quando disponivel.
    pares = [(r["atraso_metrica"], r["atraso_replica"]) for r in registros
             if r["atraso_metrica"] is not None]
    if pares:
        met = [a for a, _ in pares]
        dif = [b - a for a, b in pares]
        print()
        print("DECOMPOSICAO DO ATRASO")
        print(f"  ate a metrica tornar-se visivel   {statistics.mean(met):>6.1f} s")
        print(f"  da metrica ate a replica          {statistics.mean(dif):>6.1f} s")
        print()
        print("  A primeira parcela corresponde ao pipeline de metricas e a")
        print("  segunda ao controlador. A comparacao entre elas indica qual")
        print("  componente domina o tempo de reacao observado.")


if __name__ == "__main__":
    main()
