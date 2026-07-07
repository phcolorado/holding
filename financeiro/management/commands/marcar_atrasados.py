from django.core.management.base import BaseCommand
from django.utils import timezone

from financeiro.models import ReceitaAluguel, Despesa


class Command(BaseCommand):
    help = (
        'Marca como atrasadas as receitas previstas e despesas previstas já vencidas. '
        'Agende via cron/Agendador de Tarefas para rodar diariamente.'
    )

    def handle(self, *args, **options):
        hoje = timezone.localdate()

        receitas = ReceitaAluguel.objects.filter(
            status='previsto', data_vencimento__lt=hoje
        ).update(status='atrasado')

        despesas = Despesa.objects.filter(
            status='prevista', data_vencimento__lt=hoje
        ).update(status='atrasada')

        self.stdout.write(self.style.SUCCESS(
            f'{receitas} receita(s) e {despesas} despesa(s) marcadas como atrasadas.'
        ))
