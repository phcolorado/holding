import tempfile
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from financeiro.models import ReceitaAluguel, Despesa
from patrimonio.models import Imovel, Pessoa, Contrato
from .models import ContaBancaria, ExtratoImportado, TransacaoExtrato, RegraClassificacao
from .services import (
    ExtratoJaImportadoError, OFXInvalidoError,
    importar_ofx, sugerir_receitas, sugerir_classificacao,
    conciliar_com_receitas, lancar_despesa,
)

MEDIA_TEMP = tempfile.mkdtemp(prefix='holding_test_extratos_')


def _ofx(transacoes, data_ref=None):
    """Gera um OFX SGML mínimo. transacoes = [(fitid, 'yyyymmdd', 'valor', 'memo'), ...]"""
    linhas = []
    for fitid, dtposted, valor, memo in transacoes:
        tipo = 'CREDIT' if not valor.startswith('-') else 'DEBIT'
        linhas.append(
            f'<STMTTRN><TRNTYPE>{tipo}<DTPOSTED>{dtposted}<TRNAMT>{valor}'
            f'<FITID>{fitid}<MEMO>{memo}</STMTTRN>'
        )
    corpo = '\n'.join(linhas)
    return f"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>
<DTSERVER>20260101000000<LANGUAGE>POR</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1
<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>BRL
<BANKACCTFROM><BANKID>0341<ACCTID>12345-6<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260101<DTEND>20261231
{corpo}
</BANKTRANLIST>
<LEDGERBAL><BALAMT>0.00<DTASOF>20261231</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""


def _arquivo_ofx(transacoes, nome='extrato.ofx'):
    return SimpleUploadedFile(nome, _ofx(transacoes).encode('cp1252'), content_type='application/x-ofx')


def _base():
    imovel = Imovel.objects.create(nome='Sala 101', endereco='Rua A', cidade='BH', estado='MG')
    locatario = Pessoa.objects.create(nome='Inquilino Um', tipo='locatario')
    return imovel, locatario


def _contrato(imovel, locatario, **kwargs):
    defaults = dict(
        data_inicio=date(2020, 1, 1), data_fim=date(2030, 12, 31),
        valor_aluguel=Decimal('2000.00'), dia_vencimento=10, status='ativo',
    )
    defaults.update(kwargs)
    return Contrato.objects.create(imovel=imovel, locatario=locatario, **defaults)


def _receita(contrato, vencimento, valor, mes=None, ano=None):
    return ReceitaAluguel.objects.create(
        contrato=contrato, imovel=contrato.imovel,
        competencia_mes=mes or vencimento.month, competencia_ano=ano or vencimento.year,
        data_vencimento=vencimento, valor_previsto=valor, status='previsto',
    )


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ImportarOFXTest(TestCase):
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta Teste')

    def test_importa_transacoes_e_periodo(self):
        arquivo = _arquivo_ofx([
            ('t1', '20260310', '2000.00', 'PIX RECEBIDO'),
            ('t2', '20260315', '-350.00', 'CEMIG ENERGIA'),
        ])
        extrato = importar_ofx(arquivo, self.conta)

        self.assertEqual(extrato.transacoes_novas, 2)
        self.assertEqual(extrato.transacoes_duplicadas, 0)
        self.assertEqual(extrato.periodo_inicio, date(2026, 3, 10))
        self.assertEqual(extrato.periodo_fim, date(2026, 3, 15))

        credito = TransacaoExtrato.objects.get(fitid='t1')
        self.assertEqual(credito.tipo, 'credito')
        self.assertEqual(credito.valor, Decimal('2000.00'))
        debito = TransacaoExtrato.objects.get(fitid='t2')
        self.assertEqual(debito.tipo, 'debito')
        self.assertEqual(debito.valor, Decimal('-350.00'))

    def test_mesmo_arquivo_rejeitado_por_hash(self):
        transacoes = [('t1', '20260310', '2000.00', 'PIX')]
        importar_ofx(_arquivo_ofx(transacoes), self.conta)
        with self.assertRaises(ExtratoJaImportadoError):
            importar_ofx(_arquivo_ofx(transacoes), self.conta)

    def test_fitid_repetido_em_extrato_sobreposto_nao_duplica(self):
        importar_ofx(_arquivo_ofx([('t1', '20260310', '2000.00', 'PIX')]), self.conta)
        # Segundo arquivo (conteúdo diferente → hash diferente) repete t1 e traz t2
        extrato2 = importar_ofx(_arquivo_ofx([
            ('t1', '20260310', '2000.00', 'PIX'),
            ('t2', '20260320', '-100.00', 'TARIFA'),
        ], nome='extrato2.ofx'), self.conta)

        self.assertEqual(extrato2.transacoes_novas, 1)
        self.assertEqual(extrato2.transacoes_duplicadas, 1)
        self.assertEqual(TransacaoExtrato.objects.filter(conta=self.conta).count(), 2)

    def test_arquivo_invalido_levanta_erro(self):
        arquivo = SimpleUploadedFile('extrato.ofx', b'isto nao e um ofx')
        with self.assertRaises(OFXInvalidoError):
            importar_ofx(arquivo, self.conta)


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class SugerirReceitasTest(TestCase):
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta Teste')
        self.imovel, self.locatario = _base()

    def _transacao(self, valor, data_tx=date(2026, 3, 12)):
        extrato = ExtratoImportado.objects.create(
            conta=self.conta, hash_arquivo=f'hash-{valor}-{data_tx}',
        )
        return TransacaoExtrato.objects.create(
            extrato=extrato, conta=self.conta, fitid=f'f-{valor}-{data_tx}',
            data=data_tx, valor=Decimal(valor), tipo='credito' if Decimal(valor) >= 0 else 'debito',
            descricao='REPASSE',
        )

    def test_match_exato_receita_unica(self):
        contrato = _contrato(self.imovel, self.locatario)
        receita = _receita(contrato, date(2026, 3, 10), Decimal('2000.00'))

        sugestao = sugerir_receitas(self._transacao('2000.00'))
        self.assertIsNotNone(sugestao)
        self.assertEqual(sugestao['tipo'], 'exata')
        self.assertEqual(sugestao['receitas'], [receita])

    def test_receita_fora_da_janela_nao_sugerida(self):
        contrato = _contrato(self.imovel, self.locatario)
        _receita(contrato, date(2026, 1, 10), Decimal('2000.00'))
        self.assertIsNone(sugerir_receitas(self._transacao('2000.00')))

    def test_repasse_consolidado_por_imobiliaria(self):
        """Um crédito de 10 mil casa com a soma de 3 aluguéis da mesma imobiliária."""
        imobiliaria = Pessoa.objects.create(nome='Imob Alfa', tipo='imobiliaria')
        valores = [Decimal('4000.00'), Decimal('3500.00'), Decimal('2500.00')]
        receitas = []
        for i, valor in enumerate(valores):
            im = Imovel.objects.create(nome=f'Loja {i}', endereco='X', cidade='BH', estado='MG')
            c = _contrato(im, self.locatario, valor_aluguel=valor, imobiliaria=imobiliaria)
            receitas.append(_receita(c, date(2026, 3, 10), valor))
        # Receita de contrato sem imobiliária não deve entrar no grupo
        outro = _contrato(self.imovel, self.locatario)
        _receita(outro, date(2026, 3, 10), Decimal('1000.00'))

        sugestao = sugerir_receitas(self._transacao('10000.00'))
        self.assertIsNotNone(sugestao)
        self.assertEqual(sugestao['tipo'], 'repasse')
        self.assertEqual(sorted(r.pk for r in sugestao['receitas']), sorted(r.pk for r in receitas))
        self.assertEqual(sugestao['total'], Decimal('10000.00'))

    def test_repasse_liquido_de_comissao(self):
        """Repasse já descontado da taxa de administração (10%)."""
        from financeiro.services import gerar_receitas_para_contrato

        imobiliaria = Pessoa.objects.create(nome='Imob Beta', tipo='imobiliaria')
        receitas = []
        for i, valor in enumerate([Decimal('6000.00'), Decimal('4000.00')]):
            im = Imovel.objects.create(nome=f'Sala {i}', endereco='X', cidade='BH', estado='MG')
            c = _contrato(
                im, self.locatario, valor_aluguel=valor, imobiliaria=imobiliaria,
                comissao_imobiliaria_percentual=Decimal('10.00'),
                data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
            )
            gerar_receitas_para_contrato(c, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
            receitas.append(ReceitaAluguel.objects.get(contrato=c))

        # 10.000 brutos − 1.000 de comissão = 9.000 líquidos
        sugestao = sugerir_receitas(self._transacao('9000.00'))
        self.assertIsNotNone(sugestao)
        self.assertEqual(sugestao['tipo'], 'repasse_liquido')
        self.assertEqual(len(sugestao['comissoes']), 2)
        self.assertEqual(sugestao['total'], Decimal('9000.00'))

    def test_debito_nao_gera_sugestao_de_receita(self):
        self.assertIsNone(sugerir_receitas(self._transacao('-500.00')))


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ConciliarTest(TestCase):
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta Teste')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='h1')
        self.imovel, self.locatario = _base()

    def _transacao(self, valor, data_tx=date(2026, 3, 12), tipo=None):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=f'f-{valor}',
            data=data_tx, valor=Decimal(valor),
            tipo=tipo or ('credito' if Decimal(valor) >= 0 else 'debito'),
            descricao='TESTE',
        )

    def test_conciliar_receita_unica_da_baixa(self):
        contrato = _contrato(self.imovel, self.locatario)
        receita = _receita(contrato, date(2026, 3, 10), Decimal('2000.00'))
        transacao = self._transacao('2000.00')

        conciliar_com_receitas(transacao, [receita])

        receita.refresh_from_db()
        transacao.refresh_from_db()
        self.assertEqual(receita.status, 'recebido')
        self.assertEqual(receita.valor_recebido, Decimal('2000.00'))
        self.assertEqual(receita.data_recebimento, date(2026, 3, 12))
        self.assertEqual(transacao.status, 'conciliada')
        item = transacao.itens_receita.get()
        self.assertEqual(item.valor_atribuido, Decimal('2000.00'))

    def test_conciliar_repasse_multiplas_receitas(self):
        c1 = _contrato(self.imovel, self.locatario, valor_aluguel=Decimal('4000.00'))
        im2 = Imovel.objects.create(nome='Sala 202', endereco='X', cidade='BH', estado='MG')
        c2 = _contrato(im2, self.locatario, valor_aluguel=Decimal('6000.00'))
        r1 = _receita(c1, date(2026, 3, 10), Decimal('4000.00'))
        r2 = _receita(c2, date(2026, 3, 10), Decimal('6000.00'))
        transacao = self._transacao('10000.00')

        conciliar_com_receitas(transacao, [r1, r2])

        r1.refresh_from_db(); r2.refresh_from_db()
        self.assertEqual(r1.status, 'recebido')
        self.assertEqual(r2.status, 'recebido')
        self.assertEqual(transacao.itens_receita.count(), 2)
        soma = sum(i.valor_atribuido for i in transacao.itens_receita.all())
        self.assertEqual(soma, Decimal('10000.00'))

    def test_conciliacao_parcial(self):
        contrato = _contrato(self.imovel, self.locatario)
        receita = _receita(contrato, date(2026, 3, 10), Decimal('2000.00'))
        transacao = self._transacao('1500.00')

        conciliar_com_receitas(transacao, [receita])

        receita.refresh_from_db()
        self.assertEqual(receita.status, 'parcial')
        self.assertEqual(receita.valor_recebido, Decimal('1500.00'))

    def test_repasse_liquido_marca_comissoes_pagas(self):
        from financeiro.services import gerar_receitas_para_contrato

        imobiliaria = Pessoa.objects.create(nome='Imob Gama', tipo='imobiliaria')
        c = _contrato(
            self.imovel, self.locatario, valor_aluguel=Decimal('2000.00'),
            imobiliaria=imobiliaria, comissao_imobiliaria_percentual=Decimal('10.00'),
            data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
        )
        gerar_receitas_para_contrato(c, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        receita = ReceitaAluguel.objects.get(contrato=c)
        comissao = Despesa.objects.get(contrato=c, origem_automatica=True)
        transacao = self._transacao('1800.00')  # líquido

        conciliar_com_receitas(transacao, [receita], marcar_comissoes=True)

        receita.refresh_from_db(); comissao.refresh_from_db()
        # Inquilino pagou integral — receita recebida pelo valor bruto
        self.assertEqual(receita.status, 'recebido')
        self.assertEqual(receita.valor_recebido, Decimal('2000.00'))
        # Comissão retida pela imobiliária vira despesa paga
        self.assertEqual(comissao.status, 'paga')
        self.assertEqual(comissao.data_pagamento, date(2026, 3, 12))

    def test_lancar_despesa_de_debito(self):
        fornecedor = Pessoa.objects.create(nome='CEMIG', tipo='fornecedor')
        transacao = self._transacao('-350.00')

        despesa = lancar_despesa(
            transacao, categoria='outro', descricao='Energia elétrica',
            fornecedor=fornecedor, imovel=self.imovel,
        )

        transacao.refresh_from_db()
        self.assertEqual(transacao.status, 'conciliada')
        self.assertEqual(transacao.despesa, despesa)
        self.assertEqual(despesa.status, 'paga')
        self.assertEqual(despesa.valor, Decimal('350.00'))
        self.assertEqual(despesa.competencia_mes, 3)
        self.assertEqual(despesa.data_pagamento, date(2026, 3, 12))

    def test_regra_classificacao_sugerida(self):
        regra_generica = RegraClassificacao.objects.create(
            texto_contem='TARIFA', categoria='taxa_bancaria', prioridade=50,
        )
        RegraClassificacao.objects.create(
            texto_contem='CEMIG', categoria='outro', prioridade=10, ativo=False,
        )
        transacao = self._transacao('-25.00')
        transacao.descricao = 'TARIFA MANUTENCAO CONTA'
        transacao.save()

        self.assertEqual(sugerir_classificacao(transacao), regra_generica)

    def test_regra_inativa_nao_aplica(self):
        RegraClassificacao.objects.create(texto_contem='TESTE', categoria='outro', ativo=False)
        transacao = self._transacao('-25.00')
        self.assertIsNone(sugerir_classificacao(transacao))


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ConciliacaoViewsTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user('conc_user', password='pass')
        perms = Permission.objects.filter(codename__in=[
            'add_extratoimportado', 'change_transacaoextrato',
            'change_receitaaluguel', 'add_despesa',
        ])
        self.user.user_permissions.add(*perms)
        self.client.login(username='conc_user', password='pass')
        self.conta = ContaBancaria.objects.create(nome='Conta Teste')
        self.imovel, self.locatario = _base()

    def test_lista_exige_login(self):
        self.client.logout()
        response = self.client.get(reverse('extrato_list'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_upload_ofx_cria_extrato_e_redireciona(self):
        arquivo = _arquivo_ofx([('t1', '20260310', '2000.00', 'PIX')])
        response = self.client.post(reverse('extrato_list'), {'conta': self.conta.pk, 'arquivo': arquivo})
        extrato = ExtratoImportado.objects.get()
        self.assertRedirects(response, reverse('conciliar_extrato', args=[extrato.pk]))
        self.assertEqual(extrato.transacoes_novas, 1)

    def test_upload_sem_permissao_403(self):
        User.objects.create_user('sem_perm_conc', password='pass')
        self.client.login(username='sem_perm_conc', password='pass')
        arquivo = _arquivo_ofx([('t1', '20260310', '2000.00', 'PIX')])
        response = self.client.post(reverse('extrato_list'), {'conta': self.conta.pk, 'arquivo': arquivo})
        self.assertEqual(response.status_code, 403)

    def test_upload_arquivo_nao_ofx_mostra_erro(self):
        arquivo = SimpleUploadedFile('extrato.txt', b'qualquer coisa')
        response = self.client.post(
            reverse('extrato_list'), {'conta': self.conta.pk, 'arquivo': arquivo}, follow=True
        )
        self.assertEqual(ExtratoImportado.objects.count(), 0)
        mensagens = [str(m) for m in response.context['messages']]
        self.assertTrue(any('OFX' in m for m in mensagens))

    def test_conciliar_via_view(self):
        contrato = _contrato(self.imovel, self.locatario)
        receita = _receita(contrato, date(2026, 3, 10), Decimal('2000.00'))
        extrato = importar_ofx(_arquivo_ofx([('t1', '20260312', '2000.00', 'PIX')]), self.conta)
        transacao = extrato.transacoes.get()

        response = self.client.post(reverse('conciliar_extrato', args=[extrato.pk]), {
            'transacao_id': transacao.pk, 'action': 'conciliar', 'receita_ids': [receita.pk],
        })
        self.assertEqual(response.status_code, 302)
        receita.refresh_from_db()
        self.assertEqual(receita.status, 'recebido')

    def test_lancar_despesa_via_view(self):
        extrato = importar_ofx(_arquivo_ofx([('t1', '20260312', '-350.00', 'CEMIG')]), self.conta)
        transacao = extrato.transacoes.get()

        response = self.client.post(reverse('conciliar_extrato', args=[extrato.pk]), {
            'transacao_id': transacao.pk, 'action': 'lancar_despesa',
            'categoria': 'outro', 'descricao': 'Energia', 'imovel': self.imovel.pk,
        })
        self.assertEqual(response.status_code, 302)
        transacao.refresh_from_db()
        self.assertEqual(transacao.status, 'conciliada')
        self.assertIsNotNone(transacao.despesa)

    def test_ignorar_e_reabrir(self):
        extrato = importar_ofx(_arquivo_ofx([('t1', '20260312', '-10.00', 'TARIFA')]), self.conta)
        transacao = extrato.transacoes.get()

        self.client.post(reverse('conciliar_extrato', args=[extrato.pk]),
                         {'transacao_id': transacao.pk, 'action': 'ignorar'})
        transacao.refresh_from_db()
        self.assertEqual(transacao.status, 'ignorada')

        self.client.post(reverse('conciliar_extrato', args=[extrato.pk]),
                         {'transacao_id': transacao.pk, 'action': 'reabrir'})
        transacao.refresh_from_db()
        self.assertEqual(transacao.status, 'pendente')

    def test_tela_conciliar_mostra_sugestao(self):
        contrato = _contrato(self.imovel, self.locatario)
        _receita(contrato, date(2026, 3, 10), Decimal('2000.00'))
        extrato = importar_ofx(_arquivo_ofx([('t1', '20260312', '2000.00', 'PIX')]), self.conta)

        response = self.client.get(reverse('conciliar_extrato', args=[extrato.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'valor exato')
        self.assertContains(response, 'Sala 101')

    def test_checklist_inclui_extrato(self):
        importar_ofx(
            _arquivo_ofx([(f't-{timezone.localdate()}', timezone.localdate().strftime('%Y%m%d'), '99.00', 'PIX')]),
            self.conta,
        )
        response = self.client.get(reverse('checklist_mensal'))
        itens = {item['item']: item for item in response.context['checklist']}
        item = itens['Extrato bancário conciliado']
        self.assertFalse(item['ok'])
