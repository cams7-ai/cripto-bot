# Cripto Bot — coleta local PAPER

A aplicação valida configuração, preserva o ciclo vazio `NOOP/PAPER/orders_sent=0`
e coleta candles Spot, metadados e bid/ask pela CLI. A Sprint 02 adiciona fixtures
offline, SQLite transacional e exportações verificáveis. Não há carteira ou negociação.

## Preparação

Raiz funcional: `/home/cams7/Dev/Cripto/cripto-bot`. Requer uv **0.12.19** e Python **3.13.15**.
Instale uv por um método da documentação oficial, fixando essa versão. Se o Python
não estiver disponível no PATH, uv pode descobrir uma instalação existente.
Downloads iniciais podem exigir rede; aplicação e testes executam offline.

```bash
cd /home/cams7/Dev/Cripto/cripto-bot
export UV_CACHE_DIR="$PWD/.uv-cache"
uv sync --locked --group dev
uv run --locked cripto-bot check-config --config config/paper.example.toml
uv run --locked cripto-bot run-once --config config/paper.example.toml
```

Sem `--config`, a CLI usa defaults internos. `mode` aceita somente `PAPER`;
a tabela opcional `market` configura a coleta. `CRIPTO_BOT_MODE` sobrepõe arquivo e default,
mas o arquivo é validado antes do override. Variáveis desconhecidas `CRIPTO_BOT_*`
e a presença de `BINANCE_API_KEY`/`BINANCE_API_SECRET` são rejeitadas, inclusive
vazias. A aplicação não carrega `.env` ou credenciais de arquivos.

Cada sucesso produz um objeto JSON em stdout e um evento JSON em stderr.
Exit codes: 0 sucesso, 2 argumentos/configuração inválidos, 3 coleta incompleta
ou falha operacional, 1 falha interna.
`--help` apresenta ajuda textual. Erros não reproduzem os valores recebidos.

## Coleta reproduzível sem rede

```bash
cd /home/cams7/Dev/Cripto/cripto-bot
export UV_CACHE_DIR="$PWD/.uv-cache"
uv run --locked cripto-bot check-config --config config/market.fixture.example.toml
uv run --locked cripto-bot collect-market --config config/market.fixture.example.toml --start 2026-10-01T00:00:00Z --end 2026-10-01T00:30:00Z
```

A primeira coleta insere dois candles sintéticos; repetir o comando insere zero.
A janela é `[start, end)`, com UTC/offset explícito e alinhamento de 15 minutos.
Dados vão para `data/market-fixture`, ignorado pelo Git. `run-once` permanece
NOOP e não coleta implicitamente, mesmo com um arquivo de configuração de mercado.

Campos de `market`: `source` (`fixture` ou `binance_public`), `symbol` (somente
`BTCUSDT`), `interval` (somente `15m`), `fixture_file`, `data_dir`, `page_limit`
(1–1000, default 1000) e `max_pages` (1–10, default 10). Fixture exige arquivo;
fonte pública proíbe esse campo. Não aceitar endpoints/credenciais na configuração.

Paths relativos são resolvidos a partir do diretório de execução, mesmo quando
`--config` é absoluto. Fixture e dados devem ficar dentro do repositório, inclusive
após resolver symlinks. O módulo é encontrado nos ancestrais do diretório atual
pelo manifest `cripto-bot`; para o wheel em diretório neutro, usar paths absolutos
no TOML e executar dentro do módulo. `check-config` não abre banco ou rede.

A fonte pública exige `--allow-network` em cada chamada; sem ele, falha antes de
criar cliente ou banco. Usa apenas quatro GETs em `https://data-api.binance.vision`,
sem autenticação, redirects ou proxies herdados. O exemplo é
`config/market.public.example.toml`. Coleta real não foi executada na validação.
Não usar `--allow-network` com fixture. A janela não pode incluir candle aberto.

SQLite é a fonte de verdade. Dados brutos, candles e checkpoint são confirmados
juntos por página; lacuna, conflito ou falta de progresso impedem o avanço.
Um lock local impede escritores concorrentes no mesmo diretório. O bloqueio
`not_before` público é compartilhado pelos datasets do mesmo banco; bancos em
outros diretórios ou processos em outros hosts não têm coordenação distribuída.

Cada exportação fica em `data_dir/exports/<run_id>` com `manifest.json`,
`candles.jsonl.gz`, `raw.jsonl.gz`, `snapshots.json` e `provenance.json`.
O manifesto inclui hashes, revisão e fingerprint dos fontes/configuração.
O hash canônico dos candles exclui os tempos de recebimento/processamento,
run_id e proveniência, que ficam registrados separadamente.

Se a exportação falhar, o comando retorna 3; o banco confirmado é preservado.
Repetir a janela verifica o prefixo e reexporta. Banco corrompido ou schema
incompatível não é recriado automaticamente. Não apagar o banco para esconder
um erro. Limpeza e diagnóstico estão no
[guia da Sprint 02](../docs/testes/sprint-02-validacao.md).

## Verificação

```bash
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy modules/trading/src
uv run --locked pytest -q --cov=cripto_bot --cov-branch --cov-report=term-missing
uv build
```

Consulte [o guia de validação](../docs/testes/sprint-01-validacao.md) para instalação
do wheel em outro ambiente, bloqueio de sockets, resultados e limitações.
O lock deve ser alterado deliberadamente; instalações normais usam `--locked`.

## Documentação

- [Planejamento](../docs/planejamento.md)
- [Sprint 01](../docs/sprints/sprint-01.md)
- [Sprint 02](../docs/sprints/sprint-02.md)
- [Arquitetura](../docs/arquitetura.md)
- [ADR 0001](../docs/decisoes/0001-monolito-modular.md)
- [ADR 0002](../docs/decisoes/0002-modos-e-decimais.md)

Este módulo possui repositório Git próprio em `cripto-bot`. A documentação
central permanece no repositório independente `../docs`. Cada novo módulo
será criado diretamente em `/home/cams7/Dev/Cripto` com seu próprio Git.
Nenhum recurso AWS, credencial, ordem externa ou notificação é necessário à Sprint 01.
