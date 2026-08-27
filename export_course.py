"""把本機測試介面裡「進行中的課程」完整匯出成 Markdown 檔（D4 驗收用）。

為什麼需要它：本機測試模式的資料庫在記憶體（未設 GCP_PROJECT），
終端機一關課綱就消失。這個腳本從外部呼叫正在跑的 /dev/message API，
把課綱、每堂內容、檢核題與答案全部抓下來存檔 —— 檔案落地後就不怕關機。

用法（測試伺服器跑著的時候）：
  python export_course.py            # 匯出到 d4_reports/課程_<時間戳>.md
  python export_course.py --status   # 只看生成進度，不匯出

注意：匯出過程會替每一堂送出「完成」以取得檢核題，課程進度會被走完。
本機記憶體模式下資料本來就不會留存，這不造成損失；接了 Firestore 之後
請改用資料庫直接查詢，不要對想保留進度的課程跑本腳本。
"""
from __future__ import annotations

import datetime
import pathlib
import re
import sys

import httpx

BASE = "http://127.0.0.1:8787"
OUT_DIR = pathlib.Path(__file__).parent / "d4_reports"


def send(text: str) -> list[dict]:
    r = httpx.post(f"{BASE}/dev/message", json={"text": text}, timeout=60)
    r.raise_for_status()
    return r.json()["messages"]


def texts_of(messages: list[dict]) -> list[str]:
    """把 text 與 flex 訊息都攤平成文字。flex 依序取出所有 text 欄位。"""
    out = []
    for m in messages:
        if m.get("type") == "flex":
            chunks: list[str] = []

            def walk(node):
                if isinstance(node, dict):
                    if node.get("type") == "text" and node.get("text"):
                        chunks.append(str(node["text"]))
                    if node.get("type") == "uri" and node.get("uri"):   # 觀看影片按鈕的連結
                        chunks.append(f"📺 {node['uri']}")
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)

            walk(m.get("contents", {}))
            out.append("\n".join(chunks))
        elif m.get("text"):
            out.append(m["text"])
    return out


def main() -> None:
    try:
        httpx.get(f"{BASE}/healthz", timeout=5).raise_for_status()
    except Exception:
        sys.exit("❌ 測試伺服器沒有在跑（先執行 啟動測試.command）")

    status = "\n".join(texts_of(send("我的課程")))
    print(status, "\n")
    if "--status" in sys.argv:
        return
    if "生成中" in status or "排隊中" in status:
        sys.exit("⏳ 課程還在生成，先用 --status 盯進度，完成後再匯出。")

    listing = "\n".join(texts_of(send("課程列表")))
    if "還沒有進行中的課程" in listing:
        sys.exit("❌ 沒有可匯出的課程。")
    orders = [int(n) for n in re.findall(r"第 (\d+) 堂", listing)]
    total = max(orders) if orders else 0
    if not total:
        sys.exit(f"❌ 解析不到堂數：\n{listing}")

    topic_m = re.search(r"📚 (.+?) 課程列表", listing)
    topic = topic_m.group(1) if topic_m else "課程"
    lines = [f"# 課程匯出：{topic}", "",
             f"> 匯出時間：{datetime.datetime.now():%Y-%m-%d %H:%M}", "",
             "## 課程列表", "", listing]

    for i in range(1, total + 1):
        lines += ["", "---", "", f"## 第 {i} 堂", ""]
        lines += texts_of(send(f"第 {i} 堂"))

    lines += ["", "---", "", "## 檢核題與答案", ""]
    for i in range(1, total + 1):
        lines += ["", f"### 第 {i} 堂"]
        lines += texts_of(send("完成"))     # 標記完成 → 吐出該堂檢核題
        lines += ["", "**答案**"]
        lines += texts_of(send("答案"))

    OUT_DIR.mkdir(exist_ok=True)
    safe_topic = re.sub(r"[^\w一-鿿-]", "_", topic)[:30]
    path = OUT_DIR / f"{safe_topic}_{datetime.datetime.now():%m%d_%H%M}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 已匯出 {total} 堂 → {path}")


if __name__ == "__main__":
    main()
