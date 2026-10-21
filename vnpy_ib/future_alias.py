"""
期货简写代码（自定义扩展，非官方代码）

在 vnpy_ib 中输入简写代码，自动换算为当前主力合约，例如：
    MNQ  ->  MNQ-202612-USD-FUT  （交易所 CME）

换月日期按日历规则估算，可在 FUTURE_ALIASES 中增删品种或调整规则。
同一次运行中解析结果会被缓存，避免盘中跨过换月日导致订阅和下单用到不同合约。
"""

from dataclasses import dataclass
from datetime import date, timedelta
from collections.abc import Callable


QUARTERLY: tuple[int, ...] = (3, 6, 9, 12)
MONTHLY: tuple[int, ...] = tuple(range(1, 13))
METAL: tuple[int, ...] = (2, 4, 6, 8, 12)


def third_friday(year: int, month: int) -> date:
    """当月第三个周五（股指期货到期日）"""
    d: date = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def roll_equity(year: int, month: int) -> date:
    """股指：到期日前8天换月"""
    return third_friday(year, month) - timedelta(days=8)


def roll_prev_month(day: int) -> Callable[[int, int], date]:
    """在合约月份的前一个月第day日换月"""
    def roll(year: int, month: int) -> date:
        if month == 1:
            return date(year - 1, 12, day)
        return date(year, month - 1, day)
    return roll


@dataclass(frozen=True)
class FutureAlias:
    exchange: str                           # vnpy Exchange 的值
    months: tuple[int, ...]                 # 参与换月的合约月份
    roll: Callable[[int, int], date]        # 返回该月份合约的换月日
    currency: str = "USD"


EQUITY = roll_equity
CRUDE = roll_prev_month(12)     # 原油：前一个月12日（到期约在前月20日前后）
NOTICE = roll_prev_month(24)    # 国债/金属：第一通知日（前月月底）之前

FUTURE_ALIASES: dict[str, FutureAlias] = {
    # 股指
    "MNQ": FutureAlias("CME", QUARTERLY, EQUITY),
    "NQ": FutureAlias("CME", QUARTERLY, EQUITY),
    "MES": FutureAlias("CME", QUARTERLY, EQUITY),
    "ES": FutureAlias("CME", QUARTERLY, EQUITY),
    "M2K": FutureAlias("CME", QUARTERLY, EQUITY),
    "RTY": FutureAlias("CME", QUARTERLY, EQUITY),
    "MYM": FutureAlias("CBOT", QUARTERLY, EQUITY),
    "YM": FutureAlias("CBOT", QUARTERLY, EQUITY),
    # 原油
    "MCL": FutureAlias("NYMEX", MONTHLY, CRUDE),
    "CL": FutureAlias("NYMEX", MONTHLY, CRUDE),
    # 黄金
    "MGC": FutureAlias("COMEX", METAL, NOTICE),
    "GC": FutureAlias("COMEX", METAL, NOTICE),
    # 美债
    "ZT": FutureAlias("CBOT", QUARTERLY, NOTICE),
    "ZF": FutureAlias("CBOT", QUARTERLY, NOTICE),
    "ZN": FutureAlias("CBOT", QUARTERLY, NOTICE),
    "ZB": FutureAlias("CBOT", QUARTERLY, NOTICE),
    "UB": FutureAlias("CBOT", QUARTERLY, NOTICE),
}


_cache: dict[str, str] = {}


def is_alias(symbol: str) -> bool:
    """是否为期货简写代码"""
    return symbol.upper() in FUTURE_ALIASES


def resolve_month(symbol: str, today: date | None = None) -> str:
    """计算当前主力合约月份，返回YYYYMM"""
    alias: FutureAlias = FUTURE_ALIASES[symbol.upper()]
    if today is None:
        today = date.today()

    year, month = today.year, today.month
    for _ in range(36):
        if month in alias.months and alias.roll(year, month) > today:
            return f"{year}{month:02d}"

        month += 1
        if month > 12:
            month = 1
            year += 1

    raise ValueError(f"无法计算{symbol}的主力合约月份")


def resolve_alias(symbol: str) -> tuple[str, str]:
    """返回 (完整代码, 交易所)，例如 ("MNQ-202612-USD-FUT", "CME")"""
    key: str = symbol.upper()
    alias: FutureAlias = FUTURE_ALIASES[key]

    if key not in _cache:
        _cache[key] = f"{key}-{resolve_month(key)}-{alias.currency}-FUT"

    return _cache[key], alias.exchange
