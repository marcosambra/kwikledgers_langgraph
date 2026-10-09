# TODO

Backlog operacional do `kwikledgers_langgraph/`.

## Estado Atual

- `fastmcp` é o backend padrão; `legacy` é rollback explícito temporário.
- As 30 capacidades originais são nativas no FastMCP. O catálogo LangGraph
  separa 12 leituras, duas prévias, nove ações atômicas aprovadas e sete rotas de lifecycle.
- Imports neutros foram adicionados pelo fluxo principal.
- A criação governada de `UserStory`, `Task` e `Bug`, com 15 campos nativos
  e restrições fornecidas pelo host, passou nos testes locais com emissor real.
- O fechamento diário e o hook são nativos e somente de prévia, sem escrita Azure.
- A desativação do legado não foi concluída.

## Evidências de 2026-10-09

Resultados executados na árvore de trabalho desta implementação.
Comandos e limites estão na [evidência de validação](docs/native-runtime-validation-2026-10-09.md).

| Verificação | Resultado executado |
|---|---|
| FastMCP completo | 139 testes aprovados. |
| LangGraph completo | 408 testes aprovados. |
| Lifecycle operacional e adapters | 52 testes aprovados. |
| Catálogo atômico e preparação de débito | 67 testes aprovados. |
| Criação e emissor real | 39 testes aprovados. |
| Instalação LangGraph e CLIs | `poetry install` e cinco entrypoints `--help` aprovados. |
| Locks | `poetry check --lock` aprovado em ambos os projetos. |

## Implementado Localmente

- [x] Fluxos compiláveis de resumo, PR, branch e status, com contratos compartilhados preview-first.
- [x] Resolução nativa de identidade; leitura stdio anterior de Marcos Pereira bem-sucedida.
- [x] Lifecycle e catálogo atômico com gates, sem bypass genérico das sete rotas de processo.
- [x] Estados, journals e checkpoints assinados.
- [x] Recibos com TTL máximo de 600 segundos e nonce vinculado à prévia exata pelo fluxo principal.
- [x] Tokens HMAC de execução de cinco minutos e ledger estável.
- [x] Bootstrap e workflows de especialistas preparados, sem instalações nem pipelines executados.
- [x] Studio local com portas mockadas e resumo mínimo de observabilidade para tracking.

## Gates Técnicos e de Validação

- [x] Executar e registrar a suíte completa final do LangGraph.
- [x] Concluir integração local da criação governada com recibo e emissor reais sob fixtures.
- [ ] Validar restrições reais do host/processo para a criação governada.
- [ ] Verificar aquisição externa de recibo assinado real pelo host, vínculo à prévia, expiração e rejeição de replay.
- [ ] Executar smoke Azure autenticado de leitura; identidade local não comprova autenticação Azure.
- [ ] Executar calendário Windows real no host suportado.
- [ ] Executar instalações de especialistas preparadas e registrar resultados.
- [ ] Executar pipelines preparados e registrar resultados.

Não houve escrita Azure real nem instalação do cron. Commits locais autorizados
e testes com fixtures não autorizam mutações externas.

## Gates Operacionais de Retirada

- [ ] Definir janela limitada de observação, responsáveis e rollback.
- [ ] Identificar consumidores ainda legados, incluindo agente raiz e ferramentas, scheduler e configurações de projetos.
- [ ] Obter aprovação explícita do usuário antes de alterar qualquer superfície protegida.
- [ ] Desativar apenas consumidores autorizados e registrar a validação pós-retirada.

A troca de backend não equivale ao desligamento desses consumidores.
Pendências operacionais são separadas das implementações técnicas locais.

## Critérios de Conclusão

Cada gate exige comando ou ação, resultado, revisão e data registrados.
Testes focados não substituem a suíte final; mocks não substituem smokes
externos; workflows preparados não equivalem a execução de CI.

O registro desta implementação está no [histórico de 2026-10-09](../docs/history/kwikledgers-alteration-2026-10-09.md).

Consulte [README.md](README.md) e o [guia de validação e desativação](../docs/validation/fastmcp-langgraph-legacy-sunset.md)
para comandos, diagrama e checklist. Não há percentual estimado nem
declaração de conclusão operacional.