# Gestão Patrimonial Familiar

Sistema web interno para gestão de uma holding imobiliária familiar.  
Construído com Python + Django + Bootstrap 5 + SQLite.

---

## Funcionalidades

- **Dashboard** com indicadores em tempo real: receitas, inadimplência, contratos vencendo, reajustes, manutenções e documentos pendentes
- **Imóveis**: cadastro completo com matrícula, IPTU, valores e histórico
- **Pessoas**: locatários, fiadores, fornecedores, imobiliárias e familiares
- **Contratos**: vigência, reajuste por índice (IPCA/IGP-M/INPC), garantias e comissão
- **Receitas de aluguel**: controle por competência, status automático de atraso
- **Despesas**: categorias, competência e controle de pagamento
- **Documentos**: upload de arquivos organizados por imóvel e tipo, controle de envio à contabilidade
- **Manutenções**: registro de solicitações com status e valores
- **Geração automática de receitas**: gera `ReceitaAluguel` para todos os contratos ativos de um mês via interface web ou Admin; idempotente (sem duplicatas)
- **Inadimplência por vencimento**: regra baseada em `data_vencimento`, independente do campo de status — usada no dashboard, na listagem e nas exportações
- **Exportações** em CSV e XLSX (imóveis, contratos com filtro de status/imóvel, receitas, despesas, inadimplência, relatório mensal)
- **Django Admin** completo com filtros, buscas e ordenação para todos os modelos

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

O backup é salvo em `backups/<timestamp>/` com `db.sqlite3` e a pasta `media/`.  
A pasta `backups/` está no `.gitignore` e não é versionada.

---

## Geração automática de receitas

Para gerar receitas de aluguel do mês atual para todos os contratos ativos:

- **Via web**: acesse `/financeiro/gerar-receitas/`, selecione mês/ano e clique em "Gerar"
- **Via Admin**: na listagem de Contratos, selecione um ou mais e use a action "Gerar receitas esperadas"

A operação é idempotente — chamar múltiplas vezes não cria duplicatas.

---

## Testes

Para executar a suite de testes:

```
python manage.py test financeiro patrimonio
```

---

## Migração futura para PostgreSQL

Quando quiser migrar, altere `settings.py`:

```python
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'nome_do_banco',
        'USER': 'usuario',
        'PASSWORD': 'senha',
        'HOST': 'localhost',
        'PORT': '5432',
    }
}
```

E instale: `pip install psycopg2-binary`

---

## Variáveis importantes em produção

Em produção, substitua no `settings.py`:

```python
SECRET_KEY = 'gerar-chave-com-python-secrets'   # python -c "import secrets; print(secrets.token_hex(50))"
DEBUG = False
ALLOWED_HOSTS = ['seu-dominio.com']
```

---

## Dependências principais

| Pacote              | Versão  | Finalidade                      |
|---------------------|---------|---------------------------------|
| Django              | >=4.2   | Framework web                   |
| openpyxl            | >=3.1   | Exportação XLSX                 |
| Pillow              | >=10.0  | Processamento de imagens        |
| django-widget-tweaks| >=1.5   | Templates de formulários        |
