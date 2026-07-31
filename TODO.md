# TODO

Backlog operacional do `kwikledgers_langgraph/`.

## Como usar este backlog

- Mantenha os slices pequenos, validados e focados.
- Ao concluir um item, atualize testes e documentação quando o comportamento mudar.
- Enquanto este repositório ainda estiver dentro do monorepo, registre a mudança em `../docs/history/kwikledgers-alteration-YYYY-MM-DD.md`.

## Em andamento

- Nenhum slice aberto no momento.

## Estado atual verificado

- `daily_summary`, `story_pull_request`, `story_branch_association` e `story_status_transition` existem como fluxos compiláveis no runtime.
- O runtime ja aceita selecao explicita de backend por CLI ou `KWIKLEDGERS_LANGGRAPH_BACKEND`, defaultando para `legacy`.
- O backend `fastmcp` agora atende provider Azure, tracking local e notificacao de `daily_summary`; os fluxos mutaveis continuam no legado.
- A suíte atual está verde com `94` testes unitários.
- Os fluxos mutáveis agora compartilham um seam interno preview-first com tratamento uniforme de preview, gate e execute.

## Concluído

- [x] Criar um seam real de resolução de identidade do usuário para o runtime novo.
- [x] Fazer o CI instalar as dependências reais do projeto e validar a superfície que depende de `langgraph`.
- [x] Endurecer o contrato de aprovação para mutações reais, separando o pedido emitido pelo runtime da confirmação humana e reduzindo a dependência em `pending_approval` vindo pronto no estado inicial.
- [x] Cobrir a superfície CLI do fluxo mutável de PR da story com testes focados.
- [x] Cobrir o grafo `story_pull_request` com testes comportamentais: payload inválido, preview sem aprovação, parada limpa e caminho com aprovação.
- [x] Adicionar um seam de backend selector no runtime, preservando `legacy` como default e impedindo retomadas de preview entre backends diferentes.
- [x] Emitir um stream estruturado externo de observabilidade do runtime, sem perder o resumo enxuto em `AI_Tracking/Metrics/ai-usage-log.md`.
- [x] Usar revisao nativa da story no preview de status quando o adapter legado consegue acessar o cliente Azure, preservando fallback compativel.
- [x] Sanitizar a observabilidade externa para reduzir payloads e redigir campos sensiveis antes de persistir artefatos em `AI_Tracking`.
- [x] Oferecer assinatura HMAC opcional para os arquivos de estado salvos dos fluxos mutaveis e rejeitar retomadas nao assinadas quando essa protecao estiver configurada.

## Próximos slices prioritários

### P1

- [x] Adicionar o fluxo preview-first de associação de branch da story.
	Critério mínimo: criar contrato em `state.py`, porta em `adapters.py`, adapter legado, grafo `validate -> preview -> gate -> execute/stop`, builder em `runtime.py`, CLI dedicada e testes do caminho sem aprovação, com aprovação e parada limpa.
- [x] Adicionar o fluxo preview-first de transição de status da story.
	Critério mínimo: repetir a mesma superfície do fluxo de PR, com `request_id` vinculado ao preview e retomada explícita via estado salvo.
- [x] Extrair um seam compartilhado para fluxos mutáveis preview-first.
	Objetivo: reduzir duplicação entre PR, associação de branch e transição de status sem enfraquecer guardrails, observabilidade e approval gate.

### P2

- [x] Integrar observabilidade do runtime com um destino externo do stack da KwikLedgers, como `kl_observatory`, OpenTelemetry ou LangSmith.
- [x] Normalizar melhor a política de payload mínimo em preview, tracking e timeline.
	Foco: persistir só os campos operacionais necessários para retomada, auditoria e troubleshooting.
- [x] Expandir testes dos adapters legados com cenários de erro e payload inválido.
	Cobrir pelo menos: JSON inválido, payload raiz não-dict, falha ao localizar `agent/mcp_servers` e erro propagado do legado.
- [x] Cobrir o caminho de erro dos grafos mutáveis quando o adapter de preview ou execute falha.
	Hoje a suíte cobre principalmente payload inválido e approval gate, mas ainda não trata falhas reais do adapter.

### P3

- [x] Expor os novos fluxos mutáveis também em documentação operacional no `README.md` com exemplos de ida e retomada por arquivo de estado.
- [x] Revisar se `story` no estado tipado deve voltar a ser usado ou removido do contrato enquanto não participa dos fluxos atuais.
- [x] Decidir se o runtime principal deve ganhar um registry explícito de fluxos mutáveis além dos builders soltos atuais.

## Critérios de pronto por slice

- O comportamento novo ou ajustado está coberto por teste focado.
- O caminho de validação mais barato foi executado ou, se indisponível, documentado.
- O `README.md` foi atualizado quando a superfície pública do repositório mudou.
- O histórico em `../docs/history/` foi atualizado.

## Lacunas concretas observadas hoje

- O backend `fastmcp` ainda nao cobre os fluxos mutaveis; essas superfícies continuam no legado.

## Ordem sugerida de execução

1. Abrir a primeira capability mutavel ou preview deterministico do Azure no `fastmcp`, mantendo o contrato preview-first no LangGraph.