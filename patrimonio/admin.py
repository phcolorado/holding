from decimal import Decimal

from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.utils import timezone
from simple_history.admin import SimpleHistoryAdmin

from .forms import ReajusteContratoInlineForm
from .models import (
    Imovel, Pessoa, Contrato, Manutencao,
    ContratoParte, EncargoContrato, ReajusteContrato,
)
from documentos.models import Documento


@admin.register(Imovel)
class ImovelAdmin(SimpleHistoryAdmin):
    list_display = (
        'nome', 'cidade', 'estado', 'tipo_imovel', 'uso', 'status',
        'imovel_pai', 'proprietario', 'valor_estimado', 'criado_em',
    )
    list_filter = ('status', 'tipo_imovel', 'uso', 'estado', 'cidade')
    search_fields = ('nome', 'endereco', 'cidade', 'matricula', 'inscricao_iptu', 'proprietario')
    date_hierarchy = 'data_aquisicao'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('nome',)
    raw_id_fields = ('imovel_pai',)
    fieldsets = (
        ('Identificação', {
            'fields': ('nome', 'status', 'proprietario', 'tipo_imovel', 'uso')
        }),
        ('Prédio / Unidades', {
            'fields': ('imovel_pai', 'unidade_locavel'),
            'description': 'Preencha "Imóvel Pai" quando este imóvel for uma unidade de um prédio maior.',
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
class PessoaAdmin(SimpleHistoryAdmin):
    list_display = ('nome', 'tipo', 'cpf_cnpj', 'email', 'telefone', 'criado_em')
    list_filter = ('tipo',)
    search_fields = ('nome', 'cpf_cnpj', 'email', 'telefone')
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('nome',)
    fieldsets = (
        ('Dados Principais', {
            'fields': ('nome', 'tipo', 'cpf_cnpj'),
            'description': (
                'A categoria é apenas uma referência de busca — a mesma pessoa pode '
                'assumir papéis diferentes (locatário, fiador, imobiliária etc.) em '
                'contratos diferentes, cadastrados em "Partes do Contrato".'
            ),
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


class ContratoParteInline(admin.TabularInline):
    model = ContratoParte
    extra = 1
    fields = ('pessoa', 'papel', 'principal', 'observacoes')
    raw_id_fields = ('pessoa',)


class EncargoContratoInline(admin.TabularInline):
    model = EncargoContrato
    extra = 0
    fields = ('tipo', 'descricao', 'valor', 'periodicidade', 'data_inicio_cobranca', 'data_fim_cobranca', 'ativo')


class ReajusteContratoInlineFormSet(BaseInlineFormSet):
    """
    Desabilita, por linha, os campos financeiros e a exclusão de reajustes
    já aplicados — disabled=True no form é reforço no SERVIDOR (dados
    submetidos para um campo disabled são ignorados em favor do initial),
    não apenas uma dica visual no navegador. Reajustes pendentes continuam
    totalmente editáveis, inclusive o comando "Aplicar agora".
    """
    def add_fields(self, form, index):
        super().add_fields(form, index)
        instance = getattr(form, 'instance', None)
        if instance and instance.pk and instance.aplicado_em is not None:
            if 'DELETE' in form.fields:
                form.fields['DELETE'].disabled = True
            for campo in ReajusteContrato.CAMPOS_PROTEGIDOS_APOS_APLICACAO:
                nome = campo[:-3] if campo.endswith('_id') else campo
                if nome in form.fields:
                    form.fields[nome].disabled = True
            # Já aplicado: "Aplicar agora" não é uma opção editável — o
            # próprio save_formset() também ignora este campo para linhas
            # já aplicadas (defesa em profundidade, não depende só do
            # disabled do form).
            if 'aplicar_agora' in form.fields:
                form.fields['aplicar_agora'].disabled = True
                form.fields['aplicar_agora'].initial = False


class ReajusteContratoInline(admin.TabularInline):
    model = ReajusteContrato
    form = ReajusteContratoInlineForm
    formset = ReajusteContratoInlineFormSet
    extra = 0
    fields = (
        'data_reajuste', 'indice', 'percentual_aplicado', 'valor_anterior',
        'valor_novo', 'periodo_indice', 'aplicar_agora', 'aplicado', 'aplicado_em', 'observacoes',
    )
    # `aplicado` não é mais um campo de comando editável pelo usuário — fica
    # somente-leitura (exibição do estado real); `aplicar_agora` (definido
    # em ReajusteContratoInlineForm) é o único comando de interface para
    # aplicar um reajuste novo/pendente.
    readonly_fields = ('aplicado', 'aplicado_em')


class DocumentoContratoInline(admin.TabularInline):
    """Permite anexar documentos (contrato assinado, aditivos, vistoria) direto no cadastro do contrato."""
    model = Documento
    fk_name = 'contrato'
    fields = ('titulo', 'tipo', 'arquivo_link', 'arquivo', 'data_documento')
    readonly_fields = ('arquivo_link',)
    extra = 0
    verbose_name = 'Documento do Contrato'
    verbose_name_plural = 'Documentos do Contrato'

    @admin.display(description='Arquivo atual')
    def arquivo_link(self, obj):
        from documentos.admin import _arquivo_link
        return _arquivo_link(obj)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if db_field.name == 'arquivo':
            from documentos.admin import ArquivoSemLinkPublicoWidget
            formfield.widget = ArquivoSemLinkPublicoWidget()
        return formfield


@admin.register(Contrato)
class ContratoAdmin(SimpleHistoryAdmin):
    list_display = (
        'imovel', 'locatarios_display', 'vigencia_display', 'valor_aluguel',
        'status', 'indice_reajuste',
    )
    list_filter = ('status', 'indice_reajuste', 'tipo_garantia', 'prazo_indeterminado')
    search_fields = (
        'imovel__nome', 'locatario__nome', 'fiador__nome',
        'partes__pessoa__nome',
    )
    date_hierarchy = 'data_inicio'
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-data_inicio',)
    raw_id_fields = ('imovel', 'locatario', 'fiador', 'imobiliaria')
    actions = ['gerar_receitas_esperadas', 'garantir_encargo_aluguel_action', 'sugerir_reajuste_indice']
    inlines = [ContratoParteInline, EncargoContratoInline, ReajusteContratoInline, DocumentoContratoInline]

    @admin.action(description='Garantir encargo de aluguel')
    def garantir_encargo_aluguel_action(self, request, queryset):
        total = 0
        for contrato in queryset:
            if contrato.garantir_encargo_aluguel() is not None:
                total += 1
        if total:
            self.message_user(request, f'{total} encargo(s) de aluguel criado(s).', messages.SUCCESS)
        else:
            self.message_user(request, 'Nenhum encargo de aluguel precisou ser criado.', messages.WARNING)

    @admin.action(description='Sugerir reajuste pelo índice acumulado 12m (Banco Central)')
    def sugerir_reajuste_indice(self, request, queryset):
        """
        Consulta o acumulado de 12 meses do índice do contrato na API do
        Banco Central e cria um ReajusteContrato pendente (aplicado=False)
        para revisão. Não altera valores vigentes.
        """
        from .indices import (
            variacao_acumulada_12m, competencia_final_para_reajuste,
            IndiceIndisponivelError, SERIES_SGS,
        )

        hoje = timezone.localdate()
        criados = 0
        sem_indice = 0
        ja_pendentes = 0
        inativos = 0

        for contrato in queryset:
            if contrato.status != 'ativo':
                inativos += 1
                continue
            if contrato.indice_reajuste not in SERIES_SGS:
                sem_indice += 1
                continue
            # Pendência oficial: aplicado_em IS NULL (não o flag aplicado)
            if contrato.reajustes.filter(aplicado_em__isnull=True).exists():
                ja_pendentes += 1
                continue

            # O período do índice é ancorado na data efetiva do reajuste do
            # contrato (12 meses encerrados no mês anterior ao reajuste) —
            # nunca nos "últimos 12 divulgados" da data de hoje.
            data_reajuste = contrato.data_proximo_reajuste or hoje
            try:
                resultado = variacao_acumulada_12m(
                    contrato.indice_reajuste,
                    competencia_final=competencia_final_para_reajuste(data_reajuste),
                )
            except IndiceIndisponivelError as exc:
                self.message_user(
                    request,
                    f'{contrato}: {exc}',
                    messages.ERROR,
                )
                continue

            percentual = resultado['percentual']
            valor_novo = (
                contrato.valor_aluguel * (Decimal('1') + percentual / Decimal('100'))
            ).quantize(Decimal('0.01'))

            ReajusteContrato.objects.create(
                contrato=contrato,
                data_reajuste=data_reajuste,
                indice=contrato.indice_reajuste,
                percentual_aplicado=percentual,
                valor_anterior=contrato.valor_aluguel,
                valor_novo=valor_novo,
                aplicado=False,
                periodo_indice=f'{resultado["inicio"]} a {resultado["fim"]}',
                observacoes=(
                    f'Sugestão automática — {contrato.get_indice_reajuste_display()} acumulado '
                    f'de {resultado["inicio"]} a {resultado["fim"]}: {percentual}% (fonte: Banco '
                    'Central/SGS). Revise e marque "Aplicado" para efetivar. Atenção à defasagem '
                    'de divulgação do índice (IPCA/INPC ~dia 10 do mês seguinte; IGP-M no fim do mês).'
                ),
            )
            criados += 1

        if criados:
            self.message_user(
                request,
                f'{criados} sugestão(ões) de reajuste criada(s) — revise na seção '
                '"Reajustes de Contrato" e marque "Aplicado" para efetivar.',
                messages.SUCCESS,
            )
        if ja_pendentes:
            self.message_user(
                request,
                f'{ja_pendentes} contrato(s) ignorado(s): já possuem reajuste pendente de aplicação.',
                messages.WARNING,
            )
        if sem_indice:
            self.message_user(
                request,
                f'{sem_indice} contrato(s) ignorado(s): índice sem série no Banco Central (fixo/outro).',
                messages.WARNING,
            )
        if inativos:
            self.message_user(request, f'{inativos} contrato(s) ignorado(s) por não estarem ativos.', messages.WARNING)

    @admin.action(description='Gerar receitas esperadas (escolher mês/ano)')
    def gerar_receitas_esperadas(self, request, queryset):
        """
        Ação em duas etapas: primeiro pede a competência (mês/ano) explícita,
        depois gera. Evita geração sem período definido — especialmente
        ambígua em contratos por prazo indeterminado.
        """
        from calendar import monthrange
        from datetime import date as date_cls

        from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
        from django.template.response import TemplateResponse

        from core.utils import anos_para_filtro, int_param
        from financeiro.models import MESES
        from financeiro.services import gerar_receitas_para_contrato

        if 'aplicar_geracao' in request.POST:
            mes = int_param(request.POST.get('mes'), 0, 1, 12)
            ano = int_param(request.POST.get('ano'), 0, 1990, 2200)
            if not mes or not ano:
                self.message_user(request, 'Informe um mês e ano válidos.', messages.ERROR)
                return None

            inicio = date_cls(ano, mes, 1)
            fim = date_cls(ano, mes, monthrange(ano, mes)[1])
            total_criadas = total_existiam = ignorados = 0
            for contrato in queryset:
                if contrato.status != 'ativo':
                    ignorados += 1
                    continue
                criadas, existiam = gerar_receitas_para_contrato(contrato, data_inicio=inicio, data_fim=fim)
                total_criadas += criadas
                total_existiam += existiam

            if total_criadas:
                self.message_user(request, f'{total_criadas} receita(s) gerada(s) para {mes:02d}/{ano}.', messages.SUCCESS)
            if total_existiam:
                self.message_user(request, f'{total_existiam} receita(s) já existiam e foram ignoradas.', messages.WARNING)
            if ignorados:
                self.message_user(request, f'{ignorados} contrato(s) ignorado(s) por não estarem ativos.', messages.WARNING)
            if not total_criadas and not total_existiam and not ignorados:
                self.message_user(request, f'Nenhuma receita para gerar em {mes:02d}/{ano}.', messages.WARNING)
            return None

        hoje = timezone.localdate()
        context = {
            **self.admin_site.each_context(request),
            'title': 'Gerar receitas esperadas',
            'contratos': queryset,
            'meses': MESES,
            'anos': anos_para_filtro(),
            'mes_atual': hoje.month,
            'ano_atual': hoje.year,
            'action_checkbox_name': ACTION_CHECKBOX_NAME,
            'opts': self.model._meta,
        }
        return TemplateResponse(request, 'admin/patrimonio/gerar_receitas_confirm.html', context)

    fieldsets = (
        ('Imóvel e Vigência', {
            'fields': (
                'imovel', 'data_inicio', 'data_fim', 'prazo_indeterminado',
                'data_encerramento_real', 'status',
            )
        }),
        ('Partes (campos legados)', {
            'fields': ('locatario', 'fiador', 'imobiliaria'),
            'description': (
                'Mantidos por compatibilidade com contratos já cadastrados. '
                'Para múltiplos locatários, fiadores ou representantes, use a '
                'seção "Partes do Contrato" logo abaixo.'
            ),
        }),
        ('Valores', {
            'fields': ('valor_aluguel', 'dia_vencimento')
        }),
        ('Reajuste e Garantia', {
            'fields': ('indice_reajuste', 'data_proximo_reajuste', 'tipo_garantia', 'comissao_imobiliaria_percentual')
        }),
        ('Multa e Juros por Atraso', {
            'fields': ('multa_atraso_percentual', 'juros_mora_percentual_mes', 'dias_carencia_multa')
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )

    def save_formset(self, request, form, formset, change):
        if formset.model is ReajusteContrato:
            # "Aplicar agora" (ReajusteContratoInlineForm) é um campo de
            # FORMULÁRIO, não persistido no model — o campo `aplicado` do
            # model nunca é tocado pelo form (está em readonly_fields), então
            # a instância chega em formset.save(commit=False) sempre com
            # aplicado=False/aplicado_em=None para linhas novas/pendentes,
            # sem precisar zerar `obj.aplicado` manualmente. Mapeia a
            # intenção de aplicar por identidade da instância ANTES de
            # formset.save() persistir, pois cleaned_data só existe no form.
            aplicar_por_instancia = {}
            for linha in formset.forms:
                dados = getattr(linha, 'cleaned_data', None)
                if not dados or dados.get('DELETE'):
                    continue
                aplicar_por_instancia[id(linha.instance)] = dados.get('aplicar_agora', False)

            instances = formset.save(commit=False)
            for obj in instances:
                if obj.aplicado_em is not None:
                    # Já aplicado: clean()/save() bloqueiam alteração dos
                    # campos financeiros; "aplicar agora" é ignorado aqui
                    # independente do form (defesa em profundidade — o form
                    # já vem com o campo disabled para linhas aplicadas).
                    obj.save()
                    continue
                obj.save()
                if aplicar_por_instancia.get(id(obj)):
                    # aplicar() é idempotente (guard por aplicado_em) e atômico
                    obj.aplicar()
            for obj in formset.deleted_objects:
                try:
                    obj.delete()
                except ValidationError as exc:
                    # Reajuste aplicado: o checkbox de exclusão já vem
                    # desabilitado no formset, mas a proteção real é aqui —
                    # avisa e preserva o registro em vez de quebrar a página.
                    messages.error(request, '; '.join(exc.messages))
            formset.save_m2m()
        elif formset.model is Documento:
            instances = formset.save(commit=False)
            for obj in instances:
                if not obj.imovel_id:
                    obj.imovel_id = form.instance.imovel_id
                obj.save()
            for obj in formset.deleted_objects:
                obj.delete()
            formset.save_m2m()
        else:
            formset.save()

    def save_related(self, request, form, formsets, change):
        # Os inlines (incluindo Encargos do Contrato) só terminam de ser
        # salvos aqui. Chamar garantir_encargo_aluguel() em save_model()
        # rodaria antes do inline salvar um encargo de aluguel manual,
        # podendo duplicá-lo — por isso a garantia roda só depois.
        super().save_related(request, form, formsets, change)
        form.instance.garantir_encargo_aluguel()


@admin.register(Manutencao)
class ManutencaoAdmin(SimpleHistoryAdmin):
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
