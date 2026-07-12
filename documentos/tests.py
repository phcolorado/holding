from datetime import timedelta

from django.contrib.auth.models import User
from core.test_utils import com_leitura
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Documento, documentos_vencendo_qs
from patrimonio.models import Imovel

import tempfile

MEDIA_TEMP = tempfile.mkdtemp(prefix='holding_test_media_')


def _arquivo(nome='doc.pdf'):
    return SimpleUploadedFile(nome, b'%PDF-1.4 conteudo de teste', content_type='application/pdf')


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class ValidadeDocumentoTest(TestCase):
    def setUp(self):
        self.imovel = Imovel.objects.create(nome='Imóvel Doc', endereco='Rua A', cidade='SP', estado='SP')
        self.hoje = timezone.localdate()

    def _doc(self, validade, titulo='Seguro'):
        return Documento.objects.create(
            titulo=titulo, tipo='seguro', arquivo=_arquivo(),
            imovel=self.imovel, data_validade=validade,
        )

    def test_documento_vencido(self):
        doc = self._doc(self.hoje - timedelta(days=1))
        self.assertTrue(doc.vencido)
        self.assertFalse(doc.vence_em_breve)

    def test_documento_vencendo_em_breve(self):
        doc = self._doc(self.hoje + timedelta(days=15))
        self.assertFalse(doc.vencido)
        self.assertTrue(doc.vence_em_breve)

    def test_documento_com_validade_distante_nao_alerta(self):
        doc = self._doc(self.hoje + timedelta(days=90))
        self.assertFalse(doc.vencido)
        self.assertFalse(doc.vence_em_breve)

    def test_documento_sem_validade_nao_alerta(self):
        doc = self._doc(None)
        self.assertFalse(doc.vencido)
        self.assertFalse(doc.vence_em_breve)

    def test_queryset_vencendo(self):
        vencido = self._doc(self.hoje - timedelta(days=10), 'Vencido')
        em_breve = self._doc(self.hoje + timedelta(days=10), 'Em breve')
        distante = self._doc(self.hoje + timedelta(days=90), 'Distante')
        sem_validade = self._doc(None, 'Sem validade')

        pks = set(documentos_vencendo_qs().values_list('pk', flat=True))
        self.assertIn(vencido.pk, pks)
        self.assertIn(em_breve.pk, pks)
        self.assertNotIn(distante.pk, pks)
        self.assertNotIn(sem_validade.pk, pks)

    def test_filtro_vencendo_na_lista(self):
        com_leitura(User.objects.create_user('doc_venc_user', password='pass'))
        client = Client()
        client.login(username='doc_venc_user', password='pass')

        vencido = self._doc(self.hoje - timedelta(days=10), 'Vencido')
        distante = self._doc(self.hoje + timedelta(days=90), 'Distante')

        response = client.get(reverse('documento_list'), {'vencendo': '1'})
        docs = list(response.context['documentos'])
        self.assertIn(vencido, docs)
        self.assertNotIn(distante, docs)

    def test_checklist_inclui_item_de_validade(self):
        com_leitura(User.objects.create_user('doc_chk_user', password='pass'))
        client = Client()
        client.login(username='doc_chk_user', password='pass')

        self._doc(self.hoje - timedelta(days=1))
        response = client.get(reverse('checklist_mensal'))
        itens = {item['item']: item for item in response.context['checklist']}
        item = itens['Validade dos documentos em dia']
        self.assertFalse(item['ok'])


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class UploadValidacaoTest(TestCase):
    def setUp(self):
        self.imovel = Imovel.objects.create(nome='Imóvel Upload', endereco='Rua B', cidade='SP', estado='SP')

    def test_extensao_permitida_passa(self):
        doc = Documento(titulo='Contrato', tipo='contrato_aluguel', arquivo=_arquivo('contrato.pdf'), imovel=self.imovel)
        doc.full_clean()  # não deve levantar

    def test_extensao_proibida_rejeitada(self):
        executavel = SimpleUploadedFile('virus.exe', b'MZ...', content_type='application/octet-stream')
        doc = Documento(titulo='Suspeito', tipo='outro', arquivo=executavel, imovel=self.imovel)
        with self.assertRaises(ValidationError):
            doc.full_clean()

    def test_arquivo_grande_demais_rejeitado(self):
        from .models import TAMANHO_MAXIMO_UPLOAD_MB
        grande = SimpleUploadedFile('grande.pdf', b'x' * (TAMANHO_MAXIMO_UPLOAD_MB * 1024 * 1024 + 1))
        doc = Documento(titulo='Grande', tipo='outro', arquivo=grande, imovel=self.imovel)
        with self.assertRaises(ValidationError):
            doc.full_clean()


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class DownloadProtegidoTest(TestCase):
    def setUp(self):
        self.imovel = Imovel.objects.create(nome='Imóvel Down', endereco='Rua C', cidade='SP', estado='SP')
        self.doc = Documento.objects.create(
            titulo='Matrícula', tipo='matricula', arquivo=_arquivo('matricula.pdf'), imovel=self.imovel,
        )
        self.client = Client()

    def test_download_exige_login(self):
        response = self.client.get(reverse('documento_download', args=[self.doc.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login/', response['Location'])

    def test_download_autenticado_serve_arquivo(self):
        com_leitura(User.objects.create_user('down_user', password='pass'))
        self.client.login(username='down_user', password='pass')
        response = self.client.get(reverse('documento_download', args=[self.doc.pk]))
        self.assertEqual(response.status_code, 200)
        conteudo = b''.join(response.streaming_content)
        self.assertIn(b'conteudo de teste', conteudo)

    def test_download_autenticado_sem_permissao_403(self):
        User.objects.create_user('sem_perm_doc', password='pass')  # sem nenhuma permissão
        self.client.login(username='sem_perm_doc', password='pass')
        response = self.client.get(reverse('documento_download', args=[self.doc.pk]))
        self.assertEqual(response.status_code, 403)

    def test_nao_existe_rota_publica_para_media(self):
        """Item 7: /media/... não pode ser servido sem passar pela view autenticada."""
        response = self.client.get(f'/media/{self.doc.arquivo.name}')
        self.assertEqual(response.status_code, 404)

        com_leitura(User.objects.create_user('media_publica_user', password='pass'))
        self.client.login(username='media_publica_user', password='pass')
        response = self.client.get(f'/media/{self.doc.arquivo.name}')
        self.assertEqual(response.status_code, 404)


@override_settings(MEDIA_ROOT=MEDIA_TEMP)
class DocumentoAdminLinkProtegidoTest(TestCase):
    """Item 7 (rodada pós-revisão): Admin nunca expõe arquivo.url — só a rota autenticada."""

    def setUp(self):
        self.imovel = Imovel.objects.create(nome='Imóvel Admin Doc', endereco='Rua D', cidade='SP', estado='SP')
        self.doc = Documento.objects.create(
            titulo='Matrícula Admin', tipo='matricula', arquivo=_arquivo('matricula_admin.pdf'),
            imovel=self.imovel,
        )
        self.client = Client()
        self.staff = User.objects.create_superuser('doc_admin_staff', 'a@a.com', 'pass')
        self.client.login(username='doc_admin_staff', password='pass')

    def test_admin_change_form_nao_contem_media(self):
        response = self.client.get(reverse('admin:documentos_documento_change', args=[self.doc.pk]))
        self.assertEqual(response.status_code, 200)
        conteudo = response.content.decode()
        self.assertNotIn('/media/', conteudo)

    def test_admin_change_form_link_aponta_para_documento_download(self):
        response = self.client.get(reverse('admin:documentos_documento_change', args=[self.doc.pk]))
        conteudo = response.content.decode()
        self.assertIn(reverse('documento_download', args=[self.doc.pk]), conteudo)

    def test_usuario_sem_permissao_de_documento_recebe_403_no_admin(self):
        self.client.logout()
        user = User.objects.create_user('doc_admin_sem_perm', password='pass')
        user.is_staff = True
        user.save()
        self.client.login(username='doc_admin_sem_perm', password='pass')
        response = self.client.get(reverse('admin:documentos_documento_change', args=[self.doc.pk]))
        self.assertEqual(response.status_code, 403)

    def test_substituicao_de_arquivo_continua_funcionando(self):
        novo_arquivo = _arquivo('substituto.pdf')
        response = self.client.post(
            reverse('admin:documentos_documento_change', args=[self.doc.pk]),
            data={
                'titulo': self.doc.titulo, 'tipo': self.doc.tipo, 'arquivo': novo_arquivo,
                'imovel': self.imovel.pk, 'observacoes': '', 'enviado_contabilidade': '',
                '_continue': 'Save and continue editing',
            },
        )
        self.doc.refresh_from_db()
        self.assertIn('substituto', self.doc.arquivo.name)

    def test_editar_outros_campos_nao_apaga_arquivo_acidentalmente(self):
        nome_original = self.doc.arquivo.name
        response = self.client.post(
            reverse('admin:documentos_documento_change', args=[self.doc.pk]),
            data={
                'titulo': 'Título Alterado', 'tipo': self.doc.tipo,
                'imovel': self.imovel.pk, 'observacoes': 'nota nova', 'enviado_contabilidade': '',
                '_continue': 'Save and continue editing',
            },
        )
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.titulo, 'Título Alterado')
        self.assertEqual(self.doc.arquivo.name, nome_original)
        self.assertTrue(self.doc.arquivo)

    def test_inline_de_contrato_nao_gera_link_publico(self):
        from datetime import date
        from patrimonio.models import Pessoa, Contrato
        locatario = Pessoa.objects.create(nome='Locatário Admin Doc', tipo='locatario')
        contrato = Contrato.objects.create(
            imovel=self.imovel, locatario=locatario,
            data_inicio=date(2024, 1, 1), data_fim=date(2024, 12, 31),
            dia_vencimento=10, valor_aluguel=1000,
        )
        doc = Documento.objects.create(
            titulo='Contrato Assinado', tipo='contrato_aluguel', arquivo=_arquivo('contrato_assinado.pdf'),
            imovel=self.imovel, contrato=contrato,
        )
        response = self.client.get(reverse('admin:patrimonio_contrato_change', args=[contrato.pk]))
        self.assertEqual(response.status_code, 200)
        conteudo = response.content.decode()
        self.assertNotIn('/media/', conteudo)
        self.assertIn(reverse('documento_download', args=[doc.pk]), conteudo)
