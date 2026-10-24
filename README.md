# Cripto Bot — base local PAPER

A Sprint 01 oferece uma CLI offline para validar configuração e executar um ciclo
vazio. O resultado é `NOOP`, `PAPER`, `orders_sent=0`; ainda não há coleta de dados,
carteira ou negociação.

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

Sem `--config`, a CLI usa defaults internos. O único campo TOML é `mode`, cujo
único valor permitido é `PAPER`. `CRIPTO_BOT_MODE` sobrepõe arquivo e default,
mas o arquivo é validado antes do override. Variáveis desconhecidas `CRIPTO_BOT_*`
e a presença de `BINANCE_API_KEY`/`BINANCE_API_SECRET` são rejeitadas, inclusive
vazias. A aplicação não carrega `.env` ou credenciais de arquivos.

Cada sucesso produz um objeto JSON em stdout e um evento JSON em stderr.
Exit codes: 0 sucesso, 2 argumentos/configuração inválidos, 1 falha interna.
`--help` apresenta ajuda textual. Erros não reproduzem os valores recebidos.

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
- [Arquitetura](../docs/arquitetura.md)
- [ADR 0001](../docs/decisoes/0001-monolito-modular.md)
- [ADR 0002](../docs/decisoes/0002-modos-e-decimais.md)

Este módulo possui repositório Git próprio em `cripto-bot`. A documentação
central permanece no repositório independente `../docs`. Cada novo módulo
será criado diretamente em `/home/cams7/Dev/Cripto` com seu próprio Git.
Nenhum recurso AWS, credencial, ordem externa ou notificação é necessário à Sprint 01.
