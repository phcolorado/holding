from django import forms


class RegistrarRecebimentoForm(forms.Form):
    """
    Registra um recebimento manual na tela de Baixa de Aluguéis.
    Cria um RecebimentoReceita (origem='manual') — os campos consolidados da
    receita são recalculados automaticamente a partir dos recebimentos.
    """
    valor = forms.DecimalField(
        max_digits=12, decimal_places=2,
        error_messages={'invalid': 'Valor inválido.', 'required': 'Informe o valor recebido.'},
    )
    data_recebimento = forms.DateField(
        error_messages={'invalid': 'Data de recebimento inválida.', 'required': 'Informe a data do recebimento.'},
    )
    observacoes = forms.CharField(required=False)

    def clean_valor(self):
        valor = self.cleaned_data['valor']
        if valor <= 0:
            raise forms.ValidationError('O valor do recebimento deve ser maior que zero.')
        return valor


class EditarEncargosForm(forms.Form):
    """
    Edita multa, juros, desconto e observações da receita na tela de baixa.
    Campos de recebimento (valor_recebido/data/status) NÃO são editáveis aqui —
    são derivados dos recebimentos registrados.
    """
    multa = forms.DecimalField(
        required=False, min_value=0, max_digits=10, decimal_places=2,
        error_messages={'invalid': 'Multa inválida.'},
    )
    juros = forms.DecimalField(
        required=False, min_value=0, max_digits=10, decimal_places=2,
        error_messages={'invalid': 'Juros inválidos.'},
    )
    desconto = forms.DecimalField(
        required=False, min_value=0, max_digits=10, decimal_places=2,
        error_messages={'invalid': 'Desconto inválido.'},
    )
    observacoes = forms.CharField(required=False)

    def aplicar(self, receita):
        """Aplica os dados validados na receita (sem salvar). None = manter."""
        dados = self.cleaned_data
        if dados['multa'] is not None:
            receita.multa = dados['multa']
        if dados['juros'] is not None:
            receita.juros = dados['juros']
        if dados['desconto'] is not None:
            receita.desconto = dados['desconto']
        receita.observacoes = dados['observacoes']
        return receita
