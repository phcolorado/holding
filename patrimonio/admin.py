from django.contrib import admin
from .models import Imovel, Pessoa, Contrato, Manutencao


@admin.register(Imovel)
class ImovelAdmin(admin.ModelAdmin):
    list_display = ('nome', 'cidade', 'estado', 'status', 'proprietario', 'valor_estimado', 'criado_em')
    list_filter = ('status', 'estado', 'cidade')
    search_fields = ('nome', 'endereco', 'cidade', 'matricula', 'inscricao_iptu', 'proprietario')
    date_hierarchy = 'data_aquisicao'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('nome',)
    fieldsets = (
        ('Identificação', {
            'fields': ('nome', 'status', 'proprietario')
        }),
        ('Localização', {
            'fields': ('endereco', 'cidade', 'estado', 'cep')
        }),
        ('Registro', {
            'fields': ('matricula', 'cartorio', 'inscricao_iptu')
        }),
        ('Valores', {
            'fields': ('data_aquisicao', 'valor_aquisicao', 'valor_estimado')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )


@admin.register(Pessoa)
class PessoaAdmin(admin.ModelAdmin):
    list_display = ('nome', 'tipo', 'cpf_cnpj', 'email', 'telefone', 'criado_em')
    list_filter = ('tipo',)
    search_fields = ('nome', 'cpf_cnpj', 'email', 'telefone')
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('nome',)
    fieldsets = (
        ('Dados Principais', {
            'fields': ('nome', 'tipo', 'cpf_cnpj')
        }),
        ('Contato', {
            'fields': ('email', 'telefone', 'endereco')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )


@admin.register(Contrato)
class ContratoAdmin(admin.ModelAdmin):
    list_display = ('imovel', 'locatario', 'data_inicio', 'data_fim', 'valor_aluguel', 'status', 'indice_reajuste')
    list_filter = ('status', 'indice_reajuste', 'tipo_garantia')
    search_fields = ('imovel__nome', 'locatario__nome', 'fiador__nome')
    date_hierarchy = 'data_inicio'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-data_inicio',)
    raw_id_fields = ('imovel', 'locatario', 'fiador', 'imobiliaria')
    fieldsets = (
        ('Partes', {
            'fields': ('imovel', 'locatario', 'fiador', 'imobiliaria')
        }),
        ('Vigência e Valores', {
            'fields': ('data_inicio', 'data_fim', 'valor_aluguel', 'dia_vencimento', 'status')
        }),
        ('Reajuste e Garantia', {
            'fields': ('indice_reajuste', 'data_proximo_reajuste', 'tipo_garantia', 'comissao_imobiliaria_percentual')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )


@admin.register(Manutencao)
class ManutencaoAdmin(admin.ModelAdmin):
    list_display = ('descricao', 'imovel', 'categoria', 'fornecedor', 'status', 'data_solicitacao', 'valor_final')
    list_filter = ('status', 'categoria', 'imovel')
    search_fields = ('descricao', 'imovel__nome', 'fornecedor__nome')
    date_hierarchy = 'data_solicitacao'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-data_solicitacao',)
    fieldsets = (
        ('Identificação', {
            'fields': ('imovel', 'descricao', 'categoria', 'status')
        }),
        ('Fornecedor', {
            'fields': ('fornecedor',)
        }),
        ('Datas e Valores', {
            'fields': ('data_solicitacao', 'data_conclusao', 'valor_estimado', 'valor_final')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )
