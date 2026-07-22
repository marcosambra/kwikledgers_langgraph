# Contributing

## English

### Scope

This repository owns the LangGraph-based runtime being introduced for
KwikLedgers orchestration.

Changes here should stay focused on the standalone runtime, its adapters,
guardrails, observability, tests, and packaging surface.

Do not modify the legacy KwikLedgers runtime from this repository. Legacy
integration must happen through explicit compatibility adapters.

### First Steps

1. Read `README.md` and `.github/copilot-instructions.md`.
2. Confirm whether the change belongs in this repository or in the legacy
   KwikLedgers workspace.
3. Keep changes small, testable, and easy to validate.
4. Update tests and documentation when behavior changes.

### Pull Request Expectations

- Explain the runtime seam or problem being changed.
- Include focused validation steps and command results.
- Avoid unrelated refactors in the same pull request.
- Call out any dependency on the legacy runtime explicitly.
- Commit `poetry.lock` together with `pyproject.toml` whenever dependencies change.

### Validation

Use the documented quick validation path whenever possible:

```bash
poetry install
poetry run python -m unittest discover -s tests
```

## Português

### Escopo

Este repositório cuida do runtime baseado em LangGraph que está sendo
introduzido para a orquestração da KwikLedgers.

As mudanças aqui devem ficar focadas no runtime standalone, nos adapters,
guardrails, observabilidade, testes e superfície de empacotamento.

Não altere o runtime legado da KwikLedgers a partir deste repositório. A
integração com o legado deve acontecer por adapters de compatibilidade
explícitos.

### Primeiros Passos

1. Leia `README.md` e `.github/copilot-instructions.md`.
2. Confirme se a mudança pertence a este repositório ou ao workspace legado da
   KwikLedgers.
3. Mantenha as mudanças pequenas, testáveis e fáceis de validar.
4. Atualize testes e documentação quando o comportamento mudar.

### Expectativas Para Pull Request

- Explique qual seam do runtime ou qual problema está sendo alterado.
- Inclua passos de validação focados e resultados de comandos.
- Evite refactors sem relação no mesmo pull request.
- Destaque explicitamente qualquer dependência no runtime legado.
- Faça commit do `poetry.lock` junto com o `pyproject.toml` sempre que as dependências mudarem.

### Validação

Use o caminho rápido de validação documentado sempre que possível:

```bash
poetry install
poetry run python -m unittest discover -s tests
```