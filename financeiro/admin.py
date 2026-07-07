from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from .models import ReceitaAluguel, ReceitaAluguelItem, Despesa, FechamentoMensal


class ReceitaAluguelItemInline(admin.TabularInline):
    model = ReceitaAluguelItem
    extra = 0
    fields = ('tipo', 'descricao', 'valor')


@admin.register(ReceitaAluguel)
class ReceitaAluguelAdmin(SimpleHistoryAdmin):
    list_display = (
        'imovel', 'competencia_mes', 'competencia_ano',
        'valor_previsto', 'valor_recebido', 'data_vencimento', 'status',
    )
    list_filter = ('status', 'competencia_ano', 'competencia_mes', 'imovel')
    search_fields = ('imovel__nome', 'contrato__locatario__nome')
    date_hierarchy = 'data_vencimento'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-competencia_ano', '-competencia_mes')
    raw_id_fields = ('contrato',)
    inlines = [ReceitaAluguelItemInline]

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + ('imovel',)

    fieldsets = (
        ('Referência', {
            'description': 'O imóvel é preenchido automaticamente pelo contrato selecionado ao salvar.',
            'fields': ('contrato', 'imovel', 'competencia_mes', 'competencia_ano')
        }),
        ('Vencimento e Valores', {
            'fields': ('data_vencimento', 'valor_previsto', 'multa', 'juros', 'desconto')
        }),
        ('Recebimento', {
            'fields': ('valor_recebido', 'data_recebimento', 'status')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )


@admin.register(Despesa)
class DespesaAdmin(SimpleHistoryAdmin):
    list_display = (
        'descricao', 'imovel', 'categoria', 'valor', 'data_vencimento',
        'status', 'origem_automatica',
    )
    list_filter = ('status', 'categoria', 'imovel', 'competencia_ano', 'origem_automatica')
    search_fields = ('descricao', 'imovel__nome', 'fornecedor__nome')
    date_hierarchy = 'data_vencimento'
    readonly_fields = ('criado_em', 'atualizado_em', 'origem_automatica')
    ordering = ('-data_vencimento',)
    raw_id_fields = ('contrato', 'receita')
    fieldsets = (
        ('Identificação', {
            'fields': ('descricao', 'categoria', 'imovel', 'fornecedor')
        }),
        ('Vínculos', {
            'fields': ('contrato', 'receita', 'origem_automatica'),
            'description': 'Preenchidos automaticamente quando a despesa é gerada pelo sistema (ex.: taxa de administração).',
        }),
        ('Competência e Vencimento', {
            'fields': ('competencia_mes', 'competencia_ano', 'data_vencimento')
        }),
        ('Pagamento', {
            'fields': ('valor', 'data_pagamento', 'status')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )


@admin.register(FechamentoMensal)
class FechamentoMensalAdmin(SimpleHistoryAdmin):
    list_display = ('mes', 'ano', 'data_fechamento', 'enviado_contabilidade', 'data_envio_contabilidade')
    list_filter = ('enviado_contabilidade', 'ano')
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-ano', '-mes')
