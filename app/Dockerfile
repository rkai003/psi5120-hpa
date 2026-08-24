# =============================================================================
# PSI5120 - Atividade Pratica 1
# Imagem do servidor web usado na demonstracao de Horizontal Pod Autoscaler.
#
# A mesma imagem e executada nos dois ambientes exigidos pelo trabalho, o
# cluster local em Minikube e o cluster gerenciado em Amazon EKS. Publicar a
# imagem em um registro publico e usar o mesmo digest nos dois clusters
# elimina a diferenca de artefato como possivel causa de divergencia nos
# resultados.
# =============================================================================

# -----------------------------------------------------------------------------
# Imagem base.
#
# A variante slim contem o interpretador e a biblioteca padrao sem ferramentas
# de compilacao, o que reduz o tamanho final e a superficie exposta. A versao
# e fixada em 3.12 em vez de latest porque latest e um apelido mutavel: o
# conteudo por tras dele muda a cada nova publicacao, o que comprometeria a
# reprodutibilidade do experimento em execucoes futuras.
# -----------------------------------------------------------------------------
FROM python:3.12-slim

# -----------------------------------------------------------------------------
# Metadados da imagem, no padrao OCI.
# Nao afetam a execucao. Documentam origem e proposito para quem inspecionar a
# imagem com docker inspect.
# -----------------------------------------------------------------------------
LABEL org.opencontainers.image.title="PSI5120 HPA Demo"
LABEL org.opencontainers.image.description="Servidor web com endpoint de carga controlada para testes de autoescalamento"
LABEL org.opencontainers.image.licenses="MIT"

# -----------------------------------------------------------------------------
# Valores padrao de configuracao.
#
# Sao apenas padroes. Os manifestos do Kubernetes podem sobrescreve-los sem
# reconstruir a imagem, o que mantem configuracao e artefato separados.
# -----------------------------------------------------------------------------
ENV APP_PORT=8080
ENV TRABALHO_PADRAO_MS=50
ENV TRABALHO_MAXIMO_MS=2000

# Impede que o Python armazene em buffer a saida padrao. Sem isso, as linhas
# emitidas pela aplicacao poderiam demorar a aparecer em kubectl logs, o que
# atrapalharia a observacao durante os testes de carga.
ENV PYTHONUNBUFFERED=1

# Evita a escrita de arquivos .pyc no container, desnecessarios em execucao
# efemera.
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# -----------------------------------------------------------------------------
# Usuario sem privilegios.
#
# Por padrao um container executa como root, e nesse caso o identificador
# numerico do processo dentro do container e o mesmo identificador no host,
# pois o Docker nao remapeia UIDs sem configuracao adicional. Criar um usuario
# proprio e passar a executar como ele reduz o impacto de uma eventual falha
# na aplicacao.
#
# O UID 10001 e escolhido acima da faixa usada por contas de sistema para
# evitar colisao com usuarios existentes na imagem base.
# -----------------------------------------------------------------------------
RUN useradd --create-home --uid 10001 appuser

# A copia ocorre depois da criacao do usuario e ja define a propriedade do
# arquivo, evitando uma camada adicional apenas para ajustar permissoes.
COPY --chown=appuser:appuser app.py /app/app.py

USER appuser

# -----------------------------------------------------------------------------
# Documentacao da porta de escuta.
#
# EXPOSE nao publica porta alguma e nao altera o comportamento de rede. E um
# metadado. A exposicao efetiva e responsabilidade do Service do Kubernetes,
# declarado em k8s/20-service.yaml.
# -----------------------------------------------------------------------------
EXPOSE 8080

# -----------------------------------------------------------------------------
# Comando executado quando o container inicia.
#
# A forma de lista evita a interposicao de um shell, de modo que o processo do
# Python recebe o PID 1 e, com ele, os sinais enviados pelo Kubernetes. Isso
# importa durante o autoescalamento: ao reduzir replicas, o kubelet envia
# SIGTERM ao processo principal, e um shell intermediario poderia nao repassar
# o sinal, atrasando o encerramento.
# -----------------------------------------------------------------------------
CMD ["python", "/app/app.py"]
