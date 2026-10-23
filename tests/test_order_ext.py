from decimal import Decimal
import pytest

pytest.importorskip("ibapi", reason="缺少 ibapi")

from ibapi.contract import Contract  # noqa: E402
from ibapi.order import Order  # noqa: E402
from ibapi.order_state import OrderState  # noqa: E402

from vnpy.trader.constant import Direction, Exchange, OrderType, Status  # noqa: E402
from vnpy.trader.object import OrderRequest  # noqa: E402

from tests.test_ib_gateway import CallRecorder, Sink, arm_orders, recorder, sink  # noqa: E402,F401
from vnpy_ib import order_ext  # noqa: E402
from vnpy_ib.ib_gateway import IbApi, IbGateway  # noqa: E402


@pytest.fixture
def gateway(sink: Sink) -> IbGateway:     # noqa: F811
    from vnpy.event import EventEngine
    ib: IbGateway = IbGateway(EventEngine(), "IB")
    sink.attach(ib)
    return ib


@pytest.fixture
def armed(gateway: IbGateway, recorder: CallRecorder, monkeypatch: pytest.MonkeyPatch) -> IbApi:  # noqa: F811
    api: IbApi = gateway.api
    arm_orders(api, recorder, monkeypatch)
    return api


def mnq_request(direction: Direction = Direction.LONG, order_type: OrderType = OrderType.LIMIT,
                price: float = 29800.25, volume: float = 2) -> OrderRequest:
    return OrderRequest(
        symbol="MNQ-202612-USD-FUT", exchange=Exchange.CME, direction=direction,
        type=order_type, volume=volume, price=price,
    )


def placed(recorder: CallRecorder) -> list[tuple[int, Contract, Order]]:     # noqa: F811
    return [args for name, args in recorder.calls if name == "placeOrder"]


def test_not_connected_does_nothing(gateway: IbGateway, sink: Sink) -> None:   # noqa: F811
    assert order_ext.place_order(gateway, mnq_request()) == ""
    assert order_ext.place_bracket(gateway, mnq_request(), 29850, 29780) == []
    assert sink.orders == []


def test_place_order_is_gtc_and_cached(gateway: IbGateway, armed: IbApi, recorder: CallRecorder) -> None:  # noqa: F811
    vt_orderid: str = order_ext.place_order(gateway, mnq_request())

    assert vt_orderid == "IB.1"
    order_id, contract, order = placed(recorder)[0]
    assert order_id == 1 and order.tif == "GTC" and order.lmtPrice == 29800.25
    assert contract.symbol == "MNQ" and contract.lastTradeDateOrContractMonth == "202612"
    assert armed.chart_orders["1"][1] is order


def test_bracket_long(gateway: IbGateway, armed: IbApi, recorder: CallRecorder, sink: Sink) -> None:  # noqa: F811
    ids: list[str] = order_ext.place_bracket(gateway, mnq_request(), take_profit=29850.0, stop_loss=29780.0)

    assert ids == ["IB.1", "IB.2", "IB.3"]
    calls = placed(recorder)
    parent, tp, sl = (c[2] for c in calls)

    # 入场单先不发送，最后一条腿发送整组
    assert (parent.transmit, tp.transmit, sl.transmit) == (False, False, True)
    assert parent.action == "BUY" and parent.orderType == "LMT"
    assert tp.action == "SELL" and tp.orderType == "LMT" and tp.lmtPrice == 29850.0
    assert sl.action == "SELL" and sl.orderType == "STP" and sl.auxPrice == 29780.0
    assert tp.parentId == 1 and sl.parentId == 1
    assert tp.totalQuantity == Decimal("2") and tp.tif == "GTC"
    assert parent.outsideRth and tp.outsideRth and sl.outsideRth      # 止损在常规时段之外也能触发

    assert armed.order_parents == {"2": "1", "3": "1"}
    assert order_ext.get_parent(gateway, "3") == "1"
    assert [o.direction for o in sink.orders] == [Direction.LONG, Direction.SHORT, Direction.SHORT]
    assert recorder.names()[-1] == "reqIds"


def test_bracket_only_stop_loss(gateway: IbGateway, armed: IbApi, recorder: CallRecorder) -> None:  # noqa: F811
    ids: list[str] = order_ext.place_bracket(
        gateway, mnq_request(Direction.SHORT, OrderType.STOP, 29700.0), take_profit=None, stop_loss=29760.0
    )
    assert len(ids) == 2
    parent, sl = (c[2] for c in placed(recorder))
    assert parent.action == "SELL" and parent.orderType == "STP" and parent.transmit is False
    assert sl.action == "BUY" and sl.orderType == "STP" and sl.transmit is True and sl.parentId == parent.orderId


def test_bracket_without_exits_is_plain_order(gateway: IbGateway, armed: IbApi, recorder: CallRecorder) -> None:  # noqa: F811
    assert order_ext.place_bracket(gateway, mnq_request(), None, None) == ["IB.1"]
    assert placed(recorder)[0][2].transmit is True


def test_position_exits_share_oca_group(gateway: IbGateway, armed: IbApi, recorder: CallRecorder) -> None:  # noqa: F811
    group: str = order_ext.new_oca_group("MNQ")
    tp_id: str = order_ext.place_exit(gateway, "MNQ", Exchange.CME, -3, OrderType.LIMIT, 29700.0, group)
    sl_id: str = order_ext.place_exit(gateway, "MNQ", Exchange.CME, -3, OrderType.STOP, 29900.0, group)

    assert tp_id and sl_id
    tp, sl = (c[2] for c in placed(recorder))
    assert tp.action == "BUY" and sl.action == "BUY"          # 空头持仓用买单平仓
    assert tp.totalQuantity == Decimal("3")
    assert tp.ocaGroup == sl.ocaGroup == group and tp.ocaType == sl.ocaType == 1
    assert order_ext.get_oca_group(gateway, "2") == group


def test_modify_keeps_order_id(gateway: IbGateway, armed: IbApi, recorder: CallRecorder, sink: Sink) -> None:  # noqa: F811
    order_ext.place_bracket(gateway, mnq_request(), take_profit=29850.0, stop_loss=29780.0)
    for orderid in ("1", "2", "3"):
        armed.orders[orderid].status = Status.NOTTRADED

    assert order_ext.modify_order(gateway, "3", 29770.0)
    order_id, _, order = placed(recorder)[-1]
    assert order_id == 3 and order.auxPrice == 29770.0 and order.transmit is True
    assert order.parentId == 1                      # 仍然挂在入场单下
    assert sink.orders[-1].price == 29770.0

    assert order_ext.modify_order(gateway, "1", 29801.0)
    assert placed(recorder)[-1][2].lmtPrice == 29801.0


def test_modify_rejects_inactive_or_unknown(gateway: IbGateway, armed: IbApi) -> None:
    assert not order_ext.modify_order(gateway, "99", 1.0)
    order_ext.place_order(gateway, mnq_request())
    armed.orders["1"].status = Status.ALLTRADED
    assert not order_ext.modify_order(gateway, "1", 1.0)


def test_open_order_records_parent_for_restored_orders(armed: IbApi, recorder: CallRecorder) -> None:  # noqa: F811
    contract: Contract = Contract()
    contract.symbol = "MNQ"
    contract.secType = "FUT"
    contract.exchange = "CME"
    contract.currency = "USD"
    contract.lastTradeDateOrContractMonth = "20261218"

    order: Order = Order()
    order.action = "SELL"
    order.orderType = "STP"
    order.totalQuantity = Decimal("1")
    order.auxPrice = 29700.0
    order.parentId = 41
    order.orderRef = ""

    armed.openOrder(42, contract, order, OrderState())
    assert armed.order_parents["42"] == "41"
    assert isinstance(armed.orders["42"].volume, float)          # 恢复的委托数量是 float
    assert armed.ib_open_orders["42"][1] is order

    # 重启后恢复的单子也能改价
    armed.orders["42"].status = Status.NOTTRADED
    assert order_ext.modify_order(armed.gateway, "42", 29710.0)
    order_id, _, sent = placed(recorder)[-1]
    assert order_id == 42 and sent.auxPrice == 29710.0 and sent.parentId == 41
    assert sent.outsideRth          # 旧单改价时补上盘外触发


def _stub_sync(api: IbApi, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(api.client, "reqAllOpenOrders", lambda: calls.append("reqOpenOrders"))
    monkeypatch.setattr(api.client, "reqExecutions", lambda *a: calls.append("reqExecutions"))
    return calls


def _restored(orderid: int, order_type: str, price: float) -> tuple[Contract, Order]:
    contract: Contract = Contract()
    contract.symbol = "MNQ"
    contract.secType = "FUT"
    contract.exchange = "CME"
    contract.currency = "USD"
    contract.lastTradeDateOrContractMonth = "20261218"
    order: Order = Order()
    order.orderId = orderid
    order.action = "SELL"
    order.orderType = order_type
    order.totalQuantity = Decimal("1")
    order.orderRef = ""
    if order_type == "LMT":
        order.lmtPrice = price
    else:
        order.auxPrice = price
    return contract, order


def test_reconnect_sync_removes_orders_gone_during_disconnect(
    gateway: IbGateway, armed: IbApi, sink: Sink, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    calls = _stub_sync(armed, monkeypatch)

    # 断线前：一个止盈、一个止损挂着
    for oid, kind, price in ((10, "LMT", 29700.0), (11, "STP", 29600.0)):
        armed.openOrder(oid, *_restored(oid, kind, price), OrderState())
        armed.orders[str(oid)].status = Status.NOTTRADED
    # 刚发出、尚未确认的单子不能被误判
    order_ext.place_order(gateway, mnq_request())
    armed.orders["1"].status = Status.SUBMITTING

    # TWS 重启期间止损成交、止盈被 OCA 撤销；重连后 IB 只回报还挂着的单（这里一个都没有）
    armed.managedAccounts("DU123")
    assert calls == ["reqOpenOrders", "reqExecutions"]
    armed.openOrderEnd()

    assert armed.orders["10"].status == Status.CANCELLED
    assert armed.orders["11"].status == Status.CANCELLED
    assert armed.orders["1"].status == Status.SUBMITTING
    assert any("已不存在" in log for log in sink.logs)


def test_sync_keeps_orders_still_open(armed: IbApi, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_sync(armed, monkeypatch)
    armed.openOrder(20, *_restored(20, "LMT", 29700.0), OrderState())
    armed.orders["20"].status = Status.NOTTRADED

    order_ext.start_order_sync(armed)
    armed.openOrder(20, *_restored(20, "LMT", 29700.0), OrderState())   # IB 回报：仍然挂着
    armed.openOrderEnd()
    assert armed.orders["20"].status == Status.NOTTRADED


def test_cancel_not_found_error_triggers_sync(armed: IbApi, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_sync(armed, monkeypatch)
    armed.openOrder(30, *_restored(30, "STP", 29600.0), OrderState())
    armed.orders["30"].status = Status.NOTTRADED

    armed.error(30, 0, 10147, "OrderId 30 that needs to be cancelled is not found.")
    assert calls[0] == "reqOpenOrders"
    armed.openOrderEnd()
    assert armed.orders["30"].status == Status.CANCELLED

    # 普通通知不会触发核对
    calls.clear()
    armed.error(-1, 0, 2104, "Market data farm connection is OK")
    assert calls == []


def test_foreign_orders_get_prefixed_ids(gateway: IbGateway, armed: IbApi, sink: Sink) -> None:  # noqa: F811
    """别的客户号下的单：编号加前缀，不能改单"""
    armed.clientid = 1
    contract, order = _restored(7, "STP", 29600.0)
    order.clientId = 950
    armed.openOrder(7, contract, order, OrderState())
    assert "c950-7" in armed.orders and "7" not in armed.orders
    assert not order_ext.modify_order(gateway, "c950-7", 29610.0)

    contract, order = _restored(0, "LMT", 29700.0)
    order.clientId, order.permId = 0, 123456
    armed.openOrder(0, contract, order, OrderState())
    assert "p123456" in armed.orders


def test_modify_order_volume(gateway: IbGateway, armed: IbApi, sink: Sink, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    calls: list = []
    monkeypatch.setattr(armed.client, "placeOrder", lambda *a: calls.append(a))
    monkeypatch.setattr(armed.client, "reqIds", lambda *a: None)
    req = OrderRequest(symbol="MNQ", exchange=Exchange.CME, direction=Direction.LONG, type=OrderType.LIMIT,
                       volume=1, price=29000)
    vt = order_ext.place_order(gateway, req)
    oid = vt.split(".")[1]
    armed.orders[oid].status = Status.NOTTRADED
    assert order_ext.modify_order(gateway, oid, 29001.0, 3)
    assert calls[-1][2].totalQuantity == Decimal("3") and armed.orders[oid].volume == 3


def test_foreign_order_with_non_time_order_ref_and_unsupported_type(gateway: IbGateway, armed: IbApi, sink: Sink) -> None:  # noqa: F811
    armed.clientid = 1
    contract, order = _restored(9, "LMT", 29700.0)
    order.clientId, order.orderRef = 950, "ChartPro"
    armed.openOrder(9, contract, order, OrderState())
    assert "c950-9" in armed.orders

    contract, order = _restored(10, "LMT", 29700.0)
    order.clientId, order.orderType = 950, "TRAIL"
    armed.openOrder(10, contract, order, OrderState())
    assert "c950-10" not in armed.orders


def test_rejected_order_marked(gateway: IbGateway, armed: IbApi, sink: Sink, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(armed.client, "placeOrder", lambda *a: None)
    monkeypatch.setattr(armed.client, "reqIds", lambda *a: None)
    monkeypatch.setattr(armed, "sync_order_id", lambda: None)
    req = OrderRequest(symbol="MNQ", exchange=Exchange.CME, direction=Direction.LONG, type=OrderType.LIMIT,
                       volume=1, price=29000)
    vt = order_ext.place_order(gateway, req)
    oid = vt.split(".")[1]
    armed.error(int(oid), 0, 103, "Duplicate order id")
    assert armed.orders[oid].status == Status.REJECTED
