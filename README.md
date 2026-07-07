# Gestão Patrimonial Familiar

Sistema web interno para gestão de uma holding imobiliária familiar.  
Construído com Python + Django + Bootstrap 5 + SQLite.

---

## Funcionalidades

- **Dashboard** com indicadores em tempo real: receitas, inadimplência, contratos vencendo, reajustes (próximos e pendentes), manutenções, documentos pendentes, **taxa de ocupação**, **documentos com validade vencendo** e **gráfico de fluxo de caixa dos últimos 12 meses**
- **Imóveis**: cadastro completo com matrícula, IPTU, valores, histórico, tipo/uso e hierarquia de prédio/unidades
- **Pessoas**: cadastro único por pessoa, com categoria preferencial apenas para busca — o mesmo cadastro pode assumir papéis diferentes (locatário, fiador, imobiliária etc.) em contratos diferentes
- **Contratos**: vigência (determinada ou por prazo indeterminado), múltiplos locatários e fiadores, reajuste por índice (IPCA/IGP-M/INPC), garantias, taxa de administração, multa e juros por atraso
- **Encargos do contrato**: aluguel, IPTU, condomínio/taxa de manutenção, seguro e outros, cada um com periodicidade e período de cobrança próprios
- **Reajustes de contrato**: histórico de reajustes aplicados, com atualização controlada do valor vigente
- **Receitas de aluguel**: controle por competência, status automático de atraso, composição em itens (por tipo de encargo)
- **Despesas**: categorias, competência, controle de pagamento e vínculo com contrato/receita (inclui despesas automáticas de taxa de administração)
- **Documentos**: upload de arquivos organizados por imóvel e tipo, controle de envio à contabilidade, anexo direto no cadastro do contrato
- **Manutenções**: registro de solicitações com status e valores
- **Geração automática de receitas**: gera `ReceitaAluguel` (com itens e despesa de administração, se configurada) para todos os contratos ativos de um mês via interface web ou Admin; idempotente (sem duplicatas); respeita prazo indeterminado e encerramento real
- **Baixa de aluguéis**: tela de conferência mensal com marcação rápida de recebimento, composição da receita, sugestão de multa/juros e edição inline de cada receita
- **Inadimplência por vencimento**: regra baseada em `data_vencimento`, independente do campo de status — a aba "Inadimplência Aberta" do relatório para contabilidade lista **todas** as receitas vencidas e não quitadas, sem filtro de mês de competência
- **Checklist Mensal**: tela `/financeiro/checklist-mensal/` com 10 etapas de fechamento (incluindo reajustes pendentes e validade de documentos) e ação de marcar envio à contabilidade via `FechamentoMensal`
- **Alertas de validade de documentos**: documentos com `data_validade` vencida ou vencendo em 30 dias aparecem no Dashboard, no Checklist Mensal e no filtro "Vencendo/vencidos" da lista de documentos
- **Sugestão de reajuste pelo Banco Central**: action no Admin de Contratos que consulta o acumulado de 12 meses do IPCA/IGP-M/INPC na API SGS do BCB e cria um reajuste pendente para revisão
- **Indicadores por imóvel**: yield bruto/líquido anual, receita/despesa/resultado de 12 meses e taxa de ocupação na tela de detalhe do imóvel
- **Auditoria de alterações** (django-simple-history): histórico de quem alterou o quê em imóveis, pessoas, contratos, encargos, reajustes, receitas, despesas e documentos — visível no botão "Histórico" de cada registro no Admin
- **Relatório para contabilidade** em XLSX (6 abas: resumo, receitas, despesas, inadimplência aberta, documentos pendentes, resultado por imóvel)
- **Documentos obrigatórios por imóvel**: controle de documentos esperados por imóvel com indicador de pendência
- **Exportações** em CSV e XLSX (imóveis com tipo/uso, contratos com locatários/fiadores/imobiliária e filtro de status/imóvel/imobiliária, receitas, despesas, inadimplência, relatório mensal)
- **Django Admin** completo com filtros, buscas, ordenação e inlines (partes do contrato, encargos, reajustes, documentos) para todos os modelos

---

## Requisitos

- Python 3.10 ou superior
- Windows 10/11 (ou Linux/macOS)
- Conexão com internet para carregar Bootstrap via CDN (na primeira execução)

---

## Como executar no Windows

### 1. Clonar / baixar o projeto

```
git clone <url-do-repositorio>
cd holding
```

### 2. Criar o ambiente virtual

```
python -m venv venv
```

### 3. Ativar o ambiente virtual

```
venv\Scripts\activate
```

> No PowerShell, se aparecer erro de política de execução:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

### 4. Instalar as dependências

```
pip install -r requirements.txt
```

### 5. Criar o banco de dados e aplicar as migrations

```
python manage.py migrate
```

### 6. Criar o superusuário administrador

```
python manage.py createsuperuser
```

Ou use o comando de dados de demonstração que já cria o usuário `admin / admin123`:

```
python manage.py popular_banco
```

> **Atenção:** nunca use `admin123` em produção.

### 7. Iniciar o servidor local

```
python manage.py runserver
```

Acesse: [http://127.0.0.1:8000](http://127.0.0.1:8000)

---

## Estrutura de pastas

```
holding/
├── gestao_patrimonial/     # Configurações do projeto Django
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── core/                   # Dashboard e utilitários
│   └── management/commands/
│       ├── popular_banco.py    # Dados de demonstração
│       ├── criar_grupos.py     # Grupos e permissões básicos
│       └── backup_local.py     # Backup de banco e mídia
├── patrimonio/             # Imóveis, Pessoas, Contratos, Manutenções
├── financeiro/             # Receitas, Despesas, Fechamento Mensal, Exportações
├── documentos/             # Upload e gestão de documentos
├── templates/              # Templates HTML (Bootstrap 5)
├── media/                  # Uploads de arquivos (gerado automaticamente)
├── manage.py
└── requirements.txt
```

---

## Usuários e permissões

O sistema usa a autenticação padrão do Django. Você pode criar grupos no Admin:

| Grupo             | Permissão sugerida                          |
|-------------------|---------------------------------------------|
| administrador     | Acesso total                                |
| familiar_edicao   | Adicionar e editar registros                |
| familiar_leitura  | Somente visualização                        |

Para criar grupos automaticamente via comando:

```
python manage.py criar_grupos
```

Ou acesse manualmente: `/admin/` → Autenticação → Grupos.

---

## Upload de arquivos

Os documentos são salvos em `media/documentos/imovel_<ID>/<tipo>/`.  
Configure `MEDIA_ROOT` em `settings.py` para apontar para o local desejado.

---

## Backup local

Para criar um backup local do banco de dados e dos arquivos de mídia:

```
python manage.py backup_local
```

O backup é salvo como `backups/backup_<timestamp>.zip` contendo:
- `db.sqlite3` — banco de dados
- `media/` — arquivos de upload
- `manifest.txt` — metadados do backup

**Opções disponíveis:**

```
python manage.py backup_local --destino /caminho/personalizado
python manage.py backup_local --manter 10   # mantém apenas os 10 mais recentes
python manage.py backup_local --copia-extra "G:\Meu Drive\backups-holding"   # cópia p/ pasta sincronizada com a nuvem
```

A pasta `backups/` está no `.gitignore` e não é versionada.

### Agendando o backup (e a marcação de atrasados)

**Windows (Agendador de Tarefas):** crie uma tarefa diária executando:

```
C:\caminho\holding\venv\Scripts\python.exe C:\caminho\holding\manage.py backup_local --manter 30 --copia-extra "G:\Meu Drive\backups-holding"
```

**Linux/macOS (cron):** `crontab -e` e adicione:

```
# backup diário às 2h, mantendo 30 e copiando para pasta sincronizada com a nuvem
0 2 * * * /caminho/holding/venv/bin/python /caminho/holding/manage.py backup_local --manter 30 --copia-extra /caminho/nuvem/backups
# marca receitas/despesas vencidas como atrasadas todo dia às 6h
0 6 * * * /caminho/holding/venv/bin/python /caminho/holding/manage.py marcar_atrasados
```

> Aponte `--copia-extra` para uma pasta sincronizada (Google Drive, Dropbox, OneDrive) para ter backup fora da máquina sem nenhuma configuração adicional.

---

## Geração automática de receitas

Para gerar receitas de aluguel do mês atual para todos os contratos ativos:

- **Via web**: acesse `/financeiro/gerar-receitas/`, selecione mês/ano e clique em "Gerar"
- **Via Admin**: na listagem de Contratos, selecione um ou mais e use a action "Gerar receitas esperadas"
- **Na tela de baixa**: use o botão "Gerar Receitas Esperadas" presente na tela `/financeiro/baixa-receitas/`

A operação é idempotente — chamar múltiplas vezes não cria duplicatas.

---

## Fluxo Mensal Recomendado

1. **Gerar Receitas** — acesse `/financeiro/gerar-receitas/` e gere as receitas do mês (operação idempotente; considera encargos, prazo indeterminado e encerramento real).
2. **Conferir Recebimentos** — acesse `/financeiro/baixa-receitas/` e marque cada aluguel como recebido (filtrável por imóvel; sugestão de multa/juros para receitas atrasadas).
3. **Revisar Inadimplência** — verifique receitas vencidas no Dashboard ou no Checklist.
4. **Lançar e Pagar Despesas** — lance despesas no Admin e marque como pagas (inclui despesas automáticas de taxa de administração).
5. **Enviar Documentos à Contabilidade** — marque `enviado_contabilidade = True` nos documentos relevantes.
6. **Revisar Documentos Obrigatórios** — certifique-se de que todos os documentos obrigatórios dos imóveis estão vinculados.
7. **Revisar Reajustes Pendentes** — verifique contratos com `data_proximo_reajuste` vencida e registre o reajuste quando aplicável.
8. **Exportar Relatório Contábil** — baixe o XLSX pela tela de Relatórios ou diretamente do Checklist Mensal.
9. **Fechar o Mês** — acesse `/financeiro/checklist-mensal/` e clique em "Marcar como Enviado à Contabilidade".
10. **Backup** — execute `python manage.py backup_local` para gerar um ZIP de segurança.

> A tela de Checklist Mensal consolida todas as etapas acima com indicadores de status em tempo real.

---

## Checklist Mensal

Acesse `/financeiro/checklist-mensal/` (menu lateral: "Checklist Mensal") ou clique em **"Checklist Mensal"** no Dashboard.

A tela exibe 10 etapas com indicador visual (✓ verde / ! amarelo / ↓ azul para ações):

| Etapa | Critério de conclusão |
|---|---|
| Receitas geradas | Existe ao menos uma receita no mês |
| Recebimentos confirmados | Nenhuma receita pendente (exceto canceladas) |
| Inadimplência em dia | Zero receitas vencidas e não quitadas (histórico) |
| Reajustes em dia | Zero contratos ativos com `data_proximo_reajuste` vencida |
| Despesas pagas | Todas as despesas do mês estão pagas |
| Documentos enviados à contabilidade | Nenhum documento pendente para contabilidade |
| Documentos obrigatórios revisados | Todos os `DocumentoObrigatorio` com documento vinculado |
| Validade dos documentos em dia | Nenhum documento com `data_validade` vencida ou vencendo em 30 dias |
| Relatório contábil exportado | Etapa informativa com botão de download direto |
| Fechamento registrado e enviado | `FechamentoMensal` marcado como enviado |

Ao clicar em **"Marcar como Enviado à Contabilidade"**, o sistema cria ou atualiza o registro `FechamentoMensal` com `enviado_contabilidade = True` e a data de envio.

---

## Baixa de Aluguéis (conferência de recebimentos)

Acesse `/financeiro/baixa-receitas/` (menu lateral: "Baixa de Aluguéis") ou clique em **"Conferir Recebimentos"** no Dashboard.

A tela suporta filtro por **imóvel específico** — ao clicar em "Baixa de Aluguéis" na tela de detalhe de um imóvel, a tela já vem filtrada para aquele imóvel. Todas as ações POST (marcar recebida, editar, gerar receitas) preservam o filtro no redirect.

Diferença entre as duas operações:
| Operação | O que faz |
|---|---|
| **Gerar Receitas Esperadas** | Cria registros de `ReceitaAluguel` para contratos ativos de um mês. Não altera registros existentes. |
| **Baixa de Aluguéis** | Confere e atualiza os registros já criados — marca como recebido, registra valor e data. |

Na tela de baixa é possível:
- **Marcar como recebida** (botão verde ✓): preenche `valor_recebido = valor_previsto` e `data_recebimento = hoje` automaticamente.
- **Editar**: abre modal para alterar valor recebido, data, status e observações.

---

## Pessoas com Múltiplos Papéis

O campo `tipo` de `Pessoa` é apenas uma **categoria preferencial** usada para busca e organização — ele não restringe o uso da pessoa em contratos. A mesma pessoa pode ser locatária em um contrato e fiadora (ou representante, cônjuge, imobiliária) em outro.

Os papéis efetivos de cada pessoa em cada contrato são registrados em **Partes do Contrato** (model `ContratoParte`), com `papel` e `principal`. Os campos `locatario`, `fiador` e `imobiliaria` em `Contrato` são mantidos como **campos legados** por compatibilidade; quando não há partes cadastradas, o sistema usa automaticamente esses campos legados.

## Contratos com Múltiplos Locatários e Fiadores

Para cadastrar mais de um locatário, fiador ou representante em um contrato:

1. Acesse o Admin → Patrimônio → Contratos → abra o contrato desejado.
2. Na seção **"Partes do Contrato"** (inline), adicione uma linha por pessoa, escolhendo o papel (Locatário, Fiador, Representante, Cônjuge, Imobiliária, Outro).
3. Marque **"Principal"** na parte que deve aparecer em destaque, se desejar.

A listagem de contratos (`/patrimonio/contratos/`) e as exportações usam `locatarios_display`/`fiadores_display`, que juntam os nomes de todas as partes cadastradas (com fallback para os campos legados quando não há partes).

## Contratos por Prazo Indeterminado

Contratos prorrogados após o término do prazo original (comum em locações residenciais) podem ser marcados com **"Prazo Indeterminado"**:

- `data_fim` continua representando a **data de término original** do prazo determinado.
- Ao marcar `prazo_indeterminado = True`, o contrato deixa de ser considerado vencido enquanto estiver ativo, e a geração de receitas continua normalmente além da `data_fim` original.
- Se o contrato for efetivamente encerrado, preencha `data_encerramento_real` — essa data passa a limitar tanto a geração de receitas quanto a checagem de vencimento, independentemente de `prazo_indeterminado`.
- A validação de sobreposição de contratos ativos no mesmo imóvel considera contratos por prazo indeterminado como "abertos" até que tenham um encerramento real.

## Encargos do Contrato (IPTU, Condomínio, Seguro etc.)

Além do aluguel, um contrato pode ter outros encargos cadastrados em **Encargos do Contrato** (inline no Admin ou model `EncargoContrato`): IPTU, condomínio/taxa de manutenção, seguro, água/luz de área comum ou outros.

Cada encargo tem:
- **Periodicidade**: mensal, anual (cobrado apenas no mês de aniversário de `data_inicio_cobranca`) ou único (cobrado uma única vez, no mês de `data_inicio_cobranca`).
- **Início/Fim de cobrança**: use `data_inicio_cobranca` no futuro para configurar carência (ex.: isenção dos primeiros meses); use `data_fim_cobranca` para encerrar a cobrança de um encargo sem excluir o histórico.

Ao gerar a receita mensal, o `valor_previsto` passa a ser a **soma dos encargos ativos aplicáveis** naquele mês, e cada encargo gera um item em `ReceitaAluguelItem` (visível na tela de baixa e no Admin). Contratos sem nenhum encargo cadastrado continuam usando `valor_aluguel` diretamente, sem itens — nenhuma migração de dados é necessária para contratos já existentes (a migração automática já criou o encargo "Aluguel" a partir do valor vigente).

**Encargo de aluguel sempre garantido:** ao salvar um contrato pelo Admin, ou ao gerar receitas (via web, Admin ou action "Gerar receitas esperadas"), o sistema chama automaticamente `Contrato.garantir_encargo_aluguel()` — se o contrato ativo não tiver nenhum encargo de aluguel ativo, um é criado com o valor de `valor_aluguel`. Isso evita que um contrato com apenas IPTU/condomínio cadastrado gere uma receita sem o aluguel. Um encargo de aluguel já existente (automático ou cadastrado manualmente com valor diferente) nunca é sobrescrito. Há também a action "Garantir encargo de aluguel" na listagem de Contratos do Admin, para aplicar isso em lote a contratos já cadastrados.

No Admin, essa verificação roda em `save_related()` — ou seja, **depois** que os Encargos do Contrato cadastrados no mesmo formulário (inline) já foram salvos — para não duplicar um encargo de aluguel que o próprio usuário acabou de cadastrar manualmente.

## Taxa de Administração da Imobiliária

O campo `comissao_imobiliaria_percentual` do contrato ("Taxa de Administração Imobiliária (%)") gera, ao gerar a receita mensal, uma **despesa automática** (categoria "Comissão Imobiliária", `origem_automatica = True`) vinculada ao contrato e à receita do mês — o valor é sempre calculado sobre o encargo de aluguel (nunca sobre IPTU/condomínio). A receita continua representando o valor bruto devido pelo inquilino; a taxa nunca é abatida diretamente do aluguel.

## Reajustes de Contrato

Cada contrato tem uma seção **"Reajustes de Contrato"** (inline no Admin). Para registrar um reajuste:

1. Adicione uma linha com data do reajuste, índice, percentual (opcional), valor anterior e valor novo.
2. Marque **"Aplicado"** e salve.

Ao salvar um reajuste novo marcado como aplicado, o sistema automaticamente:
- Atualiza `Contrato.valor_aluguel` e o encargo de aluguel ativo para o valor novo.
- Avança `data_proximo_reajuste` em 12 meses (exceto para índice "Fixo").
- **Nunca** altera receitas já geradas — apenas receitas futuras (ainda não geradas) usarão o novo valor.

O Dashboard e o Checklist Mensal alertam quando há contratos ativos com `data_proximo_reajuste` vencida.

## Multa e Juros por Atraso

Os campos `multa_atraso_percentual`, `juros_mora_percentual_mes` e `dias_carencia_multa` do contrato definem as regras de atraso. Na tela de baixa de aluguéis, receitas atrasadas exibem uma **sugestão** de multa/juros calculada (`ReceitaAluguel.calcular_multa_juros`), com um link "Usar sugestão" no modal de edição — os valores nunca são aplicados automaticamente, apenas sugeridos.

## Tipo de Imóvel e Unidades Locáveis

Cada imóvel tem `tipo_imovel` (apartamento, casa, sala comercial, prédio, andar, etc.) e `uso` (residencial, comercial, misto). Um imóvel "guarda-chuva" (ex.: um prédio inteiro) pode ser cadastrado com `unidade_locavel = False`, e suas unidades (andares, salas) cadastradas como imóveis separados com `imovel_pai` apontando para o prédio. A tela de detalhe do imóvel exibe as unidades filhas na aba "Unidades".

## Documentos do Contrato

Documentos relacionados diretamente a um contrato (contrato assinado, aditivos, laudo de vistoria) podem ser anexados na seção **"Documentos do Contrato"** (inline no Admin, dentro do cadastro do contrato) — o campo `imovel` do documento é preenchido automaticamente com o imóvel do contrato.

---

## Limitações Conhecidas

- **Receitas já geradas não são recalculadas automaticamente.** Se você adicionar, remover ou alterar encargos (`EncargoContrato`) ou a taxa de administração de um contrato **depois** que a receita do mês já foi gerada, essa receita e seus itens **não são atualizados retroativamente** — o novo valor só vale a partir da próxima geração (próximo mês, ou meses futuros ainda não gerados). Isso é intencional, para preservar lançamentos já revisados/conferidos na baixa de aluguéis. Se for necessário corrigir uma receita já gerada, edite-a manualmente (ou seus itens) pelo Admin; não há, nesta fase, uma action de recálculo em massa.
- A mesma lógica vale para reajustes: `ReajusteContrato.aplicar()` nunca altera receitas já existentes, mesmo as futuras já geradas para o mês seguinte — apenas receitas geradas **depois** do reajuste usam o novo valor.

---

## Relatório para Contabilidade

Acesse **Relatórios / Exportar** e clique em "Baixar Relatório Contabilidade".

O XLSX gerado contém 6 abas:
1. **Resumo** — totais, resultado líquido e indicadores do mês
2. **Receitas** — lista completa com coluna "Atrasada" calculada
3. **Despesas** — lista completa com coluna "Atrasada" calculada
4. **Inadimplência Aberta** — todas as receitas vencidas e não quitadas em qualquer mês (regra de vencimento, independente do status); escopo histórico, não restrito ao mês selecionado
5. **Docs. Pendentes** — documentos com `enviado_contabilidade = False`
6. **Por Imóvel** — resultado financeiro por imóvel

---

## Controle de Documentos Obrigatórios

Cada imóvel pode ter uma lista de documentos obrigatórios esperados (matrícula, IPTU, contrato, laudo, etc.).

Para gerenciar: acesse o Admin → Documentos → Documentos Obrigatórios.  
Na tela de detalhe de cada imóvel há uma aba "Docs. Obrigatórios" com o status de cada um.

O Dashboard exibe um contador de documentos obrigatórios pendentes (obrigatório sem documento vinculado).

---

## Testes

Para executar a suite de testes:

```
python manage.py test financeiro patrimonio documentos
```

---

## Migrations de Refatoração (contratos reais)

Esta versão introduziu `ContratoParte`, `EncargoContrato`, `ReajusteContrato`, `ReceitaAluguelItem`, novos campos em `Contrato`/`Imovel`/`Despesa`, e duas **data migrations** que preenchem automaticamente dados a partir de contratos já cadastrados:

- `patrimonio.0004_migrar_partes_contrato` — cria `ContratoParte` a partir dos campos legados `locatario`/`fiador`/`imobiliaria`.
- `patrimonio.0005_migrar_encargo_aluguel` — cria o encargo "Aluguel" a partir de `valor_aluguel` para cada contrato existente.

Nenhum dado existente é apagado ou sobrescrito — os campos legados continuam funcionando normalmente. Para aplicar:

```
python manage.py migrate
```

---

## Sugestão automática de reajuste (Banco Central)

No Admin → Contratos, selecione contratos e use a action **"Sugerir reajuste pelo índice acumulado 12m (Banco Central)"**. O sistema:

1. Consulta a API pública SGS do Banco Central (IPCA = série 433, IGP-M = 189, INPC = 188) — resultado fica em cache por 12h.
2. Calcula o acumulado de 12 meses por juros compostos.
3. Cria um `ReajusteContrato` **pendente** (`aplicado=False`) com percentual, valor anterior e valor novo calculados.
4. Você revisa e marca "Aplicado" para efetivar (o fluxo normal de reajuste).

Contratos com índice "Fixo"/"Outro", inativos ou que já têm reajuste pendente são ignorados (sem duplicação). Requer conexão com a internet.

---

## Configuração por variáveis de ambiente

O `settings.py` lê tudo do ambiente, com padrões de desenvolvimento:

| Variável | Padrão | Uso |
|---|---|---|
| `DJANGO_SECRET_KEY` | chave insegura de dev | **Obrigatória em produção** — gere com `python -c "import secrets; print(secrets.token_hex(50))"` |
| `DJANGO_DEBUG` | `1` | Use `0` em produção (ativa cookies seguros automaticamente) |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Lista separada por vírgula |
| `DB_ENGINE` | sqlite3 | `django.db.backends.postgresql` para Postgres |
| `DB_NAME` / `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` | — | Credenciais do Postgres |

### Migração para PostgreSQL

```
pip install psycopg2-binary
export DB_ENGINE=django.db.backends.postgresql
export DB_NAME=holding DB_USER=holding DB_PASSWORD=senha DB_HOST=localhost DB_PORT=5432
python manage.py migrate
```

Para migrar os dados do SQLite: `python manage.py dumpdata --natural-foreign --natural-primary -e contenttypes -e auth.permission -o dados.json` no banco antigo e `python manage.py loaddata dados.json` no novo.

---

## Dependências principais

| Pacote                | Versão  | Finalidade                      |
|-----------------------|---------|---------------------------------|
| Django                | >=4.2   | Framework web                   |
| openpyxl              | >=3.1   | Exportação XLSX                 |
| Pillow                | >=10.0  | Processamento de imagens        |
| django-widget-tweaks  | >=1.5   | Templates de formulários        |
| django-simple-history | >=3.7   | Auditoria de alterações         |
| psycopg2-binary       | >=2.9   | Driver Postgres (opcional)      |
