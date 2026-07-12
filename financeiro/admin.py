from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.forms.models import BaseInlineFormSet
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from simple_history.admin import SimpleHistoryAdmin
from .models import (
    ReceitaAluguel, ReceitaAluguelItem, RecebimentoReceita, Despesa, FechamentoMensal,
    despesa_tem_conciliacao_ativa,
)
from .services import (
    atualizar_recebimentos_da_receita,
    validar_ids_e_protecao_recebimentos, validar_total_final_recebimentos,
)

CAMPOS_DESPESA_PROTEGIDOS_ADMIN = (
    'valor', 'status', 'data_pagamento', 'categoria', 'fornecedor', 'imovel', 'contrato', 'receita',
)


class ReceitaAluguelItemInline(admin.TabularInline):
    model = ReceitaAluguelItem
    extra = 0
    fields = ('tipo', 'descricao', 'valor')


class RecebimentoReceitaInlineForm(forms.ModelForm):
    """
    Pula a checagem de saldo POR LINHA do model (RecebimentoReceita.clean())
    — o inline salva o formset inteiro como uma única operação financeira via
    atualizar_recebimentos_da_receita() (ReceitaAluguelAdmin.save_formset),
    que calcula o total FINAL de todas as inclusões/alterações/exclusões em
    CONJUNTO. Validar cada linha isolada aqui, contra o saldo ATUAL (antes
    das outras mudanças do mesmo envio), rejeitaria ou aceitaria combinações
    incorretamente. A validação valor>0 continua ativa (não é pulada).
    """
    class Meta:
        model = RecebimentoReceita
        fields = '__all__'

    def _post_clean(self):
        self.instance._pular_validacao_saldo = True
        super()._post_clean()


class RecebimentoReceitaInlineFormSet(BaseInlineFormSet):
    """
    Desabilita, por linha, a edição e exclusão de recebimentos originados de
    conciliação bancária (origem='conciliacao' ou transacao_extrato
    preenchida) — para corrigi-los, desfaça a conciliação correspondente na
    tela de Conciliação Bancária. disabled=True é reforço de SERVIDOR (dado
    submetido para um campo disabled é ignorado em favor do initial).
    """
    def add_fields(self, form, index):
        super().add_fields(form, index)
        instance = getattr(form, 'instance', None)
        if instance and instance.pk and (
            instance.origem == 'conciliacao' or instance.transacao_extrato_id
        ):
            if 'DELETE' in form.fields:
                form.fields['DELETE'].disabled = True
            for campo in ('data_recebimento', 'valor', 'observacoes'):
                if campo in form.fields:
                    form.fields[campo].disabled = True

    def clean(self):
        """
        Valida o LOTE inteiro (existentes + novos + alterados + excluídos)
        em conjunto, no ciclo de validação do próprio Django Admin — ANTES
        de qualquer persistência. Se o lote é inconsistente, o formset fica
        inválido: nada é salvo (nem os recebimentos, nem multa/juros/
        desconto do formulário principal da MESMA submissão), a página é
        reapresentada com erro e nenhuma mensagem de sucesso é exibida —
        substitui a checagem antiga, que só rodava DEPOIS de o formulário
        principal já ter sido persistido (ver save_formset() abaixo, que
        continua chamando o service transacional como segunda camada, nunca
        substituída por esta).

        self.instance é o ReceitaAluguel já com os dados LIMPOS do
        formulário principal desta mesma submissão (multa/juros/desconto
        novos, ainda não persistidos) — por isso valor_total_devido aqui já
        reflete os valores que SERÃO salvos, não os antigos do banco.
        """
        super().clean()
        if any(self.errors):
            # Deixa os erros de campo de cada linha (ex.: valor <= 0)
            # aparecerem primeiro — evita mascará-los com o erro do lote.
            return

        receita = self.instance
        existentes = {r.pk: r for r in receita.recebimentos.all()}
        excluidos_pks = set()
        alterados_por_pk = {}
        novos_valores = []
        vistos_pks = set()

        for form in self.forms:
            dados = getattr(form, 'cleaned_data', None)
            if not dados:
                continue
            instancia = form.instance
            if dados.get('DELETE'):
                if instancia.pk:
                    if instancia.pk in vistos_pks:
                        raise ValidationError('Recebimento duplicado na mesma submissão.')
                    vistos_pks.add(instancia.pk)
                    excluidos_pks.add(instancia.pk)
                continue
            if instancia.pk:
                if instancia.pk in vistos_pks:
                    raise ValidationError('Recebimento duplicado na mesma submissão.')
                vistos_pks.add(instancia.pk)
                # Compara contra o valor REALMENTE persistido (não contra a
                # instância em memória, que já reflete os dados limpos) —
                # linhas protegidas têm o campo disabled=True, então seu
                # valor submetido é sempre igual ao original (Django ignora
                # dado enviado para campo disabled, usa o initial).
                rec_atual = existentes.get(instancia.pk)
                valor_novo = dados.get('valor')
                if rec_atual is not None and valor_novo != rec_atual.valor:
                    alterados_por_pk[instancia.pk] = valor_novo
            elif dados.get('valor') is not None:
                novos_valores.append(dados.get('valor'))

        validar_ids_e_protecao_recebimentos(
            receita, existentes, set(alterados_por_pk), excluidos_pks, bool(novos_valores),
        )
        validar_total_final_recebimentos(receita, existentes, alterados_por_pk, excluidos_pks, novos_valores)


class RecebimentoReceitaInline(admin.TabularInline):
    model = RecebimentoReceita
    form = RecebimentoReceitaInlineForm
    formset = RecebimentoReceitaInlineFormSet
    extra = 0
    fields = (
        'data_recebimento', 'valor', 'origem', 'transacao_extrato',
        'criado_por', 'observacoes', 'nota_edicao',
    )
    readonly_fields = ('origem', 'transacao_extrato', 'criado_por', 'nota_edicao')

    @admin.display(description='Nota')
    def nota_edicao(self, obj):
        if obj and obj.pk and (obj.origem == 'conciliacao' or obj.transacao_extrato_id):
            return 'Origem: conciliação bancária — desfaça a conciliação correspondente para corrigir.'
        return ''


@admin.register(ReceitaAluguel)
class ReceitaAluguelAdmin(SimpleHistoryAdmin):
    list_display = (
        'imovel', 'competencia_mes', 'competencia_ano',
        'valor_previsto', 'valor_recebido', 'data_vencimento', 'status',
    )
    list_filter = ('status', 'competencia_ano', 'competencia_mes', 'imovel')
    search_fields = ('imovel__nome', 'contrato__locatario__nome', 'contrato__partes__pessoa__nome')
    date_hierarchy = 'data_vencimento'
    # valor_recebido/data_recebimento/status são CONSOLIDADOS derivados dos
    # recebimentos (inline abaixo) — nunca editados diretamente. Cancelamento
    # e reabertura passam pelas actions dedicadas.
    readonly_fields = ('valor_recebido', 'data_recebimento', 'status', 'criado_em', 'atualizado_em')
    ordering = ('-competencia_ano', '-competencia_mes')
    raw_id_fields = ('contrato',)
    inlines = [ReceitaAluguelItemInline, RecebimentoReceitaInline]
    actions = ['cancelar_receitas', 'reabrir_receitas']

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
        ('Recebimento (consolidado — derivado dos recebimentos abaixo)', {
            'description': (
                'Estes campos são calculados automaticamente a partir dos '
                '"Recebimentos da Receita". Para registrar um pagamento, adicione '
                'um recebimento no inline; para cancelar/reabrir a receita, use as '
                'ações da listagem.'
            ),
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

    @admin.action(description='Cancelar receitas selecionadas')
    def cancelar_receitas(self, request, queryset):
        total = sum(1 for r in queryset if r.cancelar())
        if total:
            self.message_user(
                request,
                f'{total} receita(s) cancelada(s) — recebimentos registrados foram preservados.',
                messages.SUCCESS,
            )
        else:
            self.message_user(request, 'Nenhuma receita precisou ser cancelada.', messages.WARNING)

    @admin.action(description='Reabrir receitas canceladas')
    def reabrir_receitas(self, request, queryset):
        total = sum(1 for r in queryset if r.reabrir())
        if total:
            self.message_user(
                request,
                f'{total} receita(s) reaberta(s) — status recalculado pelo saldo, vencimento e recebimentos.',
                messages.SUCCESS,
            )
        else:
            self.message_user(request, 'Nenhuma receita selecionada estava cancelada.', messages.WARNING)

    def save_formset(self, request, form, formset, change):
        if formset.model is RecebimentoReceita:
            # formset.save(commit=False) NÃO persiste nada — apenas popula
            # new_objects/changed_objects/deleted_objects (mesma técnica já
            # usada para ReajusteContrato/Documento neste admin). O formset
            # inteiro é então salvo como UMA ÚNICA operação financeira
            # atômica por atualizar_recebimentos_da_receita(), que valida o
            # total final de todas as linhas em conjunto — nunca uma
            # transação independente por linha.
            formset.save(commit=False)
            novos = [
                {
                    'data_recebimento': obj.data_recebimento,
                    'valor': obj.valor,
                    'observacoes': obj.observacoes,
                }
                for obj in formset.new_objects
            ]
            alterados = [
                {
                    'pk': obj.pk,
                    'data_recebimento': obj.data_recebimento,
                    'valor': obj.valor,
                    'observacoes': obj.observacoes,
                }
                for obj, _campos in formset.changed_objects
            ]
            excluidos = [obj.pk for obj in formset.deleted_objects]
            if novos or alterados or excluidos:
                # NÃO captura ValidationError aqui para só emitir
                # messages.error: o formset.clean() (RecebimentoReceitaInline
                # FormSet, acima) já barrou toda inconsistência ESPERADA
                # antes de chegarmos a este ponto — se o service ainda assim
                # rejeitar (ex.: corrida real de concorrência entre a
                # validação e este save), a exceção deve propagar e abortar
                # TODA a operação (a view do Admin roda dentro de
                # transaction.atomic(): nada fica parcialmente salvo, nem o
                # formulário principal já "salvo" antes deste ponto, e
                # nenhuma mensagem de sucesso chega a ser exibida).
                atualizar_recebimentos_da_receita(
                    form.instance.pk, novos=novos, alterados=alterados,
                    excluidos=excluidos, usuario=request.user,
                )
            formset.save_m2m()
        else:
            formset.save()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Depois de salvar/alterar/excluir inlines (e possíveis mudanças em
        # multa/juros/desconto no form principal), reconsolida a receita.
        form.instance.refresh_from_db()
        form.instance.recalcular_recebimentos()


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
    actions = ['reabrir_despesas_action', 'cancelar_despesas_action']

    def get_readonly_fields(self, request, obj=None):
        base = list(self.readonly_fields)
        if obj is not None and despesa_tem_conciliacao_ativa(obj):
            # Campos financeiros somente-leitura enquanto a despesa estiver
            # vinculada a uma conciliação bancária ativa (item 4) — a
            # descrição do card "Conciliação" abaixo orienta a desfazer.
            base += [c for c in CAMPOS_DESPESA_PROTEGIDOS_ADMIN if c not in base]
            base.append('conciliacao_info')
        return base

    def has_delete_permission(self, request, obj=None):
        if obj is not None and despesa_tem_conciliacao_ativa(obj):
            return False
        return super().has_delete_permission(request, obj)

    @admin.display(description='Conciliação bancária')
    def conciliacao_info(self, obj):
        if not obj or not obj.pk:
            return '—'
        transacoes = []
        vinculo_comissao = obj.conciliacoes_comissao.select_related('transacao').first()
        if vinculo_comissao:
            transacoes.append(vinculo_comissao.transacao)
        transacoes += list(obj.transacoes_extrato.filter(status='conciliada'))
        if not transacoes:
            return '—'
        links = format_html_join(
            '<br>', '<a href="{}">extrato #{} — transação de {}</a>',
            (
                (reverse('conciliar_extrato', args=[t.extrato_id]), t.extrato_id, t.data)
                for t in transacoes
            ),
        )
        return format_html(
            '{}<br><span style="color:#a00;">Desfaça a conciliação na tela de Conciliação '
            'Bancária para editar os campos financeiros desta despesa.</span>',
            links,
        )

    @admin.action(description='Reabrir despesas selecionadas (limpa data de pagamento)')
    def reabrir_despesas_action(self, request, queryset):
        from .services import reabrir_despesa
        total = 0
        bloqueadas = 0
        for despesa in queryset.exclude(status__in=('prevista', 'atrasada')):
            if despesa_tem_conciliacao_ativa(despesa):
                bloqueadas += 1
                continue
            reabrir_despesa(despesa.pk)
            total += 1
        if total:
            self.message_user(request, f'{total} despesa(s) reaberta(s).', messages.SUCCESS)
        if bloqueadas:
            self.message_user(
                request,
                f'{bloqueadas} despesa(s) ignorada(s) por estarem vinculadas a uma conciliação '
                'bancária ativa — desfaça a conciliação correspondente para reabri-las.',
                messages.WARNING,
            )
        if not total and not bloqueadas:
            self.message_user(request, 'Nenhuma despesa selecionada precisava ser reaberta.', messages.WARNING)

    @admin.action(description='Cancelar despesas selecionadas')
    def cancelar_despesas_action(self, request, queryset):
        from .services import cancelar_despesa
        total = 0
        bloqueadas = 0
        for despesa in queryset.exclude(status='cancelada'):
            if despesa_tem_conciliacao_ativa(despesa):
                bloqueadas += 1
                continue
            cancelar_despesa(despesa.pk)
            total += 1
        if total:
            self.message_user(request, f'{total} despesa(s) cancelada(s).', messages.SUCCESS)
        if bloqueadas:
            self.message_user(
                request,
                f'{bloqueadas} despesa(s) ignorada(s) por estarem vinculadas a uma conciliação '
                'bancária ativa — desfaça a conciliação correspondente para cancelá-las.',
                messages.WARNING,
            )
        if not total and not bloqueadas:
            self.message_user(request, 'Nenhuma despesa selecionada precisava ser cancelada.', messages.WARNING)

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
        ('Conciliação Bancária', {
            'fields': ('conciliacao_info',),
        }),
        ('Observações', {
            'fields': ('observacoes',)
        }),
        ('Controle', {
            'fields': ('criado_em', 'atualizado_em'),
            'classes': ('collapse',),
        }),
    )

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if obj is not None and despesa_tem_conciliacao_ativa(obj):
            return fieldsets
        # Sem conciliação ativa: não exibe o card "Conciliação Bancária"
        # (conciliacao_info sempre voltaria '—').
        return tuple(fs for fs in fieldsets if fs[0] != 'Conciliação Bancária')


@admin.register(FechamentoMensal)
class FechamentoMensalAdmin(SimpleHistoryAdmin):
    list_display = ('mes', 'ano', 'data_fechamento', 'enviado_contabilidade', 'data_envio_contabilidade')
    list_filter = ('enviado_contabilidade', 'ano')
    readonly_fields = ('criado_em', 'atualizado_em')
    ordering = ('-ano', '-mes')
