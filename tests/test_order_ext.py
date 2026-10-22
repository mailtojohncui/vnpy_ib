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
    assert armed.ib_open_orders["42"][1] is order

    # 重启后恢复的单子也能改价
    armed.orders["42"].status = Status.NOTTRADED
    assert order_ext.modify_order(armed.gateway, "42", 29710.0)
    order_id, _, sent = placed(recorder)[-1]
    assert order_id == 42 and sent.auxPrice == 29710.0 and sent.parentId == 41
