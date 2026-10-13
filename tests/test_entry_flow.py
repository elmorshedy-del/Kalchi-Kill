import asyncio
from decimal import Decimal as D
import pytest

from app.engine import Book
from app.engine_v2 import StopEngine
from app.store import EntryPlan, StopStore


class Exchange:
    configured = True
    env = 'demo'

    def __init__(self):
        self.entries = []
        self.exits = []

    async def get_market(self, ticker):
        return {'ticker': ticker, 'title': 'Test market', 'status': 'active'}

    async def create_entry_ioc(self, ticker, direction, quantity, price):
        self.entries.append((ticker, direction, quantity, price))
        return {'fill_count': '4.00', 'average_fill_price': '0.4000'}

    async def create_reduce_ioc_count(self, ticker, direction, quantity, price):
        self.exits.append((ticker, direction, quantity, price))
        return {'fill_count': str(quantity)}


def make_engine(tmp_path):
    exchange = Exchange()
    engine = StopEngine(exchange, StopStore(str(tmp_path)), True)
    engine.ws_connected = True
    book = Book()
    book.snapshot({'yes_dollars_fp': [['0.39', '100']], 'no_dollars_fp': [['0.60', '100']]})
    engine.books['TEST'] = book
    return engine, exchange


def test_active_engine_creates_and_persists_price_entry(tmp_path):
    async def run():
        engine, _ = make_engine(tmp_path)
        plan = await engine.create_entry_plan('test', 'yes', D('10'), 'price', 'lte', D('.30'), D('.02'), 'price', 'gte', D('.80'), D('.03'))
        assert plan.entry_mode == 'price'
        restored = StopStore(str(tmp_path))
        await restored.load()
        assert restored.entry_plans[plan.plan_id].entry_mode == 'price'
        assert engine.state()['entry_plans'][0]['entry_metric'] == '0.40'
    asyncio.run(run())


def test_existing_explicit_pnl_plan_uses_pnl_after_restart(tmp_path):
    engine, _ = make_engine(tmp_path)
    engine.positions['TEST'] = D('10')
    engine.cost_basis['TEST'] = D('.30')
    plan = EntryPlan.new(ticker='TEST', direction='yes', quantity=D('2'), entry_mode='pnl_dollars', entry_operator='gte', entry_trigger=D('1'), entry_slippage=D('.02'), exit_mode='price', exit_operator='gte', exit_trigger=D('.8'), exit_slippage=D('.03'))
    engine.store.entry_plans[plan.plan_id] = plan
    row = engine.state()['entry_plans'][0]
    assert row['entry_mode'] == 'pnl_dollars'
    assert D(row['entry_metric']) == D('.90')


def test_filled_entry_waits_for_account_position_before_linked_exit(tmp_path):
    async def run():
        engine, exchange = make_engine(tmp_path)
        plan = EntryPlan.new(ticker='TEST', direction='yes', quantity=D('10'), entry_mode='price', entry_operator='lte', entry_trigger=D('.40'), entry_slippage=D('.02'), exit_mode='price', exit_operator='lte', exit_trigger=D('.39'), exit_slippage=D('.03'))
        engine.store.entry_plans[plan.plan_id] = plan
        await engine._fire_entry(plan.plan_id)
        await asyncio.sleep(0)
        assert plan.status == 'waiting_exit'
        assert plan.open_qty == D('4')
        assert plan.armed
        assert len(exchange.entries) == 1
        engine.positions['TEST'] = D('4')
        await engine._fire_linked_exit(plan.plan_id)
        assert plan.status == 'completed'
        assert exchange.exits[0][2] == D('4')
    asyncio.run(run())


@pytest.mark.parametrize('direction,expected_price', [('yes', D('.40')), ('no', D('.60'))])
def test_concurrent_entry_triggers_buy_once_and_use_actual_fill(tmp_path, direction, expected_price):
    async def run():
        engine, exchange = make_engine(tmp_path)
        plan = EntryPlan.new(ticker='TEST', direction=direction, quantity=D('10'), entry_mode='price', entry_operator='lte', entry_trigger=D('.70'), entry_slippage=D('.02'), exit_mode='price', exit_operator='gte', exit_trigger=D('.90'), exit_slippage=D('.03'))
        engine.store.entry_plans[plan.plan_id] = plan
        await asyncio.gather(engine._fire_entry(plan.plan_id), engine._fire_entry(plan.plan_id))
        assert len(exchange.entries) == 1
        assert plan.open_qty == D('4')
        assert plan.entry_price == expected_price
        assert plan.status == 'waiting_exit'
    asyncio.run(run())


def test_legacy_encoded_pnl_rule_and_closed_linked_position(tmp_path):
    async def run():
        engine, _ = make_engine(tmp_path)
        plan = EntryPlan.new(ticker='TEST', direction='yes', quantity=D('10'), entry_mode='price', entry_operator='pnl_percent:lte', entry_trigger=D('-20'), entry_slippage=D('.02'), exit_mode='price', exit_operator='gte', exit_trigger=D('.90'), exit_slippage=D('.03'), status='waiting_exit', open_qty=D('4'), entry_price=D('.40'))
        engine.store.entry_plans[plan.plan_id] = plan
        assert engine._decode_entry_condition(plan) == ('pnl_percent', 'lte')
        await engine._handle_position_change('TEST', D('4'), D('0'))
        assert plan.status == 'completed'
        assert not plan.armed
        assert plan.open_qty == 0
    asyncio.run(run())
