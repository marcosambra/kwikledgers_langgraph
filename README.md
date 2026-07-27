# KwikLedgers LangGraph Runtime

Repositório Python do runtime futuro de orquestração da KwikLedgers em
LangGraph.

Objetivos deste primeiro slice:
- manter o agente legado atual intocado
- abrir um seam tecnico para migracao incremental dos fluxos
- definir estado tipado e portas de integracao independentes do MCP atual
- compilar o primeiro subgrafo de `Daily Summary`

Este repositório ainda nao substitui o runtime atual por completo. Ele define a
base do novo runtime em LangGraph para que os adapters de Azure, tracking local
e notificacoes sejam plugados sem editar o runtime legado nesta fase.

## Modo de repositorio standalone

O projeto foi estruturado para viver como um repositório próprio no GitHub.

- O codigo-fonte vive sob `src/kwikledgers_langgraph/`.
- A validacao basica vive sob `tests/`.
- O runtime legado da KwikLedgers e opcional e entra apenas por adapters de
	compatibilidade.
- Os adapters legados nao dependem mais de um caminho fixo do monorepo; eles
	aceitam configuracao explicita por variaveis de ambiente.

Variaveis de ambiente suportadas para compatibilidade com o runtime legado:

- `KWIKLEDGERS_MCP_SERVERS_PATH`: caminho absoluto para o diretorio
	`agent/mcp_servers` do workspace legado.
- `KWIKLEDGERS_LEGACY_ROOT`: caminho absoluto para o root do workspace legado;
	o adapter resolve `agent/mcp_servers` a partir dele.
- `KWIKLEDGERS_USER_EMAIL`: email explicito do usuario atual para o seam de identidade do runtime novo.
- `KWIKLEDGERS_USER_DISPLAY_NAME`: nome exibido do usuario atual quando a identidade for resolvida por ambiente.

Variaveis de ambiente suportadas para tracing externo com LangSmith:

- `KWIKLEDGERS_LANGSMITH_ENABLED`: habilita o tracing externo do runtime quando estiver como `true`.
- `KWIKLEDGERS_LANGSMITH_PROJECT`: nome do projeto do LangSmith para os runs deste runtime.
- `LANGSMITH_API_KEY`: chave local do LangSmith; nao deve ser commitada.
- `LANGSMITH_ENDPOINT`: endpoint HTTP da API do LangSmith, quando nao usar o padrao cloud.
- `LANGSMITH_PROJECT`: alternativa padrao da SDK caso voce nao use o prefixo `KWIKLEDGERS_`.
- `LANGSMITH_TRACING` ou `LANGSMITH_TRACING_V2`: alternativas da SDK aceitas como sinal explicito de tracing.

Exemplo local sem commitar segredo:

```bash
cp .env.langsmith.example .env.langsmith.local
export $(grep -v '^#' .env.langsmith.local | xargs)
```

Preencha `LANGSMITH_API_KEY` localmente antes de exportar o arquivo.

## Estrutura inicial

- `src/kwikledgers_langgraph/state.py`: contrato tipado do estado compartilhado
- `src/kwikledgers_langgraph/adapters.py`: portas/protocolos para integracoes
- `src/kwikledgers_langgraph/legacy_adapters.py`: adapters de compatibilidade com o runtime atual
- `src/kwikledgers_langgraph/guardrails.py`: gates explicitos de aprovacao e classificacao de operacoes
- `src/kwikledgers_langgraph/observability.py`: correlation ID, run ID e timeline da execucao
- `src/kwikledgers_langgraph/graphs/daily_summary.py`: primeiro subgrafo
- `src/kwikledgers_langgraph/graphs/story_pull_request.py`: fluxo de preview/aprovacao/criacao de PR da story
- `src/kwikledgers_langgraph/runtime.py`: fabrica de compilacao do runtime
- `src/kwikledgers_langgraph/cli.py`: entrypoint manual do novo runtime
- `src/kwikledgers_langgraph/story_pr_cli.py`: entrypoint dedicado ao fluxo de PR da story

## Execucao manual

Instale as dependencias do ambiente com Poetry:

```bash
poetry install
```

Quando as dependencias declaradas em `pyproject.toml` mudarem, regenere e versione
tambem o `poetry.lock`:

```bash
poetry lock
```

```bash
poetry run kwikledgers-langgraph --help
```

```bash
poetry run kwikledgers-langgraph-story-pr --help
```

## Validacao rapida

```bash
poetry run python -m unittest discover -s tests
```

Para executar com compatibilidade legada, o runtime novo importa os servidores
atuais apenas como adapters e sem modificar nenhum arquivo do runtime legado.

Exemplo:

```bash
export KWIKLEDGERS_LEGACY_ROOT=/path/to/legacy/workspace
poetry run kwikledgers-langgraph --disable-notifications
```

## Guardrails e observabilidade

O runtime novo agora registra em codigo:
- classificacao explicita das operacoes por tipo
- decisao de guardrail para cada etapa do fluxo
- `correlation_id` e `active_run_id` por execucao
- timeline de eventos por node no estado compartilhado

Quando LangSmith estiver habilitado por ambiente, as CLIs tambem abrem um run
raiz por execucao com metadata minima do fluxo:
- `flow_name`, `source`, `correlation_id` e `active_run_id`
- `story_id` e `repository_name` apenas quando o fluxo for de PR da story
- nenhum email bruto, payload secreto ou `LANGSMITH_API_KEY` entra no estado tipado, tracking ou metadata serializada

O fluxo `daily_summary` agora tambem aceita um seam real de identidade:
- preserva `user_identity` quando ela ja vem do estado
- tenta resolver apenas nome e email explicitamente fornecidos por adapter configurado
- evita persistir email bruto em tracking e timeline, registrando apenas flags operacionais sobre a resolucao

As operacoes mutaveis de Azure ficam reservadas para fluxos futuros com
aprovacao explicita; o `Daily Summary` atual e classificado como fluxo sem
mutacao Azure, mas ja passa pela mesma camada de guardrails.

O primeiro fluxo mutavel agora existe para PR de story, mas segue preview-first:
- o preview da PR e sempre executado como leitura
- o runtime emite um `pending_approval` com `request_id` proprio para o preview atual
- a criacao da PR so segue quando uma `approval_confirmation` separada reaproveita esse `request_id`, o preview anterior e o `active_run_id` da execucao
- o CLI dedicado aceita `--approve-create` apenas como simulacao local apos o preview,
	nao como aprovacao real de producao

Fluxo operacional da CLI de PR da story:

```bash
poetry run kwikledgers-langgraph-story-pr \
	--story-id 120204 \
	--repository-name accounting \
	--source-branch feature/KL-120204-balance-sheet-by-class \
	--what-was-changed "Resumo formal da alteracao" \
	--affected-processes "Fluxo de PR da story" \
	--expected-impacts "Maior previsibilidade do runtime" \
	--important-points "Validar preview e aprovacao" \
	--mermaid-diagram "graph TD; A-->B;" \
	--save-state-file /tmp/story-pr-preview.json \
	--print-state
```

- o primeiro comando gera o preview e grava o estado final completo, incluindo `pull_request_preview`, `pending_approval.request_id` e `observability.active_run_id`
- a aprovacao humana deve acontecer fora do runtime e referenciar o `request_id` exibido no estado salvo

```bash
poetry run kwikledgers-langgraph-story-pr \
	--resume-state-file /tmp/story-pr-preview.json \
	--approval-request-id REQUEST_ID_APROVADO \
	--save-state-file /tmp/story-pr-final.json \
	--print-state
```

- o segundo comando reaproveita o mesmo estado serializado para preservar preview, `request_id` e `active_run_id`
- a retomada real depende desse mesmo arquivo de estado; reiniciar a CLI com um estado novo invalida o vinculo exigido pelo guardrail

## Proximo passo natural

Expandir o mesmo padrao preview-first para outros fluxos mutaveis, como
associacao de branch e transicao de status da story.