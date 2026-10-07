# """图片理解：把截图转成文字描述。

# 图片理解是"预处理"，不是 Agent 能力。
# API 层先调 qwen-vl-plus 得到文字，再把文字喂给 Agent。
# """

# import logging
# import os

# from openai import AsyncOpenAI

# logger = logging.getLogger(__name__)

# VISION_TIMEOUT_S = 30.0

# # 让模型输出"可以直接给 LLM 用的文本"
# VISION_PROMPT = """请详细描述这张图片的内容，规则：
# - 如果图片是文字（文章截图、聊天记录、笔记等），完整转录文字内容，保持段落结构
# - 如果图片是数据/图表，描述关键数据和结论
# - 如果图片是照片，描述画面内容、场景、主体
# - 不要加任何"这张图片显示……"之类的开头，直接输出内容
# - 不要总结，只转录/描述，让后续的 LLM 来处理"""


# def _get_client() -> AsyncOpenAI:
#     return AsyncOpenAI(
#         api_key=os.getenv("LLM_API_KEY"),
#         base_url=os.getenv("LLM_BASE_URL"),
#         timeout=VISION_TIMEOUT_S,
#         max_retries=0,
#     )


# async def describe_image(image_b64: str) -> str:
#     """
#     把 base64 图片转成文字描述。

#     Args:
#         image_b64: 不含 data URI 前缀的纯 base64 字符串。
#                    （比如 "iVBORw0KGgo..."，不要 "data:image/png;base64,iVBORw0KGgo..."）

#     Returns:
#         模型输出的文字描述。失败时返回 "❌ ..." 前缀的错误消息。
#     """
#     if not image_b64:
#         return "❌ 图片数据为空"

#     model = os.getenv("LLM_VISION_MODEL_ID", "qwen-vl-plus")

#     try:
#         client = _get_client()
#         response = await client.chat.completions.create(
#             model=model,
#             messages=[
#                 {
#                     "role": "user",
#                     "content": [
#                         {"type": "text", "text": VISION_PROMPT},
#                         {
#                             "type": "image_url",
#                             "image_url": {"url": f"data:image/png;base64,{image_b64}"},
#                         },
#                     ],
#                 }
#             ],
#             max_tokens=8000,
#         )
#     except Exception as e:
#         logger.exception("[vision] 识别失败")
#         return f"❌ 图片识别失败：{type(e).__name__}: {e}"

#     return (response.choices[0].message.content or "").strip()


"""图片理解：把截图转成文字描述。

图片理解是"预处理"，不是 Agent 能力。
API 层先调 qwen-vl-plus 得到文字，再把文字喂给 Agent。
"""

import logging
import os

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

VISION_TIMEOUT_S = 60.0  # 长图识别慢，给足时间
VISION_MAX_TOKENS = 8000  # 从 2000 提到 8000，覆盖大多数长图


VISION_PROMPT = """请详细描述这张图片的内容，规则：
- 如果图片是文字（文章截图、聊天记录、笔记等），完整转录文字内容，保持段落结构
- 如果图片是数据/图表，描述关键数据和结论
- 如果图片是照片，描述画面内容、场景、主体
- 不要加任何"这张图片显示……"之类的开头，直接输出内容
- 不要总结，只转录/描述，让后续的 LLM 来处理
- **如果图片内容太长、超出输出上限，请在结尾明确写一行：**
  **「[内容过长，未能完整识别，请分段上传]」**
  **绝对不要为了"看起来完整"而跳过中间内容或直接总结**"""


def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=os.getenv("LLM_API_KEY"),
        base_url=os.getenv("LLM_BASE_URL"),
        timeout=VISION_TIMEOUT_S,
        max_retries=0,
    )


async def describe_image(image_b64: str) -> str:
    """
    把 base64 图片转成文字描述。

    Args:
        image_b64: 不含 data URI 前缀的纯 base64 字符串。

    Returns:
        模型输出的文字描述。失败时返回 "❌ ..." 前缀的错误消息。
    """
    if not image_b64:
        return "❌ 图片数据为空"

    model = os.getenv("LLM_VISION_MODEL_ID", "vanchin/deepseek-ocr")

    try:
        client = _get_client()
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                }
            ],
            max_tokens=VISION_MAX_TOKENS,
            # extra_body={"enable_thinking": False},
        )
    except Exception as e:
        logger.exception("[vision] 识别失败")
        return f"❌ 图片识别失败：{type(e).__name__}: {e}"

    text = (response.choices[0].message.content or "").strip()
    finish_reason = response.choices[0].finish_reason

    # ← 关键日志：以后排查全靠这一行
    logger.info(
        f"[vision] model={model} "
        f"输出长度={len(text)} 字符 "
        f"finish_reason={finish_reason}"
    )

    # finish_reason=length 说明被 max_tokens 截断了
    if finish_reason == "length":
        text += (
            "\n\n[系统提示：识别达到输出上限，内容可能不完整。"
            "建议将长图拆成 2-3 张分段上传。]"
        )

    return text
