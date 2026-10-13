import asyncio
from decimal import Decimal as D

import pytest
from app.kalshi import KalshiClient


@pytest.mark.parametrize('direction,reduce,side,price', [
    ('yes', False, 'bid', '0.5400'),
    ('no', False, 'ask', '0.4600'),
    ('yes', True, 'ask', '0.5400'),
    ('no', True, 'bid', '0.4600'),
])
def test_ioc_yes_no_order_routing(direction, reduce, side, price):
    async def run():
        client = object.__new__(KalshiClient)
        client.subaccount = 0
        requests = []
        async def request(method, path, *, json_body):
            requests.append((method, path, json_body))
            return {'fill_count': '2.00'}
        client.request = request
        if reduce:
            await client.create_reduce_ioc_count('TEST', direction, D('2'), D('.54'))
        else:
            await client.create_entry_ioc('TEST', direction, D('2'), D('.54'))
        method, path, payload = requests[0]
        assert (method, path) == ('POST', '/portfolio/events/orders')
        assert payload['side'] == side
        assert payload['price'] == price
        assert payload['count'] == '2.00'
        assert payload['time_in_force'] == 'immediate_or_cancel'
        assert payload['reduce_only'] is reduce
        assert payload['exchange_index'] == -1
    asyncio.run(run())
