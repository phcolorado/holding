from django.db import migrations


def migrar_encargo_aluguel(apps, schema_editor):
    """Cria EncargoContrato(tipo='aluguel') para contratos existentes a partir de valor_aluguel."""
    Contrato = apps.get_model('patrimonio', 'Contrato')
    EncargoContrato = apps.get_model('patrimonio', 'EncargoContrato')

    for contrato in Contrato.objects.all():
        EncargoContrato.objects.get_or_create(
            contrato=contrato, tipo='aluguel',
            defaults={
                'descricao': 'Aluguel',
                'valor': contrato.valor_aluguel,
                'periodicidade': 'mensal',
                'ativo': True,
            },
        )


def reverter_encargo_aluguel(apps, schema_editor):
    EncargoContrato = apps.get_model('patrimonio', 'EncargoContrato')
    EncargoContrato.objects.filter(tipo='aluguel').delete()


class Migration(migrations.Migration):

    dependencies = [
        ("patrimonio", "0004_migrar_partes_contrato"),
    ]

    operations = [
        migrations.RunPython(migrar_encargo_aluguel, reverter_encargo_aluguel),
    ]
