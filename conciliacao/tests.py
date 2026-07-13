import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User, Permission
from core.test_utils import com_leitura
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, Client, override_settings
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
        self.user = com_leitura(User.objects.create_user('conc_user', password='pass'))
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
        com_leitura(User.objects.create_user('sem_perm_conc', password='pass'))
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


# ─── Conciliação por saldo, validações e desfazer (rodada de correções) ───────

def _ofx_conta(transacoes, bankid='0341', acctid='12345-6', nome='extrato.ofx'):
    corpo = '\n'.join(
        f'<STMTTRN><TRNTYPE>{"CREDIT" if not v.startswith("-") else "DEBIT"}'
        f'<DTPOSTED>{d}<TRNAMT>{v}<FITID>{f}<MEMO>{m}</STMTTRN>'
        for f, d, v, m in transacoes
    )
    conteudo = f"""OFXHEADER:100
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
<DTSERVER>20260101<LANGUAGE>POR</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1
<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>BRL
<BANKACCTFROM><BANKID>{bankid}<ACCTID>{acctid}<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260101<DTEND>20261231
{corpo}
</BANKTRANLIST>
<LEDGERBAL><BALAMT>0.00<DTASOF>20261231</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""
    return SimpleUploadedFile(nome, conteudo.encode('cp1252'))


def _ofx_duas_contas(nome='duascontas.ofx'):
    bloco = """<STMTTRNRS><TRNUID>{n}
<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>BRL
<BANKACCTFROM><BANKID>0341<ACCTID>{acct}<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260101<DTEND>20261231
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260310<TRNAMT>100.00<FITID>x{n}<MEMO>PIX</STMTTRN>
</BANKTRANLIST>
<LEDGERBAL><BALAMT>0.00<DTASOF>20261231</LEDGERBAL>
</STMTRS></STMTTRNRS>"""
    conteudo = f"""OFXHEADER:100
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
<DTSERVER>20260101<LANGUAGE>POR</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1>{bloco.format(n=1, acct='11111-1')}
{bloco.format(n=2, acct='22222-2')}</BANKMSGSRSV1>
</OFX>
"""
    return SimpleUploadedFile(nome, conteudo.encode('cp1252'))


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ConciliacaoSaldoTest(TestCase):
    def setUp(self):
        from financeiro.models import RecebimentoReceita
        self.RecebimentoReceita = RecebimentoReceita
        self.conta = ContaBancaria.objects.create(nome='Conta Teste')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hsaldo')
        self.imovel, self.locatario = _base()
        self.contrato = _contrato(self.imovel, self.locatario)
        self.receita = _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))

    def _tx(self, valor, fitid=None):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid or f'f{valor}',
            data=date(2026, 3, 12), valor=Decimal(valor),
            tipo='credito' if Decimal(valor) >= 0 else 'debito', descricao='PIX',
        )

    def test_duas_conciliacoes_sucessivas_somam(self):
        conciliar_com_receitas(self._tx('1000.00', 'a'), [self.receita])
        self.receita.refresh_from_db()
        self.assertEqual(self.receita.status, 'parcial')
        self.assertEqual(self.receita.saldo_em_aberto, Decimal('1000.00'))

        conciliar_com_receitas(self._tx('1000.00', 'b'), [self.receita])
        self.receita.refresh_from_db()
        self.assertEqual(self.receita.status, 'recebido')
        self.assertEqual(self.receita.valor_recebido, Decimal('2000.00'))
        self.assertEqual(self.receita.recebimentos.count(), 2)

    def test_credito_maior_que_saldo_selecionado_rejeitado(self):
        from .services import ConciliacaoInvalidaError
        tx = self._tx('2500.00')
        with self.assertRaises(ConciliacaoInvalidaError):
            conciliar_com_receitas(tx, [self.receita])
        tx.refresh_from_db()
        self.receita.refresh_from_db()
        # operação atômica: nada foi gravado
        self.assertEqual(tx.status, 'pendente')
        self.assertIsNone(self.receita.valor_recebido)
        self.assertEqual(self.receita.recebimentos.count(), 0)

    def test_transacao_ja_conciliada_nao_processa_de_novo(self):
        from .services import ConciliacaoInvalidaError
        tx = self._tx('2000.00')
        conciliar_com_receitas(tx, [self.receita])
        outra = _receita(self.contrato, date(2026, 3, 20), Decimal('500.00'), mes=4)
        with self.assertRaises(ConciliacaoInvalidaError):
            conciliar_com_receitas(tx, [outra])

    def test_receita_quitada_rejeitada(self):
        from .services import ConciliacaoInvalidaError
        conciliar_com_receitas(self._tx('2000.00', 'a'), [self.receita])
        self.receita.refresh_from_db()
        with self.assertRaises(ConciliacaoInvalidaError):
            conciliar_com_receitas(self._tx('100.00', 'b'), [self.receita])

    def test_repasse_liquido_invalido_rejeitado(self):
        from .services import ConciliacaoInvalidaError
        # sem comissões vinculadas, marcar_comissoes é seleção arbitrária → erro
        with self.assertRaises(ConciliacaoInvalidaError):
            conciliar_com_receitas(self._tx('2000.00'), [self.receita], marcar_comissoes=True)

    def test_repasse_liquido_com_valor_errado_rejeitado(self):
        from django.core.exceptions import ObjectDoesNotExist
        from .services import ConciliacaoInvalidaError
        from financeiro.services import gerar_receitas_para_contrato

        imob = Pessoa.objects.create(nome='Imob L', tipo='imobiliaria')
        c = _contrato(
            Imovel.objects.create(nome='Sala L', endereco='X', cidade='BH', estado='MG'),
            self.locatario, valor_aluguel=Decimal('2000.00'), imobiliaria=imob,
            comissao_imobiliaria_percentual=Decimal('10.00'),
            data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
        )
        gerar_receitas_para_contrato(c, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        receita = ReceitaAluguel.objects.get(contrato=c)
        # líquido correto seria 1800; 1700 deve ser rejeitado
        with self.assertRaises(ConciliacaoInvalidaError):
            conciliar_com_receitas(self._tx('1700.00'), [receita], marcar_comissoes=True)

    def test_desfazer_restaura_receitas_e_comissoes(self):
        from .services import desfazer_conciliacao
        from financeiro.services import gerar_receitas_para_contrato

        imob = Pessoa.objects.create(nome='Imob D', tipo='imobiliaria')
        c = _contrato(
            Imovel.objects.create(nome='Sala D', endereco='X', cidade='BH', estado='MG'),
            self.locatario, valor_aluguel=Decimal('2000.00'), imobiliaria=imob,
            comissao_imobiliaria_percentual=Decimal('10.00'),
            data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
        )
        gerar_receitas_para_contrato(c, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        receita = ReceitaAluguel.objects.get(contrato=c)
        comissao = Despesa.objects.get(contrato=c, origem_automatica=True)

        tx = self._tx('1800.00')
        conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        receita.refresh_from_db(); comissao.refresh_from_db(); tx.refresh_from_db()
        self.assertEqual(receita.status, 'recebido')
        self.assertEqual(comissao.status, 'paga')
        self.assertEqual(tx.status, 'conciliada')

        desfazer_conciliacao(tx)
        receita.refresh_from_db(); comissao.refresh_from_db(); tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertFalse(tx.comissoes_marcadas)
        self.assertEqual(tx.itens_receita.count(), 0)
        self.assertIsNone(receita.valor_recebido)
        self.assertIn(receita.status, ('previsto', 'atrasado'))
        self.assertEqual(comissao.status, 'prevista')
        self.assertIsNone(comissao.data_pagamento)

    def test_desfazer_via_view_exige_permissao(self):
        tx = self._tx('2000.00')
        conciliar_com_receitas(tx, [self.receita])
        user = com_leitura(User.objects.create_user('sem_desfazer', password='pass'))
        client = Client()
        client.login(username='sem_desfazer', password='pass')
        resposta = client.post(reverse('conciliar_extrato', args=[self.extrato.pk]), {
            'transacao_id': tx.pk, 'action': 'desfazer',
        })
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ConciliacaoExataDeCentavosTest(TestCase):
    """
    Item 8: a conciliação exige igualdade EXATA entre a soma atribuída e o
    crédito — nenhuma tolerância de centavos é aceita no commit (a
    tolerância de R$0,05 continua existindo só na SUGESTÃO, não na gravação).
    """

    def setUp(self):
        from .services import ConciliacaoInvalidaError
        self.ConciliacaoInvalidaError = ConciliacaoInvalidaError
        self.conta = ContaBancaria.objects.create(nome='Conta Centavos')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hcentavos')
        self.imovel, self.locatario = _base()
        self.contrato = _contrato(self.imovel, self.locatario)
        self.receita = _receita(self.contrato, date(2026, 3, 10), Decimal('1000.00'))

    def _tx(self, valor, fitid):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal(valor), tipo='credito', descricao='PIX',
        )

    # Nos 4 testes abaixo o crédito é MAIOR que o saldo da receita (único
    # jeito de sobrar dinheiro sem destino) — a diferença é o excedente que
    # antes ficava silenciosamente "perdido" (tolerado até R$0,05).

    def test_diferenca_de_1_centavo_rejeitada(self):
        tx = self._tx('1000.01', 'c1')
        with self.assertRaises(self.ConciliacaoInvalidaError):
            conciliar_com_receitas(tx, [self.receita])
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')

    def test_diferenca_de_4_centavos_rejeitada(self):
        tx = self._tx('1000.04', 'c4')
        with self.assertRaises(self.ConciliacaoInvalidaError):
            conciliar_com_receitas(tx, [self.receita])

    def test_diferenca_de_5_centavos_rejeitada(self):
        """Antes da correção, R$0,05 era exatamente o limite tolerado — agora é rejeitado."""
        tx = self._tx('1000.05', 'c5')
        with self.assertRaises(self.ConciliacaoInvalidaError):
            conciliar_com_receitas(tx, [self.receita])

    def test_diferenca_de_6_centavos_rejeitada(self):
        tx = self._tx('1000.06', 'c6')
        with self.assertRaises(self.ConciliacaoInvalidaError):
            conciliar_com_receitas(tx, [self.receita])

    def test_igualdade_exata_aceita(self):
        tx = self._tx('1000.00', 'c0')
        conciliar_com_receitas(tx, [self.receita])
        tx.refresh_from_db()
        self.receita.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(self.receita.status, 'recebido')
        self.assertEqual(self.receita.saldo_em_aberto, Decimal('0.00'))

    def test_repasse_liquido_com_diferenca_de_1_centavo_rejeitado(self):
        from financeiro.services import gerar_receitas_para_contrato
        imob = Pessoa.objects.create(nome='Imob Centavo', tipo='imobiliaria')
        c = _contrato(
            Imovel.objects.create(nome='Sala Centavo', endereco='X', cidade='BH', estado='MG'),
            self.locatario, valor_aluguel=Decimal('2000.00'), imobiliaria=imob,
            comissao_imobiliaria_percentual=Decimal('10.00'),
            data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
        )
        gerar_receitas_para_contrato(c, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        receita = ReceitaAluguel.objects.get(contrato=c)
        # líquido correto seria 1800.00; 1799.99 (1 centavo a menos) deve ser rejeitado
        tx = self._tx('1799.99', 'rl1')
        with self.assertRaises(self.ConciliacaoInvalidaError):
            conciliar_com_receitas(tx, [receita], marcar_comissoes=True)


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ImportarOFXContasTest(TestCase):
    """Validações de conta e FITID na importação (item 14)."""

    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta X')

    def test_arquivo_com_multiplas_contas_rejeitado(self):
        with self.assertRaises(OFXInvalidoError) as ctx:
            importar_ofx(_ofx_duas_contas(), self.conta)
        self.assertIn('2 contas', str(ctx.exception))
        self.assertEqual(ExtratoImportado.objects.count(), 0)

    def test_conta_divergente_rejeitada(self):
        conta = ContaBancaria.objects.create(nome='Conta Y', numero_conta='99999-9')
        with self.assertRaises(OFXInvalidoError) as ctx:
            importar_ofx(_ofx_conta([('t1', '20260310', '100.00', 'PIX')]), conta)
        self.assertIn('não corresponde', str(ctx.exception))

    def test_bank_id_divergente_rejeitado(self):
        conta = ContaBancaria.objects.create(nome='Conta Z', bank_id='0237')
        with self.assertRaises(OFXInvalidoError):
            importar_ofx(_ofx_conta([('t1', '20260310', '100.00', 'PIX')], nome='z.ofx'), conta)

    def test_conta_correspondente_aceita(self):
        conta = ContaBancaria.objects.create(nome='Conta OK', bank_id='0341', numero_conta='12345-6')
        extrato = importar_ofx(_ofx_conta([('t1', '20260310', '100.00', 'PIX')], nome='ok.ofx'), conta)
        self.assertEqual(extrato.transacoes_novas, 1)

    def test_transacao_sem_fitid_ganha_identificador_deterministico(self):
        arq = _ofx_conta([('', '20260310', '150.00', 'SEM FITID')], nome='semfitid.ofx')
        extrato = importar_ofx(arq, self.conta)
        self.assertEqual(extrato.transacoes_novas, 1)
        tx = extrato.transacoes.get()
        self.assertTrue(tx.fitid.startswith('gerado-'))
        # reimportar arquivo com a mesma transação (nome/hash diferentes) não duplica
        arq2 = _ofx_conta([('', '20260310', '150.00', 'SEM FITID'),
                           ('t2', '20260311', '10.00', 'OUTRA')], nome='semfitid2.ofx')
        extrato2 = importar_ofx(arq2, self.conta)
        self.assertEqual(extrato2.transacoes_duplicadas, 1)
        self.assertEqual(extrato2.transacoes_novas, 1)

    def test_mesmo_fitid_em_contas_diferentes_permitido(self):
        conta2 = ContaBancaria.objects.create(nome='Conta 2')
        importar_ofx(_ofx_conta([('t1', '20260310', '100.00', 'PIX')], nome='c1.ofx'), self.conta)
        # arquivo da outra conta tem ACCTID diferente (conteúdo/hash diferentes)
        extrato2 = importar_ofx(
            _ofx_conta([('t1', '20260310', '100.00', 'PIX')], acctid='22222-2', nome='c2.ofx'),
            conta2,
        )
        self.assertEqual(extrato2.transacoes_novas, 1)
        self.assertEqual(TransacaoExtrato.objects.filter(fitid='t1').count(), 2)


# ─── Repasse líquido rigoroso e vínculo explícito de comissões (rodada 2) ──────

@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class RepasseLiquidoRigorosoTest(TestCase):
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta RL')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hrl')
        _, self.locatario = _base()

    def _tx(self, valor, fitid):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal(valor), tipo='credito', descricao='REPASSE',
        )

    def _contrato_com_imob(self, nome, imob, aluguel='2000.00', pct='10.00'):
        from financeiro.services import gerar_receitas_para_contrato
        c = _contrato(
            Imovel.objects.create(nome=nome, endereco='X', cidade='BH', estado='MG'),
            self.locatario, valor_aluguel=Decimal(aluguel), imobiliaria=imob,
            comissao_imobiliaria_percentual=Decimal(pct) if pct else None,
            data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
        )
        gerar_receitas_para_contrato(c, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        receita = ReceitaAluguel.objects.get(contrato=c)
        comissao = Despesa.objects.filter(contrato=c, origem_automatica=True).first()
        return c, receita, comissao

    def test_receitas_da_mesma_imobiliaria_aceitas(self):
        from .models import ConciliacaoComissao
        imob = Pessoa.objects.create(nome='Imob Única', tipo='imobiliaria')
        _, r1, c1 = self._contrato_com_imob('Sala RL1', imob)
        _, r2, c2 = self._contrato_com_imob('Sala RL2', imob)
        tx = self._tx('3600.00', 'rl-ok')  # 2×(2000 − 200)
        conciliar_com_receitas(tx, [r1, r2], marcar_comissoes=True)
        r1.refresh_from_db(); r2.refresh_from_db(); c1.refresh_from_db(); c2.refresh_from_db()
        self.assertEqual(r1.status, 'recebido')
        self.assertEqual(r2.status, 'recebido')
        self.assertEqual(c1.status, 'paga')
        self.assertEqual(c2.status, 'paga')
        # vínculo explícito criado para cada comissão marcada
        vinculos = ConciliacaoComissao.objects.filter(transacao=tx)
        self.assertEqual(vinculos.count(), 2)
        self.assertEqual(
            sorted(v.despesa_id for v in vinculos), sorted([c1.pk, c2.pk])
        )

    def test_imobiliarias_diferentes_rejeitadas_mesmo_com_soma_correta(self):
        from .services import ConciliacaoInvalidaError
        imob_a = Pessoa.objects.create(nome='Imob A', tipo='imobiliaria')
        imob_b = Pessoa.objects.create(nome='Imob B', tipo='imobiliaria')
        _, r1, _ = self._contrato_com_imob('Sala RA', imob_a)
        _, r2, _ = self._contrato_com_imob('Sala RB', imob_b)
        tx = self._tx('3600.00', 'rl-mix')  # soma bate, mas imobiliárias diferem
        with self.assertRaises(ConciliacaoInvalidaError) as ctx:
            conciliar_com_receitas(tx, [r1, r2], marcar_comissoes=True)
        self.assertIn('imobiliárias diferentes', str(ctx.exception))
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')

    def test_receita_sem_imobiliaria_rejeitada_no_liquido(self):
        from .services import ConciliacaoInvalidaError
        _, receita, _ = self._contrato_com_imob('Sala SI', None, pct=None)
        tx = self._tx('2000.00', 'rl-semimob')
        with self.assertRaises(ConciliacaoInvalidaError) as ctx:
            conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        self.assertIn('não tem imobiliária', str(ctx.exception))

    def test_comissao_maior_ou_igual_ao_saldo_rejeitada(self):
        from .services import ConciliacaoInvalidaError
        imob = Pessoa.objects.create(nome='Imob C100', tipo='imobiliaria')
        _, receita, comissao = self._contrato_com_imob('Sala C100', imob)
        Despesa.objects.filter(pk=comissao.pk).update(valor=Decimal('2000.00'))
        tx = self._tx('1.00', 'rl-c100')
        with self.assertRaises(ConciliacaoInvalidaError) as ctx:
            conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        self.assertIn('maior ou igual ao saldo', str(ctx.exception))

    def test_comissao_manual_nao_vinculada_nao_e_usada(self):
        from .services import ConciliacaoInvalidaError
        imob = Pessoa.objects.create(nome='Imob Manual', tipo='imobiliaria')
        _, receita, _ = self._contrato_com_imob('Sala Man', imob, pct=None)
        # comissão manual (origem_automatica=False) vinculada à receita: ignorada
        Despesa.objects.create(
            imovel=receita.imovel, receita=receita, categoria='comissao_imobiliaria',
            descricao='Comissão manual', data_vencimento=date(2026, 3, 12),
            valor=Decimal('200.00'), status='prevista', origem_automatica=False,
        )
        tx = self._tx('1800.00', 'rl-man')
        with self.assertRaises(ConciliacaoInvalidaError) as ctx:
            conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        self.assertIn('não há despesas de comissão em aberto', str(ctx.exception))

    def test_desfazer_reabre_somente_comissoes_vinculadas(self):
        from .services import desfazer_conciliacao
        imob = Pessoa.objects.create(nome='Imob DV', tipo='imobiliaria')
        _, receita, comissao = self._contrato_com_imob('Sala DV', imob)
        # outra comissão da MESMA receita, paga MANUALMENTE na mesma data da
        # transação — a heurística antiga (receita+categoria+status+data) a
        # reabriria por engano; o vínculo explícito não a toca.
        paga_manual = Despesa.objects.create(
            imovel=receita.imovel, receita=receita, contrato=receita.contrato,
            categoria='comissao_imobiliaria', descricao='Comissão avulsa paga à parte',
            data_vencimento=date(2026, 3, 12), data_pagamento=date(2026, 3, 12),
            valor=Decimal('50.00'), status='paga', origem_automatica=True,
        )
        tx = self._tx('1800.00', 'rl-dv')
        conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        desfazer_conciliacao(tx)
        comissao.refresh_from_db(); paga_manual.refresh_from_db(); tx.refresh_from_db()
        self.assertEqual(comissao.status, 'prevista')
        self.assertIsNone(comissao.data_pagamento)
        # a comissão paga manualmente na mesma data permanece intocada
        self.assertEqual(paga_manual.status, 'paga')
        self.assertEqual(paga_manual.data_pagamento, date(2026, 3, 12))
        self.assertEqual(tx.itens_comissao.count(), 0)
        self.assertEqual(tx.status, 'pendente')

    def test_comissao_fornecedor_diferente_da_imobiliaria_rejeitada(self):
        """
        Item 9: contrato cuja imobiliária foi trocada DEPOIS da despesa de
        comissão ter sido gerada — a comissão automática guardou o fornecedor
        antigo, que já não corresponde à imobiliária atual do contrato.
        """
        from .services import ConciliacaoInvalidaError
        imob_antiga = Pessoa.objects.create(nome='Imob Antiga', tipo='imobiliaria')
        imob_nova = Pessoa.objects.create(nome='Imob Nova', tipo='imobiliaria')
        contrato, receita, comissao = self._contrato_com_imob('Sala Troca', imob_antiga)
        self.assertEqual(comissao.fornecedor_id, imob_antiga.pk)

        # Imobiliária do contrato é trocada depois — a despesa já gerada
        # mantém o fornecedor antigo (não há backfill retroativo automático).
        contrato.imobiliaria = imob_nova
        contrato.save()
        receita.refresh_from_db()

        tx = self._tx('1800.00', 'rl-troca')
        with self.assertRaises(ConciliacaoInvalidaError) as ctx:
            conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        self.assertIn('fornecedor diferente', str(ctx.exception))
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')

    def test_comissao_sem_fornecedor_legado_aceita_compativel(self):
        """Comissão antiga (dado legado) sem fornecedor preenchido é aceita por compatibilidade."""
        imob = Pessoa.objects.create(nome='Imob Legado F', tipo='imobiliaria')
        _, receita, comissao = self._contrato_com_imob('Sala LegF', imob)
        Despesa.objects.filter(pk=comissao.pk).update(fornecedor=None)

        tx = self._tx('1800.00', 'rl-legf')
        conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        receita.refresh_from_db()
        self.assertEqual(receita.status, 'recebido')

    def test_desfazer_conciliacao_antiga_sem_vinculo_exige_revisao_manual(self):
        from .models import ConciliacaoReceita
        from .services import ConciliacaoInvalidaError, desfazer_conciliacao
        from financeiro.models import RecebimentoReceita
        imob = Pessoa.objects.create(nome='Imob Legada', tipo='imobiliaria')
        _, receita, comissao = self._contrato_com_imob('Sala Leg', imob)
        # simula conciliação ANTIGA: sem ConciliacaoComissao
        tx = self._tx('1800.00', 'rl-leg')
        ConciliacaoReceita.objects.create(transacao=tx, receita=receita, valor_atribuido=Decimal('1800.00'))
        RecebimentoReceita.objects.create(
            receita=receita, data_recebimento=tx.data, valor=Decimal('2000.00'),
            transacao_extrato=tx, origem='conciliacao',
        )
        Despesa.objects.filter(pk=comissao.pk).update(status='paga', data_pagamento=tx.data)
        tx.comissoes_marcadas = True
        tx.status = 'conciliada'
        tx.save()

        with self.assertRaises(ConciliacaoInvalidaError) as ctx:
            desfazer_conciliacao(tx)
        self.assertIn('Revisão manual', str(ctx.exception))

        # após reabrir manualmente a comissão, o desfazer prossegue
        Despesa.objects.filter(pk=comissao.pk).update(status='prevista', data_pagamento=None)
        desfazer_conciliacao(tx)
        tx.refresh_from_db(); receita.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertEqual(receita.recebimentos.count(), 0)


class ConciliacaoComissaoProtectTest(TestCase):
    """Item 14: despesa vinculada por ConciliacaoComissao não pode ser apagada em CASCADE."""

    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta CCP')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hccp')
        _, self.locatario = _base()

    def _tx(self, valor, fitid):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal(valor), tipo='credito', descricao='REPASSE',
        )

    def _receita_com_comissao(self, imob):
        from financeiro.services import gerar_receitas_para_contrato
        c = _contrato(
            Imovel.objects.create(nome='Sala CCP', endereco='X', cidade='BH', estado='MG'),
            self.locatario, valor_aluguel=Decimal('2000.00'), imobiliaria=imob,
            comissao_imobiliaria_percentual=Decimal('10.00'),
            data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
        )
        gerar_receitas_para_contrato(c, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        receita = ReceitaAluguel.objects.get(contrato=c)
        comissao = Despesa.objects.filter(contrato=c, origem_automatica=True).first()
        return receita, comissao

    def test_despesa_vinculada_nao_pode_ser_apagada(self):
        from .models import ConciliacaoComissao
        imob = Pessoa.objects.create(nome='Imob CCP', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        tx = self._tx('1800.00', 'ccp-1')
        conciliar_com_receitas(tx, [receita], marcar_comissoes=True)

        self.assertTrue(ConciliacaoComissao.objects.filter(despesa=comissao).exists())
        # Despesa.delete() (item 4, rodada final) bloqueia ANTES de chegar ao
        # PROTECT do banco — mensagem mais específica orientando a desfazer
        # a conciliação; o PROTECT continua como defesa em profundidade.
        with self.assertRaises(ValidationError):
            comissao.delete()
        # a despesa e o vínculo continuam intactos
        self.assertTrue(Despesa.objects.filter(pk=comissao.pk).exists())
        self.assertTrue(ConciliacaoComissao.objects.filter(despesa=comissao).exists())

    def test_apos_desfazer_conciliacao_despesa_pode_ser_apagada_normalmente(self):
        from .models import ConciliacaoComissao
        from .services import desfazer_conciliacao
        imob = Pessoa.objects.create(nome='Imob CCP2', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        tx = self._tx('1800.00', 'ccp-2')
        conciliar_com_receitas(tx, [receita], marcar_comissoes=True)

        desfazer_conciliacao(tx)
        self.assertFalse(ConciliacaoComissao.objects.filter(despesa=comissao).exists())
        comissao.delete()  # não deve levantar — vínculo já removido pelo desfazer
        self.assertFalse(Despesa.objects.filter(pk=comissao.pk).exists())


class DespesaConciliacaoAtivaTest(TestCase):
    """
    Item 4 (rodada final): despesas vinculadas a uma conciliação bancária
    ATIVA (comissão de repasse líquido OU débito lançado do extrato) ficam
    protegidas contra reabertura/cancelamento/alteração/exclusão — a única
    forma de voltar a editá-las é desfazer a conciliação correspondente.
    """
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta DCA')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hdca')
        self.imovel, self.locatario = _base()

    def _tx(self, valor, fitid, tipo='credito', descricao='TX'):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal(valor), tipo=tipo, descricao=descricao,
        )

    def _receita_com_comissao(self, imob):
        from financeiro.services import gerar_receitas_para_contrato
        c = _contrato(
            self.imovel, self.locatario, imobiliaria=imob,
            comissao_imobiliaria_percentual=Decimal('10.00'),
            data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
        )
        gerar_receitas_para_contrato(c, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        receita = ReceitaAluguel.objects.get(contrato=c)
        comissao = Despesa.objects.filter(contrato=c, origem_automatica=True).first()
        return receita, comissao

    def test_comissao_conciliada_nao_pode_ser_reaberta(self):
        from financeiro.services import reabrir_despesa
        imob = Pessoa.objects.create(nome='Imob DCA1', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        conciliar_com_receitas(self._tx('1800.00', 'dca-1'), [receita], marcar_comissoes=True)
        comissao.refresh_from_db()
        with self.assertRaises(ValidationError):
            reabrir_despesa(comissao.pk)
        comissao.refresh_from_db()
        self.assertEqual(comissao.status, 'paga')

    def test_comissao_conciliada_nao_pode_ser_cancelada(self):
        from financeiro.services import cancelar_despesa
        imob = Pessoa.objects.create(nome='Imob DCA2', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        conciliar_com_receitas(self._tx('1800.00', 'dca-2'), [receita], marcar_comissoes=True)
        comissao.refresh_from_db()
        with self.assertRaises(ValidationError):
            cancelar_despesa(comissao.pk)
        comissao.refresh_from_db()
        self.assertEqual(comissao.status, 'paga')

    def test_comissao_conciliada_nao_pode_ter_valor_alterado(self):
        imob = Pessoa.objects.create(nome='Imob DCA3', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        conciliar_com_receitas(self._tx('1800.00', 'dca-3'), [receita], marcar_comissoes=True)
        comissao.refresh_from_db()
        valor_original = comissao.valor
        comissao.valor = valor_original + Decimal('1.00')
        with self.assertRaises(ValidationError):
            comissao.save()
        comissao.refresh_from_db()
        self.assertEqual(comissao.valor, valor_original)

    def test_comissao_conciliada_nao_pode_ter_categoria_alterada(self):
        imob = Pessoa.objects.create(nome='Imob DCA3b', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        conciliar_com_receitas(self._tx('1800.00', 'dca-3b'), [receita], marcar_comissoes=True)
        comissao.refresh_from_db()
        comissao.categoria = 'iptu'
        with self.assertRaises(ValidationError):
            comissao.save()
        comissao.refresh_from_db()
        self.assertEqual(comissao.categoria, 'comissao_imobiliaria')

    def test_despesa_de_debito_conciliado_nao_pode_ter_descricao_alterada(self):
        tx = self._tx('-350.00', 'dca-3c', tipo='debito', descricao='CONTA')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Descrição original')
        despesa.refresh_from_db()
        despesa.descricao = 'Descrição alterada'
        with self.assertRaises(ValidationError):
            despesa.save()
        despesa.refresh_from_db()
        self.assertEqual(despesa.descricao, 'Descrição original')

    def test_despesa_de_debito_conciliado_nao_pode_ter_competencia_mes_alterada(self):
        tx = self._tx('-350.00', 'dca-3d', tipo='debito', descricao='CONTA')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Conta')
        despesa.refresh_from_db()
        mes_original = despesa.competencia_mes
        despesa.competencia_mes = (mes_original % 12) + 1
        with self.assertRaises(ValidationError):
            despesa.save()
        despesa.refresh_from_db()
        self.assertEqual(despesa.competencia_mes, mes_original)

    def test_despesa_de_debito_conciliado_nao_pode_ter_competencia_ano_alterada(self):
        tx = self._tx('-350.00', 'dca-3e', tipo='debito', descricao='CONTA')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Conta')
        despesa.refresh_from_db()
        ano_original = despesa.competencia_ano
        despesa.competencia_ano = ano_original + 1
        with self.assertRaises(ValidationError):
            despesa.save()
        despesa.refresh_from_db()
        self.assertEqual(despesa.competencia_ano, ano_original)

    def test_despesa_de_debito_conciliado_nao_pode_ter_vencimento_alterado(self):
        tx = self._tx('-350.00', 'dca-3f', tipo='debito', descricao='CONTA')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Conta')
        despesa.refresh_from_db()
        vencimento_original = despesa.data_vencimento
        despesa.data_vencimento = vencimento_original + timedelta(days=5)
        with self.assertRaises(ValidationError):
            despesa.save()
        despesa.refresh_from_db()
        self.assertEqual(despesa.data_vencimento, vencimento_original)

    def test_despesa_de_debito_conciliado_nao_pode_ter_origem_automatica_alterada(self):
        tx = self._tx('-350.00', 'dca-3g', tipo='debito', descricao='CONTA')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Conta')
        despesa.refresh_from_db()
        self.assertFalse(despesa.origem_automatica)
        despesa.origem_automatica = True
        with self.assertRaises(ValidationError):
            despesa.save()
        despesa.refresh_from_db()
        self.assertFalse(despesa.origem_automatica)

    def test_observacao_pode_ser_alterada_com_conciliacao_ativa(self):
        imob = Pessoa.objects.create(nome='Imob DCA4', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        conciliar_com_receitas(self._tx('1800.00', 'dca-4'), [receita], marcar_comissoes=True)
        comissao.refresh_from_db()
        comissao.observacoes = 'nota administrativa'
        comissao.save()
        comissao.refresh_from_db()
        self.assertEqual(comissao.observacoes, 'nota administrativa')

    def test_despesa_de_debito_conciliado_nao_pode_ser_excluida(self):
        tx = self._tx('-350.00', 'dca-5', tipo='debito', descricao='CONTA DE LUZ')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Conta de luz')
        with self.assertRaises(ValidationError):
            despesa.delete()
        self.assertTrue(Despesa.objects.filter(pk=despesa.pk).exists())

    def test_transacao_conciliada_nunca_fica_sem_despesa(self):
        """
        Tentativas de excluir a despesa (direto ou em massa) nunca deixam a
        transação com status='conciliada' e despesa=None — o único caminho
        que desvincula é desfazer_conciliacao(), que também reabre a
        transação para 'pendente' (nunca 'conciliada' sem despesa).
        """
        tx = self._tx('-200.00', 'dca-6', tipo='debito', descricao='ÁGUA')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Água')
        with self.assertRaises(ValidationError):
            despesa.delete()
        with self.assertRaises(ValidationError):
            Despesa.objects.filter(pk=despesa.pk).delete()
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(tx.despesa_id, despesa.pk)

    def test_desfazer_conciliacao_de_debito_exclui_despesa_criada_exclusivamente_por_ele(self):
        """
        Item 1 (rodada de fechamento estrutural): desfazer_conciliacao() de
        um débito agora EXCLUI a despesa que ele mesmo criou (não apenas
        desvincula) — lancar_despesa() cria uma despesa NOVA a cada
        lançamento; manter a despesa "paga" existindo permitiria relançar o
        mesmo débito e duplicar a despesa.
        """
        from .services import desfazer_conciliacao
        tx = self._tx('-150.00', 'dca-7', tipo='debito', descricao='INTERNET')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Internet')
        pk_despesa = despesa.pk
        desfazer_conciliacao(tx)
        tx.refresh_from_db()
        self.assertIsNone(tx.despesa_id)
        self.assertEqual(tx.status, 'pendente')
        self.assertFalse(Despesa.objects.filter(pk=pk_despesa).exists())

    def test_acoes_em_massa_nao_alteram_parcialmente_o_conjunto(self):
        """
        Uma ação em massa sobre um conjunto que MISTURA despesas protegidas
        e livres é bloqueada por inteiro — nenhuma das duas é alterada
        parcialmente (nem mesmo a livre).
        """
        imob = Pessoa.objects.create(nome='Imob DCA8', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        conciliar_com_receitas(self._tx('1800.00', 'dca-8'), [receita], marcar_comissoes=True)
        comissao.refresh_from_db()

        livre = Despesa.objects.create(
            imovel=self.imovel, descricao='Despesa livre', categoria='outro',
            data_vencimento=date(2026, 3, 12), valor=Decimal('100.00'), status='prevista',
        )
        with self.assertRaises(ValidationError):
            Despesa.objects.filter(pk__in=[comissao.pk, livre.pk]).update(categoria='iptu')
        comissao.refresh_from_db()
        livre.refresh_from_db()
        self.assertNotEqual(comissao.categoria, 'iptu')
        self.assertNotEqual(livre.categoria, 'iptu')

    def test_admin_change_form_mostra_campos_financeiros_readonly(self):
        superuser = User.objects.create_superuser('admin_dca', 'a@a.com', 'pass')
        client = Client()
        client.force_login(superuser)
        imob = Pessoa.objects.create(nome='Imob DCA9', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        conciliar_com_receitas(self._tx('1800.00', 'dca-9'), [receita], marcar_comissoes=True)
        comissao.refresh_from_db()

        url = reverse('admin:financeiro_despesa_change', args=[comissao.pk])
        resposta = client.get(url)
        self.assertEqual(resposta.status_code, 200)
        conteudo = resposta.content.decode()
        self.assertIn('Conciliação Bancária', conteudo)
        self.assertIn('Desfaça a conciliação', conteudo)
        # campo "valor" não aparece mais como <input> editável
        self.assertNotIn('name="valor"', conteudo)

    def test_admin_nao_oferece_exclusao_com_conciliacao_ativa(self):
        superuser = User.objects.create_superuser('admin_dca2', 'a@a.com', 'pass')
        client = Client()
        client.force_login(superuser)
        imob = Pessoa.objects.create(nome='Imob DCA10', tipo='imobiliaria')
        receita, comissao = self._receita_com_comissao(imob)
        conciliar_com_receitas(self._tx('1800.00', 'dca-10'), [receita], marcar_comissoes=True)
        comissao.refresh_from_db()

        url_change = reverse('admin:financeiro_despesa_change', args=[comissao.pk])
        resposta = client.get(url_change)
        self.assertNotIn('deletelink', resposta.content.decode())

        url_delete = reverse('admin:financeiro_despesa_delete', args=[comissao.pk])
        resposta_delete = client.get(url_delete)
        self.assertEqual(resposta_delete.status_code, 403)
        self.assertTrue(Despesa.objects.filter(pk=comissao.pk).exists())


# ─── Desfazer conciliação de débito exclui a despesa criada (item 1, rodada de fechamento estrutural) ──

class DesfazerConciliacaoDebitoTest(TestCase):
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta DCD')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hdcd')
        self.imovel, self.locatario = _base()

    def _tx(self, valor, fitid, descricao='CONTA'):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal(valor), tipo='debito', descricao=descricao,
        )

    def test_lancar_debito_cria_despesa(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'dcd-1')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        self.assertTrue(Despesa.objects.filter(pk=despesa.pk, status='paga').exists())
        tx.refresh_from_db()
        self.assertEqual(tx.despesa_id, despesa.pk)
        self.assertEqual(tx.status, 'conciliada')

    def test_desfazer_remove_despesa_criada_exclusivamente_por_ele(self):
        from .services import desfazer_conciliacao, lancar_despesa
        tx = self._tx('-500.00', 'dcd-2')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        desfazer_conciliacao(tx)
        self.assertFalse(Despesa.objects.filter(pk=despesa.pk).exists())

    def test_transacao_volta_a_pendente_apos_desfazer(self):
        from .services import desfazer_conciliacao, lancar_despesa
        tx = self._tx('-500.00', 'dcd-3')
        lancar_despesa(tx, categoria='outro', descricao='Reparo')
        desfazer_conciliacao(tx)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertIsNone(tx.despesa_id)

    def test_relancar_apos_desfazer_resulta_em_apenas_uma_despesa(self):
        from .services import desfazer_conciliacao, lancar_despesa
        tx = self._tx('-500.00', 'dcd-4')
        primeira = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        desfazer_conciliacao(tx)
        tx.refresh_from_db()
        segunda = lancar_despesa(tx, categoria='outro', descricao='Reparo (relançado)')
        self.assertNotEqual(primeira.pk, segunda.pk)
        self.assertEqual(Despesa.objects.filter(descricao__startswith='Reparo').count(), 1)
        self.assertEqual(Despesa.objects.count(), 1)

    def test_nenhum_registro_de_caixa_duplicado_apos_relancar(self):
        """
        O caixa (soma de despesas pagas) não duplica: desfazer removeu a
        primeira despesa antes do relançamento criar a segunda.
        """
        from django.db.models import Sum
        from .services import desfazer_conciliacao, lancar_despesa
        tx = self._tx('-500.00', 'dcd-5')
        lancar_despesa(tx, categoria='outro', descricao='Reparo')
        desfazer_conciliacao(tx)
        tx.refresh_from_db()
        lancar_despesa(tx, categoria='outro', descricao='Reparo (relançado)')
        total_pago = Despesa.objects.filter(status='paga').aggregate(total=Sum('valor'))['total']
        self.assertEqual(total_pago, Decimal('500.00'))

    def test_despesa_com_outros_vinculos_bloqueia_exclusao_automatica(self):
        """
        Uma despesa lançada pelo extrato que DEPOIS ganhou outro vínculo
        financeiro (aqui simulado vinculando-a a uma receita, algo que
        lancar_despesa() nunca faz sozinho) não pode ser excluída
        automaticamente pelo desfazer — exige revisão manual.
        """
        from django.db import connection
        from .services import desfazer_conciliacao, lancar_despesa, ConciliacaoInvalidaError
        contrato = _contrato(self.imovel, self.locatario)
        receita = _receita(contrato, date(2026, 3, 10), Decimal('100.00'))
        tx = self._tx('-500.00', 'dcd-6')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        # Simula um estado legado/inconsistente (a despesa ganhou um vínculo
        # de receita por fora do fluxo normal) via SQL bruto — item 2 já
        # bloqueia esse vínculo por QUALQUER caminho sancionado (save() e
        # QuerySet.update() rejeitam alterar campos protegidos com
        # conciliação ativa), então só uma alteração direta no banco
        # reproduz o cenário que a checagem de origem precisa cobrir.
        with connection.cursor() as cursor:
            cursor.execute(
                'UPDATE financeiro_despesa SET receita_id = %s WHERE id = %s',
                [receita.pk, despesa.pk],
            )
        with self.assertRaises(ConciliacaoInvalidaError) as ctx:
            desfazer_conciliacao(tx)
        self.assertIn('Revisão manual', str(ctx.exception))
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertTrue(Despesa.objects.filter(pk=despesa.pk).exists())

    def test_erro_intermediario_causa_rollback_completo(self):
        from unittest.mock import patch
        from .services import desfazer_conciliacao, lancar_despesa
        from financeiro.models import Despesa as DespesaModel
        tx = self._tx('-500.00', 'dcd-7')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Reparo')

        with patch.object(DespesaModel, 'delete', side_effect=RuntimeError('falha simulada')):
            with self.assertRaises(RuntimeError):
                desfazer_conciliacao(tx)

        tx.refresh_from_db()
        despesa.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(tx.despesa_id, despesa.pk)
        self.assertEqual(despesa.status, 'paga')

    def test_mensagens_de_credito_e_debito_diferentes(self):
        from .services import conciliar_com_receitas, lancar_despesa
        contrato = _contrato(self.imovel, self.locatario)
        receita = _receita(contrato, date(2026, 3, 10), Decimal('2000.00'))
        tx_credito = TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid='dcd-8c',
            data=date(2026, 3, 12), valor=Decimal('2000.00'), tipo='credito', descricao='PIX',
        )
        conciliar_com_receitas(tx_credito, [receita])
        tx_debito = self._tx('-500.00', 'dcd-8d')
        lancar_despesa(tx_debito, categoria='outro', descricao='Reparo')

        # Matriz completa de desfazer: crédito sem comissões exige
        # change_receitaaluguel; débito exige delete_despesa (exclui a
        # despesa criada pela conciliação). Este teste verifica a MENSAGEM
        # por tipo, então concede as duas para exercitar ambos os fluxos.
        user = User.objects.create_user('dcd_msg', password='pass')
        user.user_permissions.add(*Permission.objects.filter(codename__in=[
            'view_extratoimportado', 'change_transacaoextrato',
            'change_receitaaluguel', 'delete_despesa',
        ]))
        client = Client()
        client.login(username='dcd_msg', password='pass')

        resposta_credito = client.post(
            reverse('conciliar_extrato', args=[self.extrato.pk]),
            {'transacao_id': tx_credito.pk, 'action': 'desfazer'}, follow=True,
        )
        mensagens_credito = [str(m) for m in resposta_credito.context['messages']]
        self.assertTrue(any('recebimentos removidos' in m for m in mensagens_credito))
        self.assertFalse(any('despesa vinculada removida' in m for m in mensagens_credito))

        resposta_debito = client.post(
            reverse('conciliar_extrato', args=[self.extrato.pk]),
            {'transacao_id': tx_debito.pk, 'action': 'desfazer'}, follow=True,
        )
        mensagens_debito = [str(m) for m in resposta_debito.context['messages']]
        self.assertTrue(any('despesa vinculada removida' in m for m in mensagens_debito))
        self.assertFalse(any('recebimentos removidos' in m for m in mensagens_debito))

    def test_historico_registra_lancamento_e_desfazer(self):
        from .models import HistoricalTransacaoExtrato
        from .services import desfazer_conciliacao, lancar_despesa
        tx = self._tx('-500.00', 'dcd-9')
        lancar_despesa(tx, categoria='outro', descricao='Reparo')
        desfazer_conciliacao(tx)
        historico = HistoricalTransacaoExtrato.objects.filter(id=tx.pk).order_by('history_date')
        status_historicos = list(historico.values_list('status', flat=True))
        self.assertIn('conciliada', status_historicos)
        self.assertEqual(status_historicos[-1], 'pendente')

    def test_desfazer_repetido_e_rejeitado_claramente(self):
        from .services import desfazer_conciliacao, lancar_despesa, ConciliacaoInvalidaError
        tx = self._tx('-500.00', 'dcd-10')
        lancar_despesa(tx, categoria='outro', descricao='Reparo')
        desfazer_conciliacao(tx)
        tx.refresh_from_db()
        with self.assertRaises(ConciliacaoInvalidaError) as ctx:
            desfazer_conciliacao(tx)
        self.assertIn('Só é possível desfazer transações conciliadas', str(ctx.exception))


# ─── Origem explícita da despesa de débito (identificação inequívoca) ─────────

class OrigemExplicitaDespesaDebitoTest(TestCase):
    """
    despesa_criada_pela_conciliacao é a ÚNICA prova de origem aceita por
    desfazer_conciliacao() para excluir automaticamente a despesa de um
    débito — nunca mais inferida por valor/data/descrição/status, que só
    demonstram compatibilidade, não comprovam que lancar_despesa() criou
    aquele registro especificamente.
    """
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta OED')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hoed')
        self.imovel, self.locatario = _base()

    def _tx(self, valor, fitid, descricao='CONTA'):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal(valor), tipo='debito', descricao=descricao,
        )

    # 1-2: lancar_despesa() marca a origem explícita e concilia a transação
    def test_lancar_despesa_marca_origem_explicita(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'oed-1')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Reparo')

        tx.refresh_from_db()
        self.assertTrue(tx.despesa_criada_pela_conciliacao)
        self.assertEqual(tx.despesa_id, despesa.pk)
        self.assertEqual(tx.status, 'conciliada')

    # 3: erro intermediário em lancar_despesa() causa rollback completo
    def test_erro_intermediario_em_lancar_despesa_causa_rollback(self):
        from unittest.mock import patch
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'oed-2')

        with patch.object(TransacaoExtrato, 'save', side_effect=RuntimeError('falha simulada')):
            with self.assertRaises(RuntimeError):
                lancar_despesa(tx, categoria='outro', descricao='Reparo')

        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertIsNone(tx.despesa_id)
        self.assertFalse(tx.despesa_criada_pela_conciliacao)
        # a despesa criada antes da falha também não pode sobreviver — a
        # transação atômica reverte TUDO, inclusive o Despesa.objects.create()
        self.assertFalse(Despesa.objects.filter(descricao='Reparo').exists())

    # 4-5-6: desfazer exclui a despesa com origem explícita e não permite duplicidade
    def test_desfazer_exclui_despesa_com_origem_explicita(self):
        from .services import desfazer_conciliacao, lancar_despesa
        tx = self._tx('-500.00', 'oed-3')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        pk_despesa = despesa.pk

        desfazer_conciliacao(tx)

        self.assertFalse(Despesa.objects.filter(pk=pk_despesa).exists())
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertIsNone(tx.despesa_id)
        self.assertFalse(tx.despesa_criada_pela_conciliacao)

    def test_relancar_apos_desfazer_resulta_em_apenas_uma_despesa(self):
        from .services import desfazer_conciliacao, lancar_despesa
        tx = self._tx('-500.00', 'oed-4')
        primeira = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        desfazer_conciliacao(tx)
        tx.refresh_from_db()
        segunda = lancar_despesa(tx, categoria='outro', descricao='Reparo (relançado)')

        self.assertNotEqual(primeira.pk, segunda.pk)
        self.assertEqual(Despesa.objects.count(), 1)
        tx.refresh_from_db()
        self.assertTrue(tx.despesa_criada_pela_conciliacao)
        self.assertEqual(tx.despesa_id, segunda.pk)

    # 7-8: despesa manual compatível (mesmo valor/data/status), origem False, não é excluída
    def test_despesa_manual_compativel_nao_e_excluida(self):
        """
        O teste crítico desta tarefa: uma despesa lançada MANUALMENTE (não
        por lancar_despesa()) que coincide em TODOS os critérios que o
        sistema usava antes como prova indireta — mesmo valor, mesma data,
        status paga, sem outros vínculos — mas com despesa_criada_pela_
        conciliacao=False (o padrão) NUNCA é excluída ao desfazer.
        """
        from django.db import connection
        from .services import desfazer_conciliacao, ConciliacaoInvalidaError

        tx = self._tx('-500.00', 'oed-5')
        despesa_manual = Despesa.objects.create(
            imovel=self.imovel, categoria='outro', descricao='Despesa lançada manualmente',
            data_vencimento=date(2026, 3, 12), data_pagamento=date(2026, 3, 12),
            valor=Decimal('500.00'), status='paga',
        )
        # Vincula manualmente (fora de lancar_despesa()) via SQL bruto — o
        # FK despesa é PROTECT/editável só pelos services normalmente, mas
        # a vinculação em si (sem passar por lancar_despesa()) é exatamente
        # o cenário legado/manual que este teste precisa reproduzir.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE conciliacao_transacaoextrato SET despesa_id = %s, status = 'conciliada' "
                'WHERE id = %s',
                [despesa_manual.pk, tx.pk],
            )
        tx.refresh_from_db()
        self.assertEqual(tx.despesa_id, despesa_manual.pk)
        self.assertFalse(tx.despesa_criada_pela_conciliacao)
        self.assertEqual(despesa_manual.valor, tx.valor_absoluto)
        self.assertEqual(despesa_manual.data_pagamento, tx.data)
        self.assertEqual(despesa_manual.status, 'paga')

        with self.assertRaises(ConciliacaoInvalidaError) as ctx:
            desfazer_conciliacao(tx)
        self.assertIn('origem', str(ctx.exception).lower())

        tx.refresh_from_db()
        despesa_manual.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(tx.despesa_id, despesa_manual.pk)
        self.assertTrue(Despesa.objects.filter(pk=despesa_manual.pk).exists())

    # 9: registro legado (despesa preenchida, campo False) é bloqueado
    def test_registro_legado_sem_origem_e_bloqueado(self):
        from django.db import connection
        from .services import desfazer_conciliacao, ConciliacaoInvalidaError

        tx = self._tx('-350.00', 'oed-6')
        despesa_legada = Despesa.objects.create(
            categoria='outro', descricao='Despesa de extrato anterior a este controle',
            data_vencimento=date(2026, 3, 12), data_pagamento=date(2026, 3, 12),
            valor=Decimal('350.00'), status='paga',
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE conciliacao_transacaoextrato SET despesa_id = %s, status = 'conciliada' "
                'WHERE id = %s',
                [despesa_legada.pk, tx.pk],
            )
        tx.refresh_from_db()
        self.assertFalse(tx.despesa_criada_pela_conciliacao)

        with self.assertRaises(ConciliacaoInvalidaError):
            desfazer_conciliacao(tx)

        tx.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertTrue(Despesa.objects.filter(pk=despesa_legada.pk).exists())

    # 10: save() direto tentando marcar a origem é rejeitado
    def test_save_direto_nao_marca_origem(self):
        tx = self._tx('-200.00', 'oed-7')
        tx.despesa_criada_pela_conciliacao = True
        with self.assertRaises(ValidationError):
            tx.save()
        tx.refresh_from_db()
        self.assertFalse(tx.despesa_criada_pela_conciliacao)

    def test_save_direto_de_registro_ja_marcado_nao_e_bloqueado(self):
        """Resalvar um campo já True (sem alterar despesa_criada_pela_
        conciliacao) não deve ser bloqueado — só a TRANSIÇÃO False→True
        exige o sinalizador privado."""
        from .services import lancar_despesa
        tx = self._tx('-200.00', 'oed-7b')
        lancar_despesa(tx, categoria='outro', descricao='Reparo')
        tx.refresh_from_db()
        tx.observacoes = 'nota'
        tx.save(update_fields=['observacoes'])  # não deve levantar
        tx.refresh_from_db()
        self.assertTrue(tx.despesa_criada_pela_conciliacao)
        self.assertEqual(tx.observacoes, 'nota')

    # 11: QuerySet.update() tentando mudar o campo é rejeitado
    def test_queryset_update_nao_pode_alterar_origem(self):
        tx = self._tx('-200.00', 'oed-8')
        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.filter(pk=tx.pk).update(despesa_criada_pela_conciliacao=True)
        tx.refresh_from_db()
        self.assertFalse(tx.despesa_criada_pela_conciliacao)

    def _insert_bruto(self, fitid, valor, tipo, despesa_id, origem):
        """
        INSERT via SQL bruto — bulk_create() agora rejeita origem=True na
        camada de aplicação ANTES de chegar ao banco, então só uma inserção
        direta consegue exercitar a CheckConstraint em si.
        """
        from django.db import connection
        from django.utils import timezone as tz
        agora = tz.now()
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO conciliacao_transacaoextrato '
                '(extrato_id, conta_id, fitid, data, valor, tipo, descricao, memo, status, '
                'comissoes_marcadas, despesa_id, despesa_criada_pela_conciliacao, observacoes, '
                'criado_em, atualizado_em) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                [self.extrato.pk, self.conta.pk, fitid, date(2026, 3, 12), valor, tipo,
                 'X', '', 'pendente', False, despesa_id, origem, '', agora, agora],
            )

    # 12-13: a CheckConstraint garante coerência no banco
    def test_constraint_rejeita_true_sem_despesa(self):
        from django.db import IntegrityError, transaction as db_transaction
        with self.assertRaises(IntegrityError):
            with db_transaction.atomic():
                self._insert_bruto('oed-9', Decimal('-200.00'), 'debito', None, True)

    def test_constraint_rejeita_true_em_transacao_de_credito(self):
        from django.db import IntegrityError, transaction as db_transaction
        despesa = Despesa.objects.create(
            categoria='outro', descricao='Despesa qualquer',
            data_vencimento=date(2026, 3, 12), data_pagamento=date(2026, 3, 12),
            valor=Decimal('200.00'), status='paga',
        )
        with self.assertRaises(IntegrityError):
            with db_transaction.atomic():
                self._insert_bruto('oed-10', Decimal('200.00'), 'credito', despesa.pk, True)

    def test_constraint_aceita_false_em_qualquer_situacao(self):
        """Sanidade: despesa_criada_pela_conciliacao=False nunca viola a
        constraint, mesmo sem despesa/em crédito — é o estado de todo
        registro legado."""
        tx = TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid='oed-11',
            data=date(2026, 3, 12), valor=Decimal('200.00'), tipo='credito',
            descricao='PIX', despesa=None,
        )
        self.assertFalse(tx.despesa_criada_pela_conciliacao)


class IntegridadeParDespesaOrigemTest(TestCase):
    """
    Integridade do PAR despesa × despesa_criada_pela_conciliacao: depois
    que o estado persistido está marcado (True), nenhuma operação comum
    pode trocar/limpar o vínculo nem apagar a prova de origem — só
    desfazer_conciliacao() (via sinalizador privado). Sem isso, trocar a
    despesa mantendo o booleano True faria o desfazer excluir uma despesa
    manual que lancar_despesa() nunca criou.
    """
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta IPD')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hipd')
        self.imovel, self.locatario = _base()

    def _tx(self, valor, fitid, descricao='CONTA'):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal(valor), tipo='debito', descricao=descricao,
        )

    def _despesa_manual(self, descricao='Despesa manual'):
        return Despesa.objects.create(
            imovel=self.imovel, categoria='outro', descricao=descricao,
            data_vencimento=date(2026, 3, 12), data_pagamento=date(2026, 3, 12),
            valor=Decimal('500.00'), status='paga',
        )

    # 1: o teste crítico — trocar a despesa com origem True é rejeitado
    def test_save_nao_pode_trocar_despesa_quando_origem_true(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'ipd-1')
        despesa_original = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        despesa_manual = self._despesa_manual()

        tx.refresh_from_db()
        tx.despesa = despesa_manual
        with self.assertRaises(ValidationError):
            tx.save(update_fields=['despesa'])

        tx.refresh_from_db()
        self.assertEqual(tx.despesa_id, despesa_original.pk)
        self.assertTrue(tx.despesa_criada_pela_conciliacao)
        self.assertTrue(Despesa.objects.filter(pk=despesa_manual.pk).exists())

    # 2: limpar a despesa com origem True é rejeitado
    def test_save_nao_pode_limpar_despesa_quando_origem_true(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'ipd-2')
        despesa_original = lancar_despesa(tx, categoria='outro', descricao='Reparo')

        tx.refresh_from_db()
        tx.despesa = None
        with self.assertRaises(ValidationError):
            tx.save(update_fields=['despesa'])

        tx.refresh_from_db()
        self.assertEqual(tx.despesa_id, despesa_original.pk)
        self.assertTrue(tx.despesa_criada_pela_conciliacao)

    # 3: apagar a prova de origem (True→False) é rejeitado
    def test_save_nao_pode_limpar_origem(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'ipd-3')
        despesa_original = lancar_despesa(tx, categoria='outro', descricao='Reparo')

        tx.refresh_from_db()
        tx.despesa_criada_pela_conciliacao = False
        with self.assertRaises(ValidationError):
            tx.save(update_fields=['despesa_criada_pela_conciliacao'])

        tx.refresh_from_db()
        self.assertTrue(tx.despesa_criada_pela_conciliacao)
        self.assertEqual(tx.despesa_id, despesa_original.pk)

    # 4: resalvar sem tocar no par continua permitido
    def test_save_continua_permitindo_alterar_apenas_observacoes(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'ipd-4')
        lancar_despesa(tx, categoria='outro', descricao='Reparo')

        tx.refresh_from_db()
        tx.observacoes = 'Nota administrativa'
        tx.save(update_fields=['observacoes'])  # não deve levantar

        tx.refresh_from_db()
        self.assertEqual(tx.observacoes, 'Nota administrativa')
        self.assertTrue(tx.despesa_criada_pela_conciliacao)

    # 5-6-7: QuerySet.update() com qualquer campo do par é rejeitado
    def test_queryset_update_nao_pode_trocar_despesa(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'ipd-5')
        despesa_original = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        despesa_manual = self._despesa_manual()

        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.filter(pk=tx.pk).update(despesa=despesa_manual)

        tx.refresh_from_db()
        self.assertEqual(tx.despesa_id, despesa_original.pk)

    def test_queryset_update_nao_pode_trocar_despesa_id(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'ipd-6')
        despesa_original = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        despesa_manual = self._despesa_manual()

        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.filter(pk=tx.pk).update(despesa_id=despesa_manual.pk)

        tx.refresh_from_db()
        self.assertEqual(tx.despesa_id, despesa_original.pk)

    def test_queryset_update_nao_pode_limpar_origem(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'ipd-7')
        lancar_despesa(tx, categoria='outro', descricao='Reparo')

        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.filter(pk=tx.pk).update(despesa_criada_pela_conciliacao=False)

        tx.refresh_from_db()
        self.assertTrue(tx.despesa_criada_pela_conciliacao)

    # 8: bulk_update() dos campos do par é rejeitado
    def test_bulk_update_dos_campos_do_par_e_rejeitado(self):
        from .services import lancar_despesa
        tx = self._tx('-500.00', 'ipd-8')
        lancar_despesa(tx, categoria='outro', descricao='Reparo')
        tx.refresh_from_db()

        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.bulk_update([tx], ['despesa'])
        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.bulk_update([tx], ['despesa_id'])
        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.bulk_update([tx], ['despesa_criada_pela_conciliacao'])
        # campo comum junto com um protegido também bloqueia (conjunto todo)
        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.bulk_update([tx], ['observacoes', 'despesa'])

        tx.refresh_from_db()
        self.assertTrue(tx.despesa_criada_pela_conciliacao)

    # 9: bulk_create() não pode declarar origem — mesmo estruturalmente válido
    def test_bulk_create_nao_pode_declarar_origem(self):
        despesa = self._despesa_manual('Despesa para bulk_create')
        tx = TransacaoExtrato(
            extrato=self.extrato, conta=self.conta, fitid='ipd-9',
            data=date(2026, 3, 12), valor=Decimal('-500.00'), tipo='debito',
            descricao='CONTA', despesa=despesa, despesa_criada_pela_conciliacao=True,
        )
        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.bulk_create([tx])
        self.assertFalse(TransacaoExtrato.objects.filter(fitid='ipd-9').exists())

    # 10: bulk_create() normal (origem False) continua funcionando
    def test_bulk_create_com_origem_false_continua_funcionando(self):
        txs = [
            TransacaoExtrato(
                extrato=self.extrato, conta=self.conta, fitid=f'ipd-10-{i}',
                data=date(2026, 3, 12), valor=Decimal('-100.00'), tipo='debito',
                descricao='CONTA',
            )
            for i in range(2)
        ]
        criadas = TransacaoExtrato.objects.bulk_create(txs)
        self.assertEqual(len(criadas), 2)
        self.assertEqual(
            TransacaoExtrato.objects.filter(fitid__startswith='ipd-10-').count(), 2,
        )

    # 11: o service continua conseguindo limpar o par legitimamente
    def test_desfazer_service_limpa_origem_e_despesa(self):
        from .services import desfazer_conciliacao, lancar_despesa
        tx = self._tx('-500.00', 'ipd-11')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        pk_despesa = despesa.pk

        resultado = desfazer_conciliacao(tx)

        tx.refresh_from_db()
        self.assertIsNone(tx.despesa_id)
        self.assertFalse(tx.despesa_criada_pela_conciliacao)
        self.assertEqual(tx.status, 'pendente')
        self.assertFalse(Despesa.objects.filter(pk=pk_despesa).exists())
        # o sinalizador privado não permanece na instância usada pelo
        # service depois da operação (é setado antes do save() e removido
        # logo em seguida)
        self.assertFalse(hasattr(resultado, '_alterando_origem_despesa_via_service'))

    # 12: falha DEPOIS da limpeza reverte tudo (o par volta ao estado marcado)
    def test_falha_apos_limpeza_causa_rollback_completo(self):
        from unittest.mock import patch
        from .services import desfazer_conciliacao, lancar_despesa
        from financeiro.models import Despesa as DespesaModel

        tx = self._tx('-500.00', 'ipd-12')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Reparo')

        with patch.object(DespesaModel, 'delete', side_effect=RuntimeError('falha simulada')):
            with self.assertRaises(RuntimeError):
                desfazer_conciliacao(tx)

        tx.refresh_from_db()
        despesa.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(tx.despesa_id, despesa.pk)
        self.assertTrue(tx.despesa_criada_pela_conciliacao)
        self.assertEqual(despesa.status, 'paga')

    # 13: após tentativa (rejeitada) de troca, o desfazer exclui SÓ a original
    def test_desfazer_apos_tentativa_de_troca_exclui_somente_a_despesa_original(self):
        from .services import desfazer_conciliacao, lancar_despesa
        tx = self._tx('-500.00', 'ipd-13')
        despesa_original = lancar_despesa(tx, categoria='outro', descricao='Reparo')
        despesa_manual = self._despesa_manual()

        tx.refresh_from_db()
        tx.despesa = despesa_manual
        with self.assertRaises(ValidationError):
            tx.save(update_fields=['despesa'])
        tx.refresh_from_db()

        desfazer_conciliacao(tx)

        self.assertFalse(Despesa.objects.filter(pk=despesa_original.pk).exists())
        self.assertTrue(Despesa.objects.filter(pk=despesa_manual.pk).exists())
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertIsNone(tx.despesa_id)
        self.assertFalse(tx.despesa_criada_pela_conciliacao)


class MigrationBackfillDespesaCriadaTest(TransactionTestCase):
    """
    Item 14: a migration 0007 preenche despesa_criada_pela_conciliacao=
    False para TODOS os registros existentes — inclusive uma transação
    ANTIGA que já tem despesa vinculada e seria "compatível" com os
    critérios indiretos usados antes desta tarefa. Usa TransactionTestCase
    porque o schema editor do SQLite (via MigrationExecutor) não pode rodar
    dentro da transação do TestCase comum.
    """

    def test_migration_backfill_deixa_registros_antigos_como_false(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        app = 'conciliacao'
        estado_anterior = [(app, '0006_transacaoextrato_extrato_protect')]
        executor.migrate(estado_anterior)
        executor.loader.build_graph()

        apps_antigos = executor.loader.project_state(estado_anterior).apps
        ContaBancariaHist = apps_antigos.get_model('conciliacao', 'ContaBancaria')
        ExtratoImportadoHist = apps_antigos.get_model('conciliacao', 'ExtratoImportado')
        TransacaoExtratoHist = apps_antigos.get_model('conciliacao', 'TransacaoExtrato')
        DespesaHist = apps_antigos.get_model('financeiro', 'Despesa')

        conta = ContaBancariaHist.objects.create(nome='Conta Legada Migration')
        extrato = ExtratoImportadoHist.objects.create(conta=conta, hash_arquivo='hash-legado-migration')
        despesa = DespesaHist.objects.create(
            descricao='Despesa legada (pré-campo)', categoria='outro',
            data_vencimento=date(2020, 1, 1), data_pagamento=date(2020, 1, 1),
            valor=Decimal('321.00'), status='paga',
        )
        transacao_legada = TransacaoExtratoHist.objects.create(
            extrato=extrato, conta=conta, fitid='legado-migration-1',
            data=date(2020, 1, 1), valor=Decimal('-321.00'), tipo='debito',
            descricao='Conta antiga', despesa=despesa, status='conciliada',
        )
        pk_transacao = transacao_legada.pk
        pk_despesa = despesa.pk

        # migra de volta até o estado mais recente (aplica a migration 0007
        # e as seguintes, se houver)
        executor.loader.build_graph()
        alvo_final = executor.loader.graph.leaf_nodes(app)
        executor.migrate(alvo_final)

        from .models import TransacaoExtrato as TransacaoExtratoAtual
        atualizada = TransacaoExtratoAtual.objects.get(pk=pk_transacao)
        self.assertFalse(atualizada.despesa_criada_pela_conciliacao)
        self.assertEqual(atualizada.despesa_id, pk_despesa)
        self.assertEqual(atualizada.status, 'conciliada')


# ─── FITID por assinatura + conferência rigorosa da conta (rodada 2) ──────────

def _ofx_conta_bruta(corpo_transacoes, bankid='0341', acctid='12345-6', nome='bruto.ofx'):
    """Como _ofx_conta, mas recebe o bloco <STMTTRN> pronto (para casos sem FITID)."""
    conteudo = f"""OFXHEADER:100
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
<DTSERVER>20260101<LANGUAGE>POR</SONRS></SIGNONMSGSRSV1>
<BANKMSGSRSV1><STMTTRNRS><TRNUID>1
<STATUS><CODE>0<SEVERITY>INFO</STATUS>
<STMTRS><CURDEF>BRL
<BANKACCTFROM><BANKID>{bankid}<ACCTID>{acctid}<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260101<DTEND>20261231
{corpo_transacoes}
</BANKTRANLIST>
<LEDGERBAL><BALAMT>0.00<DTASOF>20261231</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1>
</OFX>
"""
    return SimpleUploadedFile(nome, conteudo.encode('cp1252'))


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class FitidAssinaturaTest(TestCase):
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta FA')

    def test_fitid_none_apos_parse_ganha_fallback_deterministico(self):
        """t.id None (parser não achou FITID) → fallback pós-parse por assinatura."""
        from datetime import datetime
        from types import SimpleNamespace
        from unittest.mock import patch

        def _fake_ofx():
            t = SimpleNamespace(
                id=None, date=datetime(2026, 3, 10), amount='150.00',
                payee='PIX SEM ID', memo='PIX SEM ID', type='credit',
            )
            acct = SimpleNamespace(
                statement=SimpleNamespace(transactions=[t]),
                account_id='', routing_number='', number='', bank_id='',
            )
            return SimpleNamespace(accounts=[acct], account=acct)

        with patch('ofxparse.OfxParser.parse', return_value=_fake_ofx()):
            e1 = importar_ofx(SimpleUploadedFile('f1.ofx', b'conteudo-1'), self.conta)
            self.assertEqual(e1.transacoes_novas, 1)
            tx = e1.transacoes.get()
            self.assertTrue(tx.fitid.startswith('gerado-'))
            # mesma transação em outro arquivo → mesmo id → duplicada
            e2 = importar_ofx(SimpleUploadedFile('f2.ofx', b'conteudo-2'), self.conta)
            self.assertEqual(e2.transacoes_novas, 0)
            self.assertEqual(e2.transacoes_duplicadas, 1)

    def test_fitid_tag_ausente_no_arquivo(self):
        corpo = '<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260310<TRNAMT>150.00<MEMO>SEM TAG</STMTTRN>'
        extrato = importar_ofx(_ofx_conta_bruta(corpo, nome='semtag.ofx'), self.conta)
        self.assertEqual(extrato.transacoes_novas, 1)
        self.assertTrue(extrato.transacoes.get().fitid.startswith('gerado-'))

    def test_mesma_transacao_em_posicao_diferente_gera_mesmo_id(self):
        """Extratos sobrepostos com a transação sem FITID em outra posição não duplicam."""
        e1 = importar_ofx(_ofx_conta([
            ('', '20260310', '150.00', 'SEM FITID'),
            ('t9', '20260311', '10.00', 'OUTRA'),
        ], nome='pos1.ofx'), self.conta)
        self.assertEqual(e1.transacoes_novas, 2)

        # no segundo arquivo a transação sem FITID vem DEPOIS de t9 e de uma nova
        e2 = importar_ofx(_ofx_conta([
            ('t9', '20260311', '10.00', 'OUTRA'),
            ('t10', '20260312', '20.00', 'NOVA'),
            ('', '20260310', '150.00', 'SEM FITID'),
        ], nome='pos2.ofx'), self.conta)
        self.assertEqual(e2.transacoes_duplicadas, 2)  # t9 + a sem FITID
        self.assertEqual(e2.transacoes_novas, 1)       # só t10

    def test_duas_transacoes_identicas_no_mesmo_arquivo_sao_distintas(self):
        extrato = importar_ofx(_ofx_conta([
            ('', '20260310', '150.00', 'DUPLA'),
            ('', '20260310', '150.00', 'DUPLA'),
        ], nome='dupla.ofx'), self.conta)
        self.assertEqual(extrato.transacoes_novas, 2)
        fitids = sorted(extrato.transacoes.values_list('fitid', flat=True))
        self.assertNotEqual(fitids[0], fitids[1])
        self.assertTrue(fitids[0].endswith('-1'))
        self.assertTrue(fitids[1].endswith('-2'))

    def test_mesma_assinatura_em_contas_diferentes_permitida(self):
        conta2 = ContaBancaria.objects.create(nome='Conta FA2')
        e1 = importar_ofx(
            _ofx_conta([('', '20260310', '150.00', 'IGUAL')], nome='ca.ofx'), self.conta
        )
        e2 = importar_ofx(
            _ofx_conta([('', '20260310', '150.00', 'IGUAL')], acctid='22222-2', nome='cb.ofx'), conta2
        )
        self.assertEqual(e1.transacoes_novas, 1)
        self.assertEqual(e2.transacoes_novas, 1)


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ContaOFXRigorosaTest(TestCase):
    def test_cadastro_com_numero_e_ofx_sem_acctid_rejeitado(self):
        conta = ContaBancaria.objects.create(nome='Exigente', numero_conta='12345-6')
        with self.assertRaises(OFXInvalidoError) as ctx:
            importar_ofx(_ofx_conta([('t1', '20260310', '100.00', 'PIX')], acctid='', nome='sa.ofx'), conta)
        self.assertIn('ACCTID', str(ctx.exception))

    def test_cadastro_com_bank_id_e_ofx_sem_bankid_rejeitado(self):
        conta = ContaBancaria.objects.create(nome='Exigente B', bank_id='0341')
        with self.assertRaises(OFXInvalidoError) as ctx:
            importar_ofx(_ofx_conta([('t1', '20260310', '100.00', 'PIX')], bankid='', nome='sb.ofx'), conta)
        self.assertIn('BANKID', str(ctx.exception))

    def test_zeros_a_esquerda_sao_significativos(self):
        # '0341' ≠ '341': dígitos comparados como texto, nunca como inteiro
        conta = ContaBancaria.objects.create(nome='Zeros', bank_id='0341')
        with self.assertRaises(OFXInvalidoError):
            importar_ofx(_ofx_conta([('t1', '20260310', '100.00', 'PIX')], bankid='341', nome='z1.ofx'), conta)
        # coincidência exata (com zeros) é aceita
        extrato = importar_ofx(
            _ofx_conta([('t2', '20260310', '100.00', 'PIX')], bankid='0341', nome='z2.ofx'), conta
        )
        self.assertEqual(extrato.transacoes_novas, 1)

    def test_cadastro_sem_numero_nao_exige_acctid(self):
        conta = ContaBancaria.objects.create(nome='Livre')
        extrato = importar_ofx(
            _ofx_conta([('t1', '20260310', '100.00', 'PIX')], acctid='', nome='livre.ofx'), conta
        )
        self.assertEqual(extrato.transacoes_novas, 1)


# ─── Rodada 3 (item 7): proteção de arquivos privados (extrato OFX) ───────────

@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ExtratoDownloadProtegidoTest(TestCase):
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta Download')
        self.extrato = importar_ofx(
            _arquivo_ofx([('t1', '20260310', '100.00', 'PIX')], nome='download.ofx'), self.conta
        )
        self.client = Client()

    def test_download_exige_login(self):
        response = self.client.get(reverse('extrato_download', args=[self.extrato.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_download_autenticado_sem_permissao_403(self):
        User.objects.create_user('sem_perm_extrato', password='pass')
        self.client.login(username='sem_perm_extrato', password='pass')
        response = self.client.get(reverse('extrato_download', args=[self.extrato.pk]))
        self.assertEqual(response.status_code, 403)

    def test_download_com_permissao_serve_arquivo(self):
        com_leitura(User.objects.create_user('com_perm_extrato', password='pass'))
        self.client.login(username='com_perm_extrato', password='pass')
        response = self.client.get(reverse('extrato_download', args=[self.extrato.pk]))
        self.assertEqual(response.status_code, 200)
        conteudo = b''.join(response.streaming_content)
        self.assertIn(b'STMTTRN', conteudo)

    def test_nao_existe_rota_publica_para_media(self):
        response = self.client.get(f'/media/{self.extrato.arquivo.name}')
        self.assertEqual(response.status_code, 404)


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ExclusaoDeExtratoTest(TestCase):
    """Item 6 (rodada pós-revisão): proteção da trilha de auditoria de extratos."""

    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta Exclusão')
        self.client = Client()
        self.imovel, self.locatario = _base()
        self.contrato = _contrato(self.imovel, self.locatario)

    def _importar(self, transacoes, nome='extrato.ofx'):
        return importar_ofx(_arquivo_ofx(transacoes, nome=nome), self.conta)

    def test_extrato_totalmente_pendente_pode_ser_excluido_pelo_fluxo_explicito(self):
        from .services import excluir_extrato_sem_movimentacoes
        extrato = self._importar([('e1', '20260310', '100.00', 'PIX')])
        excluir_extrato_sem_movimentacoes(extrato.pk)
        self.assertFalse(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_extrato_com_transacao_ignorada_nao_pode_ser_excluido(self):
        from .services import excluir_extrato_sem_movimentacoes, ConciliacaoInvalidaError
        extrato = self._importar([('e2', '20260310', '100.00', 'PIX')])
        t = extrato.transacoes.get()
        t.status = 'ignorada'
        t.save(update_fields=['status'])
        with self.assertRaises(ConciliacaoInvalidaError):
            excluir_extrato_sem_movimentacoes(extrato.pk)
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_extrato_com_credito_conciliado_nao_pode_ser_excluido(self):
        from .services import excluir_extrato_sem_movimentacoes, ConciliacaoInvalidaError
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('100.00'))
        extrato = self._importar([('e3', '20260310', '100.00', 'PIX')])
        t = extrato.transacoes.get()
        conciliar_com_receitas(t, [receita])
        with self.assertRaises(ConciliacaoInvalidaError):
            excluir_extrato_sem_movimentacoes(extrato.pk)
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_extrato_com_despesa_vinculada_nao_pode_ser_excluido(self):
        from .services import excluir_extrato_sem_movimentacoes, ConciliacaoInvalidaError
        extrato = self._importar([('e4', '20260310', '-100.00', 'CEMIG')])
        t = extrato.transacoes.get()
        lancar_despesa(t, categoria='outro', descricao='Energia', fornecedor=None, imovel=None)
        with self.assertRaises(ConciliacaoInvalidaError):
            excluir_extrato_sem_movimentacoes(extrato.pk)
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_extrato_com_conciliacao_comissao_nao_pode_ser_excluido(self):
        from .services import excluir_extrato_sem_movimentacoes, ConciliacaoInvalidaError
        imob = Pessoa.objects.create(nome='Imob Excl', tipo='imobiliaria')
        contrato = _contrato(
            self.imovel, self.locatario, imobiliaria=imob, comissao_imobiliaria_percentual=Decimal('10.00'),
        )
        from financeiro.services import gerar_receitas_para_contrato
        gerar_receitas_para_contrato(contrato, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        receita = ReceitaAluguel.objects.get(contrato=contrato)
        extrato = self._importar([('e5', '20260312', '1800.00', 'REPASSE')])
        t = extrato.transacoes.get()
        conciliar_com_receitas(t, [receita], marcar_comissoes=True)
        with self.assertRaises(ConciliacaoInvalidaError):
            excluir_extrato_sem_movimentacoes(extrato.pk)
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_tentativa_de_exclusao_nao_remove_recebimentos_nem_altera_comissoes(self):
        from financeiro.models import RecebimentoReceita, Despesa
        from .services import excluir_extrato_sem_movimentacoes, ConciliacaoInvalidaError
        imob = Pessoa.objects.create(nome='Imob Excl 2', tipo='imobiliaria')
        contrato = _contrato(
            self.imovel, self.locatario, imobiliaria=imob, comissao_imobiliaria_percentual=Decimal('10.00'),
        )
        from financeiro.services import gerar_receitas_para_contrato
        gerar_receitas_para_contrato(contrato, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        receita = ReceitaAluguel.objects.get(contrato=contrato)
        comissao = Despesa.objects.get(contrato=contrato, origem_automatica=True)
        extrato = self._importar([('e6', '20260312', '1800.00', 'REPASSE')])
        t = extrato.transacoes.get()
        conciliar_com_receitas(t, [receita], marcar_comissoes=True)

        with self.assertRaises(ConciliacaoInvalidaError):
            excluir_extrato_sem_movimentacoes(extrato.pk)

        self.assertEqual(RecebimentoReceita.objects.filter(receita=receita).count(), 1)
        comissao.refresh_from_db()
        self.assertEqual(comissao.status, 'paga')

    def test_admin_nao_oferece_exclusao_insegura(self):
        from django.contrib import admin as django_admin
        from .admin import ExtratoImportadoAdmin
        admin_instance = ExtratoImportadoAdmin(ExtratoImportado, django_admin.site)
        self.assertFalse(admin_instance.has_delete_permission(None))

    def test_view_exclui_extrato_pendente_com_permissao(self):
        extrato = self._importar([('e7', '20260310', '100.00', 'PIX')])
        user = User.objects.create_user('excl_ok', password='pass')
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=['view_extratoimportado', 'delete_extratoimportado']
        ))
        self.client.login(username='excl_ok', password='pass')
        response = self.client.post(reverse('extrato_list'), {'action': 'excluir', 'extrato_id': extrato.pk})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_view_exclusao_exige_permissao_especifica(self):
        extrato = self._importar([('e8', '20260310', '100.00', 'PIX')])
        com_leitura(User.objects.create_user('excl_sem_perm', password='pass'))  # só permissões de view
        self.client.login(username='excl_sem_perm', password='pass')
        response = self.client.post(reverse('extrato_list'), {'action': 'excluir', 'extrato_id': extrato.pk})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_view_exclusao_de_extrato_tratado_falha_com_mensagem(self):
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('100.00'))
        extrato = self._importar([('e9', '20260310', '100.00', 'PIX')])
        t = extrato.transacoes.get()
        conciliar_com_receitas(t, [receita])
        user = User.objects.create_user('excl_tratado', password='pass')
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=['view_extratoimportado', 'delete_extratoimportado']
        ))
        self.client.login(username='excl_tratado', password='pass')
        response = self.client.post(
            reverse('extrato_list'), {'action': 'excluir', 'extrato_id': extrato.pk}, follow=True,
        )
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())
        mensagens = [str(m) for m in response.context['messages']]
        self.assertTrue(any('não pode ser excluído' in m for m in mensagens) or any('tratada' in m for m in mensagens))


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ExclusaoEstruturalTest(TestCase):
    """
    Item 5 (rodada final): a proteção contra exclusão de extratos/transações
    passa a ser ESTRUTURAL (nos models/QuerySets), não apenas na view/Admin —
    extrato.delete() e TransacaoExtrato.delete()/QuerySet.delete() diretos
    são bloqueados fora do fluxo seguro.
    """
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta Estrutural')
        self.imovel, self.locatario = _base()
        self.contrato = _contrato(self.imovel, self.locatario)

    def _importar(self, transacoes, nome='extrato.ofx'):
        return importar_ofx(_arquivo_ofx(transacoes, nome=nome), self.conta)

    def test_extrato_delete_direto_e_rejeitado(self):
        extrato = self._importar([('ee1', '20260310', '100.00', 'PIX')])
        with self.assertRaises(ValidationError):
            extrato.delete()
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_queryset_delete_de_extrato_e_rejeitado(self):
        extrato = self._importar([('ee2', '20260310', '100.00', 'PIX')])
        with self.assertRaises(ValidationError):
            ExtratoImportado.objects.filter(pk=extrato.pk).delete()
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_transacao_conciliada_nao_pode_ser_excluida_diretamente(self):
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('100.00'))
        extrato = self._importar([('ee3', '20260310', '100.00', 'PIX')])
        t = extrato.transacoes.get()
        conciliar_com_receitas(t, [receita])
        t.refresh_from_db()
        with self.assertRaises(ValidationError):
            t.delete()
        self.assertTrue(TransacaoExtrato.objects.filter(pk=t.pk).exists())

    def test_transacao_ignorada_nao_pode_ser_excluida_diretamente(self):
        extrato = self._importar([('ee4', '20260310', '100.00', 'PIX')])
        t = extrato.transacoes.get()
        t.status = 'ignorada'
        t.save(update_fields=['status'])
        with self.assertRaises(ValidationError):
            t.delete()
        self.assertTrue(TransacaoExtrato.objects.filter(pk=t.pk).exists())

    def test_transacao_pendente_sem_vinculo_tambem_e_rejeitada_diretamente(self):
        """
        Item 4 (rodada de fechamento estrutural): a partir desta rodada,
        NENHUMA transação importada pode ser excluída isoladamente — nem
        mesmo pendente e sem vínculo. Só o service seguro
        (excluir_extrato_sem_movimentacoes, testado em
        ExclusaoDeExtratoTest) pode excluir transações, como parte da
        exclusão integral de um extrato totalmente pendente.
        """
        extrato = self._importar([('ee5', '20260310', '100.00', 'PIX')])
        t = extrato.transacoes.get()
        with self.assertRaises(ValidationError):
            t.delete()
        self.assertTrue(TransacaoExtrato.objects.filter(pk=t.pk).exists())

    def test_queryset_delete_de_transacao_tratada_e_rejeitado(self):
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('100.00'))
        extrato = self._importar([('ee6', '20260310', '100.00', 'PIX')])
        t = extrato.transacoes.get()
        conciliar_com_receitas(t, [receita])
        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.filter(pk=t.pk).delete()
        self.assertTrue(TransacaoExtrato.objects.filter(pk=t.pk).exists())

    def test_queryset_delete_de_transacao_pendente_tambem_e_rejeitado(self):
        """Item 4: QuerySet.delete() é bloqueado incondicionalmente — mesmo
        um conjunto TODO pendente e sem vínculo nunca pode ser excluído em
        massa."""
        extrato = self._importar([
            ('ee9', '20260310', '100.00', 'PIX'), ('ee10', '20260311', '50.00', 'PIX'),
        ])
        with self.assertRaises(ValidationError):
            TransacaoExtrato.objects.filter(extrato=extrato).delete()
        self.assertEqual(TransacaoExtrato.objects.filter(extrato=extrato).count(), 2)

    def test_exclusao_parcial_direta_nunca_diverge_metadados_do_extrato(self):
        """
        Item 4: uma tentativa (rejeitada) de excluir isoladamente UMA das
        transações de um extrato com várias nunca altera as demais nem os
        contadores do extrato — a única forma de "perder" uma transação é
        excluir o extrato inteiro pelo fluxo seguro.
        """
        extrato = self._importar([
            ('ee11', '20260310', '100.00', 'PIX'), ('ee12', '20260311', '50.00', 'PIX'),
        ])
        novas_antes = extrato.transacoes_novas
        t1, t2 = list(extrato.transacoes.order_by('fitid'))
        with self.assertRaises(ValidationError):
            t1.delete()
        extrato.refresh_from_db()
        self.assertEqual(extrato.transacoes_novas, novas_antes)
        self.assertEqual(TransacaoExtrato.objects.filter(extrato=extrato).count(), 2)
        self.assertTrue(TransacaoExtrato.objects.filter(pk=t1.pk).exists())
        self.assertTrue(TransacaoExtrato.objects.filter(pk=t2.pk).exists())

    def test_admin_nao_oferece_exclusao_insegura_de_extrato(self):
        superuser = User.objects.create_superuser('admin_estrut', 'a@a.com', 'pass')
        client = Client()
        client.force_login(superuser)
        extrato = self._importar([('ee7', '20260310', '100.00', 'PIX')])
        url = reverse('admin:conciliacao_extratoimportado_delete', args=[extrato.pk])
        resposta = client.get(url)
        self.assertEqual(resposta.status_code, 403)
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_exclusao_registra_usuario_no_historico(self):
        from .models import HistoricalExtratoImportado
        from .services import excluir_extrato_sem_movimentacoes
        usuario = User.objects.create_user('excl_auditoria', password='pass')
        extrato = self._importar([('ee8', '20260310', '100.00', 'PIX')])
        pk = extrato.pk
        excluir_extrato_sem_movimentacoes(pk, usuario=usuario)
        historico_exclusao = HistoricalExtratoImportado.objects.filter(id=pk, history_type='-').first()
        self.assertIsNotNone(historico_exclusao)
        self.assertEqual(historico_exclusao.history_user_id, usuario.pk)


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ExclusaoDeExtratoArquivoFisicoTest(TransactionTestCase):
    """
    Item 5: o arquivo físico do extrato só é removido do storage DEPOIS do
    commit da transação — nunca antes, e nunca se a transação reverter.
    Usa TransactionTestCase (commits reais) porque transaction.on_commit()
    nunca dispara dentro do bloco atômico externo que envolve cada teste em
    TestCase comum.
    """
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta Arquivo Físico')

    def _extrato_pendente_com_arquivo(self, hash_arquivo):
        arquivo = SimpleUploadedFile('extrato_fisico.ofx', b'conteudo ofx de teste', content_type='application/x-ofx')
        extrato = ExtratoImportado.objects.create(
            conta=self.conta, arquivo=arquivo, hash_arquivo=hash_arquivo,
        )
        TransacaoExtrato.objects.create(
            extrato=extrato, conta=self.conta, fitid='af-1',
            data=date(2026, 3, 1), valor=Decimal('100.00'), tipo='credito', descricao='X',
        )
        return extrato

    def test_arquivo_fisico_excluido_apos_commit(self):
        import os
        from .services import excluir_extrato_sem_movimentacoes

        extrato = self._extrato_pendente_com_arquivo('hash-arquivo-fisico-1')
        caminho = extrato.arquivo.path
        self.assertTrue(os.path.exists(caminho))

        excluir_extrato_sem_movimentacoes(extrato.pk)

        self.assertFalse(os.path.exists(caminho))
        self.assertFalse(ExtratoImportado.objects.filter(pk=extrato.pk).exists())

    def test_rollback_mantem_arquivo(self):
        import os
        from django.db import transaction
        from .services import excluir_extrato_sem_movimentacoes

        extrato = self._extrato_pendente_com_arquivo('hash-arquivo-fisico-2')
        caminho = extrato.arquivo.path
        self.assertTrue(os.path.exists(caminho))

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                excluir_extrato_sem_movimentacoes(extrato.pk)
                # Força o rollback da transação EXTERNA depois que o service
                # (que também é @transaction.atomic, mas aqui vira apenas um
                # savepoint aninhado) já concluiu — o on_commit() registrado
                # por ele só dispararia no commit desta transação externa,
                # que nunca acontece.
                raise RuntimeError('força rollback após a exclusão')

        self.assertTrue(os.path.exists(caminho))
        self.assertTrue(ExtratoImportado.objects.filter(pk=extrato.pk).exists())


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class PermissoesResiduaisTest(TestCase):
    """
    Item 6 (rodada final): nenhuma tela consulta ou constrói dados de uma
    área para a qual o usuário não tem permissão — mesmo quando o template
    esconderia o resultado.
    """
    def setUp(self):
        self.client = Client()
        self.conta = ContaBancaria.objects.create(nome='Conta PR')
        self.imovel, self.locatario = _base()
        self.contrato = _contrato(self.imovel, self.locatario)

    def _usuario(self, nome, *codenames):
        user = User.objects.create_user(nome, password='pass')
        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.client.login(username=nome, password='pass')
        return user

    def test_extrato_list_somente_view_nao_ve_upload_nem_consulta_contas(self):
        self._usuario('pr_so_view_extrato', 'view_extratoimportado')
        resposta = self.client.get(reverse('extrato_list'))
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(resposta.context['pode_importar_extrato'])
        self.assertIsNone(resposta.context['form'])
        self.assertIsNone(resposta.context['tem_conta'])
        conteudo = resposta.content.decode()
        self.assertNotIn('Importar Extrato (OFX)', conteudo)
        self.assertNotIn('name="arquivo"', conteudo)

    def test_extrato_list_com_add_extratoimportado_mostra_upload(self):
        self._usuario('pr_com_add_extrato', 'view_extratoimportado', 'add_extratoimportado')
        resposta = self.client.get(reverse('extrato_list'))
        self.assertTrue(resposta.context['pode_importar_extrato'])
        self.assertIsNotNone(resposta.context['form'])
        self.assertIn('Importar Extrato (OFX)', resposta.content.decode())

    def test_conciliar_sem_permissao_de_receitas_nao_consulta_candidatas(self):
        from unittest.mock import patch
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))
        extrato = importar_ofx(_arquivo_ofx([('pr1', '20260312', '2000.00', 'PIX')]), self.conta)
        self._usuario('pr_sem_receitas', 'view_extratoimportado')

        with patch('conciliacao.views.receitas_candidatas') as mock_candidatas, \
             patch('conciliacao.views.sugerir_receitas') as mock_sugerir:
            resposta = self.client.get(reverse('conciliar_extrato', args=[extrato.pk]))
        self.assertEqual(resposta.status_code, 200)
        mock_candidatas.assert_not_called()
        mock_sugerir.assert_not_called()
        self.assertEqual(resposta.context['pendentes'][0]['candidatas'], [])
        self.assertNotIn(receita.imovel.nome, resposta.content.decode())

    def test_conciliar_com_permissao_de_receitas_consulta_candidatas(self):
        _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))
        extrato = importar_ofx(_arquivo_ofx([('pr2', '20260312', '2000.00', 'PIX')]), self.conta)
        self._usuario('pr_com_receitas', 'view_extratoimportado', 'change_receitaaluguel')
        resposta = self.client.get(reverse('conciliar_extrato', args=[extrato.pk]))
        self.assertEqual(len(resposta.context['pendentes'][0]['candidatas']), 1)

    def test_conciliar_sem_permissao_de_despesa_nao_constroi_formulario(self):
        from unittest.mock import patch
        extrato = importar_ofx(_arquivo_ofx([('pr3', '20260312', '-350.00', 'CEMIG')]), self.conta)
        self._usuario('pr_sem_despesa', 'view_extratoimportado')

        with patch('conciliacao.views.sugerir_classificacao') as mock_sugerir:
            resposta = self.client.get(reverse('conciliar_extrato', args=[extrato.pk]))
        self.assertEqual(resposta.status_code, 200)
        mock_sugerir.assert_not_called()
        self.assertIsNone(resposta.context['despesa_form'])
        self.assertNotIn('name="categoria"', resposta.content.decode())

    def test_conciliar_com_permissao_de_despesa_constroi_formulario(self):
        extrato = importar_ofx(_arquivo_ofx([('pr4', '20260312', '-350.00', 'CEMIG')]), self.conta)
        self._usuario('pr_com_despesa', 'view_extratoimportado', 'add_despesa')
        resposta = self.client.get(reverse('conciliar_extrato', args=[extrato.pk]))
        self.assertIsNotNone(resposta.context['despesa_form'])

    def test_posts_de_conciliacao_continuam_exigindo_permissao(self):
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))
        extrato = importar_ofx(_arquivo_ofx([('pr5', '20260312', '2000.00', 'PIX')]), self.conta)
        transacao = extrato.transacoes.get()
        self._usuario('pr_post_sem_perm', 'view_extratoimportado')

        resposta = self.client.post(reverse('conciliar_extrato', args=[extrato.pk]), {
            'transacao_id': transacao.pk, 'action': 'conciliar', 'receita_ids': [receita.pk],
        })
        self.assertEqual(resposta.status_code, 403)
        transacao.refresh_from_db()
        self.assertEqual(transacao.status, 'pendente')

    def test_relatorios_sem_view_contrato_nao_consulta_imobiliarias(self):
        from unittest.mock import patch
        user = User.objects.create_user('pr_sem_contrato', password='pass')
        user.user_permissions.add(*Permission.objects.filter(codename='view_receitaaluguel'))
        self.client.login(username='pr_sem_contrato', password='pass')

        with patch('financeiro.views.imobiliarias_queryset') as mock_imob:
            resposta = self.client.get(reverse('relatorios'))
        self.assertEqual(resposta.status_code, 200)
        mock_imob.assert_not_called()
        self.assertEqual(list(resposta.context['imobiliarias']), [])

    def test_relatorios_com_view_contrato_consulta_imobiliarias(self):
        Pessoa.objects.create(nome='Imob PR', tipo='imobiliaria')
        user = User.objects.create_user('pr_com_contrato', password='pass')
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=['view_contrato', 'view_receitaaluguel'],
        ))
        self.client.login(username='pr_com_contrato', password='pass')
        resposta = self.client.get(reverse('relatorios'))
        self.assertEqual(len(resposta.context['imobiliarias']), 1)


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class PermissoesDetalhesTransacoesTratadasTest(TestCase):
    """
    Item 3 (rodada de fechamento estrutural): ver a lista de extratos
    (conciliacao.view_extratoimportado) não concede acesso aos detalhes
    internos das transações TRATADAS — descrição/categoria/fornecedor/imóvel
    de uma despesa, e imóvel/locatário/valores de receitas vinculadas, só
    aparecem para quem também tem a permissão de VER aquela área (e
    patrimonio.view_imovel, já que ambas expõem nome de imóvel).
    """

    def setUp(self):
        self.client = Client()
        self.conta = ContaBancaria.objects.create(nome='Conta PDT')
        self.imovel, self.locatario = _base()
        self.contrato = _contrato(self.imovel, self.locatario)
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hpdt')

        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))
        tx_credito = TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid='pdt-c',
            data=date(2026, 3, 12), valor=Decimal('2000.00'), tipo='credito', descricao='PIX',
        )
        conciliar_com_receitas(tx_credito, [receita])

        tx_debito = TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid='pdt-d',
            data=date(2026, 3, 12), valor=Decimal('-350.00'), tipo='debito', descricao='CEMIG',
        )
        lancar_despesa(tx_debito, categoria='outro', descricao='Energia elétrica')

    def _usuario(self, nome, *codenames):
        user = User.objects.create_user(nome, password='pass')
        user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        self.client.login(username=nome, password='pass')
        return user

    def test_somente_view_extrato_nao_ve_detalhes_e_nao_consulta_dados_protegidos(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self._usuario('pdt_so_extrato', 'view_extratoimportado')

        with CaptureQueriesContext(connection) as ctx:
            resposta = self.client.get(reverse('conciliar_extrato', args=[self.extrato.pk]))
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(resposta.context['pode_ver_detalhes_despesas'])
        self.assertFalse(resposta.context['pode_ver_detalhes_receitas'])

        conteudo = resposta.content.decode()
        self.assertIn('Débito conciliado', conteudo)
        self.assertIn('Crédito conciliado', conteudo)
        self.assertNotIn('Energia elétrica', conteudo)
        self.assertNotIn(self.imovel.nome, conteudo)

        sql_junto = ' '.join(q['sql'] for q in ctx.captured_queries).lower()
        self.assertNotIn('financeiro_despesa', sql_junto)
        self.assertNotIn('conciliacao_conciliacaoreceita', sql_junto)

    def test_extrato_mais_view_despesa_e_view_imovel_mostra_apenas_despesa(self):
        self._usuario(
            'pdt_com_despesa', 'view_extratoimportado', 'view_despesa', 'view_imovel',
        )
        resposta = self.client.get(reverse('conciliar_extrato', args=[self.extrato.pk]))
        self.assertTrue(resposta.context['pode_ver_detalhes_despesas'])
        self.assertFalse(resposta.context['pode_ver_detalhes_receitas'])

        conteudo = resposta.content.decode()
        self.assertIn('Energia elétrica', conteudo)
        self.assertIn('Crédito conciliado', conteudo)

    def test_extrato_mais_view_receita_e_view_imovel_mostra_apenas_receita(self):
        self._usuario(
            'pdt_com_receita', 'view_extratoimportado', 'view_receitaaluguel', 'view_imovel',
        )
        resposta = self.client.get(reverse('conciliar_extrato', args=[self.extrato.pk]))
        self.assertFalse(resposta.context['pode_ver_detalhes_despesas'])
        self.assertTrue(resposta.context['pode_ver_detalhes_receitas'])

        conteudo = resposta.content.decode()
        self.assertIn('Débito conciliado', conteudo)
        self.assertIn(self.imovel.nome, conteudo)

    def test_todas_as_permissoes_mostra_ambos(self):
        self._usuario(
            'pdt_tudo', 'view_extratoimportado', 'view_despesa', 'view_receitaaluguel', 'view_imovel',
        )
        resposta = self.client.get(reverse('conciliar_extrato', args=[self.extrato.pk]))
        self.assertTrue(resposta.context['pode_ver_detalhes_despesas'])
        self.assertTrue(resposta.context['pode_ver_detalhes_receitas'])

        conteudo = resposta.content.decode()
        self.assertIn('Energia elétrica', conteudo)
        self.assertIn(self.imovel.nome, conteudo)

    def test_sem_permissao_de_patrimonio_esconde_ambos_mesmo_com_financeiro(self):
        """Sem patrimonio.view_imovel, nem despesa nem receita aparecem
        detalhadas — a apresentação evita expor nome de imóvel sem essa
        permissão, mesmo que financeiro.view_despesa/view_receitaaluguel
        estejam presentes."""
        self._usuario(
            'pdt_sem_patrimonio', 'view_extratoimportado', 'view_despesa', 'view_receitaaluguel',
        )
        resposta = self.client.get(reverse('conciliar_extrato', args=[self.extrato.pk]))
        self.assertFalse(resposta.context['pode_ver_detalhes_despesas'])
        self.assertFalse(resposta.context['pode_ver_detalhes_receitas'])

        conteudo = resposta.content.decode()
        self.assertIn('Débito conciliado', conteudo)
        self.assertIn('Crédito conciliado', conteudo)
        self.assertNotIn('Energia elétrica', conteudo)
        self.assertNotIn(self.imovel.nome, conteudo)

    def test_acoes_continuam_protegidas_no_servidor_independente_de_ver_detalhes(self):
        """Ver detalhes (view_despesa/view_receitaaluguel) é diferente de
        poder AGIR (add_despesa/change_receitaaluguel) — um usuário com
        visão completa mas sem permissão de escrita continua barrado no
        POST, servidor confirma independentemente do que o template mostra."""
        self._usuario(
            'pdt_ve_mas_nao_altera', 'view_extratoimportado', 'view_despesa',
            'view_receitaaluguel', 'view_imovel',
        )
        tx_debito = TransacaoExtrato.objects.get(fitid='pdt-d')
        resposta = self.client.post(reverse('conciliar_extrato', args=[self.extrato.pk]), {
            'transacao_id': tx_debito.pk, 'action': 'desfazer',
        })
        self.assertEqual(resposta.status_code, 403)


# ─── Matriz de permissões das ações da conciliação (esta tarefa) ──────────────

def _gerar_repasse_liquido(conta, extrato, locatario, fitid, nome_imovel):
    """
    Monta um crédito de repasse líquido conciliável: contrato com
    imobiliária + 10% de comissão, receita de R$2000 e comissão automática
    de R$200 (líquido = R$1800). Retorna (tx_credito, receita, comissao).
    """
    from financeiro.services import gerar_receitas_para_contrato
    imob = Pessoa.objects.create(nome=f'Imob {fitid}', tipo='imobiliaria')
    imovel = Imovel.objects.create(nome=nome_imovel, endereco='X', cidade='BH', estado='MG')
    contrato = _contrato(
        imovel, locatario, valor_aluguel=Decimal('2000.00'), imobiliaria=imob,
        comissao_imobiliaria_percentual=Decimal('10.00'),
        data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
    )
    gerar_receitas_para_contrato(contrato, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
    receita = ReceitaAluguel.objects.get(contrato=contrato)
    comissao = Despesa.objects.filter(contrato=contrato, origem_automatica=True).first()
    tx = TransacaoExtrato.objects.create(
        extrato=extrato, conta=conta, fitid=fitid,
        data=date(2026, 3, 12), valor=Decimal('1800.00'), tipo='credito', descricao='REPASSE',
    )
    return tx, receita, comissao


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class MatrizPermissoesAcoesTest(TestCase):
    """
    Cada POST da tela de conciliação exige TODAS as permissões dos efeitos
    da operação (transação + receitas/despesas). Falta de qualquer uma →
    403, verificado no servidor ANTES de qualquer service, sem efeito
    colateral no banco.
    """
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta MPA')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hmpa')
        self.imovel, self.locatario = _base()
        self.contrato = _contrato(self.imovel, self.locatario)

    def _cliente(self, nome, *codenames):
        user = User.objects.create_user(nome, password='pass')
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=('view_extratoimportado',) + codenames
        ))
        client = Client()
        client.login(username=nome, password='pass')
        return client

    def _post(self, client, transacao, action, **extra):
        dados = {'transacao_id': transacao.pk, 'action': action}
        dados.update(extra)
        return client.post(reverse('conciliar_extrato', args=[self.extrato.pk]), dados)

    def _tx_credito(self, fitid='mpa-c', valor='2000.00'):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal(valor), tipo='credito', descricao='PIX',
        )

    def _tx_debito(self, fitid='mpa-d', valor='-350.00'):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal(valor), tipo='debito', descricao='CEMIG',
        )

    # ── Conciliar crédito (1-5) ──────────────────────────────────────────
    def test_conciliar_somente_change_receitaaluguel_403(self):
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))
        tx = self._tx_credito()
        client = self._cliente('mpa1', 'change_receitaaluguel')
        resposta = self._post(client, tx, 'conciliar', receita_ids=[receita.pk])
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db(); receita.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertIsNone(receita.valor_recebido)
        self.assertEqual(receita.recebimentos.count(), 0)
        self.assertEqual(tx.itens_receita.count(), 0)

    def test_conciliar_somente_change_transacaoextrato_403(self):
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))
        tx = self._tx_credito()
        client = self._cliente('mpa2', 'change_transacaoextrato')
        resposta = self._post(client, tx, 'conciliar', receita_ids=[receita.pk])
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertEqual(tx.itens_receita.count(), 0)

    def test_conciliar_com_as_duas_permissoes_funciona(self):
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))
        tx = self._tx_credito()
        client = self._cliente('mpa3', 'change_transacaoextrato', 'change_receitaaluguel')
        resposta = self._post(client, tx, 'conciliar', receita_ids=[receita.pk])
        self.assertEqual(resposta.status_code, 302)
        tx.refresh_from_db(); receita.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(receita.status, 'recebido')

    def test_conciliar_marcar_comissoes_sem_change_despesa_403(self):
        from .models import ConciliacaoComissao
        tx, receita, comissao = _gerar_repasse_liquido(
            self.conta, self.extrato, self.locatario, 'mpa-rl4', 'Sala MPA4',
        )
        client = self._cliente('mpa4', 'change_transacaoextrato', 'change_receitaaluguel')
        resposta = self._post(client, tx, 'conciliar', receita_ids=[receita.pk], marcar_comissoes='1')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db(); receita.refresh_from_db(); comissao.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertIsNone(receita.valor_recebido)
        self.assertEqual(receita.recebimentos.count(), 0)
        self.assertEqual(comissao.status, 'prevista')
        self.assertEqual(ConciliacaoComissao.objects.filter(transacao=tx).count(), 0)

    def test_conciliar_marcar_comissoes_com_as_tres_permissoes_funciona(self):
        tx, receita, comissao = _gerar_repasse_liquido(
            self.conta, self.extrato, self.locatario, 'mpa-rl5', 'Sala MPA5',
        )
        client = self._cliente(
            'mpa5', 'change_transacaoextrato', 'change_receitaaluguel', 'change_despesa',
        )
        resposta = self._post(client, tx, 'conciliar', receita_ids=[receita.pk], marcar_comissoes='1')
        self.assertEqual(resposta.status_code, 302)
        tx.refresh_from_db(); receita.refresh_from_db(); comissao.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(receita.status, 'recebido')
        self.assertEqual(comissao.status, 'paga')

    # ── Lançar despesa (6-8) ─────────────────────────────────────────────
    def test_lancar_despesa_somente_add_despesa_403(self):
        tx = self._tx_debito()
        client = self._cliente('mpa6', 'add_despesa')
        resposta = self._post(client, tx, 'lancar_despesa', categoria='outro', descricao='Energia')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertIsNone(tx.despesa_id)
        self.assertEqual(Despesa.objects.count(), 0)

    def test_lancar_despesa_somente_change_transacaoextrato_403(self):
        tx = self._tx_debito()
        client = self._cliente('mpa7', 'change_transacaoextrato')
        resposta = self._post(client, tx, 'lancar_despesa', categoria='outro', descricao='Energia')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertEqual(Despesa.objects.count(), 0)

    def test_lancar_despesa_com_as_duas_permissoes_funciona(self):
        tx = self._tx_debito()
        client = self._cliente('mpa8', 'change_transacaoextrato', 'add_despesa')
        resposta = self._post(client, tx, 'lancar_despesa', categoria='outro', descricao='Energia')
        self.assertEqual(resposta.status_code, 302)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertIsNotNone(tx.despesa_id)
        self.assertEqual(Despesa.objects.count(), 1)

    # ── Ignorar e reabrir (9-12) ─────────────────────────────────────────
    def test_ignorar_sem_change_transacaoextrato_403(self):
        tx = self._tx_debito(fitid='mpa-ign', valor='-10.00')
        client = self._cliente('mpa9')  # só view_extratoimportado
        resposta = self._post(client, tx, 'ignorar')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')

    def test_ignorar_com_change_transacaoextrato_funciona(self):
        tx = self._tx_debito(fitid='mpa-ign2', valor='-10.00')
        client = self._cliente('mpa10', 'change_transacaoextrato')
        resposta = self._post(client, tx, 'ignorar')
        self.assertEqual(resposta.status_code, 302)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'ignorada')

    def test_reabrir_sem_change_transacaoextrato_403(self):
        tx = self._tx_debito(fitid='mpa-reab', valor='-10.00')
        tx.status = 'ignorada'; tx.save(update_fields=['status'])
        client = self._cliente('mpa11')  # só view_extratoimportado
        resposta = self._post(client, tx, 'reabrir')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'ignorada')

    def test_reabrir_com_change_transacaoextrato_funciona(self):
        tx = self._tx_debito(fitid='mpa-reab2', valor='-10.00')
        tx.status = 'ignorada'; tx.save(update_fields=['status'])
        client = self._cliente('mpa12', 'change_transacaoextrato')
        resposta = self._post(client, tx, 'reabrir')
        self.assertEqual(resposta.status_code, 302)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')

    # ── Desfazer crédito (13-17) ─────────────────────────────────────────
    def _conciliar_credito_simples(self, fitid):
        # contrato próprio por chamada — evita colisão de competência
        # (contrato+mês+ano é único em ReceitaAluguel).
        imovel = Imovel.objects.create(nome=f'Sala {fitid}', endereco='X', cidade='BH', estado='MG')
        contrato = _contrato(imovel, self.locatario)
        receita = _receita(contrato, date(2026, 3, 10), Decimal('2000.00'))
        tx = self._tx_credito(fitid=fitid)
        conciliar_com_receitas(tx, [receita])
        tx.refresh_from_db(); receita.refresh_from_db()
        return tx, receita

    def test_desfazer_credito_sem_comissoes_com_as_duas_permissoes_funciona(self):
        tx, receita = self._conciliar_credito_simples('mpa-dc13')
        client = self._cliente('mpa13', 'change_transacaoextrato', 'change_receitaaluguel')
        resposta = self._post(client, tx, 'desfazer')
        self.assertEqual(resposta.status_code, 302)
        tx.refresh_from_db(); receita.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertEqual(receita.recebimentos.count(), 0)
        self.assertEqual(tx.itens_receita.count(), 0)

    def test_desfazer_credito_faltando_uma_permissao_403_preserva_vinculos(self):
        # falta change_receitaaluguel
        tx, receita = self._conciliar_credito_simples('mpa-dc14a')
        client = self._cliente('mpa14a', 'change_transacaoextrato')
        resposta = self._post(client, tx, 'desfazer')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(receita.recebimentos.count(), 1)
        self.assertEqual(tx.itens_receita.count(), 1)

        # falta change_transacaoextrato
        tx2, receita2 = self._conciliar_credito_simples('mpa-dc14b')
        client2 = self._cliente('mpa14b', 'change_receitaaluguel')
        resposta2 = self._post(client2, tx2, 'desfazer')
        self.assertEqual(resposta2.status_code, 403)
        tx2.refresh_from_db()
        self.assertEqual(tx2.status, 'conciliada')
        self.assertEqual(receita2.recebimentos.count(), 1)
        self.assertEqual(tx2.itens_receita.count(), 1)

    def test_desfazer_credito_com_comissoes_exige_change_despesa(self):
        from .models import ConciliacaoComissao
        tx, receita, comissao = _gerar_repasse_liquido(
            self.conta, self.extrato, self.locatario, 'mpa-dc15', 'Sala MPA15',
        )
        conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        client = self._cliente(
            'mpa15', 'change_transacaoextrato', 'change_receitaaluguel', 'change_despesa',
        )
        resposta = self._post(client, tx, 'desfazer')
        self.assertEqual(resposta.status_code, 302)
        tx.refresh_from_db(); comissao.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertEqual(comissao.status, 'prevista')
        self.assertEqual(ConciliacaoComissao.objects.filter(transacao=tx).count(), 0)

    def test_desfazer_credito_com_comissoes_sem_change_despesa_preserva_tudo(self):
        from .models import ConciliacaoComissao
        from financeiro.models import RecebimentoReceita
        tx, receita, comissao = _gerar_repasse_liquido(
            self.conta, self.extrato, self.locatario, 'mpa-dc16', 'Sala MPA16',
        )
        conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        client = self._cliente('mpa16', 'change_transacaoextrato', 'change_receitaaluguel')
        resposta = self._post(client, tx, 'desfazer')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db(); receita.refresh_from_db(); comissao.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(receita.status, 'recebido')
        self.assertEqual(RecebimentoReceita.objects.filter(receita=receita).count(), 1)
        self.assertEqual(comissao.status, 'paga')
        self.assertEqual(ConciliacaoComissao.objects.filter(transacao=tx).count(), 1)

    def test_desfazer_credito_com_comissoes_todas_as_permissoes_funciona(self):
        tx, receita, comissao = _gerar_repasse_liquido(
            self.conta, self.extrato, self.locatario, 'mpa-dc17', 'Sala MPA17',
        )
        conciliar_com_receitas(tx, [receita], marcar_comissoes=True)
        client = self._cliente(
            'mpa17', 'change_transacaoextrato', 'change_receitaaluguel', 'change_despesa',
        )
        resposta = self._post(client, tx, 'desfazer')
        self.assertEqual(resposta.status_code, 302)
        tx.refresh_from_db(); receita.refresh_from_db(); comissao.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertIn(receita.status, ('previsto', 'atrasado'))
        self.assertEqual(comissao.status, 'prevista')

    # ── Desfazer débito (18-21) ──────────────────────────────────────────
    def _lancar_debito(self, fitid):
        tx = self._tx_debito(fitid=fitid, valor='-350.00')
        despesa = lancar_despesa(tx, categoria='outro', descricao='Energia')
        tx.refresh_from_db()
        return tx, despesa

    def test_desfazer_debito_change_transacaoextrato_sem_delete_despesa_403(self):
        tx, despesa = self._lancar_debito('mpa-dd18')
        client = self._cliente('mpa18', 'change_transacaoextrato')
        resposta = self._post(client, tx, 'desfazer')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(tx.despesa_id, despesa.pk)
        self.assertTrue(Despesa.objects.filter(pk=despesa.pk).exists())

    def test_desfazer_debito_delete_despesa_sem_change_transacaoextrato_403(self):
        tx, despesa = self._lancar_debito('mpa-dd19')
        client = self._cliente('mpa19', 'delete_despesa')
        resposta = self._post(client, tx, 'desfazer')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertTrue(Despesa.objects.filter(pk=despesa.pk).exists())

    def test_desfazer_debito_com_ambas_as_permissoes_funciona(self):
        tx, despesa = self._lancar_debito('mpa-dd20')
        client = self._cliente('mpa20', 'change_transacaoextrato', 'delete_despesa')
        resposta = self._post(client, tx, 'desfazer')
        self.assertEqual(resposta.status_code, 302)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'pendente')
        self.assertIsNone(tx.despesa_id)
        self.assertFalse(Despesa.objects.filter(pk=despesa.pk).exists())

    def test_desfazer_debito_403_preserva_despesa_e_transacao(self):
        tx, despesa = self._lancar_debito('mpa-dd21')
        client = self._cliente('mpa21')  # nenhuma permissão de ação
        resposta = self._post(client, tx, 'desfazer')
        self.assertEqual(resposta.status_code, 403)
        tx.refresh_from_db()
        self.assertEqual(tx.status, 'conciliada')
        self.assertEqual(tx.despesa_id, despesa.pk)
        self.assertTrue(Despesa.objects.filter(pk=despesa.pk).exists())


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class MatrizPermissoesTemplateTest(TestCase):
    """
    Os controles (botões/formulários) só aparecem no HTML quando o usuário
    tem TODAS as permissões da ação — a matriz do botão nunca diverge da do
    POST. Verificação de AUSÊNCIA no HTML, não só o 403 do POST.
    """
    def setUp(self):
        self.conta = ContaBancaria.objects.create(nome='Conta MPT')
        self.extrato = ExtratoImportado.objects.create(conta=self.conta, hash_arquivo='hmpt')
        self.imovel, self.locatario = _base()
        self.contrato = _contrato(self.imovel, self.locatario)

    def _cliente(self, nome, *codenames):
        # view_despesa/view_receitaaluguel/view_imovel para poder VER os
        # detalhes das transações tratadas (item 3) — ortogonal às ações.
        user = User.objects.create_user(nome, password='pass')
        user.user_permissions.add(*Permission.objects.filter(
            codename__in=('view_extratoimportado', 'view_despesa',
                          'view_receitaaluguel', 'view_imovel') + codenames
        ))
        client = Client()
        client.login(username=nome, password='pass')
        return client

    def _get(self, client):
        return client.get(reverse('conciliar_extrato', args=[self.extrato.pk]))

    def _tx_credito_pendente(self, fitid='mpt-c'):
        _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal('2000.00'), tipo='credito', descricao='PIX',
        )

    def _tx_debito_pendente(self, fitid='mpt-d'):
        return TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid=fitid,
            data=date(2026, 3, 12), valor=Decimal('-350.00'), tipo='debito', descricao='CEMIG',
        )

    # 22: botão de conciliar crédito não aparece sem as duas permissões
    def test_botao_conciliar_credito_exige_as_duas_permissoes(self):
        self._tx_credito_pendente()
        # só change_receitaaluguel → sem botão
        html = self._get(self._cliente('mpt22a', 'change_receitaaluguel')).content.decode()
        self.assertNotIn('value="conciliar"', html)
        # só change_transacaoextrato → sem botão
        html = self._get(self._cliente('mpt22b', 'change_transacaoextrato')).content.decode()
        self.assertNotIn('value="conciliar"', html)
        # ambas → botão aparece
        html = self._get(self._cliente('mpt22c', 'change_transacaoextrato', 'change_receitaaluguel')).content.decode()
        self.assertIn('value="conciliar"', html)

    # 23: opção de marcar comissões não aparece sem change_despesa
    def test_opcao_marcar_comissoes_exige_change_despesa(self):
        from financeiro.services import gerar_receitas_para_contrato
        # A sugestão de repasse líquido (que faz o checkbox de comissões
        # aparecer) exige >= 2 receitas da MESMA imobiliária. Duas de R$2000
        # com 10% → bruto 4000, comissões 400, líquido 3600.
        imob = Pessoa.objects.create(nome='Imob MPT23', tipo='imobiliaria')
        for i in range(2):
            c = _contrato(
                Imovel.objects.create(nome=f'Sala MPT23-{i}', endereco='X', cidade='BH', estado='MG'),
                self.locatario, valor_aluguel=Decimal('2000.00'), imobiliaria=imob,
                comissao_imobiliaria_percentual=Decimal('10.00'),
                data_inicio=date(2026, 1, 1), data_fim=date(2026, 12, 31),
            )
            gerar_receitas_para_contrato(c, data_inicio=date(2026, 3, 1), data_fim=date(2026, 3, 31))
        TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid='mpt-rl23',
            data=date(2026, 3, 12), valor=Decimal('3600.00'), tipo='credito', descricao='REPASSE',
        )
        # sem change_despesa: conciliar sim, comissões não
        html = self._get(self._cliente('mpt23a', 'change_transacaoextrato', 'change_receitaaluguel')).content.decode()
        self.assertIn('value="conciliar"', html)
        self.assertNotIn('name="marcar_comissoes"', html)
        # com change_despesa: checkbox aparece
        html = self._get(self._cliente(
            'mpt23b', 'change_transacaoextrato', 'change_receitaaluguel', 'change_despesa',
        )).content.decode()
        self.assertIn('name="marcar_comissoes"', html)

    # 24: formulário de lançar despesa não aparece sem as duas permissões
    def test_form_lancar_despesa_exige_as_duas_permissoes(self):
        self._tx_debito_pendente()
        html = self._get(self._cliente('mpt24a', 'add_despesa')).content.decode()
        self.assertNotIn('value="lancar_despesa"', html)
        html = self._get(self._cliente('mpt24b', 'change_transacaoextrato')).content.decode()
        self.assertNotIn('value="lancar_despesa"', html)
        html = self._get(self._cliente('mpt24c', 'change_transacaoextrato', 'add_despesa')).content.decode()
        self.assertIn('value="lancar_despesa"', html)

    # 25: ignorar e reabrir respeitam change_transacaoextrato
    def test_botoes_ignorar_e_reabrir_exigem_change_transacaoextrato(self):
        self._tx_debito_pendente(fitid='mpt-ign')
        ignorada = self._tx_debito_pendente(fitid='mpt-reab')
        ignorada.status = 'ignorada'; ignorada.save(update_fields=['status'])

        html = self._get(self._cliente('mpt25a')).content.decode()  # só view
        self.assertNotIn('value="ignorar"', html)
        self.assertNotIn('value="reabrir"', html)

        html = self._get(self._cliente('mpt25b', 'change_transacaoextrato')).content.decode()
        self.assertIn('value="ignorar"', html)
        self.assertIn('value="reabrir"', html)

    # 26: botão desfazer crédito segue a matriz de crédito
    def test_botao_desfazer_credito_segue_matriz_de_credito(self):
        receita = _receita(self.contrato, date(2026, 3, 10), Decimal('2000.00'))
        tx = TransacaoExtrato.objects.create(
            extrato=self.extrato, conta=self.conta, fitid='mpt-dc',
            data=date(2026, 3, 12), valor=Decimal('2000.00'), tipo='credito', descricao='PIX',
        )
        conciliar_com_receitas(tx, [receita])
        # só change_transacaoextrato (falta change_receitaaluguel) → sem botão
        html = self._get(self._cliente('mpt26a', 'change_transacaoextrato')).content.decode()
        self.assertNotIn('value="desfazer"', html)
        # matriz completa de crédito → botão aparece
        html = self._get(self._cliente('mpt26b', 'change_transacaoextrato', 'change_receitaaluguel')).content.decode()
        self.assertIn('value="desfazer"', html)

    # 27: botão desfazer débito segue a matriz de débito
    def test_botao_desfazer_debito_segue_matriz_de_debito(self):
        tx = self._tx_debito_pendente(fitid='mpt-dd')
        lancar_despesa(tx, categoria='outro', descricao='Energia')
        # change_transacaoextrato + change_receitaaluguel (matriz de crédito)
        # NÃO basta para débito (precisa delete_despesa) → sem botão
        html = self._get(self._cliente('mpt27a', 'change_transacaoextrato', 'change_receitaaluguel')).content.decode()
        self.assertNotIn('value="desfazer"', html)
        # matriz de débito (change_transacaoextrato + delete_despesa) → botão
        html = self._get(self._cliente('mpt27b', 'change_transacaoextrato', 'delete_despesa')).content.decode()
        self.assertIn('value="desfazer"', html)
