"""
Explica por que a ficha de locação da DIMOB de um ano saiu vazia.

Uso:  python manage.py diagnostico_dimob 2025

Só LÊ o banco — não altera nada.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from financeiro.dimob import diagnostico


class Command(BaseCommand):
    help = 'Mostra o que existe no banco por trás da ficha de locação da DIMOB de um ano.'

    def add_arguments(self, parser):
        parser.add_argument(
            'ano', nargs='?', type=int, default=None,
            help='Ano-calendário a conferir (padrão: o ano anterior ao atual).',
        )

    @staticmethod
    def _reais(valor):
        # O agregado do banco pode vir sem as casas decimais (ex.: 2000);
        # a contabilidade confere valores em centavos.
        return f"R$ {Decimal(valor).quantize(Decimal('0.01'))}"

    def handle(self, *args, **options):
        ano = options['ano'] or (timezone.localdate().year - 1)
        d = diagnostico(ano)

        self.stdout.write(self.style.MIGRATE_HEADING(f'DIMOB — diagnóstico do ano {ano}'))
        linhas = [
            ('Contratos cadastrados', d['contratos_total']),
            ('  dos quais ativos', d['contratos_ativos']),
            ('Receitas de aluguel cadastradas', d['receitas_total']),
            (f'  com competência em {ano}', d['receitas_competencia_no_ano']),
            ('Recebimentos registrados (todos os anos)', d['recebimentos_total']),
            (f'  com data de recebimento em {ano}', d['recebimentos_no_ano']),
            (f'  soma recebida em {ano}', self._reais(d['valor_recebimentos_no_ano'])),
            (f'Receitas com valor consolidado sem recebimento em {ano}', d['legadas_no_ano']),
            ('  soma consolidada', self._reais(d['valor_legadas_no_ano'])),
            (f'Comissões pagas em {ano}', d['comissoes_no_ano']),
            ('  soma das comissões', self._reais(d['valor_comissoes_no_ano'])),
            ('CONTRATOS NA FICHA DE LOCAÇÃO', d['linhas_geradas']),
        ]
        largura = max(len(rotulo) for rotulo, _ in linhas)
        for rotulo, valor in linhas:
            self.stdout.write(f'{rotulo.ljust(largura)}  {valor}')

        anos = d['anos_com_movimento']
        self.stdout.write('')
        self.stdout.write(
            'Anos com entrada de aluguel: ' + (', '.join(str(a) for a in anos) if anos else 'nenhum')
        )

        self.stdout.write('')
        estilo = self.style.SUCCESS if d['linhas_geradas'] else self.style.WARNING
        for conclusao in d['conclusoes'] or ['Nada a apontar.']:
            self.stdout.write(estilo(f'-> {conclusao}'))
