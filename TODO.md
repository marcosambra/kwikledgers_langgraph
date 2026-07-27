# TODO

Backlog operacional do `kwikledgers_langgraph/`.

## Como usar este backlog

- Mantenha os slices pequenos, validados e focados.
- Ao concluir um item, atualize testes e documentação quando o comportamento mudar.
- Enquanto este repositório ainda estiver dentro do monorepo, registre a mudança em `../docs/history/kwikledgers-alteration-YYYY-MM-DD.md`.

## Em andamento

- Nenhum slice aberto no momento.

## Estado atual verificado

- `daily_summary` e `story_pull_request` existem como fluxos compiláveis no runtime.
- A suíte atual está verde com `28` testes unitários.
- Ainda não existem contratos, runtime, CLI e testes equivalentes para os outros fluxos mutáveis previstos no README.

## Concluído

- [x] Criar um seam real de resolução de identidade do usuário para o runtime novo.
- [x] Fazer o CI instalar as dependências reais do projeto e validar a superfície que depende de `langgraph`.
- [x] Endurecer o contrato de aprovação para mutações reais, separando o pedido emitido pelo runtime da confirmação humana e reduzindo a dependência em `pending_approval` vindo pronto no estado inicial.
- [x] Cobrir a superfície CLI do fluxo mutável de PR da story com testes focados.
- [x] Cobrir o grafo `story_pull_request` com testes comportamentais: payload inválido, preview sem aprovação, parada limpa e caminho com aprovação.

## Próximos slices prioritários

### P1

- [ ] Adicionar o fluxo preview-first de associação de branch da story.
	Critério mínimo: criar contrato em `state.py`, porta em `adapters.py`, adapter legado, grafo `validate -> preview -> gate -> execute/stop`, builder em `runtime.py`, CLI dedicada e testes do caminho sem aprovação, com aprovação e parada limpa.
- [ ] Adicionar o fluxo preview-first de transição de status da story.
	Critério mínimo: repetir a mesma superfície do fluxo de PR, com `request_id` vinculado ao preview e retomada explícita via estado salvo.
- [ ] Extrair um seam compartilhado para fluxos mutáveis preview-first.
	Objetivo: reduzir duplicação entre PR, associação de branch e transição de status sem enfraquecer guardrails, observabilidade e approval gate.

### P2

- [ ] Integrar observabilidade do runtime com um destino externo do stack da KwikLedgers, como `kl_observatory`, OpenTelemetry ou LangSmith.
- [ ] Normalizar melhor a política de payload mínimo em preview, tracking e timeline.
	Foco: persistir só os campos operacionais necessários para retomada, auditoria e troubleshooting.
- [ ] Expandir testes dos adapters legados com cenários de erro e payload inválido.
	Cobrir pelo menos: JSON inválido, payload raiz não-dict, falha ao localizar `agent/mcp_servers` e erro propagado do legado.
- [ ] Cobrir o caminho de erro dos grafos mutáveis quando o adapter de preview ou execute falha.
	Hoje a suíte cobre principalmente payload inválido e approval gate, mas ainda não trata falhas reais do adapter.

### P3

- [ ] Expor os novos fluxos mutáveis também em documentação operacional no `README.md` com exemplos de ida e retomada por arquivo de estado.
- [ ] Revisar se `story` no estado tipado deve voltar a ser usado ou removido do contrato enquanto não participa dos fluxos atuais.
- [ ] Decidir se o runtime principal deve ganhar um registry explícito de fluxos mutáveis além dos builders soltos atuais.

## Critérios de pronto por slice

- O comportamento novo ou ajustado está coberto por teste focado.
- O caminho de validação mais barato foi executado ou, se indisponível, documentado.
- O `README.md` foi atualizado quando a superfície pública do repositório mudou.
- O histórico em `../docs/history/` foi atualizado.

## Lacunas concretas observadas hoje

- Não há porta específica em `adapters.py` para associação de branch.
- Não há porta específica em `adapters.py` para transição de status.
- `runtime.py` só compila `daily_summary` e `story_pull_request`.
- Só existe CLI dedicada para PR de story.
- Só existe grafo mutável para PR de story em `src/kwikledgers_langgraph/graphs/`.
- Os adapters legados atuais só cobrem daily summary, tracking, notificação, identidade e PR de story.

## Ordem sugerida de execução

1. Associação de branch da story.
2. Transição de status da story.
3. Extração do seam compartilhado de fluxos mutáveis.
4. Cobertura de erro dos adapters e grafos mutáveis.
5. Observabilidade externa e documentação operacional final.