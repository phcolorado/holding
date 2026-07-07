"""Helpers compartilhados entre as views (filtros de período, validação de parâmetros)."""
from django.utils import timezone

# Primeiro ano oferecido nos filtros de período das telas financeiras.
ANO_FILTRO_INICIAL = 2020


def int_param(valor, padrao, minimo=None, maximo=None):
    """
    Converte um parâmetro de request para int de forma segura.
    Retorna `padrao` quando o valor é ausente, não numérico ou fora da faixa.
    """
    try:
        convertido = int(valor)
    except (TypeError, ValueError):
        return padrao
    if minimo is not None and convertido < minimo:
        return padrao
    if maximo is not None and convertido > maximo:
        return padrao
    return convertido


def pk_param(valor):
    """Retorna o valor apenas se parecer uma PK válida (dígitos); senão string vazia."""
    valor = (valor or '').strip()
    return valor if valor.isdigit() else ''


def mes_ano_da_request(request):
    """Extrai mês/ano validados (GET ou POST), com fallback para a competência atual."""
    hoje = timezone.localdate()
    mes = int_param(request.GET.get('mes') or request.POST.get('mes'), hoje.month, 1, 12)
    ano = int_param(request.GET.get('ano') or request.POST.get('ano'), hoje.year, 1990, 2200)
    return mes, ano


def anos_para_filtro():
    """Faixa de anos exibida nos selects de período."""
    return range(ANO_FILTRO_INICIAL, timezone.localdate().year + 2)
