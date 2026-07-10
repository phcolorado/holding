import hashlib
import os
import sqlite3
import tempfile
import zipfile

from django.core.management.base import BaseCommand, CommandError

TABELAS_ESSENCIAIS = [
    'patrimonio_imovel',
    'patrimonio_contrato',
    'financeiro_receitaaluguel',
    'financeiro_despesa',
]


class Command(BaseCommand):
    help = (
        'Verifica um backup gerado por backup_local: abre o ZIP, valida o manifest, '
        'confere o checksum e consulta as tabelas essenciais da cópia SQLite. '
        'Não restaura nada — é somente leitura.'
    )

    def add_arguments(self, parser):
        parser.add_argument('zip_path', type=str, help='Caminho do arquivo backup_*.zip')

    def handle(self, *args, **options):
        zip_path = options['zip_path']
        if not os.path.exists(zip_path):
            raise CommandError(f'Arquivo não encontrado: {zip_path}')

        with zipfile.ZipFile(zip_path) as zf:
            nomes = zf.namelist()
            if 'manifest.txt' not in nomes:
                raise CommandError('Backup inválido: manifest.txt ausente.')
            manifest = zf.read('manifest.txt').decode('utf-8')
            self.stdout.write('Manifest encontrado:')
            for linha in manifest.splitlines():
                if linha.strip():
                    self.stdout.write(f'  {linha}')

            checksum_esperado = ''
            for linha in manifest.splitlines():
                if linha.startswith('SHA-256 do banco:'):
                    checksum_esperado = linha.split(':', 1)[1].strip()

            if 'db.sqlite3' not in nomes:
                self.stdout.write(self.style.WARNING(
                    'O backup não contém banco SQLite (banco não-SQLite ou ausente na origem).'
                ))
                self.stdout.write(self.style.SUCCESS('Verificação concluída (somente mídia/manifest).'))
                return

            with tempfile.TemporaryDirectory() as tmpdir:
                caminho_db = zf.extract('db.sqlite3', tmpdir)

                if checksum_esperado and checksum_esperado != 'n/a':
                    h = hashlib.sha256()
                    with open(caminho_db, 'rb') as f:
                        for bloco in iter(lambda: f.read(1024 * 1024), b''):
                            h.update(bloco)
                    if h.hexdigest() != checksum_esperado:
                        raise CommandError('CHECKSUM DIVERGENTE: o banco dentro do ZIP não confere com o manifest.')
                    self.stdout.write(self.style.SUCCESS('Checksum SHA-256 do banco confere.'))

                con = sqlite3.connect(f'file:{caminho_db}?mode=ro', uri=True)
                try:
                    cursor = con.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tabelas = {linha[0] for linha in cursor.fetchall()}
                    faltando = [t for t in TABELAS_ESSENCIAIS if t not in tabelas]
                    if faltando:
                        raise CommandError(f'Tabelas essenciais ausentes no backup: {", ".join(faltando)}')
                    for tabela in TABELAS_ESSENCIAIS:
                        total = con.execute(f'SELECT COUNT(*) FROM {tabela}').fetchone()[0]
                        self.stdout.write(f'  {tabela}: {total} registro(s)')
                finally:
                    con.close()

        self.stdout.write(self.style.SUCCESS('Backup válido: manifest, checksum e tabelas essenciais OK.'))
