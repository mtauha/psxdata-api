"""Unit tests for screener router."""
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from psxdata.exceptions import PSXUnavailableError

from api.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_list_screener_returns_200(client: TestClient) -> None:
    df = pd.DataFrame({
        "symbol": ["ENGRO", "LUCK"],
        "sector": [8.0, 6.0],
        "listed_in": ["REGULAR", "REGULAR"],
        "market_cap": [1.2e9, 5.6e8],
        "price": [250.5, 900.1],
        "pe_ratio": [8.2, 12.4],
        "dividend_yield": [0.045, 0.02],
        "free_float": [35.0, 40.0],
        "volume_avg_30d": [1500000.0, 800000.0],
        "change_1y_pct": [12.3, -4.5],
    })
    with patch("psxdata.screener", return_value=df):
        resp = client.get("/screener")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["data"]) == 2
    assert body["data"][0]["symbol"] == "ENGRO"
    assert body["meta"]["count"] == 2


def test_list_screener_empty_returns_200(client: TestClient) -> None:
    with patch("psxdata.screener", return_value=pd.DataFrame()):
        resp = client.get("/screener")
    assert resp.status_code == 200
    assert resp.json()["data"] == []
    assert resp.json()["meta"]["count"] == 0


def test_screener_psx_unavailable_returns_503(client: TestClient) -> None:
    with patch("psxdata.screener", side_effect=PSXUnavailableError("PSX down")):
        resp = client.get("/screener")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "psx_unavailable"
