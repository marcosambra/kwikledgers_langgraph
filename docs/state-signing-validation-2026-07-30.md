# State Signing Validation - 2026-07-30

Slice: assinatura HMAC opcional dos arquivos de estado salvos dos fluxos mutaveis.

Validacoes executadas:

- `poetry install`
- `poetry run kwikledgers-langgraph-story-pr --help`
- `poetry run kwikledgers-langgraph-story-branch --help`
- `poetry run kwikledgers-langgraph-story-status --help`
- `poetry run python -m unittest discover -s tests`

Resultado esperado deste slice:

- o runtime continua aceitando arquivos de estado simples quando nao ha chave configurada
- com `KWIKLEDGERS_LANGGRAPH_STATE_SIGNING_KEY`, os arquivos salvos passam a sair assinados e retomadas sem assinatura passam a falhar
- a suite unitária deve ficar verde com `91` testes

Observacoes:

- a assinatura fica toda concentrada em `state_persistence.py`, sem mudar o contrato interno dos grafos