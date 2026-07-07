from django import forms

from .models import ReceitaAluguel


class BaixaReceitaForm(forms.Form):
    """
    Valida a edição de recebimento feita na tela de Baixa de Aluguéis.
    Campos em branco significam "limpar" (valor/data) ou "manter" (multa/juros).
    """
    valor_recebido = forms.DecimalField(
        required=False, min_value=0, max_digits=12, decimal_places=2,
        error_messages={'invalid': 'Valor recebido inválido.'},
    )
    data_recebimento = forms.DateField(
        required=False,
        error_messages={'invalid': 'Data de recebimento inválida.'},
    )
    multa = forms.DecimalField(
        required=False, min_value=0, max_digits=10, decimal_places=2,
        error_messages={'invalid': 'Multa inválida.'},
    )
    juros = forms.DecimalField(
        required=False, min_value=0, max_digits=10, decimal_places=2,
        error_messages={'invalid': 'Juros inválidos.'},
    )
    status = forms.ChoiceField(
        choices=ReceitaAluguel.STATUS_CHOICES,
        error_messages={'invalid_choice': 'Status inválido.', 'required': 'Status é obrigatório.'},
    )
    observacoes = forms.CharField(required=False)

    def aplicar(self, receita):
        """Aplica os dados validados na receita (sem salvar)."""
        dados = self.cleaned_data
        receita.valor_recebido = dados['valor_recebido']
        receita.data_recebimento = dados['data_recebimento']
        if dados['multa'] is not None:
            receita.multa = dados['multa']
        if dados['juros'] is not None:
            receita.juros = dados['juros']
        receita.status = dados['status']
        receita.observacoes = dados['observacoes']
        return receita
