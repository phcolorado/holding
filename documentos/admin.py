from django import forms
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from simple_history.admin import SimpleHistoryAdmin
from .models import Documento, DocumentoObrigatorio


class ArquivoSemLinkPublicoWidget(forms.ClearableFileInput):
    """
    Não existe rota pública para /media/ (item 7) — o widget padrão do
    Django (ClearableFileInput) renderia "Currently: <a href={arquivo.url}>"
    apontando direto para essa rota inexistente. Forçar is_initial=False
    remove esse link e o checkbox "Limpar" do template padrão, deixando só
    o campo de upload — trocar o arquivo continua funcionando normalmente
    (FileField.clean() preserva o arquivo atual quando nada é enviado).
    O arquivo atual é visto/baixado pelo campo somente-leitura arquivo_link,
    que usa a rota autenticada e permissionada documento_download.
    """
    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        context['widget']['is_initial'] = False
        return context


def _arquivo_link(documento):
    if not documento or not documento.pk or not documento.arquivo:
        return '—'
    url = reverse('documento_download', args=[documento.pk])
    nome = documento.arquivo.name.rsplit('/', 1)[-1]
    return format_html('<a href="{}">{}</a>', url, nome)


@admin.register(Documento)
class DocumentoAdmin(SimpleHistoryAdmin):
    list_display = ('titulo', 'tipo', 'imovel', 'data_documento', 'data_validade', 'enviado_contabilidade')
    list_filter = ('tipo', 'enviado_contabilidade', 'imovel')
    search_fields = ('titulo', 'imovel__nome', 'pessoa__nome', 'observacoes')
    date_hierarchy = 'data_documento'
    readonly_fields = ('arquivo_link', 'criado_em', 'atualizado_em')
    ordering = ('-criado_em',)
    fieldsets = (
        ('Identificação', {
            'fields': ('titulo', 'tipo', 'arquivo_link', 'arquivo')
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

    @admin.display(description='Arquivo atual')
    def arquivo_link(self, obj):
        return _arquivo_link(obj)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == 'arquivo':
            formfield.widget = ArquivoSemLinkPublicoWidget()
        return formfield


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
