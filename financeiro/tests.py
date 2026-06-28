from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.test import TestCase

from patrimonio.models import Imovel, Pessoa, Contrato
from financeiro.models import ReceitaAluguel
from financeiro.services import gerar_receitas_para_contrato, gerar_receitas_mes


def _criar_base():
    """Cria imóvel e locatário reutilizáveis entre testes."""
    imovel = Imovel.objects.create(
        nome='Apartamento Teste',
        endereco='Rua A, 100',
        cidade='São Paulo',
        estado='SP',
    )
    locatario = Pessoa.objects.create(nome='Locatário Teste', tipo='locatario')
    return imovel, locatario


def _criar_contrato(imovel, locatario, data_inicio, data_fim, status='ativo', dia_vencimento=10):
    return Contrato.objects.create(
        imovel=imovel,
        locatario=locatario,
        data_inicio=data_inicio,
        data_fim=data_fim,
        valor_aluguel=Decimal('2000.00'),
        dia_vencimento=dia_vencimento,
        status=status,
    )


class GerarReceitasParaContratoTest(TestCase):
    """Testa a função gerar_receitas_para_contrato."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()
        # Contrato de 3 meses: jan–mar 2024
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 3, 31),
        )

    def test_contrato_ativo_gera_receitas_mensais(self):
        """Contrato ativo de 3 meses gera exatamente 3 receitas."""
        criadas, existiam = gerar_receitas_para_contrato(self.contrato)

        self.assertEqual(criadas, 3)
        self.assertEqual(existiam, 0)
        self.assertEqual(ReceitaAluguel.objects.count(), 3)

        meses = list(
            ReceitaAluguel.objects.order_by('competencia_mes')
            .values_list('competencia_mes', flat=True)
        )
        self.assertEqual(meses, [1, 2, 3])

    def test_contrato_inativo_nao_gera_receitas(self):
        """Contratos com status diferente de 'ativo' são ignorados."""
        for status in ('encerrado', 'rescindido', 'suspenso'):
            self.contrato.status = status
            self.contrato.save()

            criadas, existiam = gerar_receitas_para_contrato(self.contrato)

            self.assertEqual(criadas, 0, msg=f'Status {status} não deveria gerar receitas')
            self.assertEqual(existiam, 0)

        self.assertEqual(ReceitaAluguel.objects.count(), 0)

    def test_nao_cria_duplicidade(self):
        """Segunda chamada não cria registros duplicados."""
        criadas1, existiam1 = gerar_receitas_para_contrato(self.contrato)
        criadas2, existiam2 = gerar_receitas_para_contrato(self.contrato)

        self.assertEqual(criadas1, 3)
        self.assertEqual(existiam1, 0)
        self.assertEqual(criadas2, 0)
        self.assertEqual(existiam2, 3)
        # Sem duplicatas no banco
        self.assertEqual(ReceitaAluguel.objects.count(), 3)

    def test_receita_gerada_usa_imovel_do_contrato(self):
        """O imóvel da receita gerada é sempre o imóvel do contrato."""
        gerar_receitas_para_contrato(self.contrato)

        for receita in ReceitaAluguel.objects.all():
            self.assertEqual(
                receita.imovel_id, self.contrato.imovel_id,
                msg='Imóvel da receita difere do imóvel do contrato',
            )

    def test_valores_e_status_corretos(self):
        """Receita gerada tem valor_previsto, data_vencimento e status corretos."""
        gerar_receitas_para_contrato(self.contrato)

        receita_jan = ReceitaAluguel.objects.get(competencia_mes=1, competencia_ano=2024)
        self.assertEqual(receita_jan.data_vencimento, date(2024, 1, 10))
        self.assertEqual(receita_jan.valor_previsto, Decimal('2000.00'))
        self.assertEqual(receita_jan.status, 'previsto')

    def test_dia_vencimento_ajustado_para_fim_do_mes(self):
        """Dia 31 em fevereiro (ano bissexto 2024) deve cair no dia 29."""
        self.contrato.dia_vencimento = 31
        self.contrato.save()

        gerar_receitas_para_contrato(self.contrato)

        receita_fev = ReceitaAluguel.objects.get(competencia_mes=2, competencia_ano=2024)
        self.assertEqual(receita_fev.data_vencimento, date(2024, 2, 29))

    def test_gerar_apenas_periodo_especifico(self):
        """Com data_inicio/data_fim informados, gera somente receitas do intervalo."""
        criadas, _ = gerar_receitas_para_contrato(
            self.contrato,
            data_inicio=date(2024, 2, 1),
            data_fim=date(2024, 2, 29),
        )

        self.assertEqual(criadas, 1)
        self.assertTrue(
            ReceitaAluguel.objects.filter(competencia_mes=2, competencia_ano=2024).exists()
        )
        self.assertFalse(
            ReceitaAluguel.objects.filter(competencia_mes=1).exists()
        )

    def test_periodo_fora_da_vigencia_do_contrato_nao_gera(self):
        """Solicitar um mês fora da vigência do contrato não gera nenhuma receita."""
        criadas, existiam = gerar_receitas_para_contrato(
            self.contrato,
            data_inicio=date(2025, 1, 1),
            data_fim=date(2025, 1, 31),
        )

        self.assertEqual(criadas, 0)
        self.assertEqual(existiam, 0)


class GerarReceitasMesTest(TestCase):
    """Testa a função gerar_receitas_mes (todos os contratos de um mês)."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()

    def test_geracao_mes_atual(self):
        """gerar_receitas_mes cria receita para contrato ativo no mês atual."""
        hoje = date.today()
        mes = hoje.month
        ano = hoje.year
        ultimo_dia = monthrange(ano, mes)[1]

        contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(ano, mes, 1),
            data_fim=date(ano, mes, ultimo_dia),
        )

        criadas, existiam = gerar_receitas_mes(mes, ano)

        self.assertEqual(criadas, 1)
        self.assertEqual(existiam, 0)
        self.assertTrue(
            ReceitaAluguel.objects.filter(
                contrato=contrato,
                competencia_mes=mes,
                competencia_ano=ano,
            ).exists()
        )

    def test_contrato_inativo_ignorado_em_gerar_mes(self):
        """Contratos inativos não são incluídos na geração por mês."""
        hoje = date.today()
        _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(hoje.year, hoje.month, 1),
            data_fim=date(hoje.year, hoje.month, monthrange(hoje.year, hoje.month)[1]),
            status='encerrado',
        )

        criadas, _ = gerar_receitas_mes(hoje.month, hoje.year)

        self.assertEqual(criadas, 0)

    def test_multiplos_contratos_no_mes(self):
        """Múltiplos contratos ativos geram uma receita cada."""
        hoje = date.today()
        mes, ano = hoje.month, hoje.year
        ultimo_dia = monthrange(ano, mes)[1]

        imovel2 = Imovel.objects.create(nome='Casa Teste', endereco='Rua B', cidade='SP', estado='SP')

        _criar_contrato(self.imovel, self.locatario, date(ano, mes, 1), date(ano, mes, ultimo_dia))
        _criar_contrato(imovel2, self.locatario, date(ano, mes, 1), date(ano, mes, ultimo_dia))

        criadas, _ = gerar_receitas_mes(mes, ano)

        self.assertEqual(criadas, 2)
        self.assertEqual(ReceitaAluguel.objects.count(), 2)

    def test_sem_contratos_ativos_retorna_zero(self):
        """Sem contratos ativos no período, retorna (0, 0)."""
        criadas, existiam = gerar_receitas_mes(1, 2000)
        self.assertEqual(criadas, 0)
        self.assertEqual(existiam, 0)


class ReceitaAluguelValidacaoTest(TestCase):
    """Testa clean() e save() de ReceitaAluguel."""

    def setUp(self):
        self.imovel, self.locatario = _criar_base()
        self.imovel2 = Imovel.objects.create(nome='Outro Imóvel', endereco='Rua C', cidade='SP', estado='SP')
        self.contrato = _criar_contrato(
            self.imovel, self.locatario,
            data_inicio=date(2024, 1, 1),
            data_fim=date(2024, 12, 31),
        )

    def test_save_preenche_imovel_automaticamente(self):
        """save() preenche imovel a partir do contrato quando não informado."""
        receita = ReceitaAluguel(
            contrato=self.contrato,
            competencia_mes=1,
            competencia_ano=2024,
            data_vencimento=date(2024, 1, 10),
            valor_previsto=Decimal('2000.00'),
        )
        receita.save()

        self.assertEqual(receita.imovel_id, self.imovel.pk)

    def test_clean_rejeita_imovel_diferente_do_contrato(self):
        """clean() levanta ValidationError quando imovel difere do contrato."""
        from django.core.exceptions import ValidationError

        receita = ReceitaAluguel(
            contrato=self.contrato,
            imovel=self.imovel2,
            competencia_mes=2,
            competencia_ano=2024,
            data_vencimento=date(2024, 2, 10),
            valor_previsto=Decimal('2000.00'),
        )

        with self.assertRaises(ValidationError):
            receita.clean()
