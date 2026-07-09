from django import forms

from financeiro.models import Despesa
from patrimonio.models import Imovel, Pessoa
from .models import ContaBancaria


class UploadExtratoForm(forms.Form):
    conta = forms.ModelChoiceField(
        queryset=ContaBancaria.objects.filter(ativo=True),
        label='Conta Bancária',
        error_messages={'required': 'Selecione a conta bancária do extrato.'},
    )
    arquivo = forms.FileField(
        label='Arquivo OFX',
        error_messages={'required': 'Selecione o arquivo .ofx exportado pelo banco.'},
    )

    def clean_arquivo(self):
        arquivo = self.cleaned_data['arquivo']
        nome = (arquivo.name or '').lower()
        if not nome.endswith(('.ofx', '.qfx')):
            raise forms.ValidationError('O arquivo deve ser um extrato OFX (.ofx ou .qfx).')
        return arquivo


class DespesaExtratoForm(forms.Form):
    """Valida o lançamento de um débito do extrato como Despesa."""
    categoria = forms.ChoiceField(
        choices=Despesa.CATEGORIA_CHOICES,
        error_messages={'invalid_choice': 'Categoria inválida.', 'required': 'Escolha a categoria.'},
    )
    descricao = forms.CharField(max_length=300, error_messages={'required': 'Informe a descrição.'})
    fornecedor = forms.ModelChoiceField(queryset=Pessoa.objects.all(), required=False)
    imovel = forms.ModelChoiceField(queryset=Imovel.objects.all(), required=False)
