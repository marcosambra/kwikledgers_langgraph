# Validação de Ownership de Processo

Data: 2026-09-17

## Mudanças

- `story_status_policy.py` centraliza no LangGraph o fluxo canônico da story.
- `task_closure_policy.py` centraliza no LangGraph o cálculo de horas por `ceil()` e o preview de fechamento.
- Estados resumíveis de fluxos mutáveis exigem assinatura HMAC.
- Simulação de aprovação por CLI exige ambiente de teste explícito.

## Evidências

```text
poetry run python -m unittest tests.test_story_status_policy tests.test_story_status_transition_graph tests.test_legacy_adapters
Ran 21 tests
OK

poetry run python -m unittest tests.test_state_persistence tests.test_story_pr_cli tests.test_story_branch_cli tests.test_story_status_cli tests.test_task_closure_cli
Ran 43 tests
OK

poetry run python -m unittest tests.test_task_closure_policy tests.test_task_closure_graph tests.test_task_closure_cli
Ran 17 tests
OK
```