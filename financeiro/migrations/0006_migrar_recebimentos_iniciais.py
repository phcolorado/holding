"""
Converte o valor_recebido consolidado já existente em um RecebimentoReceita
histórico inicial (origem='migracao'), para que o novo modelo de múltiplos
recebimentos reflita os dados antigos.

Idempotente: só cria quando a receita ainda não possui nenhum recebimento.
"""
from django.db import migrations


def criar_recebimentos_iniciais(apps, schema_editor):
    ReceitaAluguel = apps.get_model('financeiro', 'ReceitaAluguel')
    RecebimentoReceita = apps.get_model('financeiro', 'RecebimentoReceita')

    receitas = ReceitaAluguel.objects.filter(valor_recebido__gt=0).exclude(status='cancelado')
    for receita in receitas.iterator():
        if RecebimentoReceita.objects.filter(receita=receita).exists():
            continue
        recebimento = RecebimentoReceita(
            receita=receita,
            data_recebimento=receita.data_recebimento or receita.data_vencimento,
            valor=receita.valor_recebido,
            origem='migracao',
            observacoes='Recebimento histórico criado automaticamente a partir do valor consolidado.',
        )
        # sinaliza ao save() customizado (quando executado fora da migration,
        # ex.: em testes) que este JÁ É o recebimento legado
        recebimento._eh_legado = True
        recebimento.save()


def reverter(apps, schema_editor):
    RecebimentoReceita = apps.get_model('financeiro', 'RecebimentoReceita')
    RecebimentoReceita.objects.filter(origem='migracao').delete()


class Migration(migrations.Migration):
    dependencies = [
        ('financeiro', '0005_recebimentoreceita_historicalrecebimentoreceita'),
    ]

    operations = [
        migrations.RunPython(criar_recebimentos_iniciais, reverter),
    ]
