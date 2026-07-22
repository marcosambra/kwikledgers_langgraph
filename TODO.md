# TODO

Backlog operacional do `kwikledgers_langgraph/`.

## Como usar este backlog

- Mantenha os slices pequenos, validados e focados.
- Ao concluir um item, atualize testes e documentação quando o comportamento mudar.
- Enquanto este repositório ainda estiver dentro do monorepo, registre a mudança em `../docs/history/kwikledgers-alteration-YYYY-MM-DD.md`.

## Em andamento

- Nenhum slice aberto no momento.

## Concluído

- [x] Criar um seam real de resolução de identidade do usuário para o runtime novo.
- [x] Fazer o CI instalar as dependências reais do projeto e validar a superfície que depende de `langgraph`.
- [x] Endurecer o contrato de aprovação para mutações reais, separando o pedido emitido pelo runtime da confirmação humana e reduzindo a dependência em `pending_approval` vindo pronto no estado inicial.
- [x] Cobrir a superfície CLI do fluxo mutável de PR da story com testes focados.
- [x] Cobrir o grafo `story_pull_request` com testes comportamentais: payload inválido, preview sem aprovação, parada limpa e caminho com aprovação.

## Próximos slices prioritários

### P1

- [ ] Adicionar o fluxo preview-first de associação de branch da story.
- [ ] Adicionar o fluxo preview-first de transição de status da story.

### P2

- [ ] Integrar observabilidade do runtime com um destino externo do stack da KwikLedgers, como `kl_observatory`, OpenTelemetry ou LangSmith.
- [ ] Normalizar melhor a política de payload mínimo em preview, tracking e timeline.
- [ ] Expandir testes dos adapters legados com cenários de erro e payload inválido.

## Critérios de pronto por slice

- O comportamento novo ou ajustado está coberto por teste focado.
- O caminho de validação mais barato foi executado ou, se indisponível, documentado.
- O `README.md` foi atualizado quando a superfície pública do repositório mudou.
- O histórico em `../docs/history/` foi atualizado.

## Ordem sugerida de execução

1. Testes do fluxo mutável atual.
2. Hardening da aprovação real.
3. CI com dependências reais e checagem dos grafos.
4. Novos fluxos mutáveis.
5. Observabilidade externa e seams operacionais restantes.