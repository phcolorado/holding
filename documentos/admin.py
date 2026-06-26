from django.contrib import admin
from .models import Documento


@admin.register(Documento)
class DocumentoAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'tipo', 'imovel', 'data_documento', 'data_validade', 'enviado_contabilidade')
    list_filter = ('tipo', 'enviado_contabilidade', 'imovel')
    search_fields = ('titulo', 'imovel__nome', 'pessoa__nome', 'observacoes')
    date_hierarchy = 'data_documento'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-criado_em',)
    fieldsets = (
        ('Identificação', {
            'fields': ('titulo', 'tipo', 'arquivo')
        }),
        ('Vínculos', {
            'fields': ('imovel', 'contrato', 'pessoa', 'receita', 'despesa')
        }),
        ('Datas', {
            'fields': ('data_documento', 'data_validade')
        }),
        ('Contabilidade', {
            'fields': ('enviado_contabilidade', 'data_envio_contabilidade')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )
