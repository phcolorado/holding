"""
Matriz de permissões das ações da tela de conciliação bancária.

Cada ação da conciliação modifica tanto a TransacaoExtrato quanto registros
financeiros — a autorização precisa corresponder a TODOS os efeitos da
operação, não só à permissão da transação nem só à da área financeira:

* conciliar um crédito altera a transação E as receitas;
* lançar um débito altera a transação E cria uma despesa;
* desfazer um crédito com comissões altera receitas E despesas de comissão;
* desfazer um débito exclui a despesa criada pela conciliação;
* ignorar/reabrir alteram apenas o tratamento da transação.

Fonte ÚNICA da regra: tanto a checagem do POST na view (antes de chamar
qualquer service) quanto a montagem dos flags de exibição de botões usam
permissoes_para_acao()/usuario_pode_executar() — o botão nunca pode divergir
da regra do POST.
"""

PERM_ALTERAR_TRANSACAO = 'conciliacao.change_transacaoextrato'
PERM_CONCILIAR_RECEITA = 'financeiro.change_receitaaluguel'
PERM_ADD_DESPESA = 'financeiro.add_despesa'
PERM_CHANGE_DESPESA = 'financeiro.change_despesa'
PERM_DELETE_DESPESA = 'financeiro.delete_despesa'

# Ações da tela cobertas pela matriz — o POST é autorizado por
# permissoes_para_acao() antes de qualquer service. Ações fora deste
# conjunto não disparam nenhum efeito (nenhuma branch as trata).
ACOES_COM_MATRIZ = frozenset({'conciliar', 'lancar_despesa', 'desfazer', 'ignorar', 'reabrir'})


def permissoes_para_acao(action, transacao=None, marcar_comissoes=False):
    """
    Conjunto (frozenset) de permissões EXIGIDAS para executar `action`.
    Determinístico: mesma entrada → mesmo conjunto.

    Para 'desfazer', o tipo (crédito/débito) e a marcação de comissões vêm
    do ESTADO PERSISTIDO da transação (transacao.tipo,
    transacao.comissoes_marcadas) — nunca de campos do formulário; por isso
    `transacao` é obrigatório para essa ação.

    `marcar_comissoes` só é considerado para 'conciliar' (é uma escolha
    legítima do usuário ao conciliar um crédito). Em 'desfazer' o parâmetro
    é ignorado (usa transacao.comissoes_marcadas).
    """
    if action == 'conciliar':
        perms = {PERM_ALTERAR_TRANSACAO, PERM_CONCILIAR_RECEITA}
        if marcar_comissoes:
            perms.add(PERM_CHANGE_DESPESA)
        return frozenset(perms)

    if action == 'lancar_despesa':
        return frozenset({PERM_ALTERAR_TRANSACAO, PERM_ADD_DESPESA})

    if action in ('ignorar', 'reabrir'):
        return frozenset({PERM_ALTERAR_TRANSACAO})

    if action == 'desfazer':
        if transacao is None:
            raise ValueError(
                "permissoes_para_acao('desfazer') exige a transação persistida para "
                'determinar tipo/comissões — a autorização não pode depender do formulário.'
            )
        if transacao.tipo == 'debito':
            # o desfazer de débito EXCLUI a despesa criada pela conciliação
            return frozenset({PERM_ALTERAR_TRANSACAO, PERM_DELETE_DESPESA})
        # crédito: reverte receitas/recebimentos; se marcou comissões,
        # também reabre as despesas de comissão pagas por ela.
        perms = {PERM_ALTERAR_TRANSACAO, PERM_CONCILIAR_RECEITA}
        if transacao.comissoes_marcadas:
            perms.add(PERM_CHANGE_DESPESA)
        return frozenset(perms)

    # Ação desconhecida: nada é autorizado por omissão. Um sentinela que
    # nenhum usuário possui garante que usuario_pode_executar() retorne
    # False (nunca autoriza um action inesperado).
    return frozenset({'__acao_desconhecida__'})


def usuario_pode_executar(user, permissoes):
    """True somente se `user` tiver TODAS as permissões do conjunto."""
    return all(user.has_perm(p) for p in permissoes)
