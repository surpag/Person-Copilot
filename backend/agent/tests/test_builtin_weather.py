"""天气工具测试。

真实网络请求容易失败/慢，所以用 mock。
每次 get_weather 调用会发 3 个 HTTP 请求：
    1. geo/v2/city/lookup  → 拿 location_id
    2. v7/weather/now      → 实时天气
    3. v7/weather/3d       → 未来 3 天预报

另有一个 `@pytest.mark.network` 标记的真实测试（默认不跑）。
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.builtin import _format_weather, get_weather
from tools.registry import invoke_tool

# ============================================================
# 和风天气返回样例
# ============================================================

_FAKE_GEO_RESPONSE = {
    "code": "200",
    "location": [
        {
            "name": "南京",
            "id": "101190101",
            "lat": "32.06",
            "lon": "118.79",
        }
    ],
}

_FAKE_NOW_RESPONSE = {
    "code": "200",
    "now": {
        "temp": "22",
        "feelsLike": "21",
        "humidity": "65",
        "windSpeed": "12",
        "windDir": "东北风",
        "vis": "10",
        "pressure": "1013",
        "text": "多云",
    },
}

_FAKE_FORECAST_RESPONSE = {
    "code": "200",
    "daily": [
        {
            "fxDate": "2026-09-22",
            "textDay": "多云",
            "textNight": "晴",
            "tempMax": "26",
            "tempMin": "18",
        },
        {
            "fxDate": "2026-09-23",
            "textDay": "晴",
            "textNight": "晴",
            "tempMax": "27",
            "tempMin": "19",
        },
        {
            "fxDate": "2026-09-24",
            "textDay": "阴",
            "textNight": "小雨",
            "tempMax": "25",
            "tempMin": "20",
        },
    ],
}


# ============================================================
# mock 工具
# ============================================================


def _make_response(status_code: int, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json = MagicMock(return_value=json_data)
    else:
        resp.json = MagicMock(side_effect=ValueError("no json"))
    return resp


def _mock_httpx(mocker, responses: list):
    """
    构造一个 mock httpx.AsyncClient。

    responses: 每次 client.get() 依次返回的响应。
               元素可以是：
                 - _make_response(...)  正常返回
                 - Exception 实例       直接抛出
    """
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=responses)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    mocker.patch("tools.builtin.httpx.AsyncClient", return_value=mock_client)
    return mock_client


def _mock_httpx_simple(mocker, status_code=200, json_data=None, exc=None):
    """便捷版本：3 次请求都返回相同结果（用于测同一种错误）。"""
    if exc is not None:
        responses = [exc, exc, exc]
    else:
        responses = [
            _make_response(status_code, json_data),
            _make_response(status_code, json_data),
            _make_response(status_code, json_data),
        ]
    return _mock_httpx(mocker, responses)


# ============================================================
# _format_weather 纯函数
# ============================================================


class TestFormat:
    def test_full_fields(self):
        result = _format_weather("南京", _FAKE_NOW_RESPONSE, _FAKE_FORECAST_RESPONSE)
        assert "南京" in result
        assert "多云" in result
        assert "22" in result
        assert "65" in result

    def test_missing_now_graceful(self):
        """now 字段缺失时返回错误提示，不崩溃"""
        result = _format_weather("X", {}, {})
        assert "❌" in result or "未能获取" in result

    def test_forecast_optional(self):
        """没有 forecast 时只显示实时天气"""
        result = _format_weather("南京", _FAKE_NOW_RESPONSE, {"code": "200"})
        assert "多云" in result
        # 没有 daily 时不应显示预报段
        assert "未来天气预报" not in result

    def test_forecast_shows_first_three_days(self):
        result = _format_weather("南京", _FAKE_NOW_RESPONSE, _FAKE_FORECAST_RESPONSE)
        assert "2026-09-22" in result
        assert "2026-09-23" in result
        assert "2026-09-24" in result


# ============================================================
# get_weather 通过 invoke_tool 调用
# ============================================================


class TestGetWeather:
    @pytest.fixture(autouse=True)
    def _fake_env(self, monkeypatch, tmp_path):
        # 造一个假的私钥文件
        key_file = tmp_path / "fake.pem"
        key_file.write_text("fake-key-content")

        monkeypatch.setenv("QWEATHER_API_HOST", "https://devapi.qweather.com")
        monkeypatch.setenv("QWEATHER_PROJECT_ID", "test-project")
        monkeypatch.setenv("QWEATHER_KEY_ID", "test-key-id")
        monkeypatch.setenv("QWEATHER_PRIVATE_KEY_PATH", str(key_file))

        # mock JWT 签发，避免真的用私钥
        monkeypatch.setattr(
            "tools.builtin._generate_jwt", lambda *a, **kw: "fake-jwt-token"
        )

    @pytest.fixture(autouse=True)
    def _fake_env(self, monkeypatch, tmp_path):
        # ← 新增：清空 JWT 缓存，避免上一个测试的缓存命中
        monkeypatch.setattr("tools.builtin._jwt_cache", {"token": None, "exp": 0})

        # 造一个假的私钥文件
        key_file = tmp_path / "fake.pem"
        key_file.write_text("fake-key-content")

        monkeypatch.setenv("QWEATHER_API_HOST", "https://devapi.qweather.com")
        monkeypatch.setenv("QWEATHER_PROJECT_ID", "test-project")
        monkeypatch.setenv("QWEATHER_KEY_ID", "test-key-id")
        monkeypatch.setenv("QWEATHER_PRIVATE_KEY_PATH", str(key_file))

        monkeypatch.setattr(
            "tools.builtin._generate_jwt", lambda *a, **kw: "fake-jwt-token"
        )

    @pytest.mark.asyncio
    async def test_success(self, mocker):
        _mock_httpx(
            mocker,
            [
                _make_response(200, _FAKE_GEO_RESPONSE),
                _make_response(200, _FAKE_NOW_RESPONSE),
                _make_response(200, _FAKE_FORECAST_RESPONSE),
            ],
        )
        result = await invoke_tool("get_weather", '{"city": "南京"}')
        assert "多云" in result
        assert "22" in result
        assert "未来天气预报" in result

    @pytest.mark.asyncio
    async def test_empty_city(self, mocker):
        """城市名为空时直接返回错误，不应发起任何请求"""
        mock = _mock_httpx(mocker, [])
        result = await invoke_tool("get_weather", '{"city": "   "}')
        assert "城市名不能为空" in result
        mock.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_city_not_found(self, mocker):
        """geo 接口返回空 location → 未找到城市"""
        _mock_httpx(
            mocker,
            [
                _make_response(200, {"code": "200", "location": []}),
            ],
        )
        result = await invoke_tool("get_weather", '{"city": "不存在的地方"}')
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_city_geo_404(self, mocker):
        """geo 接口返回 404"""
        _mock_httpx(mocker, [_make_response(404)])
        result = await invoke_tool("get_weather", '{"city": "不存在的地方"}')
        assert "404" in result or "未找到" in result

    @pytest.mark.asyncio
    async def test_http_500(self, mocker):
        """接口返回 500"""
        _mock_httpx(mocker, [_make_response(500)])
        result = await invoke_tool("get_weather", '{"city": "南京"}')
        assert "HTTP 500" in result

    @pytest.mark.asyncio
    async def test_timeout(self, mocker):
        import httpx

        _mock_httpx(mocker, [httpx.TimeoutException("timeout")])
        result = await invoke_tool("get_weather", '{"city": "南京"}')
        assert "超时" in result

    @pytest.mark.asyncio
    async def test_network_error(self, mocker):
        import httpx

        _mock_httpx(mocker, [httpx.ConnectError("boom")])
        result = await invoke_tool("get_weather", '{"city": "南京"}')
        assert "网络错误" in result

    @pytest.mark.asyncio
    async def test_missing_api_key(self, mocker, monkeypatch):
        """未配置 JWT 凭据时返回友好提示"""
        monkeypatch.delenv("QWEATHER_PROJECT_ID", raising=False)
        result = await invoke_tool("get_weather", '{"city": "南京"}')
        assert "未配置" in result

    @pytest.mark.asyncio
    async def test_invalid_json_in_geo(self, mocker):
        """geo 返回非 JSON → 由 httpx 异常兜底"""
        _mock_httpx(mocker, [_make_response(200, json_data=None)])
        # resp.json() 会抛 ValueError，run 里没有单独捕获 ValueError，
        # 如果发生会走 invoke_tool 的兜底
        result = await invoke_tool("get_weather", '{"city": "南京"}')
        # 要么是 geo 解析失败，要么是 geo 未找到
        assert "❌" in result
