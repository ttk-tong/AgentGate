"""weather：只读天气查询工具桩（plan/04 §9）。

用户询问某地天气时返回对应结果。阶段 2 用固定桩数据，未接入真实气象 API，
未命中的城市返回一个确定性的兜底结果（按城市名做稳定哈希，避免用随机源）。
只读 + 并发安全 → 可与其他只读工具并行成批。
"""
from __future__ import annotations

from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.tools.base import BaseTool

# 固定桩数据：命中即返回，未命中走确定性兜底
_STUB_WEATHER = {
    "beijing": {"condition": "晴", "temp_c": 28, "humidity": 40},
    "北京": {"condition": "晴", "temp_c": 28, "humidity": 40},
    "shanghai": {"condition": "多云", "temp_c": 31, "humidity": 65},
    "上海": {"condition": "多云", "temp_c": 31, "humidity": 65},
    "shenzhen": {"condition": "雷阵雨", "temp_c": 33, "humidity": 80},
    "深圳": {"condition": "雷阵雨", "temp_c": 33, "humidity": 80},
    "hangzhou": {"condition": "阴", "temp_c": 30, "humidity": 70},
    "杭州": {"condition": "阴", "temp_c": 30, "humidity": 70},
}

_FALLBACK_CONDITIONS = ["晴", "多云", "阴", "小雨"]


def _fallback(city: str) -> dict:
    """未命中城市的确定性兜底：按城市名哈希，保证同名同结果（不用随机源）。"""
    h = sum(ord(c) for c in city)
    return {
        "condition": _FALLBACK_CONDITIONS[h % len(_FALLBACK_CONDITIONS)],
        "temp_c": 18 + h % 15,
        "humidity": 40 + h % 50,
    }


class WeatherTool(BaseTool):
    spec = ToolSpec(
        name="weather",
        description="查询指定城市当前的天气状况（温度、天气现象、湿度）。",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名，如 北京、上海"},
            },
            "required": ["city"],
        },
        is_read_only=True,
        is_concurrency_safe=True,
        idempotent=True,
    )

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        city = str(args.get("city", "")).strip()
        data = _STUB_WEATHER.get(city) or _STUB_WEATHER.get(city.lower())
        matched = data is not None
        if data is None:
            data = _fallback(city)
        return ToolResult(
            ok=True,
            content={
                "city": city,
                "condition": data["condition"],
                "temp_c": data["temp_c"],
                "humidity": data["humidity"],
            },
            display=f"{city}：{data['condition']}，{data['temp_c']}°C，湿度 {data['humidity']}%",
            meta={"matched": matched},
        )
