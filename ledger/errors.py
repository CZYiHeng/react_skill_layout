# -*- coding: utf-8 -*-
"""记账域异常定义。

所有异常均继承 LedgerError，便于上层统一捕获。
每个异常携带可读消息，违反不变量时由 ledger 模块抛出。
"""
from __future__ import annotations

from typing import Optional


class LedgerError(Exception):
    """记账域所有异常的基类。"""

    #: 供程序分支判断的稳定错误码
    code: str = "LEDGER_ERROR"

    def __init__(self, message: str, *, detail: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r}, detail={self.detail!r})"


class ValidationError(LedgerError):
    """入参非法（金额非正整数、币种位数错、方向非法等）。"""

    code = "VALIDATION_ERROR"


class UnbalancedTransactionError(LedgerError):
    """违反不变量①：同一交易内借方总额 != 贷方总额。"""

    code = "UNBALANCED_TRANSACTION"


class BalanceMismatchError(LedgerError):
    """违反不变量②：账户余额 != 其全部分录净额之和（对账不一致）。"""

    code = "BALANCE_MISMATCH"


class InsufficientFundsError(LedgerError):
    """违反不变量③：非透支账户余额将变为负数（余额不足）。"""

    code = "INSUFFICIENT_FUNDS"


class NegativeBalanceError(LedgerError):
    """违反不变量③：非透支账户余额为负（DB CHECK 触发/直接写入兜底）。"""

    code = "NEGATIVE_BALANCE"


class AccountNotFoundError(LedgerError):
    """目标账户不存在。"""

    code = "ACCOUNT_NOT_FOUND"


class AccountNotActiveError(LedgerError):
    """账户状态非 ACTIVE（FROZEN/CLOSED）不可记账。"""

    code = "ACCOUNT_NOT_ACTIVE"


class CurrencyMismatchError(LedgerError):
    """涉及账户币种不一致，或与交易币种不符。"""

    code = "CURRENCY_MISMATCH"


class DuplicateTransactionError(LedgerError):
    """幂等键重复（幂等命中：已存在的交易，附原交易信息）。"""

    code = "DUPLICATE_TRANSACTION"


# 错误码 -> 异常类 的稳定映射，供 API 层与测试引用
ERROR_CODES = {
    cls.code: cls
    for cls in (
        LedgerError,
        ValidationError,
        UnbalancedTransactionError,
        BalanceMismatchError,
        InsufficientFundsError,
        NegativeBalanceError,
        AccountNotFoundError,
        AccountNotActiveError,
        CurrencyMismatchError,
        DuplicateTransactionError,
    )
}
