# SimOS — Protótipo de Sistema Operacional

| | |
|---|---|
| **Aluno** | George Queiroz |
| **Disciplina** | Sistemas Operacionais |
| **Professor** | Douglas Veras e Silva |
| **Curso** | Bacharelado em Ciência da Computação (BCC) — UFRPE |

Protótipo de sistema operacional simples, desenvolvido em etapas.

| Etapa | Conteúdo | Status |
|---|---|---|
| 1 | Sistema de arquivos hierárquico com alocação física de blocos | ✅ |

## Etapa 1 — Sistema de Arquivos

Simulador de sistema de arquivos com:

- **Árvore de diretórios navegável** com caminhos absolutos (`/home/user/doc.txt`) e relativos (`doc.txt`, `../x`, `./y`).
- **CRUD** de arquivos e diretórios: `mkdir`, `create`, `cat`, `stat`, `write`, `append`, `mv`, `rm`, `rmdir`.
- **Disco físico simulado** dividido em blocos de tamanho fixo (configurável: 512 B, 1 KB, 4 KB...), salvo em um arquivo de imagem (`disk.img`).
- **Três estratégias de alocação**, escolhidas ao formatar o disco:
  - **Contígua**: cada arquivo ocupa um trecho de blocos consecutivos (first-fit). Ao crescer, estende no lugar ou é realocado.
  - **Encadeada (FAT)**: uma tabela FAT na área de metadados guarda o próximo bloco de cada bloco.
  - **Indexada (i-node)**: cada arquivo tem um bloco de índice gravado no disco com os ponteiros para seus blocos de dados.
- **Diagnóstico do disco** (`fs_status`): mapa visual colorido, lista bloco a bloco, fragmentação interna por arquivo, fragmentação externa, maior arquivo criável e indicação explícita da necessidade de compactação.
- **Compactação** (`defrag`): junta os blocos livres em um único trecho e elimina a fragmentação externa.

### Requisitos

- Python 3.10+
- [rich](https://github.com/Textualize/rich) (saída colorida no terminal)

```bash
pip install -r requirements.txt
```

### Como executar

```bash
# Shell interativo (cria disk.img com 32 blocos de 4 KB e alocação indexada, se não existir)
python main.py

# Disco novo com outra geometria/estratégia
python main.py --new --alloc contiguous --block-size 1KB --blocks 64

# Usar outra imagem de disco
python main.py --disk meu_disco.img

# Executar um roteiro de demonstração (usa demo.img para não tocar em disk.img)
python main.py demos/etapa1_indexada.txt
python main.py demos/contigua_fragmentacao.txt
python main.py demos/encadeada_fat.txt
python main.py demos/crud_navegacao.txt
```

Dentro do shell, `format` troca a estratégia ou a geometria a qualquer momento (`format --alloc fat --block-size 512B --blocks 64`), e `help` lista todos os comandos. A tecla Tab completa comandos e caminhos.

### Comandos

| Comando | Descrição |
|---|---|
| `pwd`, `cd [caminho]` | diretório atual / mudar de diretório |
| `ls [-l] [caminho]` | listar diretório (`-l`: tipo, i-node, tamanho, blocos) |
| `tree [caminho]` | árvore do sistema de arquivos |
| `mkdir [-p] <dir>...` | criar diretório(s) |
| `create <arq> [--size 10KB \| --content "texto"]` | criar arquivo (conteúdo gerado com N bytes ou texto) |
| `cat [-a] <arq>` | ler o conteúdo |
| `stat <caminho>` | i-node, tamanho e blocos físicos (início/comprimento, cadeia FAT ou ponteiros do índice) |
| `write <arq> <texto> \| --size N` | sobrescrever (o arquivo pode crescer ou encolher) |
| `append <arq> <texto> \| --size N` | acrescentar ao final |
| `mv <origem> <destino>` | renomear ou mover |
| `rm [-r] <caminho>...`, `rmdir <dir>...` | remover |
| `fs_status [--details]` | mapa do disco e relatório de fragmentação |
| `defrag` | compactar o disco |
| `format [--alloc E] [--block-size B] [--blocks N] [-y]` | reformatar (`E` = `contiguous`, `fat` ou `indexed`) |
| `source <roteiro>` | executar comandos de um arquivo |

### Exemplo (enunciado, exemplos 1.1 e 1.2)

```
simos:/> format --alloc indexed --block-size 4KB --blocks 16 -y
simos:/> mkdir /sistema
simos:/> mkdir /sistema/logs
simos:/> create /sistema/logs/app.log --size 10KB
simos:/> tree
[ÁRVORE DO SISTEMA DE ARQUIVOS]
/
└── sistema/ [DIR]
    └── logs/ [DIR]
        └── app.log (10 KB) [ARQ]
simos:/> fs_status --details
Tamanho do Bloco de Disco: 4 KB | Total de Blocos: 16 (64 KB) | Estratégia: Indexada (i-node)

[MAPA DE ALOCAÇÃO DO DISCO (Indexado)]
Bloco 00     [INODE_RAIZ] : Diretório '/' (i-node: 1 ponteiro)
...
Bloco 05     [INODE]      : Arquivo '/sistema/logs/app.log' (i-node: 3 ponteiros)
Bloco 06     [DADO]       : '/sistema/logs/app.log' (Bloco 1 - 4096 bytes ocupados)
Bloco 07     [DADO]       : '/sistema/logs/app.log' (Bloco 2 - 4096 bytes ocupados)
Bloco 08     [DADO]       : '/sistema/logs/app.log' (Bloco 3 - 2048 bytes ocupados, 2048 livres)
...
--- RELATÓRIO DE FRAGMENTAÇÃO ---
/sistema/logs/app.log   10240 B   12288 B (3 blocos)   2048 B   50% do bloco 8   1
...
Fragmentação Externa        : 0 bytes (não se aplica: arquivos não precisam de blocos contíguos)
Necessidade de Compactação  : NÃO (blocos alocados via i-node indexado)
```

### Modelo do sistema de arquivos

A imagem de disco (`.img`) tem duas áreas:

1. **Metadados** (cabeçalho): superbloco (tamanho do bloco, número de blocos, estratégia), tabela de i-nodes (tipo, tamanho, informação de alocação, datas) e o estado do alocador (bitmap de blocos livres e, na FAT, a tabela de encadeamento).
2. **Dados** (os N blocos): conteúdo dos arquivos, **conteúdo dos diretórios** e, na estratégia indexada, os **blocos de índice**.

Como no Unix, o i-node não guarda o nome. Um diretório é um arquivo especial cujo conteúdo são entradas de 32 bytes (nome de até 28 bytes + número do i-node), gravadas nos blocos do disco. Ao abrir a imagem, a árvore é **reconstruída lendo os blocos de diretório** a partir do i-node raiz. Por isso os diretórios também aparecem no mapa e têm fragmentação interna própria.

Toda operação é **atômica**: se faltar espaço no meio de um `create`, `append` ou `mv`, o disco e os metadados voltam ao estado anterior.

#### Como cada métrica é calculada

- **Fragmentação interna** = blocos alocados × tamanho do bloco − tamanho real (espaço sobrando no último bloco).
- **Fragmentação externa** (só na contígua) = blocos livres fora do maior trecho livre contíguo. Esses blocos existem, mas não servem para um arquivo maior que o maior trecho.
- **Necessidade de compactação** = SIM quando há fragmentação externa, ou seja, quando o maior arquivo criável agora é menor que o espaço livre total. Na FAT e na indexada a resposta é sempre NÃO, porque qualquer bloco livre serve; o `defrag` nelas é opcional e só reagrupa blocos dispersos (coluna "trechos" > 1).

### Estrutura do código

```
main.py                    ponto de entrada (argumentos, abre/cria a imagem)
simos/shell.py             shell interativo (comandos, ajuda, roteiros)
simos/fs/disk.py           camada física: disco em blocos de tamanho fixo
simos/fs/allocation.py     estratégias: contígua, FAT e indexada
simos/fs/inode.py          i-node
simos/fs/filesystem.py     árvore, caminhos, CRUD, persistência e compactação
simos/fs/diagnostics.py    cálculo de mapa de blocos e fragmentação
simos/fs/render.py         exibição (árvore, mapa visual, relatórios)
demos/                     roteiros de demonstração
tests/                     testes automatizados
```

### Testes

```bash
python -m unittest -v
```
