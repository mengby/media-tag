"""Fixed visual watermark question and conservative interpretation of its answers."""
from hashlib import sha256
import re

PROMPT = ("只看这张图，检查边角和中央的素材水印、平台角标、账号或叠加的标志。"
          "字幕、课件标题和商品包装文字不算。只输出实际看见的一行："
          "有|位置|可见字样或图案，或疑似|位置|可见字样或图案，或未见，或不确定。")
PROMPT_VERSION = "watermark-v1-" + sha256(PROMPT.encode()).hexdigest()[:16]


def interpret(answer):
    text = answer.strip().rstrip("。.")
    if text == "未见":
        return {"state":"no_hit_in_sample"}
    parts = re.split(r"[|｜]", text)
    if (len(parts) == 3 and parts[0].strip() in ("有", "疑似") and len(text) <= 200
            and all(v.strip() for v in parts[1:]) and not any(x in text for x in ("位置|", "可见字样或图案", "或未见", "\n"))):
        return {"state":"candidate_review","position":parts[1].strip(),"description":parts[2].strip()}
    return {"state":"uncertain"}
