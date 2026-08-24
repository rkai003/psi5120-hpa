#!/usr/bin/env python3
"""
PSI5120 - Atividade Pratica 1
Servidor web didatico para demonstracao de Horizontal Pod Autoscaler (HPA).

A aplicacao expoe tres endpoints:

    GET /            resposta imediata, usada como alvo das probes do Kubernetes
    GET /health      idem, separado para leitura mais clara dos manifestos
    GET /work?ms=N   consome CPU de forma deliberada por aproximadamente N
                     milissegundos de tempo de processador

DECISAO DE PROJETO IMPORTANTE
----------------------------
O endpoint /work consome CPU por TEMPO ALVO e nao por numero fixo de iteracoes.

Se a carga fosse definida por iteracoes (por exemplo, "calcule 10 milhoes de
raizes quadradas"), uma CPU mais rapida concluiria o trabalho em menos tempo e
consumiria menos CPU-segundos por requisicao. Como este trabalho compara dois
ambientes com processadores diferentes (notebook local no Minikube e instancia
EC2 no Amazon EKS), isso introduziria uma variavel de confusao: parte da
diferenca observada no autoescalamento viria do hardware, e nao do
comportamento do HPA.

Ao fixar o tempo de CPU por requisicao, cada requisicao custa aproximadamente
os mesmos CPU-segundos nos dois ambientes, e a comparacao passa a medir o que
interessa, que e o tempo de reacao do controlador de autoescalamento.

Sem dependencias externas. Apenas biblioteca padrao do Python.
"""

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# Configuracao por variavel de ambiente.
# Manter a configuracao fora do codigo permite alterar o comportamento pelo
# manifesto do Kubernetes sem reconstruir a imagem.
# ---------------------------------------------------------------------------
PORTA = int(os.environ.get("APP_PORT", "8080"))

# Custo padrao de uma requisicao a /work, em milissegundos de CPU.
# Pode ser sobrescrito por requisicao com o parametro ?ms=
TRABALHO_PADRAO_MS = int(os.environ.get("TRABALHO_PADRAO_MS", "50"))

# Teto de seguranca. Impede que um parametro mal formado prenda um worker
# por tempo indeterminado e distorca o experimento.
TRABALHO_MAXIMO_MS = int(os.environ.get("TRABALHO_MAXIMO_MS", "2000"))

# Identificacao da replica. O Kubernetes injeta o nome do Pod por downward API
# no manifesto de Deployment. Serve para evidenciar, nas respostas, que
# requisicoes distintas foram atendidas por Pods distintos apos o escalamento.
NOME_POD = os.environ.get("POD_NAME", "desconhecido")


def consumir_cpu(alvo_ms: int) -> dict:
    """
    Ocupa o processador por aproximadamente `alvo_ms` milissegundos.

    O laco executa aritmetica de ponto flutuante e verifica o relogio a cada
    bloco de iteracoes. A verificacao em blocos evita que a propria chamada de
    relogio domine o tempo de execucao, o que tornaria a medida imprecisa.

    Retorna o tempo efetivamente gasto e o numero de iteracoes concluidas, de
    modo que a resposta HTTP possa ser auditada durante os testes de carga.
    """
    alvo_s = alvo_ms / 1000.0
    inicio = time.perf_counter()
    iteracoes = 0
    acumulador = 0.0

    while True:
        # Bloco de trabalho. O valor 5000 foi escolhido para que a verificacao
        # de tempo ocorra com granularidade suficiente sem pesar no total.
        for i in range(5000):
            acumulador += (i % 7) ** 0.5
        iteracoes += 5000

        if (time.perf_counter() - inicio) >= alvo_s:
            break

    decorrido_ms = (time.perf_counter() - inicio) * 1000.0
    return {
        "alvo_ms": alvo_ms,
        "decorrido_ms": round(decorrido_ms, 2),
        "iteracoes": iteracoes,
        # O acumulador e devolvido apenas para impedir que o interpretador
        # descarte o laco por considera-lo sem efeito observavel.
        "verificacao": round(acumulador, 2),
    }


class Manipulador(BaseHTTPRequestHandler):
    """Trata as requisicoes HTTP recebidas pelo servidor."""

    def _responder(self, codigo: int, corpo: dict) -> None:
        """Serializa o corpo como JSON e escreve a resposta."""
        dados = json.dumps(corpo).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self):  # noqa: N802  (nome exigido pela biblioteca padrao)
        caminho = urlparse(self.path)
        parametros = parse_qs(caminho.query)

        # -------------------------------------------------------------------
        # Endpoints de verificacao de saude.
        # Devem responder rapidamente, pois sao consultados periodicamente
        # pelas probes do Kubernetes. Se demorassem, o kubelet poderia
        # considerar o Pod indisponivel durante o teste de carga e reiniciar
        # containers saudaveis, corrompendo o experimento.
        # -------------------------------------------------------------------
        if caminho.path in ("/", "/health"):
            self._responder(200, {"status": "ok", "pod": NOME_POD})
            return

        # -------------------------------------------------------------------
        # Endpoint de carga. Consome CPU de forma controlada.
        # -------------------------------------------------------------------
        if caminho.path == "/work":
            try:
                ms = int(parametros.get("ms", [TRABALHO_PADRAO_MS])[0])
            except (ValueError, IndexError):
                self._responder(400, {"erro": "parametro ms invalido"})
                return

            # Restringe o valor recebido ao intervalo aceito.
            ms = max(1, min(ms, TRABALHO_MAXIMO_MS))

            resultado = consumir_cpu(ms)
            resultado["pod"] = NOME_POD
            self._responder(200, resultado)
            return

        self._responder(404, {"erro": "endpoint inexistente"})

    def log_message(self, formato, *args):
        """
        Silencia o log de acesso padrao.

        Durante os testes de estresse a aplicacao recebe milhares de
        requisicoes. Registrar cada uma consumiria CPU e distorceria a
        propria medida que o experimento pretende obter.
        """
        return


def main() -> None:
    # ThreadingHTTPServer atende requisicoes em threads separadas. Isso permite
    # que as probes de saude sejam respondidas enquanto uma requisicao a /work
    # ocupa o processador. Como o trabalho e limitado por CPU e o interpretador
    # possui trava global, o consumo maximo de um Pod tende a um nucleo, o que
    # torna a carga previsivel.
    servidor = ThreadingHTTPServer(("0.0.0.0", PORTA), Manipulador)
    print(
        json.dumps({
            "evento": "servidor_iniciado",
            "porta": PORTA,
            "pod": NOME_POD,
            "trabalho_padrao_ms": TRABALHO_PADRAO_MS,
        }),
        flush=True,
    )
    servidor.serve_forever()


if __name__ == "__main__":
    main()
