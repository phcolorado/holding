import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime

from django.conf import settings
from django.core.management.base import BaseCommand


def _sha256_arquivo(caminho):
    h = hashlib.sha256()
    with open(caminho, 'rb') as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b''):
            h.update(bloco)
    return h.hexdigest()


def _versao_do_sistema():
    """Commit git curto, quando disponível — apenas informativo no manifest."""
    try:
        return subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=5,
        ).stdout.strip() or 'desconhecida'
    except Exception:
        return 'desconhecida'


class Command(BaseCommand):
    help = 'Cria backup ZIP do banco de dados SQLite (cópia consistente) e dos arquivos de mídia'

    def add_arguments(self, parser):
        parser.add_argument(
            '--destino',
            type=str,
            default='',
            help='Pasta de destino do backup (padrão: backups/ na raiz do projeto)',
        )
        parser.add_argument(
            '--manter',
            type=int,
            default=0,
            help='Manter apenas os N backups mais recentes (0 = manter todos)',
        )
        parser.add_argument(
            '--copia-extra',
            type=str,
            default='',
            help=(
                'Pasta adicional para onde copiar o ZIP gerado — aponte para uma pasta '
                'sincronizada com a nuvem (Google Drive, Dropbox, OneDrive) para ter '
                'backup externo automático.'
            ),
        )

    def handle(self, *args, **options):
        destino = options['destino'] or os.path.join(settings.BASE_DIR, 'backups')
        manter = options['manter']

        os.makedirs(destino, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        zip_path = os.path.join(destino, f'backup_{timestamp}.zip')

        db_cfg = settings.DATABASES.get('default', {})
        engine = db_cfg.get('ENGINE', '')
        db_name = str(db_cfg.get('NAME', ''))
        media_root = getattr(settings, 'MEDIA_ROOT', '')

        eh_sqlite = engine.endswith('sqlite3')
        if not eh_sqlite:
            self.stderr.write(self.style.WARNING(
                f'ATENÇÃO: o banco em uso é {engine} — este comando NÃO fez backup do banco. '
                'Use pg_dump (PostgreSQL) para o banco; apenas a mídia foi incluída no ZIP.'
            ))

        media_count = 0
        db_ok = eh_sqlite and db_name and os.path.exists(db_name)
        checksum_db = ''
        tamanho_db = 0

        with tempfile.TemporaryDirectory() as tmpdir:
            copia_db = os.path.join(tmpdir, 'db.sqlite3')
            if db_ok:
                # Cópia CONSISTENTE via API de backup do SQLite — segura mesmo
                # com o banco em uso (não copia o arquivo cru).
                origem = sqlite3.connect(db_name)
                destino_con = sqlite3.connect(copia_db)
                with destino_con:
                    origem.backup(destino_con)
                destino_con.close()
                origem.close()
                checksum_db = _sha256_arquivo(copia_db)
                tamanho_db = os.path.getsize(copia_db)

            with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                if db_ok:
                    zf.write(copia_db, 'db.sqlite3')

                if media_root and os.path.isdir(media_root):
                    for dirpath, _, filenames in os.walk(media_root):
                        for filename in filenames:
                            filepath = os.path.join(dirpath, filename)
                            arcname = os.path.join('media', os.path.relpath(filepath, media_root))
                            zf.write(filepath, arcname)
                            media_count += 1

                manifest_lines = [
                    f'Data/hora: {datetime.now().isoformat()}',
                    f'Engine do banco: {engine}',
                    f'Banco de dados: {db_name if db_ok else "não incluído"}',
                    f'Tamanho do banco (bytes): {tamanho_db}',
                    f'SHA-256 do banco: {checksum_db or "n/a"}',
                    f'Arquivos de mídia: {media_count}',
                    f'Versão do sistema (commit): {_versao_do_sistema()}',
                    'Projeto: Gestão Patrimonial Familiar',
                    '',
                    'IMPORTANTE: valide este backup com "python manage.py verificar_backup <zip>".',
                ]
                zf.writestr('manifest.txt', '\n'.join(manifest_lines))

        # Checksum do próprio ZIP, gravado ao lado (não pode ficar dentro dele)
        with open(zip_path + '.sha256', 'w') as f:
            f.write(f'{_sha256_arquivo(zip_path)}  {os.path.basename(zip_path)}\n')

        if not db_ok and eh_sqlite:
            self.stdout.write(self.style.WARNING('Banco SQLite não encontrado — omitido do backup.'))
        if media_count == 0:
            self.stdout.write(self.style.WARNING('Nenhum arquivo de mídia encontrado — omitido do backup.'))

        self.stdout.write(self.style.SUCCESS(f'Backup criado: {zip_path}'))

        copia_extra = options['copia_extra']
        if copia_extra:
            try:
                os.makedirs(copia_extra, exist_ok=True)
                destino_extra = os.path.join(copia_extra, os.path.basename(zip_path))
                shutil.copy2(zip_path, destino_extra)
                shutil.copy2(zip_path + '.sha256', destino_extra + '.sha256')
                self.stdout.write(self.style.SUCCESS(f'Cópia extra criada: {destino_extra}'))
            except OSError as exc:
                self.stderr.write(self.style.ERROR(f'Falha ao criar cópia extra em {copia_extra}: {exc}'))

        if manter:
            zips = sorted(
                f for f in os.listdir(destino)
                if f.startswith('backup_') and f.endswith('.zip')
            )
            while len(zips) > manter:
                antigo = os.path.join(destino, zips.pop(0))
                os.remove(antigo)
                if os.path.exists(antigo + '.sha256'):
                    os.remove(antigo + '.sha256')
                self.stdout.write(f'Backup antigo removido: {antigo}')
