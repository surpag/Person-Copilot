"""内置工具集合。import 本模块即触发注册。"""

import os
import time
from datetime import datetime
from pathlib import Path

import httpx
import jwt
from pydantic import BaseModel, Field

from .registry import tool

# ============================================================
# 和风天气（JWT 认证）
# ============================================================

WEATHER_TIMEOUT_S = 8.0
DEFAULT_API_HOST = "https://devapi.qweather.com"

# JWT 缓存：和风 JWT 有效期 15 分钟，这里提前 60 秒续签
_jwt_cache: dict = {"token": None, "exp": 0}


def _generate_jwt(project_id: str, key_id: str, private_key_path: str) -> str:
    """用 Ed25519 私钥签发 JWT。"""
    private_key = Path(private_key_path).read_text(encoding="utf-8")
    now = int(time.time())
    developer_id = os.getenv("QWEATHER_DEVELOPER_ID", "")
    if not developer_id:
        raise RuntimeError("天气服务未配置：缺少 QWEATHER_DEVELOPER_ID")
    payload = {
        "iss": developer_id,
        "sub": project_id,
        "iat": now - 30,  # 提前 30s 生效，避免时钟偏差
        "exp": now + 900,  # 15 分钟有效期
    }
    headers = {"kid": key_id}
    return jwt.encode(payload, private_key, algorithm="EdDSA", headers=headers)


def _get_jwt_token() -> str:
    """带缓存地获取 JWT，过期前 60s 自动续签。"""
    now = int(time.time())
    if _jwt_cache["token"] and _jwt_cache["exp"] > now + 60:
        return _jwt_cache["token"]

    project_id = os.getenv("QWEATHER_PROJECT_ID", "")
    key_id = os.getenv("QWEATHER_KEY_ID", "")
    private_key_path = os.getenv("QWEATHER_PRIVATE_KEY_PATH", "")

    if not all([project_id, key_id, private_key_path]):
        raise RuntimeError(
            "天气服务未配置：需要 QWEATHER_PROJECT_ID / QWEATHER_KEY_ID / QWEATHER_PRIVATE_KEY_PATH"
        )
    if not Path(private_key_path).exists():
        raise RuntimeError(f"天气服务未配置：私钥文件不存在 {private_key_path}")

    token = _generate_jwt(project_id, key_id, private_key_path)
    _jwt_cache["token"] = token
    _jwt_cache["exp"] = now + 900
    return token


def _get_weather_config() -> tuple[str, str]:
    """返回 (api_host, jwt_token)。"""
    api_host = os.getenv("QWEATHER_API_HOST", DEFAULT_API_HOST).rstrip("/")
    if not api_host.startswith("http"):
        api_host = "https://" + api_host
    return api_host, _get_jwt_token()


class GetWeatherArgs(BaseModel):
    city: str = Field(description="城市名称，例如：南京、上海、北京")


@tool(
    description=(
        "获取指定城市的当前天气和未来 3 天预报。当用户询问某城市天气、气温、是否下雨时使用。"
        "支持中文城市名（如 南京、上海、北京）。"
    )
)
async def get_weather(args: GetWeatherArgs) -> str:
    city = (args.city or "").strip()
    if not city:
        return "❌ 城市名不能为空"

    try:
        api_host, token = _get_weather_config()
    except RuntimeError as e:
        return f"❌ {e}"

    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=WEATHER_TIMEOUT_S) as client:
            # 1. 城市查询（注意：JWT 认证不需要传 key 参数）
            geo_resp = await client.get(
                f"{api_host}/geo/v2/city/lookup",
                params={"location": city},
                headers=headers,
            )
            if geo_resp.status_code == 403:
                return "❌ 天气服务认证失败（403），请检查 JWT 凭据配置"
            if geo_resp.status_code == 404:
                return f"❌ 未找到城市「{city}」，请确认名称是否正确"
            if geo_resp.status_code != 200:
                return f"❌ 天气服务返回错误（HTTP {geo_resp.status_code}），请稍后重试"

            geo_data = geo_resp.json()
            if geo_data.get("code") != "200" or not geo_data.get("location"):
                return f"❌ 未找到城市「{city}」，请确认名称是否正确"
            location_id = geo_data["location"][0]["id"]
            location_name = geo_data["location"][0].get("name", city)

            # 2. 实时天气
            now_resp = await client.get(
                f"{api_host}/v7/weather/now",
                params={"location": location_id},
                headers=headers,
            )
            now_data = now_resp.json()

            # 3. 未来 3 天
            forecast_resp = await client.get(
                f"{api_host}/v7/weather/3d",
                params={"location": location_id},
                headers=headers,
            )
            forecast_data = forecast_resp.json()

    except httpx.TimeoutException:
        return f"❌ 查询 {city} 天气超时，请稍后重试"
    except httpx.RequestError as e:
        return f"❌ 查询 {city} 天气网络错误：{type(e).__name__}"
    except ValueError:
        return f"❌ 天气数据解析失败，服务可能暂时不可用"

    return _format_weather(location_name, now_data, forecast_data)


# ... _format_weather 和 get_current_time 保持不变 ...


def _format_weather(city: str, now_data: dict, forecast_data: dict) -> str:
    """把和风天气的原始 JSON 精简成结构化文本。"""
    now = now_data.get("now", {})
    if not now:
        return f"❌ 未能获取到 {city} 的天气数据"

    lines = [f"🌤 {city} 当前天气：{now.get('text', '未知')}"]
    lines.append(
        f"🌡 气温：{now.get('temp', '?')}°C（体感 {now.get('feelsLike', '?')}°C）"
    )
    lines.append(
        f"💧 湿度：{now.get('humidity', '?')}%    👁 能见度：{now.get('vis', '?')} km"
    )
    lines.append(
        f"💨 风速：{now.get('windSpeed', '?')} km/h {now.get('windDir', '')}    🎚 气压：{now.get('pressure', '?')} hPa"
    )

    # 添加未来预报
    daily = forecast_data.get("daily", [])
    if daily:
        lines.append("\n--- 未来天气预报 ---")
        for day in daily[:3]:  # 最多显示3天
            date = day.get("fxDate", "")
            text_day = day.get("textDay", "")
            text_night = day.get("textNight", "")
            temp_max = day.get("tempMax", "?")
            temp_min = day.get("tempMin", "?")
            lines.append(
                f"📅 {date}：{text_day}转{text_night}，{temp_min}~{temp_max}°C"
            )

    return "\n".join(lines)


# ============================================================
# 时间工具
# ============================================================


@tool(description="获取当前的系统时间。当用户询问现在几点、今天几号时使用。")
def get_current_time() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
