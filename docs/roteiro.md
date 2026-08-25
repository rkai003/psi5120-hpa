# Roteiro de implantação e testes

PSI5120 — Atividade Prática 1
Autoescalamento horizontal de Pods em Kubernetes

Este documento registra os comandos executados, os resultados obtidos e as
decisões tomadas durante as duas implantações. Corresponde ao item 1 dos
artefatos exigidos pelo enunciado.

---

## 0. Preparação comum

As duas implantações compartilham a mesma imagem de container, os mesmos
manifestos e o mesmo perfil de carga. Apenas a infraestrutura difere.

### 0.1 Construção da imagem

Executado no host Ubuntu Server 24.04, com Docker Engine 28.5.2.

```bash
cd ~/psi5120/tp1/app
docker build -t rkai03/psi5120-hpa:v1 .
```

Verificação da imagem antes da publicação:

```bash
docker run -d --name teste-hpa -p 8080:8080 rkai03/psi5120-hpa:v1
curl -s http://localhost:8080/health
curl -s "http://localhost:8080/work?ms=200"
docker exec teste-hpa id
docker rm -f teste-hpa
```

Resultados obtidos:

```
{"status": "ok", "pod": "desconhecido"}
{"alvo_ms": 200, "decorrido_ms": 200.34, "iteracoes": 2385000, ...}
uid=10001(appuser) gid=10001(appuser) groups=10001(appuser)
```

Três verificações foram feitas nesta etapa. O endpoint de saúde responde, o
consumo de CPU do endpoint de carga apresenta erro de 0,17 por cento em relação
ao alvo de 200 ms, e o processo executa como usuário sem privilégios, conforme a
diretiva `USER` do Dockerfile.

### 0.2 Publicação

```bash
docker login
docker push rkai03/psi5120-hpa:v1
```

Digest obtido:

```
sha256:83a4fb221e3a74aedaca7be19f25e6bab6d5e1b748ebed0a45342f9d33f3507e
```

O digest é registrado porque identifica o conteúdo da imagem de forma imutável,
ao contrário da tag. Ambos os clusters obtêm o mesmo digest, o que elimina o
artefato executado como possível causa de divergência entre os resultados.

---

## 1. Implantação A — Minikube

### 1.1 Criação do cluster

```bash
minikube start -p tp1 --driver=docker --cpus=4 --memory=4096
kubectl config use-context tp1
kubectl get nodes -o wide
```

Resultado:

```
NAME   STATUS   ROLES           VERSION   INTERNAL-IP    EXTERNAL-IP   CONTAINER-RUNTIME
tp1    Ready    control-plane   v1.35.1   192.168.58.2   <none>        docker://29.2.1
```

O dimensionamento de 4 CPUs é deliberado. O padrão do Minikube são 2 CPUs, valor
insuficiente para acomodar dez réplicas com reserva de 100m cada, somadas aos
componentes do `kube-system` e ao gerador de carga. Com capacidade insuficiente,
Pods permaneceriam em estado `Pending` e o experimento mediria limitação de
recursos em vez de tempo de reação do autoescalador.

Observou-se que o nó passou por `NotReady` antes de `Ready`, com transição em
aproximadamente 57 segundos.

### 1.2 Metrics Server

```bash
minikube addons enable metrics-server -p tp1
kubectl -n kube-system get deployment metrics-server
kubectl top nodes
```

Resultado:

```
NAME             READY   UP-TO-DATE   AVAILABLE   AGE
metrics-server   1/1     1            1           72s

NAME   CPU(cores)   CPU(%)   MEMORY(bytes)   MEMORY(%)
tp1    180m         2%       905Mi           11%
```

O Metrics Server é pré-requisito do HPA. Sem ele o controlador não obtém a
métrica de utilização e nenhum escalamento ocorre. No Minikube a instalação é
feita por addon, com um único comando.

### 1.3 Aplicação dos manifestos

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/10-deployment.yaml
kubectl apply -f k8s/20-service.yaml
kubectl get all -n hpa-demo
```

O Pod inicial passou por `ContainerCreating` e atingiu `1/1 Running` em
aproximadamente 21 segundos.

### 1.4 Aplicação do HPA

```bash
kubectl apply -f k8s/30-hpa.yaml
kubectl get hpa -n hpa-demo
```

Sequência observada:

```
web-hpa   Deployment/web   cpu: <unknown>/50%   1   10   0    0s
web-hpa   Deployment/web   cpu: <unknown>/50%   1   10   0    7s
web-hpa   Deployment/web   cpu: 1%/50%          1   10   1    26s
```

O estado `<unknown>` persistiu por 26 segundos, intervalo correspondente ao
primeiro ciclo de coleta do Metrics Server e à sua propagação até o controlador.
A transição para valor numérico confirma que o pré-requisito da métrica foi
satisfeito. A coluna `REPLICAS` passando de 0 para 1 evidencia o autoescalador
assumindo o controle do Deployment.

### 1.5 Teste de carga

A coleta é iniciada antes da carga, de modo a registrar a linha de base em
repouso.

Sessão 1:

```bash
./scripts/coleta_metricas.sh dados/minikube.csv 900 5
```

Sessão 2, após aproximadamente 30 segundos:

```bash
kubectl apply -f k8s/40-gerador-carga.yaml
```

Estado durante o escalamento:

```
web-hpa   Deployment/web   cpu: 302%/50%   1   10   5    34m
web-hpa   Deployment/web   cpu: 393%/50%   1   10   10   36m
```

A distribuição de idades dos Pods no momento do pico evidencia três ondas de
criação, com um Pod original, quatro criados em um primeiro ciclo e cinco em um
segundo. O padrão é coerente com a política declarada no manifesto, que autoriza
o maior valor entre dobrar a quantidade de réplicas e acrescentar quatro
réplicas a cada 15 segundos.

Nenhum Pod permaneceu em estado `Pending`, o que confirma que a capacidade do
cluster foi suficiente e que a medida obtida corresponde ao tempo de reação do
controlador.

### 1.6 Remoção da carga

```bash
kubectl delete pod gerador-carga -n hpa-demo
```

A coleta prosseguiu até a redução completa das réplicas.

### 1.7 Resultados

| Grandeza | Valor |
|---|---|
| Réplicas iniciais | 1 |
| Até a primeira réplica adicional | 16 s |
| Até o pico de réplicas | 74 s |
| Réplicas no pico | 10 |
| Pico de utilização de CPU | 469 % |
| Até iniciar a redução | 303 s |
| Máximo de Pods pendentes | 0 |

Duas observações merecem registro.

O tempo até iniciar a redução foi de 303 segundos, contra os 300 segundos
declarados em `scaleDown.stabilizationWindowSeconds`. A diferença de 1 por cento
valida a instrumentação, uma vez que um parâmetro de valor conhecido foi medido
de forma independente e reproduziu o valor esperado.

A utilização de CPU permaneceu em torno de 400 por cento mesmo com dez réplicas
ativas. O controlador não conseguiu conduzir a métrica ao alvo de 50 por cento
porque atingiu o teto de `maxReplicas`. O comportamento evidencia que o limite
superior de réplicas é uma decisão de capacidade, e não um parâmetro
formal.

---

## 2. Implantação B — Amazon EKS

### 2.1 Recursos de infraestrutura

Diferentemente do ambiente local, o cluster gerenciado exige a criação prévia de
identidades e de rede.

| Recurso | Nome | Finalidade |
|---|---|---|
| Role IAM do cluster | `psi5120-tp1-cluster-role` | assumida pelo serviço EKS |
| Role IAM dos nós | `psi5120-tp1-node-role` | assumida pelas instâncias EC2 |
| Stack CloudFormation | `psi5120-tp1-vpc` | VPC com três subnets públicas |

As duas roles diferem na entidade que as assume, `eks.amazonaws.com` e
`ec2.amazonaws.com` respectivamente, o que caracteriza identidades distintas
para finalidades distintas.

O template de VPC utilizado não cria NAT Gateway, pois as três subnets são
públicas. A escolha tem efeito direto sobre o custo, uma vez que o NAT Gateway é
cobrado por hora e por volume processado.

Saída da stack:

```
SecurityGroups   sg-01fdbbfd1b7e2cdb4
VpcId            vpc-0d7765dea39ac97bf
SubnetIds        subnet-0604a853be69b998e, subnet-09ce309c6d850fd8c,
                 subnet-0e69bd9ed46cbefe1
```

### 2.2 Criação do cluster

Executado pelo AWS Management Console, com as três subnets, o security group da
stack e a role do cluster. Os logs do plano de controle foram mantidos
desabilitados, decisão que reduz custo de ingestão e retenção no CloudWatch e
reduz, na mesma medida, a capacidade de diagnóstico.

```bash
aws eks describe-cluster --region us-east-1 --name psi5120-tp1-eks \
  --query 'cluster.status' --output text
```

<!-- PREENCHER: tempo até ACTIVE -->

### 2.3 Node group

<!-- PREENCHER: comandos e saídas -->

Tipo de instância adotado: `c7i-flex.large`, com 2 vCPU e 4 GiB.

A escolha decorre de restrição da conta utilizada, que opera em plano gratuito e
admite apenas tipos elegíveis. Tentativas anteriores com `t3.medium` em atividade
precedente resultaram em falha de provisionamento com a mensagem
`InvalidParameterCombination - The specified instance type is not eligible for
Free Tier`. O tipo adotado possui capacidade equivalente, de modo que a diferença
entre os dois é de elegibilidade comercial e não técnica.

### 2.4 Metrics Server

Ao contrário do Minikube, o Amazon EKS não fornece o Metrics Server como
componente habilitável por comando único. A instalação é feita por manifesto.

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system get deployment metrics-server
kubectl top nodes
```

<!-- PREENCHER: saídas -->

### 2.5 Aplicação dos manifestos

Os mesmos arquivos da Implantação A, sem alteração.

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/10-deployment.yaml
kubectl apply -f k8s/20-service.yaml
kubectl apply -f k8s/30-hpa.yaml
```

<!-- PREENCHER: saídas -->

### 2.6 Teste de carga

Procedimento idêntico ao da Implantação A.

<!-- PREENCHER: saídas -->

### 2.7 Resultados

<!-- PREENCHER: tabela de métricas -->

---

## 3. Limpeza

### 3.1 Minikube

```bash
minikube stop -p tp1
minikube delete -p tp1
```

### 3.2 Amazon EKS

A remoção segue a ordem inversa da criação. Recursos criados fora do ciclo de
vida do cluster, como roles IAM e a pilha de VPC, exigem remoção explícita.

```bash
kubectl delete namespace hpa-demo
aws eks delete-nodegroup --region us-east-1 --cluster-name psi5120-tp1-eks \
  --nodegroup-name psi5120-tp1-ng
aws eks delete-cluster --region us-east-1 --name psi5120-tp1-eks
aws cloudformation delete-stack --region us-east-1 --stack-name psi5120-tp1-vpc
```

As roles IAM são removidas pelo Console.

### 3.3 Verificação de resíduos

```bash
aws eks list-clusters --region us-east-1
aws ec2 describe-instances --region us-east-1 \
  --filters "Name=instance-state-name,Values=running,pending,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId'
aws ec2 describe-volumes --region us-east-1 --query 'Volumes[].VolumeId'
aws elb describe-load-balancers --region us-east-1 \
  --query 'LoadBalancerDescriptions[].LoadBalancerName'
aws iam list-roles --query "Roles[?contains(RoleName,'psi5120-tp1')].RoleName"
```

<!-- PREENCHER: confirmação de ausência de recursos -->
