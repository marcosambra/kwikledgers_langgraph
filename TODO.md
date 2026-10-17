# TODO

Backlog operacional do `kwikledgers_langgraph/`.

## Estado Atual

- `fastmcp` é o único backend; o backend `legacy` foi removido.
- As 30 capacidades originais são nativas no FastMCP. O catálogo LangGraph
  separa 12 leituras, duas prévias, nove ações atômicas aprovadas e sete rotas de lifecycle.
- Imports neutros foram adicionados pelo fluxo principal.
- A criação governada de `UserStory`, `Task` e `Bug`, com 15 campos nativos
  e restrições fornecidas pelo host, passou nos testes locais com emissor real.
- O fechamento diário e o hook são nativos e somente de prévia, sem escrita Azure.
- O contrato local dispensa configuração manual de assinatura e recibo externo:
  prévia separada, revisão humana e retomada explícita com `--approval-request-id`.
  `ExplicitLocalApproval.confirm`/`verify` preserva ação, prévia/payload, run e request.
- A revisão nativa continua obrigatória; `unknown_outcome` exige reconciliação,
  não retry automático. Aprovação local não comprova identidade externa.
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
- [x] Estados, journals e checkpoints sem chave obrigatória; assinatura opcional. Estados antigos assinados exigem a chave original.
- [x] Aprovação local explícita posterior à prévia; recibos antigos são rejeitados, mesmo com chaves configuradas. A extensão retoma pelo request atual.
- [x] Tokens locais `.local-v1` persistidos em ledger compartilhado, com TTL máximo de cinco minutos e anti-replay; não aceitos por HTTP. Não há emissor HMAC alternativo.
- [x] Implementação/commit local contínuos dentro do plano autorizado, vinculados à árvore testada; horas e fechamento mantêm gates próprios.
- [x] Prévia isolada de PR/fechamento não dispensa o lifecycle nem suas evidências; migração explicitada no README e no catálogo.
- [x] Tasks criadas diretamente ou como filhas de débito exigem URL, responsável e confirmação no snapshot persistido, com reconciliação pós-escrita.
- [x] Bootstrap e workflows de especialistas preparados, sem instalações nem pipelines executados.
- [x] Studio local com portas mockadas e resumo mínimo de observabilidade para tracking.

A fronteira de confiança é a conta e as permissões locais sobre os artefatos.
Não há proteção contra processos da mesma conta com escrita no ledger.

## Gates Técnicos e de Validação

- [x] Executar e registrar a suíte completa do LangGraph de 2026-10-09 (evidência histórica, não validação final do novo contrato).
- [x] Concluir integração local da criação governada com recibo e emissor reais sob fixtures na rodada de 2026-10-09.
- [ ] Validar restrições reais do host/processo para a criação governada.
- [x] Rejeitar recibos obsoletos e exigir nova prévia/retomada local explícita, sem compatibilidade de aprovação assinada.
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