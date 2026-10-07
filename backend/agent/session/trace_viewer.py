"""Trace 回放工具。

用法：
    python backend/agent/trace_viewer.py list  <session_id>
    python backend/agent/trace_viewer.py show  <session_id> <turn_id>
"""

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path


def _wall_ms(data: dict) -> float:
    """端到端墙钟耗时。

    优先用 started_at / finished_at 现算：旧 trace 文件里存的 total_duration_ms
    是 step 求和的结果，嵌套时长被重复计入，不能直接当墙钟用。
    """
    try:
        started = datetime.fromisoformat(data["started_at"])
        finished = datetime.fromisoformat(data["finished_at"])
    except (KeyError, TypeError, ValueError):
        return data["total_duration_ms"]
    return (finished - started).total_seconds() * 1000


# 旧 trace 没有 parent 字段，只能用时间区间包含关系近似推导。started_at 是 wall clock
# （Windows 上约 1ms 粒度），与 perf_counter 算出的 duration 混算区间会有毫秒级误差，
# 所以区间尾部留一点容忍，避免把真正嵌套的 step 判成顶层。
_NEST_END_TOLERANCE_MS = 5.0


def _nested_flags(steps: list[dict]) -> list[bool]:
    """每个 step 是否嵌套在别的 step 里。

    新 trace 看 parent 字段；旧 trace 没有该字段，用时间区间包含关系推导。
    推导只是近似：与长步骤同秒发生的 ~0ms 步骤可能被判成嵌套，
    影响限于旧 trace 的展示（accounting 误差 < 1ms），新 trace 不受影响。
    """
    if all("parent" in s for s in steps):
        return [s["parent"] is not None for s in steps]

    spans = []
    for s in steps:
        start = datetime.fromisoformat(s["started_at"])
        spans.append(
            (
                start,
                start + timedelta(milliseconds=s["duration_ms"]),
                s["duration_ms"],
            )
        )
    tol = timedelta(milliseconds=_NEST_END_TOLERANCE_MS)
    return [
        any(
            j != i
            and spans[j][2] > spans[i][2]  # 父步骤必然更长，避免近似相等时互相包含
            and spans[j][0] <= spans[i][0]
            and spans[i][1] <= spans[j][1] + tol
            for j in range(len(steps))
        )
        for i in range(len(steps))
    ]


def _self_ms(steps: list[dict], nested: list[bool]) -> float:
    """顶层 step 耗时之和（嵌套子 step 不重复计入）。"""
    return sum(s["duration_ms"] for s, n in zip(steps, nested) if not n)


def cmd_list(args) -> None:
    session_dir = Path(args.traces_dir) / args.session_id
    if not session_dir.exists():
        print(f"没有找到 session: {args.session_id}")
        return

    files = sorted(session_dir.glob("*.json"))
    if not files:
        print("该 session 没有 trace")
        return

    print(f"Session: {args.session_id}（共 {len(files)} 条 turn）")
    print("  列：真实耗时（端到端墙钟）/ 步骤合计（顶层 step 之和，差额为未打点时间）\n")
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        steps = data["steps"]
        nested = _nested_flags(steps)
        print(
            f"  {data['turn_id']}  {data['started_at'][:19]}  "
            f"{_wall_ms(data):>8.0f}ms  {_self_ms(steps, nested):>8.0f}ms  "
            f"{data['total_tokens']:>5}tok  | {data['user_input'][:30]}"
        )


def cmd_show(args) -> None:
    file_path = Path(args.traces_dir) / args.session_id / f"{args.turn_id}.json"
    if not file_path.exists():
        print(f"没有找到 trace: {file_path}")
        return

    data = json.loads(file_path.read_text(encoding="utf-8"))
    steps = data["steps"]
    nested = _nested_flags(steps)
    wall = _wall_ms(data)
    self_time = _self_ms(steps, nested)

    print(f"═══ Turn {data['turn_id']} ═══")
    print(f"Session     : {data['session_id']}")
    print(f"用户输入    : {data['user_input']}")
    print(f"开始 / 结束 : {data['started_at'][:19]}  →  {data['finished_at'][:19]}")
    print(f"真实耗时    : {wall:.0f}ms")
    print(
        f"步骤合计    : {self_time:.0f}ms"
        f"（顶层 {len(steps) - sum(nested)} 步；未打点 {wall - self_time:.0f}ms）"
    )
    print(f"总 tokens   : {data['total_tokens']}")
    print()

    print("─── Steps ───")
    for i, s in enumerate(steps, 1):
        status = "✗" if s.get("error") else "✓"
        pad = "    " if nested[i - 1] else "  "
        print(
            f"{pad}[{i:>2}] {status} {s['type']:<10} | {s['name']:<32} | "
            f"{s['duration_ms']:>7.0f}ms"
        )

        meta = s.get("metadata") or {}
        shown = {
            k: v
            for k, v in meta.items()
            if k
            in ("total_tokens", "prompt_tokens", "completion_tokens", "message_count")
        }
        if shown:
            print(f"{pad}      {' '.join(f'{k}={v}' for k, v in shown.items())}")

        if s.get("error"):
            print(f"{pad}      ERROR: {s['error']}")

    print()
    print("─── 最终输出 ───")
    print(data.get("final_output") or "(空)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trace 查看器")
    parser.add_argument("--traces-dir", default="traces", help="trace 根目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出某 session 的所有 turn")
    p_list.add_argument("session_id")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="显示某个 turn 的详情")
    p_show.add_argument("session_id")
    p_show.add_argument("turn_id")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
