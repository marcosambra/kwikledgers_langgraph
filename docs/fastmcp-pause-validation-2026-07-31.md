# FastMCP Pause Validation - 2026-07-31

Slice: pausa formal do backend `fastmcp` no runtime ativo do `kwikledgers_langgraph`.

Validacoes executadas:

- `poetry run python -m unittest tests.test_backend_factory tests.test_daily_summary_cli`
- `poetry run python -m unittest discover -s tests`

Resultado:

- o selector do backend rejeita `fastmcp` de forma explicita para `daily_summary`
- os testes focados do selector e da CLI principal permanecem verdes
- a suíte completa do LangGraph terminou verde com `93` testes apos a pausa

Escopo coberto:

- `backend_factory.py` volta a bloquear o uso ativo de `fastmcp`
- `README.md` e `TODO.md` passam a documentar `legacy` como caminho ativo unico
- o backend `fastmcp` permanece reservado apenas como superficie congelada para eventual retomada futura