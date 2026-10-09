# TODO

Backlog operacional do `kwikledgers_langgraph/`.

## Estado Atual

- `fastmcp` é o backend padrão; `legacy` é rollback explícito temporário.
- As 30 capacidades originais são nativas no FastMCP. O catálogo LangGraph
  separa 12 leituras, duas prévias, nove ações atômicas aprovadas e sete rotas de lifecycle.
- Imports neutros foram adicionados pelo fluxo principal.
- A criação governada de `UserStory`, `Task` e `Bug`, com 15 campos nativos
  e restrições fornecidas pelo host, está em integração.
- A desativação do legado não foi concluída.

## Evidências Anteriores

As contagens abaixo foram informadas pelo fluxo principal e não foram
reexecutadas nesta edição documental. O registro antigo de 57 testes não
descreve a suíte atual.

| Verificação | Última evidência informada |
|---|---|
| FastMCP | 119 testes aprovados anteriormente. |
| LangGraph completo | 348 testes, com três falhas de expectativa de outcome; execução final após ajustes pendente. |
| Lifecycle | 81 testes locais aprovados após ajustes. |
| Atômico | 55 testes locais aprovados após ajustes. |
| Locks | `poetry check --lock` aprovado em ambos os projetos após regeneração do lock LangGraph. |

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

- [ ] Executar e registrar a suíte completa final do LangGraph; não declarar verde apenas pelos testes focados.
- [ ] Concluir integração da criação governada e validar restrições reais do host/processo.
- [ ] Verificar aquisição externa de recibo assinado real pelo host, vínculo à prévia, expiração e rejeição de replay.
- [ ] Executar smoke Azure autenticado de leitura; identidade local não comprova autenticação Azure.
- [ ] Executar calendário Windows real no host suportado.
- [ ] Executar instalações de especialistas preparadas e registrar resultados.
- [ ] Executar pipelines preparados e registrar resultados.

Não houve escrita Azure real nem commit neste recorte. Qualquer mutação
externa exige prévia e aprovação específica, mesmo durante validação.

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

Nesta atualização, complemente somente o [histórico existente de 2026-09-30](../docs/history/kwikledgers-alteration-2026-09-30.md).
A decisão da data operacional atual pertence ao fluxo principal, sem criar
outro arquivo de histórico nesta edição.

Consulte [README.md](README.md) e o [guia de validação e desativação](../docs/validation/fastmcp-langgraph-legacy-sunset.md)
para comandos, diagrama e checklist. Não há percentual estimado nem
declaração de conclusão operacional.