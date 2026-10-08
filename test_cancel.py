"""测试中断功能：发一条长回复请求，2 秒后取消。"""
import asyncio
import json
import httpx


SESSION_ID = "cancel-test-1"
BASE = "http://localhost:8000"


async def main():
    async with httpx.AsyncClient(timeout=60.0) as client:
        # ─── 1. 发起流式请求 ───
        print(f"[1] 发起请求 session={SESSION_ID}")
        collected = []

        async def stream():
            try:
                async with client.stream(
                    "POST",
                    f"{BASE}/chat/stream",
                    json={
                        "session_id": SESSION_ID,
                        "message": "详细讲讲 Python 的 GIL，至少 2000 字",
                    },
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        try:
                            evt = json.loads(line[6:])
                        except Exception:
                            continue
                        if evt.get("kind") == "content":
                            collected.append(evt["text"])
                            print(f"    收到 {len(''.join(collected))} 字...", end="\r")
                        elif evt.get("kind") == "interrupted":
                            print(f"\n    [中断事件] partial={len(evt.get('partial', ''))} 字")
                        elif evt.get("kind") == "done":
                            print(f"\n    [完成]")
                        elif evt.get("kind") == "error":
                            print(f"\n    [错误] {evt.get('error')}")
            except httpx.RemoteProtocolError:
                print("\n    [连接已关闭]")
                  

        # ─── 2. 2 秒后取消 ───
        async def cancel_after(delay: float):
            await asyncio.sleep(delay)
            print(f"\n[2] 发送取消请求")
            r = await client.post(
                f"{BASE}/chat/cancel",
                json={"session_id": SESSION_ID},
            )
            print(f"    取消结果: {r.json()}")

        await asyncio.gather(
            stream(),
            cancel_after(50.0),
        )

        print(f"\n[3] 总共收到 {len(''.join(collected))} 字")


if __name__ == "__main__":
    asyncio.run(main())