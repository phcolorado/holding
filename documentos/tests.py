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
