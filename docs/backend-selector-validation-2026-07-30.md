# Backend Selector Validation - 2026-07-30

Slice: backend selector seam no runtime `kwikledgers_langgraph`.

Validacoes executadas:

- `poetry install`
- `poetry run kwikledgers-langgraph --help`
- `poetry run kwikledgers-langgraph-story-pr --help`
- `poetry run kwikledgers-langgraph-story-branch --help`
- `poetry run kwikledgers-langgraph-story-status --help`
- `poetry run python -m unittest discover -s tests`

Resultado:

- instalacao concluida sem dependencias pendentes
- as quatro CLIs expuseram a flag `--backend`
- a suite unitária terminou verde com `83` testes

Observacoes:

- o backend `fastmcp` continua indisponivel por capability, mas a selecao agora e registrada em observabilidade e revalidada nas retomadas de preview-first
- a suite ainda emite um `LangChainPendingDeprecationWarning` do pacote `langgraph`, sem falha funcional neste slice