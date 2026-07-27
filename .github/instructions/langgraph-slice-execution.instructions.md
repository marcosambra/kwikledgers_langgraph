---
description: "Use when planning, implementing, validating, or continuing kwikledgers_langgraph backlog slices, especially preview-first mutable flows such as branch association, story status transition, observability integration, adapter hardening, and LangGraph runtime backlog execution."
name: "LangGraph Slice Execution"
applyTo:
  - "src/kwikledgers_langgraph/**/*.py"
  - "tests/**/*.py"
  - "README.md"
  - "TODO.md"
  - "pyproject.toml"
  - "poetry.lock"
---
# LangGraph Slice Execution

Use `TODO.md` as the backlog authority for `kwikledgers_langgraph` work.

## Backlog Order

Execute the backlog in this order unless the user explicitly reprioritizes it.

### P1

- Adicionar o fluxo preview-first de associação de branch da story.
  Critério mínimo: criar contrato em `state.py`, porta em `adapters.py`, adapter legado, grafo `validate -> preview -> gate -> execute/stop`, builder em `runtime.py`, CLI dedicada e testes do caminho sem aprovação, com aprovação e parada limpa.
- Adicionar o fluxo preview-first de transição de status da story.
  Critério mínimo: repetir a mesma superfície do fluxo de PR, com `request_id` vinculado ao preview e retomada explícita via estado salvo.
- Extrair um seam compartilhado para fluxos mutáveis preview-first.
  Objetivo: reduzir duplicação entre PR, associação de branch e transição de status sem enfraquecer guardrails, observabilidade e approval gate.

### P2

- Integrar observabilidade do runtime com um destino externo do stack da KwikLedgers, como `kl_observatory`, OpenTelemetry ou LangSmith.
- Normalizar melhor a política de payload mínimo em preview, tracking e timeline.
  Foco: persistir só os campos operacionais necessários para retomada, auditoria e troubleshooting.
- Expandir testes dos adapters legados com cenários de erro e payload inválido.
  Cobrir pelo menos: JSON inválido, payload raiz não-dict, falha ao localizar `agent/mcp_servers` e erro propagado do legado.
- Cobrir o caminho de erro dos grafos mutáveis quando o adapter de preview ou execute falha.
  Hoje a suíte cobre principalmente payload inválido e approval gate, mas ainda não trata falhas reais do adapter.

### P3

- Expor os novos fluxos mutáveis também em documentação operacional no `README.md` com exemplos de ida e retomada por arquivo de estado.
- Revisar se `story` no estado tipado deve voltar a ser usado ou removido do contrato enquanto não participa dos fluxos atuais.
- Decidir se o runtime principal deve ganhar um registry explícito de fluxos mutáveis além dos builders soltos atuais.

## Slice Workflow

- Mantenha os slices pequenos, validados e focados.
- Trabalhe em um slice por vez. Não abra o próximo antes de validar e encerrar o atual.
- Para fluxos mutáveis, preserve o desenho `validate -> preview -> gate -> execute/stop`.
- Siga a sequência canônica de arquivos quando aplicável: `state.py`, `adapters.py`, `legacy_adapters.py`, `graphs/`, `runtime.py`, CLI dedicada e `tests/`.
- Trate como lacunas concretas atuais: ausência de porta específica para associação de branch, ausência de porta específica para transição de status, `runtime.py` compilando apenas `daily_summary` e `story_pull_request`, ausência de CLI além da de PR e ausência de grafos mutáveis além de `story_pull_request`.

## Slice Completion Gate

- Ao parar cada slice, execute `poetry install`.
- Em seguida, execute `poetry run python -m unittest discover -s tests`.
- Se uma CLI nova ou alterada fizer parte do slice, valide também o `--help` dela antes de ampliar a validação.
- Se a validação falhar, pare no slice atual, reporte o bloqueio com o menor contexto necessário e não avance para o próximo slice.
- Se a validação passar, crie um commit focado do slice concluído e só então avance para o próximo slice do backlog.

## Definition Of Done

- O comportamento novo ou ajustado está coberto por teste focado.
- O caminho de validação mais barato foi executado ou, se indisponível, documentado.
- O `README.md` foi atualizado quando a superfície pública do repositório mudou.
- O histórico em `../docs/history/kwikledgers-alteration-YYYY-MM-DD.md` foi atualizado.

## Current Verified State

- `daily_summary` e `story_pull_request` existem como fluxos compiláveis no runtime.
- A suíte atual está verde com `28` testes unitários.
- Ainda não existem contratos, runtime, CLI e testes equivalentes para os outros fluxos mutáveis previstos no `README.md`.