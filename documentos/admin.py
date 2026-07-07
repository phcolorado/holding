from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin
from .models import Documento, DocumentoObrigatorio


@admin.register(Documento)
class DocumentoAdmin(SimpleHistoryAdmin):
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


@admin.register(DocumentoObrigatorio)
class DocumentoObrigatorioAdmin(SimpleHistoryAdmin):
    list_display = ('imovel', 'tipo', 'descricao', 'obrigatorio', 'documento', 'pendente')
    list_filter = ('tipo', 'obrigatorio', 'imovel')
    search_fields = ('imovel__nome', 'descricao', 'observacoes')
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('imovel__nome', 'tipo')
    raw_id_fields = ('documento',)
    fieldsets = (
        ('Identificação', {
            'fields': ('imovel', 'tipo', 'descricao', 'obrigatorio')
        }),
        ('Documento Vinculado', {
            'fields': ('documento',)
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(boolean=True, description='Pendente')
    def pendente(self, obj):
        return obj.pendente
