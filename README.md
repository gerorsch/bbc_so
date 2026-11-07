# SimOS — Protótipo de Sistema Operacional

| | |
|---|---|
| **Aluno** | George Queiroz |
| **Disciplina** | Sistemas Operacionais |
| **Professor** | Douglas Veras e Silva |
| **Curso** | Bacharelado em Ciência da Computação (BCC) — UFRPE |

Simulador de sistema operacional modular, desenvolvido em etapas. Cada etapa usa as anteriores: o sistema de arquivos grava em um volume, cada gravação vira requisição de E/S nos discos físicos, e assim por diante.

| Etapa | Conteúdo | Status |
|---|---|---|
| 1 | Sistema de arquivos: árvore, CRUD, alocação de blocos e fragmentação | ✅ |
| 2 | Entrada/Saída: fila e escalonamento do braço do disco, SSD (FTL e wear leveling) e RAID 0/1/4/5 | ✅ |
| 3 | Segurança e proteção | — |
| 4 | Gerenciamento de memória | — |
| 5 | Gerenciamento de processos | — |

## Arquitetura

```
 shell (comandos)
   │
 Kernel ─────────────────────────────────────────────────────────── simos/kernel.py
   │
 Sistema de arquivos (Etapa 1)        árvore, i-nodes, alocação     simos/fs/
   │  lê/grava "blocos lógicos" 0..N-1
 Volume (Etapa 2)                     disco único ou RAID 0/1/4/5   simos/io/volume.py
   │  traduz em operações nos discos físicos
 Fila de E/S + escalonador (Etapa 2)  FCFS, SSTF, SCAN, C-SCAN...   simos/io/
   │
 Discos físicos (hardware simulado)   HDD (trilhas, cabeça)         simos/hw/
                                      SSD (páginas, blocos, FTL)
```

A máquina inteira (configuração do hardware, estado de cada disco e conteúdo bruto) é salva em um arquivo de imagem (`simos.img`). O sistema de arquivos não é salvo à parte: ele vive dentro dos blocos dos discos e é **remontado** a cada execução (superbloco → bitmap → tabela de i-nodes → diretórios).

## Requisitos e execução

- Python 3.10+
- [rich](https://github.com/Textualize/rich) (saída colorida no terminal): `pip install -r requirements.txt`

```bash
# Shell interativo (cria simos.img: 1 HDD com 32 blocos de 4 KB e alocação indexada, se não existir)
python main.py

# Máquina nova com outra configuração
python main.py --new --raid 5 --disks 3 --block-size 4KB --blocks 16 --alloc contiguous
python main.py --new --disk-type ssd --alloc indexed

# Roteiros de demonstração (usam demo.img para não tocar em simos.img)
python main.py demos/etapa1_indexada.txt        # Exemplos 1.1 e 1.2 do enunciado
python main.py demos/etapa1_contigua.txt        # fragmentação externa e compactação
python main.py demos/etapa1_fat.txt             # alocação encadeada (FAT)
python main.py demos/etapa1_crud.txt            # CRUD e caminhos absolutos/relativos
python main.py demos/etapa2_escalonamento.txt   # Exemplo 2.1 + comparação dos algoritmos + fila real do FS
python main.py demos/etapa2_raid5.txt           # Exemplo 2.2: RAID 5, falha simulada e reconstrução
python main.py demos/etapa2_raid_niveis.txt     # RAID 0, 1 e 4 comparados
python main.py demos/etapa2_ssd.txt             # SSD: coleta de lixo e wear leveling (com e sem)
```

Dentro do shell, `help` lista os comandos e a tecla Tab completa comandos e caminhos. O comando `format` troca o hardware ou a estratégia a qualquer momento, por exemplo `format --raid 5 --disks 3 --alloc fat`.

## Comandos

| Comando | Descrição |
|---|---|
| `pwd`, `cd [caminho]`, `ls [-l] [caminho]`, `tree [caminho]` | navegação |
| `mkdir [-p] <dir>...` | criar diretório(s) |
| `create <arq> [--size 10KB \| --content "texto"]` | criar arquivo |
| `cat [-a] <arq>` / `read_file <arq>` | ler o conteúdo |
| `stat <caminho>` | i-node e blocos (início/comprimento, cadeia FAT ou ponteiros do índice) |
| `write <arq> <texto> \| --size N` | sobrescrever (cresce ou encolhe) |
| `write_file <arq> <texto> \| --size N` | como `write`, mas cria o arquivo se não existir |
| `append <arq> <texto> \| --size N` | acrescentar ao final |
| `mv <origem> <destino>` | renomear ou mover |
| `rm [-r] <caminho>...`, `rmdir <dir>...` | remover |
| `fs_status [--details]` | mapa do disco e relatório de fragmentação |
| `defrag` | compactar o disco |
| `io_trace [off\|resumo\|detalhado]` | mostrar a fila de E/S gerada por cada comando |
| `io_sched [fcfs\|sstf\|scan\|cscan\|look\|clook]` | escolher o escalonador do braço |
| `io_simulate [--alg A \| --all] [--head H] [--tracks N] [--dir up\|down] t1 t2 ...` | escalonar uma fila de trilhas informada |
| `disk_status` | discos físicos, cabeças, estatísticas e layout do RAID |
| `raid_map <arq>` | distribuição dos blocos de um arquivo entre os discos e a paridade |
| `raid_test <arq> [--disk N]` | teste de falha: derruba um disco e lê o arquivo por reconstrução |
| `raid_fail <N>`, `raid_replace <N>`, `raid_check` | falhar disco, substituir e reconstruir, verificar paridade |
| `ssd_status [N]` | páginas, coleta de lixo e desgaste do SSD |
| `format [--alloc E] [--block-size B] [--blocks N] [--raid R] [--disks N] [--disk-type hdd\|ssd] [--bpt N] [--wl on\|off] [--sched A] [-y]` | montar hardware novo e formatar |
| `repeat <N> <comando>`, `source <roteiro>` | repetir um comando, executar um roteiro |

---

## Etapa 1 — Sistema de Arquivos

- **Árvore de diretórios** com caminhos absolutos (`/home/user/doc.txt`) e relativos (`doc.txt`, `../x`, `./y`).
- **CRUD** de arquivos e diretórios.
- **Blocos de tamanho fixo** (512 B, 1 KB, 4 KB...), com três estratégias de alocação escolhidas no `format`:
  - **Contígua**: cada arquivo ocupa um trecho de blocos consecutivos (first-fit). Ao crescer, estende no lugar ou é realocado.
  - **Encadeada (FAT)**: a tabela FAT guarda o próximo bloco de cada bloco.
  - **Indexada (i-node)**: cada arquivo tem um bloco de índice com os ponteiros para seus blocos de dados.
- **Diagnóstico** (`fs_status`): mapa visual colorido, lista bloco a bloco, fragmentação interna por arquivo, fragmentação externa, maior arquivo criável e indicação explícita da necessidade de compactação.
- **Compactação** (`defrag`): junta os blocos livres em um único trecho e elimina a fragmentação externa.

### Formato em disco

Tudo fica nos blocos do volume, como em sistemas de arquivos reais (ext2, FAT):

```
Bloco 0           superbloco: geometria, estratégia e posição de cada região
Blocos seguintes  bitmap de blocos livres (1 bit por bloco)
                  tabela FAT (só na alocação encadeada; 4 bytes por bloco)
                  tabela de i-nodes (registros de 64 bytes: tipo, tamanho, datas, alocação)
Restante          área de dados: arquivos, diretórios e blocos de índice
```

Como no Unix, o i-node não guarda o nome. Um diretório é um arquivo especial cujo conteúdo são entradas de 32 bytes (nome de até 28 bytes + número do i-node). Em memória ficam só **caches** (i-nodes, árvore, bitmap/FAT, blocos de índice), por isso consultas como `ls` e `fs_status` não geram E/S.

Cada operação é uma **transação**: os blocos de dados são gravados durante a operação, os blocos de metadados alterados são gravados ao final, e se faltar espaço no meio do caminho o disco e os caches voltam ao estado anterior.

### Métricas

- **Fragmentação interna** = blocos alocados × tamanho do bloco − tamanho real (sobra no último bloco).
- **Fragmentação externa** (só na contígua) = blocos livres fora do maior trecho livre contíguo.
- **Necessidade de compactação** = SIM quando há fragmentação externa, isto é, quando o maior arquivo criável é menor que o espaço livre total. Na FAT e na indexada é sempre NÃO, porque qualquer bloco livre serve. Nelas o `defrag` é opcional e só reagrupa blocos dispersos.

---

## Etapa 2 — Entrada/Saída e Armazenamento

### Integração com o sistema de arquivos

O sistema de arquivos não acessa disco diretamente: ele lê e grava **blocos lógicos** em um **volume**. O volume traduz cada acesso em requisições nos **discos físicos**, e cada requisição entra na **fila de E/S** do disco correspondente, identificada pelo que está sendo lido ou gravado: dados de um arquivo, diretório, bloco de índice, bitmap, tabela de i-nodes, paridade...

As requisições de um comando são acumuladas e despachadas em lote ao fim dele, como o *plugging* do kernel Linux. No despacho:

1. **mesclagem**: pedidos idênticos ao mesmo bloco viram um só;
2. **escalonamento**: o algoritmo escolhido define a ordem de atendimento (HDD);
3. a cabeça se move e as estatísticas do disco são atualizadas.

Com `io_trace resumo` (padrão) cada comando mostra sua fila no formato do Exemplo 2.1; com `io_trace detalhado` aparece também a tabela de requisições.

### Escalonamento do braço (HDD)

O HDD tem trilhas (`--bpt` = blocos por trilha) e uma cabeça com posição e sentido. Algoritmos:

| Algoritmo | Ideia |
|---|---|
| FCFS | ordem de chegada |
| SSTF | sempre a requisição mais próxima da cabeça |
| SCAN (elevador) | varre até a extremidade do disco e inverte |
| C-SCAN | varre até a extremidade e volta ao início sem atender |
| LOOK / C-LOOK | como SCAN / C-SCAN, mas só até a última requisição |

Com a fila clássica do livro-texto (cabeça em 53; `io_simulate --all --head 53 98 183 37 122 14 124 65 67`) os totais conferem com a literatura: FCFS 640, SSTF 236, SCAN 236 (descendo), C-SCAN 382 (com o retorno), LOOK 299, C-LOOK 322. O Exemplo 2.1 dá 208 trilhas.

### SSD: acesso por páginas e desgaste das células

O SSD não tem braço (atendimento em ordem de chegada, tempo de acesso uniforme). O que ele precisa gerenciar é a flash:

- **Páginas e blocos**: grava-se por página, apaga-se por bloco (4 páginas). Uma página só pode ser gravada depois de apagada.
- **FTL** (Flash Translation Layer): não há sobrescrita no lugar. Regravar uma página lógica grava em outra página física e marca a antiga como inválida (mapeamento lógico → físico).
- **Coleta de lixo**: quando faltam blocos livres, escolhe o bloco com mais páginas inválidas, copia as válidas e o apaga.
- **Wear leveling**: o *dinâmico* grava sempre no bloco livre menos apagado. O *estático* move dados "frios" de blocos pouco apagados para que eles voltem a circular.
- **Over-provisioning** (páginas extras invisíveis ao sistema) e **TRIM** (o sistema de arquivos avisa quando libera um bloco).
- Métricas: **WAF** (escritas na flash ÷ escritas pedidas), apagamentos por bloco e desvio do desgaste.

Na demo `etapa2_ssd.txt`, o mesmo padrão de escritas dá desgaste de 0 a 16 apagamentos por bloco **sem** wear leveling e de 8 a 10 **com** wear leveling. O custo é um WAF um pouco maior.

### RAID

| Nível | Organização | Capacidade | Tolera |
|---|---|---|---|
| 0 | striping (blocos alternados entre os discos) | N discos | nenhuma falha |
| 1 | espelhamento (cada bloco em todos os discos; leituras alternadas) | 1 disco | N−1 falhas |
| 4 | striping + paridade XOR em disco dedicado | N−1 discos | 1 falha |
| 5 | striping + paridade XOR rotativa | N−1 discos | 1 falha |

- **Gravação com paridade** (read-modify-write): lê o dado antigo e a paridade antiga, grava o dado novo e a paridade nova (`P' = P ⊕ D ⊕ D'`). São 4 operações por escrita pequena, a "penalidade de escrita" do RAID 4/5, visível na fila de E/S.
- **Modo degradado**: com um disco falho, o bloco perdido é recalculado como o XOR dos demais blocos da faixa. Gravações em um disco falho ficam guardadas só na paridade.
- **Reconstrução** (`raid_replace`): o disco novo é preenchido faixa por faixa a partir dos sobreviventes.
- `raid_fail` apaga de fato o conteúdo do disco, então a leitura posterior só funciona porque a reconstrução por XOR é real.

---

## Estrutura do código

```
main.py                     ponto de entrada (argumentos, abre/cria a imagem)
simos/shell.py              shell interativo (comandos, ajuda, roteiros)
simos/kernel.py             monta hardware + volume + E/S + sistema de arquivos; imagem da máquina
simos/fs/layout.py          formato em disco: superbloco, i-nodes, bitmap, FAT
simos/fs/filesystem.py      árvore, caminhos, CRUD, transações, montagem e compactação
simos/fs/allocation.py      estratégias: contígua, FAT e indexada
simos/fs/diagnostics.py     mapa de blocos e fragmentação
simos/fs/render.py          exibição do sistema de arquivos
simos/hw/hdd.py             HDD: trilhas e cabeça
simos/hw/ssd.py             SSD: FTL, coleta de lixo, wear leveling, TRIM
simos/io/volume.py          volume único e RAID 0/1/4/5 (paridade, degradado, reconstrução)
simos/io/scheduler.py       FCFS, SSTF, SCAN, C-SCAN, LOOK, C-LOOK
simos/io/manager.py         despacho da fila: mesclagem, escalonamento, estatísticas
simos/io/render.py          exibição da E/S, do RAID e do SSD
demos/                      roteiros de demonstração
tests/                      testes automatizados
```

## Testes

```bash
python -m unittest -v
```
