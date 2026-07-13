from django import forms

from .models import ReajusteContrato


class ReajusteContratoInlineForm(forms.ModelForm):
    """
    `aplicar_agora` é um campo de FORMULÁRIO, não persistido no model —
    substitui o campo `aplicado` como comando de interface para aplicar um
    reajuste novo/pendente.

    Antes, o inline usava o próprio campo `aplicado` do model como
    checkbox de comando: marcar "Aplicado" e salvar. Isso quebrou quando o
    model passou a rejeitar corretamente o estado intermediário
    aplicado=True/aplicado_em=None — o ModelForm valida a instância
    (full_clean) durante `formset.is_valid()`, ANTES de
    ContratoAdmin.save_formset() rodar, então marcar "Aplicado" num
    reajuste novo fazia a validação falhar e o formset inteiro era
    rejeitado, sem nunca chegar a save_formset().

    Com `aplicar_agora` fora do model, a instância nunca vê aplicado=True
    vindo do formulário — permanece aplicado=False/aplicado_em=None até
    ContratoAdmin.save_formset() salvar o reajuste pendente e, só depois,
    chamar reajuste.aplicar() quando cleaned_data['aplicar_agora'] for True.
    """
    aplicar_agora = forms.BooleanField(
        required=False, label='Aplicar agora',
        help_text='Marque e salve para aplicar este reajuste imediatamente.',
    )

    class Meta:
        model = ReajusteContrato
        fields = []
