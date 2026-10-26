# Cripto Bot — coleta, carteira PAPER e SMA opt-in

A aplicação valida configuração, preserva o ciclo vazio `NOOP/PAPER/orders_sent=0`
e coleta candles Spot, metadados e bid/ask pela CLI. A Sprint 02 adiciona fixtures
offline, SQLite transacional e exportações verificáveis. A Sprint 03 acrescenta
carteira e execução integral sintética. A Sprint 04 adiciona estratégia SMA opt-in
em fixture offline, com estado modular, risco centralizado e relatório HTML.
Nenhuma ordem externa é enviada; todos os módulos começam desabilitados.

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

## Carteira e execução sintética offline

```bash
cd /home/cams7/Dev/Cripto/cripto-bot
export UV_CACHE_DIR="$PWD/.uv-cache"
uv run --locked cripto-bot check-config --config config/paper.execution.example.toml
uv run --locked cripto-bot paper-run --config config/paper.execution.example.toml
uv run --locked cripto-bot paper-reconcile --config config/paper.execution.example.toml
uv run --locked cripto-bot paper-report --config config/paper.execution.example.toml
uv run --locked cripto-bot paper-run --config config/paper.execution.example.toml
```

Em carteira nova, o roteiro gera dois fills locais, termina com
`10009.58001000 USDT`, sem BTC/reservas, taxas de `0.20999000 USDT` e resultado
realizado de `9.58001000 USDT`. Preços, regras e capital são sintéticos.
A repetição gera zero fills novos, preserva o histórico e o mesmo hash financeiro.
A demonstração executada durante o desenvolvimento foi preservada: se usar seu
diretório `data/paper-demo` já preenchido, o comando apenas identifica o replay.

A tabela opcional `paper` define identidade, diretório, roteiro, custos e limites.
Valores financeiros exigem strings, até 18 dígitos inteiros e oito fracionários;
sem float/NaN/notação exponencial. Defaults do exemplo: entrada e exposição de
10%, perda diária de 1%, drawdown de 5%, taxa/spread/slippage de 10/10/5 bps.
Os novos comandos rejeitam `--allow-network` e não constroem clientes HTTP,
mesmo se a configuração contiver tabela `market`. `run-once` permanece NOOP.

O cenário JSON é validado integralmente antes de criar estado financeiro: no
máximo 1 MiB e 1000 eventos, tipos DAY_START/MARK/ORDER, UTC com milissegundos,
identidades únicas e observações inline identificadas. O relógio é de replay.
Uma decisão precede a observação; o fill não antecede a cotação nem a usa com
idade superior a 5 s. Não há busca de preço novo para recuperar uma reserva.

`paper.sqlite3` é separado de `market.sqlite3`. Capital inicial, reservas, fills,
taxas e contrapartidas possuem ledger balanceado separadamente em BTC e USDT.
Uma transação grava plano/reserva; outra confirma fill e posição. O plano é
integral e determinístico. Rejeição de risco não gera fill; rejeição/cancelamento
local de reserva, exercitados nas APIs de aplicação/domínio, liberam o ativo.
Fills parciais e cancelamento administrativo pela CLI não integram esta Sprint.

Um flock protege cada store. Antes de novos commits, o histórico é refeito e
comparado à projeção e à versão da carteira; SQLite mantém constraints e triggers
para preservar o journal/ledger/fills. Não alterar limites/modelo/roteiro de uma
carteira existente para reinicializá-la. Não existe comando de reset. Mudança
incompatível falha; uma experiência diferente deve usar carteira sintética nova.

Divergência bloqueia novos efeitos, sem ajustar saldo por diferença.
`paper-reconcile` persiste o bloqueio quando possível. `paper-report` é somente
leitura: pode mostrar reconstrução do histórico marcada como não reconciliada
quando a projeção diverge; histórico inválido produz diagnóstico sem inventar
saldos. Sem banco existente, report falha e não inicializa carteira.

DAY_START fixa o baseline à meia-noite UTC; perda/pico/travas persistem. Uma
saída pode reduzir posição sob trava de risco, sem liquidação automática. O
simulador suporta taxas somente em USDT e filtros sintéticos de preço, lote e
notional; filtro desconhecido bloqueia a intenção. REFERENCE aplica metade do
spread por lado mais slippage; BID_ASK já contém spread e não o cobra novamente.

Relatórios em `data_dir/reports/<run_id>` são derivados do banco e têm manifesto
com hashes e proveniência. Falha de exportação preserva fills; repetir reexporta.
O hash financeiro exclui run_id/horários reais, mantendo o relógio e eventos do
replay. Exit 3 indica falha operacional/divergência; rejeição de risco esperada
fica no relatório do roteiro com status RISK_REJECTED e zero fill correspondente.

Detalhes, resultados e limitações no
[guia da Sprint 03](../docs/testes/sprint-03-validacao.md).

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
- [Sprint 03](../docs/sprints/sprint-03.md)
- [Arquitetura](../docs/arquitetura.md)
- [ADR 0001](../docs/decisoes/0001-monolito-modular.md)
- [ADR 0002](../docs/decisoes/0002-modos-e-decimais.md)

Este módulo possui repositório Git próprio em `cripto-bot`. A documentação
central permanece no repositório independente `../docs`. Cada novo módulo
será criado diretamente em `/home/cams7/Dev/Cripto` com seu próprio Git.
Nenhum recurso AWS, credencial, ordem externa ou notificação é necessário à Sprint 01.


## Sprint 04 — SMA e relatórios legíveis

A base modular aceita somente local/PAPER/fixture/BTCUSDT/15m. A instalação
sem configuração e o exemplo padrão retornam NOOP, sem carteira ou intenção.
O opt-in separado executa SMA20/50 em 71 candles sintéticos fechados:

```bash
uv run --locked cripto-bot strategy-run
uv run --locked cripto-bot check-config --config config/strategy.sma.example.toml
uv run --locked cripto-bot strategy-run --config config/strategy.sma.example.toml
uv run --locked cripto-bot strategy-run --config config/strategy.sma.fixture.toml
uv run --locked cripto-bot strategy-reconcile --config config/strategy.sma.fixture.toml
uv run --locked cripto-bot strategy-report --config config/strategy.sma.fixture.toml
uv run --locked cripto-bot strategy-run --config config/strategy.sma.fixture.toml
```

Em carteira nova, o opt-in demonstra dois fills de 9.072754 BTC, taxas totais
1.90536908 USDT, resultado -94.53818742 USDT e saldo final 9905.46181258 USDT.
São valores fictícios; o resultado negativo é esperado. Replay gera zero novos
fills. Se o diretório já existir, não apagá-lo para fabricar primeira execução;
criar outro TOML/diretório sintético identificado. A fixture de instalação usa
`data/sma-fixture-v1`; os dados de desenvolvimento anteriores não são o aceite.

`strategy-run` e `strategy-report` publicam `report.json`, `report.html` e
`manifest.json` em `<strategy.data_dir>/reports/<run_id>/`. O JSON da CLI informa
`html_report` e `manifest`. Abra o HTML no navegador: contém resumo, curva do
patrimônio, motivos/médias, operações, risco e detalhes de todos os ciclos.
O arquivo é autocontido, sem scripts ou recursos remotos, com estilo de impressão.
Report usa snapshot de banco somente leitura; não avalia nem negocia. Falha de
exportação não refaz fills; reexportar com `strategy-report`.

Os comandos `paper-*` continuam usando carteira schema v1 e roteiro manual.
Os comandos `strategy-*` usam schema v2/modelo `paper-modular-v1`, em diretório
novo; um fluxo rejeita o banco do outro. Não há migração/reset automático.
O banco de mercado permanece v1. Estratégia nunca recebe cliente HTTP ou porta
financeira; coordenador/risco não podem ser desligados por flag. Overrides em
`modules.<instance>.limits` e `coordination.symbol_limits.BTCUSDT` só restringem
limites globais. Duas instâncias ativas no mesmo recurso são incompatíveis.

Desativação conserva monitoramento e custódia: uma posição remanescente fica
DRAINING, sem venda automática. Mudança material com posição é rejeitada;
reativação em caixa não executa cruzamentos antigos. As dependências declaradas
por `depends_on` exigem instância disponível e sem ciclo. Não há import dinâmico.
TWAP/VWAP, multiativo, ciclo contínuo, parcial/UNKNOWN real,
AWS e trading privado permanecem fora deste incremento. `run-once` segue NOOP;
`--allow-network` continua exclusivo da coleta pública explícita.

Documentação: [SMA](../docs/estrategias/trend_sma.md),
[especificação 04](../docs/sprints/sprint-04.md),
[validação/evidências](../docs/testes/sprint-04-validacao.md),
[banco v1/v2](../docs/bancos/paper-sqlite.md) e
[ADR 0005](../docs/decisoes/0005-estrategia-modular-e-persistencia.md).


## Backtest histórico — Sprint 05

Somente PAPER/local/BTCUSDT/15m, opt-in. A SMA pura e o núcleo financeiro são compartilhados; o histórico usa dataset validado, protocolo congelado e banco próprio, sem converter carteiras anteriores. Sem módulo habilitado, `backtest-run` retorna NOOP sem carteira.

```bash
uv run --locked cripto-bot backtest-run --config config/backtest.sma.example.toml
uv run --locked cripto-bot backtest-prepare --config config/backtest.sma.fixture.toml
uv run --locked cripto-bot backtest-freeze --config config/backtest.sma.fixture.toml
uv run --locked cripto-bot backtest-run --config config/backtest.sma.fixture.toml --segment development
uv run --locked cripto-bot backtest-run --config config/backtest.sma.fixture.toml --segment validation
uv run --locked cripto-bot backtest-run --config config/backtest.sma.fixture.toml --segment test
uv run --locked cripto-bot backtest-reconcile --config config/backtest.sma.fixture.toml
uv run --locked cripto-bot backtest-report --config config/backtest.sma.fixture.toml
```

A fixture contém 1010 candles sintéticos (50 só para aquecimento), não cotações reais. Cada segmento/variante/perfil começa em 10000 USDT e é independente. HTML/JSON/CSV/manifesto são gerados automaticamente após cada segmento; `backtest-report` reexporta o snapshot em leitura, sem novo sinal/fill. O caminho `html_report` aparece no stdout JSON. Repetição mantém o resultado e não duplica fills.

Para dados reais, executar `collect-market` separadamente, com `config/market.backtest.example.toml`, janelas de até 10000 candles e `--allow-network` explícito. Depois informar os manifestos locais em uma configuração própria; os comandos backtest nunca acessam rede. A coleta não é avaliação. Não reutilizar a fixture curta da 04 como histórico econômico.

O freeze identifica fontes, código, parâmetros, split, custos, risco e modelos. Mudança exige outro protocolo e repetir desenvolvimento/validação; uma janela final real já consumida ou sobreposta não se torna intocada com outro ID. O registro é local e não prova ausência de conhecimento humano prévio. Filtros são proxy sintético declarado; open OHLC não demonstra liquidez/fill real. Posição final permanece marcada, sem venda implícita. Nenhum resultado autoriza LIVE ou inicia a Sprint 06.

DDL, operação/recuperação e limites em `../docs/bancos/backtest-sqlite.md`; protocolo/aceite em `../docs/sprints/sprint-05.md` e `../docs/testes/sprint-05-validacao.md`. Dependências e lock anteriores são mantidos.

## Análise histórica com DuckDB + Parquet

`backtest-run` e `backtest-report` publicam também `equity.parquet`,
`trades.parquet`, `decisions.parquet`, `analytics.duckdb` e `analytics.json`,
todos cobertos pelo manifesto SHA256. DuckDB 1.5.6 é uma dependência Python
do lock; instale com `uv sync --locked --group dev`. O bot usa o motor
embutido e não depende de um serviço Docker ou de porta de rede.

O catálogo contém `curve_summary`, `decisions_counts`, `trades_counts` e `metadata`.
Os resumos SQL são calculados lendo os Parquets e suas contagens são confrontadas
com o SQLite e os acumuladores oficiais. Valores financeiros permanecem strings
exatas; métricas continuam no núcleo Decimal e o ledger SQLite não é migrado.
CSV/HTML/JSON continuam disponíveis. Falha na conversão impede a publicação do
export e pode ser repetida via `backtest-report`, sem novos sinais/fills.

Em um diretório de exportação, consulte pelo Docker já instalado:

```bash
docker run --rm -it \
  --mount "type=bind,source=$PWD,target=/data,readonly" \
  -w /data duckdb/duckdb:1.5.6 \
  duckdb -readonly /data/analytics.duckdb
```

```sql
SELECT segment, variant, profile, final_equity, decisions, fills
FROM curve_summary;
SELECT run_key, side, count(*)
FROM read_parquet('/data/trades.parquet')
GROUP BY run_key, side;
```

A primeira exportação inclui o custo de conversão; este ajuste não elimina a
reconciliação financeira em Python nem promete acelerar o replay completo.
Exportações antigas são preservadas. Para gerar os novos formatos de um banco
existente, use `backtest-report`; mudanças de código/lock não reabrem a janela
final para novo experimento. Schema/operação: [camada analítica](../docs/bancos/backtest-analytics.md).
