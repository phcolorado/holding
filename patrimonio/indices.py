"""
Consulta de índices de reajuste (IPCA, IGP-M, INPC) na API SGS do Banco Central.

A API pública não exige autenticação:
https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados?formato=json&dataInicial=&dataFinal=
Códigos: 433 = IPCA (var. % mensal), 189 = IGP-M, 188 = INPC.

Defasagem de divulgação: IPCA e INPC saem em torno do dia 10 do mês seguinte;
o IGP-M sai no fim do próprio mês. O sistema não inventa regra jurídica — as
competências efetivamente usadas ficam registradas no reajuste sugerido.
"""
import json
from calendar import monthrange
from decimal import Decimal, InvalidOperation
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from django.core.cache import cache

SERIES_SGS = {
    'ipca': 433,
    'igpm': 189,
    'inpc': 188,
}

URL_SGS = 'https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados?{query}'
CACHE_TTL_SEGUNDOS = 60 * 60 * 12  # 12 horas — índices mensais mudam no máx. 1x/mês


class IndiceIndisponivelError(Exception):
    """Consulta ao Banco Central falhou ou o índice não tem série SGS mapeada."""


def _mes_anterior(ano, mes):
    return (ano - 1, 12) if mes == 1 else (ano, mes - 1)


def competencia_final_para_reajuste(data_reajuste):
    """
    Competência final do acumulado: o mês anterior ao mês do reajuste
    (reajuste de junho/2024 usa os 12 meses encerrados em maio/2024).
    """
    return _mes_anterior(data_reajuste.year, data_reajuste.month)


def variacao_acumulada_12m(indice, competencia_final=None):
    """
    Variação acumulada de 12 meses do índice, encerrada na competência
    informada (tupla (ano, mes)). Sem competência, usa os últimos 12 meses
    divulgados — comportamento adequado apenas para consultas "de hoje".

    Retorna dict: {'percentual': Decimal, 'inicio': 'mm/aaaa', 'fim': 'mm/aaaa'}.
    Levanta IndiceIndisponivelError quando a API está fora do ar, o índice não
    é suportado (ex.: 'fixo') ou a série ainda não tem as 12 competências
    pedidas (defasagem de divulgação).
    """
    serie = SERIES_SGS.get(indice)
    if serie is None:
        raise IndiceIndisponivelError(
            f'O índice "{indice}" não tem série do Banco Central associada.'
        )

    chave_cache = f'bcb_sgs_acumulado_12m_{indice}_{competencia_final or "ultimos"}'
    cacheado = cache.get(chave_cache)
    if cacheado is not None:
        return cacheado

    if competencia_final is not None:
        ano_fim, mes_fim = competencia_final
        ano_ini, mes_ini = ano_fim, mes_fim
        for _ in range(11):
            ano_ini, mes_ini = _mes_anterior(ano_ini, mes_ini)
        query = urlencode({
            'formato': 'json',
            'dataInicial': f'01/{mes_ini:02d}/{ano_ini}',
            'dataFinal': f'{monthrange(ano_fim, mes_fim)[1]}/{mes_fim:02d}/{ano_fim}',
        })
        url = URL_SGS.format(serie=serie, query=query)
    else:
        url = f'https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}/dados/ultimos/12?formato=json'

    try:
        with urlopen(url, timeout=15) as resposta:
            dados = json.load(resposta)
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        raise IndiceIndisponivelError(
            f'Não foi possível consultar o Banco Central agora ({exc}). Tente novamente mais tarde.'
        ) from exc

    if not isinstance(dados, list) or len(dados) < 12:
        raise IndiceIndisponivelError(
            'A série do Banco Central não retornou as 12 competências do período '
            f'solicitado ({len(dados) if isinstance(dados, list) else 0} recebidas). '
            'Verifique se o índice do período já foi divulgado (há defasagem de divulgação).'
        )
    if len(dados) > 12:
        dados = dados[-12:]

    fator = Decimal('1')
    try:
        for item in dados:
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
        'inicio': _competencia(dados[0]),
        'fim': _competencia(dados[-1]),
    }
    cache.set(chave_cache, resultado, CACHE_TTL_SEGUNDOS)
    return resultado
