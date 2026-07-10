"""
Backfills pós-schema:
- ReajusteContrato.aplicado_em: reajustes já aplicados recebem a data de
  atualização como marco, impedindo que aplicar() os reaplique.
- Pessoa.cpf_cnpj_normalizado: popula a forma só-dígitos para os cadastros
  existentes (a validação de duplicidade usa esse campo).

Idempotente: só preenche registros ainda vazios.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    ReajusteContrato = apps.get_model('patrimonio', 'ReajusteContrato')
    Pessoa = apps.get_model('patrimonio', 'Pessoa')

    for reajuste in ReajusteContrato.objects.filter(aplicado=True, aplicado_em__isnull=True):
        reajuste.aplicado_em = reajuste.atualizado_em or reajuste.criado_em
        reajuste.save(update_fields=['aplicado_em'])

    for pessoa in Pessoa.objects.filter(cpf_cnpj_normalizado='').exclude(cpf_cnpj=''):
        pessoa.cpf_cnpj_normalizado = ''.join(c for c in pessoa.cpf_cnpj if c.isdigit())
        pessoa.save(update_fields=['cpf_cnpj_normalizado'])


class Migration(migrations.Migration):
    dependencies = [
        ('patrimonio', '0007_historicalpessoa_cpf_cnpj_normalizado_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
