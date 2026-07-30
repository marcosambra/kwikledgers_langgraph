# Native Story Revision Validation - 2026-07-30

Slice: uso de revisao nativa da story no preview de transicao de status.

Validacoes executadas:

- `poetry install`
- `poetry run kwikledgers-langgraph-story-status --help`
- `poetry run python -m unittest discover -s tests`

Resultado esperado deste slice:

- o adapter de status usa a revisao nativa do work item quando o modulo legado expoe `get_client()`
- a compatibilidade com o parse textual antigo permanece como fallback
- a suite unitária deve ficar verde com `87` testes

Observacoes:

- esse ajuste fica todo dentro de `kwikledgers_langgraph/legacy_adapters.py`, sem editar o servidor legado em `agent/mcp_servers/azure_devops/`