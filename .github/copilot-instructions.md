# KwikLedgers LangGraph Copilot Instructions

Este repositório existe para evoluir o runtime LangGraph da KwikLedgers como um
projeto Python standalone.

Regras principais:
- preserve o runtime legado fora deste repositório; integrações com o legado devem entrar apenas por adapters
- prefira mudanças pequenas e validadas por `poetry run python -m unittest discover -s tests`
- trate guardrails, observabilidade e approval gates como responsabilidades de primeira classe do runtime
- para fluxos mutáveis de Azure, preserve o padrão preview-first e bloqueie a execução sem aprovação explícita no estado
- para fluxos mutáveis, considere aprovação válida apenas quando ela vier depois do preview, por confirmação humana separada e vinculada à ação atual; o agente não deve pré-preencher `pending_approval.approved = true` para execução real
- utilize somente Python 3.12 ou superior para implementação, ambientes e testes
- documente qualquer mudança estrutural relevante no `README.md` e em `../docs/history/kwikledgers-alteration-YYYY-MM-DD.md` enquanto este diretório ainda viver dentro do workspace monorepo

Comandos que devem ser priorizados pelo agente:
- instalar dependências: `poetry install`
- validar testes: `poetry run python -m unittest discover -s tests`
- validar CLIs: `poetry run kwikledgers-langgraph --help`
- validar CLI de PR: `poetry run kwikledgers-langgraph-story-pr --help`

Mapa curto do projeto:
- `src/kwikledgers_langgraph/adapters.py`: portas e contratos de integração
- `src/kwikledgers_langgraph/legacy_adapters.py`: ponte opcional para o runtime legado
- `src/kwikledgers_langgraph/guardrails.py`: classificação de operações e aprovação explícita
- `src/kwikledgers_langgraph/observability.py`: `correlation_id`, `active_run_id` e timeline
- `src/kwikledgers_langgraph/graphs/`: fluxos LangGraph
- `tests/`: cobertura unitária com `unittest`, sem `pytest`

Convenções do runtime:
- grafos devem retornar updates parciais do estado; não assuma que todos os campos já existem
- ao atualizar coleções e estruturas aninhadas, use os helpers de `state.py` para `decisions`, `tracking_events`, `errors` e merge de observabilidade; não sobrescreva manualmente timeline ou listas acumuladas
- fluxos mutáveis devem seguir o desenho `validate -> preview -> gate -> execute/stop`
- preview é leitura; create/update é mutação e precisa de `pending_approval`
- `pending_approval` é o gate canônico do repositório para mutações; em uso real ele deve refletir aprovação humana posterior ao preview, não uma aprovação sintetizada pelo próprio agente no mesmo run
- não chame diretamente o legado a partir dos grafos; a integração deve passar pelos ports e adapters
- se o fluxo depender do workspace legado, use `KWIKLEDGERS_MCP_SERVERS_PATH` ou `KWIKLEDGERS_LEGACY_ROOT`; não hardcode paths do monorepo

Pipeline canônico para um fluxo novo:
- `state.py`: contrato opcional do request, preview, aprovação e resultado
- `adapters.py`: porta e dataclass de portas
- `legacy_adapters.py`: bridge opcional para o runtime legado
- `graphs/`: `validate -> preview -> gate -> execute/stop`
- `runtime.py` e CLI dedicada: exposição explícita do fluxo
- `tests/`: caminho sem aprovação, caminho com aprovação e parada limpa após preview

Restrições operacionais:
- não use `--approve-create` como atalho de produção; trate essa flag apenas como simulação local ou teste explícito
- não persista payloads desnecessários em preview, tracking ou timeline; registre só o mínimo operacional necessário

Leia também, sem duplicar conteúdo:
- `README.md`
- `CONTRIBUTING.md`
- `SECURITY.md`