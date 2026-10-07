import datetime
import re
import pytest

from run import get_current_time, get_weather, available_tools, tool_definitions


class TestGetCurrentTime:
    def test_returns_string(self):
        result = get_current_time()
        assert isinstance(result, str)

    def test_format_is_correct(self):
        result = get_current_time()
        # 形如 2026-09-13 21:33:45
        assert re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", result)

    def test_time_is_close_to_now(self):
        result = get_current_time()
        parsed = datetime.datetime.strptime(result, "%Y-%m-%d %H:%M:%S")
        delta = abs((datetime.datetime.now() - parsed).total_seconds())
        assert delta < 5  # 5 秒内


class TestGetWeather:
    @pytest.mark.parametrize(
        "city,expected",
        [
            ("南京", "晴"),
            ("上海", "雨天"),
            ("北京", "多云"),
        ],
    )
    def test_known_cities(self, city, expected):
        assert get_weather(city) == expected

    def test_unknown_city(self):
        result = get_weather("火星")
        assert "火星" in result
        assert "抱歉" in result or "没有" in result

    def test_missing_argument_raises(self):
        with pytest.raises(TypeError):
            get_weather()  # 缺少 city


class TestToolRegistry:
    def test_available_tools_contains_all(self):
        assert "get_current_time" in available_tools
        assert "get_weather" in available_tools
        assert callable(available_tools["get_current_time"])
        assert callable(available_tools["get_weather"])

    def test_tool_definitions_shape(self):
        defs = tool_definitions()
        assert isinstance(defs, list)
        assert len(defs) == 2
        for d in defs:
            assert d["type"] == "function"
            assert "name" in d["function"]
            assert "description" in d["function"]
            assert d["function"]["parameters"]["type"] == "object"

    def test_weather_tool_has_required_city(self):
        defs = {d["function"]["name"]: d for d in tool_definitions()}
        weather = defs["get_weather"]
        assert "city" in weather["function"]["parameters"]["properties"]
        assert "city" in weather["function"]["parameters"]["required"]
