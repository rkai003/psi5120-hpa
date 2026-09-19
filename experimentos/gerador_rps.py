#!/usr/bin/env python3
"""
PSI5120 - Trabalho Final
Gerador de carga calibrado por taxa de requisicoes.

MOTIVACAO
O gerador empregado no trabalho intermediario mantinha um numero fixo de lacos
de requisicao, cada um disparando a proxima chamada somente apos receber a
resposta da anterior. Esse arranjo, conhecido como malha fechada, faz com que a
taxa efetiva dependa da latencia do servidor e da capacidade de processamento
do proprio gerador.

A consequencia foi observada experimentalmente: o ambiente local, dotado de
maior capacidade, emitiu mais requisicoes por unidade de tempo que o ambiente
gerenciado, ainda que ambos tenham recebido a mesma configuracao. A utilizacao
de processador resultante diferiu de forma expressiva, o que impediu comparar
essa grandeza entre os ambientes.

Este gerador opera em malha aberta. As requisicoes sao agendadas em intervalos
fixos determinados pela taxa alvo, independentemente de respostas pendentes. A
carga oferecida passa a ser uma variavel controlada do experimento, e nao um
resultado da interacao entre cliente e servidor.

CARGA OFERECIDA EM UNIDADES INDEPENDENTES DO AMBIENTE
Como o endpoint de destino consome um tempo de processador conhecido por
requisicao, a carga oferecida pode ser expressa em nucleos:

    nucleos_oferecidos = taxa_alvo x custo_por_requisicao

Com custo de 50 ms por requisicao, uma taxa de 40 requisicoes por segundo
corresponde a 2 nucleos de carga oferecida. A especificacao passa a ter o mesmo
significado fisico em qualquer ambiente, o que torna a utilizacao observada
comparavel entre eles.

INSTRUMENTACAO
O gerador nao apenas aplica a carga, mas tambem mede o que efetivamente
conseguiu aplicar. Se a taxa alcancada ficar abaixo da taxa alvo, o proprio
gerador tornou-se gargalo, e a execucao deve ser descartada. Sem essa
verificacao, uma limitacao do cliente seria interpretada como comportamento do
servidor.

USO
    python3 gerador_rps.py --url URL --rps 40 --duracao 300

VARIAVEIS DE AMBIENTE equivalentes: ALVO_URL, TAXA_RPS, DURACAO_S, TRABALHADORES
"""

import argparse
import json
import os
import queue
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone


class Contadores:
    """Acumula os resultados das requisicoes de forma segura entre threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.enviadas = 0
        self.concluidas = 0
        self.falhas = 0
        self.atrasadas = 0      # requisicoes disparadas apos o instante previsto
        self.latencias = []     # em milissegundos

    def registrar(self, ok: bool, latencia_ms: float, atrasou: bool) -> None:
        with self.lock:
            self.concluidas += 1
            if ok:
                self.latencias.append(latencia_ms)
            else:
                self.falhas += 1
            if atrasou:
                self.atrasadas += 1

    def instantaneo(self) -> dict:
        """Copia consistente dos contadores, para relatorio periodico."""
        with self.lock:
            lat = sorted(self.latencias)
        return {
            "enviadas": self.enviadas,
            "concluidas": self.concluidas,
            "falhas": self.falhas,
            "atrasadas": self.atrasadas,
            "latencias": lat,
        }


def trabalhador(fila: queue.Queue, url: str, contadores: Contadores,
                encerrar: threading.Event) -> None:
    """
    Consome instantes agendados e emite as requisicoes correspondentes.

    Cada item da fila e o instante em que a requisicao deveria ter partido. A
    diferenca entre esse instante e o momento em que um trabalhador ficou
    disponivel mede o atraso acumulado, que indica insuficiencia de
    trabalhadores ou do proprio gerador.
    """
    while not encerrar.is_set():
        try:
            previsto = fila.get(timeout=0.5)
        except queue.Empty:
            continue

        agora = time.perf_counter()
        atrasou = (agora - previsto) > 0.050  # tolerancia de 50 ms

        inicio = time.perf_counter()
        ok = False
        try:
            with urllib.request.urlopen(url, timeout=30) as resposta:
                resposta.read()
                ok = (resposta.status == 200)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            ok = False

        latencia_ms = (time.perf_counter() - inicio) * 1000.0
        contadores.registrar(ok, latencia_ms, atrasou)
        fila.task_done()


def relatar(contadores: Contadores, decorrido: float, rps_alvo: float,
            custo_ms: float, estado: dict, final: bool = False) -> None:
    """
    Emite uma linha de relatorio em JSON na saida padrao.

    O formato estruturado permite que os registros sejam recuperados por
    kubectl logs e processados sem analise textual.

    Alem das grandezas acumuladas desde o inicio, o relatorio informa a taxa
    observada apenas no intervalo desde o relatorio anterior. A distincao e
    necessaria porque o experimento parte de uma unica replica, incapaz de
    atender a taxa alvo. Durante o crescimento o sistema opera saturado e a
    taxa acumulada fica abaixo do alvo por razao fisica, e nao por limitacao do
    gerador. A aderencia relevante para validar a carga e a observada em
    regime, apos a conclusao do escalamento.
    """
    s = contadores.instantaneo()
    lat = s["latencias"]

    def percentil(p: float):
        if not lat:
            return None
        k = max(0, min(len(lat) - 1, int(round(p / 100.0 * (len(lat) - 1)))))
        return round(lat[k], 1)

    rps_acumulado = s["concluidas"] / decorrido if decorrido > 0 else 0.0

    # Taxa no intervalo desde o relatorio anterior.
    delta_t = decorrido - estado.get("t_anterior", 0.0)
    delta_n = s["concluidas"] - estado.get("n_anterior", 0)
    rps_janela = delta_n / delta_t if delta_t > 0 else 0.0
    estado["t_anterior"] = decorrido
    estado["n_anterior"] = s["concluidas"]

    registro = {
        "evento": "final" if final else "progresso",
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decorrido_s": round(decorrido, 1),
        "rps_alvo": rps_alvo,
        "rps_acumulado": round(rps_acumulado, 2),
        "rps_janela": round(rps_janela, 2),
        # Aderencia acumulada desde o inicio, incluindo a fase de saturacao.
        "aderencia_acumulada": round(rps_acumulado / rps_alvo, 3) if rps_alvo else None,
        # Aderencia no intervalo recente, usada para validar a carga em regime.
        "aderencia_janela": round(rps_janela / rps_alvo, 3) if rps_alvo else None,
        "nucleos_oferecidos": round(rps_alvo * custo_ms / 1000.0, 3),
        "nucleos_efetivos_janela": round(rps_janela * custo_ms / 1000.0, 3),
        "enviadas": s["enviadas"],
        "concluidas": s["concluidas"],
        "falhas": s["falhas"],
        "atrasadas": s["atrasadas"],
        "latencia_p50_ms": percentil(50),
        "latencia_p95_ms": percentil(95),
    }
    print(json.dumps(registro), flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Gerador de carga calibrado por taxa")
    p.add_argument("--url", default=os.environ.get("ALVO_URL", ""),
                   help="endereco completo do endpoint de carga")
    p.add_argument("--rps", type=float, default=float(os.environ.get("TAXA_RPS", "20")),
                   help="requisicoes por segundo a serem oferecidas")
    p.add_argument("--duracao", type=float, default=float(os.environ.get("DURACAO_S", "300")),
                   help="duracao da aplicacao de carga, em segundos")
    p.add_argument("--trabalhadores", type=int,
                   default=int(os.environ.get("TRABALHADORES", "0")),
                   help="numero de threads emissoras; 0 calcula automaticamente")
    p.add_argument("--custo-ms", type=float,
                   default=float(os.environ.get("CUSTO_MS", "50")),
                   help="custo de processador por requisicao, usado no calculo de nucleos")
    p.add_argument("--intervalo-relatorio", type=float, default=10.0)
    args = p.parse_args()

    if not args.url:
        print(json.dumps({"erro": "url nao informada"}), flush=True)
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Dimensionamento do numero de trabalhadores.
    #
    # Pela lei de Little, o numero de requisicoes simultaneas em um sistema
    # estavel e o produto da taxa de chegada pelo tempo de permanencia. Com
    # margem de seguranca, adota-se o dobro do valor estimado, considerando
    # latencia de ate um segundo, e um piso que garante paralelismo minimo.
    # Trabalhadores em excesso permanecem bloqueados na fila e consomem pouco.
    # -----------------------------------------------------------------------
    if args.trabalhadores > 0:
        n_trabalhadores = args.trabalhadores
    else:
        n_trabalhadores = max(8, int(args.rps * 1.0 * 2))

    contadores = Contadores()
    encerrar = threading.Event()
    # A fila e limitada para que o agendador perceba saturacao em vez de
    # acumular indefinidamente requisicoes nao atendidas.
    fila: queue.Queue = queue.Queue(maxsize=max(100, n_trabalhadores * 4))

    threads = []
    for _ in range(n_trabalhadores):
        t = threading.Thread(target=trabalhador,
                             args=(fila, args.url, contadores, encerrar),
                             daemon=True)
        t.start()
        threads.append(t)

    print(json.dumps({
        "evento": "inicio",
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "url": args.url,
        "rps_alvo": args.rps,
        "duracao_s": args.duracao,
        "trabalhadores": n_trabalhadores,
        "nucleos_oferecidos": round(args.rps * args.custo_ms / 1000.0, 3),
    }), flush=True)

    # -----------------------------------------------------------------------
    # Agendamento em malha aberta.
    #
    # Os instantes de disparo sao calculados a partir do inicio da execucao, e
    # nao acumulados a partir do instante anterior. Isso impede que atrasos
    # pontuais desloquem permanentemente a base de tempo, o que reduziria a
    # taxa efetiva sem que o desvio fosse percebido.
    # -----------------------------------------------------------------------
    intervalo = 1.0 / args.rps
    inicio = time.perf_counter()
    proximo_relatorio = args.intervalo_relatorio
    i = 0
    # Estado usado para calcular a taxa no intervalo entre relatorios.
    estado = {"t_anterior": 0.0, "n_anterior": 0}

    while True:
        decorrido = time.perf_counter() - inicio
        if decorrido >= args.duracao:
            break

        instante_previsto = i * intervalo
        espera = instante_previsto - decorrido
        if espera > 0:
            time.sleep(espera)

        try:
            fila.put_nowait(inicio + instante_previsto)
            contadores.enviadas += 1
        except queue.Full:
            # Saturacao do gerador. A requisicao e contabilizada como atraso em
            # vez de ser enfileirada, preservando a base de tempo.
            with contadores.lock:
                contadores.atrasadas += 1
        i += 1

        if decorrido >= proximo_relatorio:
            relatar(contadores, decorrido, args.rps, args.custo_ms, estado)
            proximo_relatorio += args.intervalo_relatorio

    # Aguarda as requisicoes pendentes por um intervalo limitado.
    limite = time.perf_counter() + 30
    while not fila.empty() and time.perf_counter() < limite:
        time.sleep(0.2)

    encerrar.set()
    relatar(contadores, time.perf_counter() - inicio, args.rps, args.custo_ms, estado, final=True)


if __name__ == "__main__":
    main()