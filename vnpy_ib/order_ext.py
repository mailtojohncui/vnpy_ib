"""
图上交易扩展（自定义扩展，非官方代码）

- 括号单：入场单 + 止盈 + 止损，作为 IB 原生 bracket 提交，
  入场成交后止盈止损由 IB 服务器管理，一个成交另一个自动取消
- 持仓止盈止损：用 IB 的 OCA 组提交，同样由服务器端互相取消
- 改单：用同一个 orderId 重新 placeOrder，IB 视为修改而不是撤单重下
"""

from copy import copy
from datetime import datetime
from decimal import Decimal
from itertools import count

from ibapi.contract import Contract
from ibapi.execution import ExecutionFilter
from ibapi.order import Order

from vnpy.trader.constant import Direction, Exchange, OrderType, Status
from vnpy.trader.object import OrderData, OrderRequest

from .ib_gateway import DIRECTION_VT2IB, ORDERTYPE_VT2IB, IbApi, IbGateway, generate_ib_contract


_oca_counter = count(1)


def get_api(gateway: IbGateway) -> IbApi | None:
    """已连接的 IbApi，未连接时返回 None"""
    api: IbApi | None = getattr(gateway, "api", None)
    if api is None or not api.status:
        return None
    return api


def opposite(direction: Direction) -> Direction:
    return Direction.SHORT if direction == Direction.LONG else Direction.LONG


def _build(api: IbApi, req: OrderRequest, tif: str) -> tuple[Contract, Order] | None:
    """按 vnpy 委托请求生成 IB 合约和委托（分配新的 orderId）"""
    if req.type not in ORDERTYPE_VT2IB:
        api.gateway.write_log(f"不支持的价格类型：{req.type}")
        return None

    contract: Contract | None = generate_ib_contract(req.symbol, req.exchange)
    if not contract:
        api.gateway.write_log(f"代码解析失败：{req.symbol}")
        return None

    api.orderid += 1

    order: Order = Order()
    order.orderId = api.orderid
    order.clientId = api.clientid
    order.action = DIRECTION_VT2IB[req.direction]
    order.orderType = ORDERTYPE_VT2IB[req.type]
    order.totalQuantity = Decimal(str(req.volume))
    order.account = api.account
    order.orderRef = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    order.tif = tif
    order.outsideRth = True     # 允许在常规交易时段之外触发/成交（期货夜盘、盘前盘后）

    if req.type == OrderType.LIMIT:
        order.lmtPrice = req.price
    elif req.type == OrderType.STOP:
        order.auxPrice = req.price

    return contract, order


def _register(api: IbApi, req: OrderRequest, contract: Contract, order: Order) -> str:
    """登记到 vnpy 的委托缓存并推送，返回 vt_orderid"""
    data: OrderData = req.create_order_data(str(order.orderId), api.gateway_name)
    api.orders[data.orderid] = data
    api.chart_orders[data.orderid] = (contract, order)
    if order.parentId:
        api.order_parents[data.orderid] = str(order.parentId)
    api.gateway.on_order(copy(data))
    return data.vt_orderid


def _exit_request(symbol: str, exchange: Exchange, direction: Direction, volume: float,
                  order_type: OrderType, price: float) -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        exchange=exchange,
        direction=direction,
        type=order_type,
        volume=volume,
        price=price,
        reference="ChartPro",
    )


def place_order(gateway: IbGateway, req: OrderRequest, tif: str = "GTC") -> str:
    """普通委托（保存 IB 委托对象，便于之后改单）"""
    api: IbApi | None = get_api(gateway)
    if not api:
        return ""

    built = _build(api, req, tif)
    if not built:
        return ""
    contract, order = built

    api.client.placeOrder(order.orderId, contract, order)
    api.client.reqIds(1)
    return _register(api, req, contract, order)


def place_bracket(
    gateway: IbGateway,
    req: OrderRequest,
    take_profit: float | None,
    stop_loss: float | None,
    tif: str = "GTC"
) -> list[str]:
    """
    括号单：返回 [入场, 止盈?, 止损?] 的 vt_orderid。
    止盈、止损都不设时等同普通委托。
    """
    api: IbApi | None = get_api(gateway)
    if not api:
        return []

    if take_profit is None and stop_loss is None:
        vt_orderid: str = place_order(gateway, req, tif)
        return [vt_orderid] if vt_orderid else []

    built = _build(api, req, tif)
    if not built:
        return []
    contract, parent = built
    parent.transmit = False

    exit_direction: Direction = opposite(req.direction)
    legs: list[tuple[OrderRequest, Contract, Order]] = [(req, contract, parent)]

    for order_type, price in ((OrderType.LIMIT, take_profit), (OrderType.STOP, stop_loss)):
        if price is None:
            continue
        child_req: OrderRequest = _exit_request(
            req.symbol, req.exchange, exit_direction, req.volume, order_type, price
        )
        child = _build(api, child_req, "GTC")
        if not child:
            return []
        child_contract, child_order = child
        child_order.parentId = parent.orderId
        child_order.transmit = False
        legs.append((child_req, child_contract, child_order))

    # 最后一条腿 transmit=True，IB 才会把整组一起提交
    legs[-1][2].transmit = True

    vt_orderids: list[str] = []
    for leg_req, leg_contract, leg_order in legs:
        api.client.placeOrder(leg_order.orderId, leg_contract, leg_order)
        vt_orderids.append(_register(api, leg_req, leg_contract, leg_order))

    api.client.reqIds(1)
    return vt_orderids


def new_oca_group(symbol: str) -> str:
    return f"ChartPro_{symbol}_{datetime.now():%H%M%S}_{next(_oca_counter)}"


def place_exit(
    gateway: IbGateway,
    symbol: str,
    exchange: Exchange,
    position: float,
    order_type: OrderType,
    price: float,
    oca_group: str
) -> str:
    """
    给已有持仓下一条止盈（LIMIT）或止损（STOP）单。
    position 为带符号的持仓数量；同一 oca_group 内的单子一个成交，其余由 IB 自动取消。
    """
    api: IbApi | None = get_api(gateway)
    if not api or not position:
        return ""

    direction: Direction = Direction.SHORT if position > 0 else Direction.LONG
    req: OrderRequest = _exit_request(symbol, exchange, direction, abs(position), order_type, price)

    built = _build(api, req, "GTC")
    if not built:
        return ""
    contract, order = built
    order.ocaGroup = oca_group
    order.ocaType = 1       # 一个成交后取消其余（带保护，避免超量成交）

    api.client.placeOrder(order.orderId, contract, order)
    api.client.reqIds(1)
    return _register(api, req, contract, order)


def get_ib_order(gateway: IbGateway, orderid: str) -> tuple[Contract, Order] | None:
    """取得委托对应的 IB 合约和委托对象：优先用本地下单时保存的，其次用 openOrder 回报的"""
    api: IbApi | None = getattr(gateway, "api", None)
    if api is None:
        return None
    return api.chart_orders.get(orderid) or api.ib_open_orders.get(orderid)


def get_oca_group(gateway: IbGateway, orderid: str) -> str:
    cached = get_ib_order(gateway, orderid)
    if not cached:
        return ""
    return cached[1].ocaGroup or ""


def get_parent(gateway: IbGateway, orderid: str) -> str:
    """括号单子单对应的入场单 orderid，没有则返回空字符串"""
    api: IbApi | None = getattr(gateway, "api", None)
    if api is None:
        return ""
    return api.order_parents.get(orderid, "")


def is_foreign(orderid: str) -> bool:
    """别的客户号下的单（只读）"""
    return orderid.startswith(("c", "p"))


def modify_order(gateway: IbGateway, orderid: str, price: float, volume: float | None = None) -> bool:
    """修改限价单/止损单的价格和数量（同一 orderId 重新提交）"""
    api: IbApi | None = get_api(gateway)
    if not api:
        return False

    data: OrderData | None = api.orders.get(orderid)
    if not data or not data.is_active() or is_foreign(orderid):
        return False

    cached = get_ib_order(gateway, orderid)
    if cached:
        contract, order = cached
        # openOrder 回报的委托对象里有些字段不能原样提交回去
        for attr in ("eTradeOnly", "firmQuoteOnly"):
            if hasattr(order, attr):
                setattr(order, attr, False)
    else:
        # 没有缓存时按 vnpy 委托数据重建（例如其他程序下的单）
        req: OrderRequest = _exit_request(
            data.symbol, data.exchange, data.direction, data.volume, data.type, data.price
        )
        contract = generate_ib_contract(req.symbol, req.exchange)
        if not contract or data.type not in ORDERTYPE_VT2IB:
            return False
        order = Order()
        order.orderId = int(orderid)
        order.clientId = api.clientid
        order.action = DIRECTION_VT2IB[data.direction]
        order.orderType = ORDERTYPE_VT2IB[data.type]
        order.totalQuantity = Decimal(str(float(data.volume) - float(data.traded)))
        order.account = api.account
        order.tif = "GTC"

    order.outsideRth = True     # 改单时一并补上，旧单拖动一次即可在盘外触发

    if order.orderType == "LMT":
        order.lmtPrice = price
    elif order.orderType == "STP":
        order.auxPrice = price
    else:
        return False

    if volume:
        order.totalQuantity = Decimal(str(volume))
        data.volume = float(volume)

    order.transmit = True
    api.client.placeOrder(int(orderid), contract, order)
    api.chart_orders[orderid] = (contract, order)

    data.price = price
    api.gateway.on_order(copy(data))
    return True


# ---------------------------------------------------------------------- 挂单核对
# vnpy_ib 断线重连后不会同步断线期间的变化：期间成交或撤销的委托在本地仍显示为挂着，
# 撤单时 IB 回复"订单不存在"。连接后主动核对，本地有而 IB 上没有的挂单标记为已撤销。

# 撤单相关错误：10147 订单不存在、10148 订单状态不可撤（已成交/已撤）、161 不在可撤状态
STALE_ORDER_ERRORS: set[int] = {10147, 10148, 161}


def start_order_sync(api: IbApi) -> None:
    """向 IB 查询全部挂单（含别的客户号下的，只读显示）和当日成交"""
    if not api.status:
        return
    api.sync_seen = set()
    api.syncing = True
    api.client.reqAllOpenOrders()

    api.reqid += 1
    api.client.reqExecutions(api.reqid, ExecutionFilter())


def finish_order_sync(api: IbApi) -> None:
    """挂单回报结束：本地仍为挂单状态、但 IB 没有回报的委托，标记为已撤销"""
    if not api.syncing:
        return
    api.syncing = False

    for orderid, order in list(api.orders.items()):
        # 只处理已被 IB 确认过的挂单；刚发出、还未确认的（SUBMITTING）不动
        if order.status not in (Status.NOTTRADED, Status.PARTTRADED):
            continue
        if orderid in api.sync_seen:
            continue

        order.status = Status.CANCELLED
        api.gateway.on_order(copy(order))
        api.gateway.write_log(f"委托{orderid}在IB上已不存在（可能在断线期间成交或撤销），已移出活动委托")


def on_order_error(api: IbApi, reqid: int, error_code: int) -> None:
    """撤单失败且原因是订单已不存在/已结束时，重新核对挂单"""
    if error_code in STALE_ORDER_ERRORS and not api.syncing:
        start_order_sync(api)
