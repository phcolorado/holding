"""
Consulta de índices de reajuste (IPCA, IGP-M, INPC) na API SGS do Banco Central.

A API pública não exige autenticação:
https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados/ultimos/{n}?formato=json
Códigos: 433 = IPCA (var. % mensal), 189 = IGP-M, 188 = INPC.
"""
import json
from decimal import Decimal, InvalidOperation
from urllib.error import URLError
from urllib.request import urlopen

from django.core.cache import cache

SERIES_SGS = {
    'ipca': 433,
    'igpm': 189,
    'inpc': 188,
}

URL_SGS = 'https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados/ultimos/{n}?formato=json'
CACHE_TTL_SEGUNDOS = 60 * 60 * 12  # 12 horas — índices mensais mudam no máx. 1x/mês


class IndiceIndisponivelError(Exception):
    """Consulta ao Banco Central falhou ou o índice não tem série SGS mapeada."""


def variacao_acumulada_12m(indice):
    """
    Variação acumulada dos últimos 12 meses divulgados do índice informado
    ('ipca', 'igpm' ou 'inpc').

    Retorna dict: {'percentual': Decimal, 'inicio': 'mm/aaaa', 'fim': 'mm/aaaa'}.
    Levanta IndiceIndisponivelError quando a API está fora do ar ou o índice
    não é suportado (ex.: 'fixo', 'outro').
    """
    serie = SERIES_SGS.get(indice)
    if serie is None:
        raise IndiceIndisponivelError(
            f'O índice "{indice}" não tem série do Banco Central associada.'
        )

    chave_cache = f'bcb_sgs_acumulado_12m_{indice}'
    cacheado = cache.get(chave_cache)
    if cacheado is not None:
        return cacheado

    url = URL_SGS.format(serie=serie, n=12)
    try:
        with urlopen(url, timeout=15) as resposta:
            dados = json.load(resposta)
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        raise IndiceIndisponivelError(
            f'Não foi possível consultar o Banco Central agora ({exc}). Tente novamente mais tarde.'
        ) from exc

    if not isinstance(dados, list) or len(dados) < 12:
        raise IndiceIndisponivelError(
            'A série do Banco Central retornou menos de 12 meses de dados.'
        )

    fator = Decimal('1')
    try:
        for item in dados[-12:]:
            fator *= Decimal('1') + Decimal(str(item['valor'])) / Decimal('100')
    except (KeyError, InvalidOperation) as exc:
        raise IndiceIndisponivelError(
            f'Resposta inesperada da série do Banco Central: {exc}'
        ) from exc

    percentual = ((fator - Decimal('1')) * Decimal('100')).quantize(Decimal('0.0001'))

    def _competencia(item):
        # 'data' vem como 'dd/mm/aaaa'
        _, mes, ano = item['data'].split('/')
        return f'{mes}/{ano}'

    resultado = {
        'percentual': percentual,
        'inicio': _competencia(dados[-12]),
        'fim': _competencia(dados[-1]),
    }
    cache.set(chave_cache, resultado, CACHE_TTL_SEGUNDOS)
    return resultado
