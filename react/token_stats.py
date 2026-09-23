"""会话级 token 统计：记录、持久化、汇总。

- 记录：ReActLoop._step 每次模型调用后追加一条调用明细（阶段/用量/耗时/时间）。
- 持久化：每会话一个 JSON 文件（data/token_stats/<sid>.json），任务结束后写入，
  服务重启后历史会话的统计仍可查。
- 汇总：从调用明细聚合（总 token、缓存命中 token、命中率）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path


def stats_dir(base_dir: Path) -> Path:
    return base_dir / "data" / "token_stats"


def save_session_stats(base_dir: Path, sid: str, meta: dict, calls: list[dict]) -> None:
    """把会话统计原子写入磁盘（先写临时文件再 rename，避免半截文件）。"""
    d = stats_dir(base_dir)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": sid,
        "task": meta.get("task", ""),
        "status": meta.get("status", ""),
        "rounds": meta.get("rounds"),
        "created": meta.get("created", ""),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "calls": calls,
    }
    tmp = d / f".{sid}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(d / f"{sid}.json")


def load_session_stats(base_dir: Path, sid: str) -> dict | None:
    f = stats_dir(base_dir) / f"{sid}.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def summarize(calls: list[dict]) -> dict:
    """聚合调用明细 → 汇总指标。usage 缺失时按 tokens 字段兜底。"""
    prompt = completion = total = cached = 0
    for c in calls:
        u = c.get("usage") or {}
        prompt += u.get("prompt", 0) or 0
        completion += u.get("completion", 0) or 0
        total += u.get("total", 0) or 0
        cached += u.get("cached", 0) or 0
    return {
        "calls": len(calls),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached,
        "cache_hit_rate": round(cached / prompt, 4) if prompt else 0,
    }


def list_session_stats(base_dir: Path) -> list[dict]:
    """扫描磁盘，返回所有已持久化会话的统计（含汇总）。"""
    out: list[dict] = []
    d = stats_dir(base_dir)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["summary"] = summarize(data.get("calls", []))
        out.append(data)
    return out
