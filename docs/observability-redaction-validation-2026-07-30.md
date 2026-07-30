# Observability Redaction Validation - 2026-07-30

Slice: reducao e redacao de payloads antes da persistencia da observabilidade externa.

Validacoes executadas:

- `poetry install`
- `poetry run kwikledgers-langgraph --help`
- `poetry run python -m unittest discover -s tests`

Resultado esperado deste slice:

- o resumo markdown continua sendo emitido
- o stream `langgraph-runtime-events.jsonl` passa a redigir campos sensiveis como `url`, `token`, `body`, `markdown` e equivalentes
- a suite unitária permanece verde com `87` testes

Observacoes:

- a sanitizacao fica concentrada no modulo `observability.py`, sem alterar o contrato interno dos grafos ou dos adapters