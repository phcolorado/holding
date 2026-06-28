from django.contrib import admin
from .models import ReceitaAluguel, Despesa, FechamentoMensal


@admin.register(ReceitaAluguel)
class ReceitaAluguelAdmin(admin.ModelAdmin):
    list_display = (
        'imovel', 'competencia_mes', 'competencia_ano',
        'valor_previsto', 'valor_recebido', 'data_vencimento', 'status',
    )
    list_filter = ('status', 'competencia_ano', 'competencia_mes', 'imovel')
    search_fields = ('imovel__nome', 'contrato__locatario__nome')
    date_hierarchy = 'data_vencimento'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-competencia_ano', '-competencia_mes')
    raw_id_fields = ('contrato', 'imovel')

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return self.readonly_fields + ('imovel',)
        return self.readonly_fields

    fieldsets = (
        ('Referência', {
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
class DespesaAdmin(admin.ModelAdmin):
    list_display = ('descricao', 'imovel', 'categoria', 'valor', 'data_vencimento', 'status')
    list_filter = ('status', 'categoria', 'imovel', 'competencia_ano')
    search_fields = ('descricao', 'imovel__nome', 'fornecedor__nome')
    date_hierarchy = 'data_vencimento'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-data_vencimento',)
    fieldsets = (
        ('Identificação', {
            'fields': ('descricao', 'categoria', 'imovel', 'fornecedor')
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
class FechamentoMensalAdmin(admin.ModelAdmin):
    list_display = ('mes', 'ano', 'data_fechamento', 'enviado_contabilidade', 'data_envio_contabilidade')
    list_filter = ('enviado_contabilidade', 'ano')
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-ano', '-mes')
