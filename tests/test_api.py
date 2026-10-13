import importlib

from fastapi.testclient import TestClient

from test_entry_flow import make_engine


def test_authenticated_entry_api_and_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    main = importlib.import_module('app.main')
    engine, exchange = make_engine(tmp_path)
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(main, 'DASHBOARD_PASSWORD', 'test-password')
    monkeypatch.setattr(main, 'COOKIE_SECRET', 'test-secret')
    client = TestClient(main.app, raise_server_exceptions=False)
    assert client.get('/api/state').status_code == 401
    assert client.post('/api/login', json={'password': 'incorrect'}).status_code == 401
    client.cookies.set(main.COOKIE_NAME, main.session_token())
    payload = dict(ticker='TEST', direction='yes', quantity='10', entry_trigger_value='30', entry_slippage_cents='2', exit_trigger_value='80', exit_slippage_cents='3', exit_mode='price')
    response = client.post('/api/entries', json=payload)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['entry_mode'] == 'price'
    assert plan['entry_trigger'] == '0.3'
    assert len(client.get('/api/state').json()['entry_plans']) == 1
    assert exchange.entries == []  # Trigger not reached: no exchange order.
    assert client.delete('/api/entries/' + plan['plan_id']).status_code == 200
    assert client.get('/api/state').json()['entry_plans'] == []
    assert client.post('/api/entries', json={**payload, 'quantity': '0'}).status_code == 422
    engine.ws_connected = False
    assert client.post('/api/entries', json=payload).status_code == 400
