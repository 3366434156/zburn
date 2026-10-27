# -*- coding: utf-8 -*-
"""批量任务驱动：把「一份队列」变成「熄屏下一个个做完」。

和 feed（无脑续命）的区别：
* 队列里每个任务自带**指令文本**和**验收命令**，做完必须验收通过才算数；
* 验收 ＝ 目标文件真的被改过（mtime 前进）**且**验收命令 exit 0；
* 做不完就记失败原因、下一条继续，绝不在一棵树上吊死；
* 状态落在 <state_dir>/batch-state.json，随时中断、随时接着跑。

队列格式（JSON）：
{
  "cwd": "E:/…/fxlib",                       # 验收命令的工作目录（可选）
  "poll_seconds": 20, "timeout_seconds": 900, # 单任务等待节奏（可选）
  "tasks": [
    {"id": "Fx211_WoodenBird", "group": "rework_a",
     "prompt": "TASK 1/240 …",                 # 必须是单行 ASCII（WM_CHAR 限制）
     "verify": "python lint_check.py Fx211_WoodenBird",
     "watch": "effects/Fx211_WoodenBird.frag"} # 相对 cwd
  ]
}
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time


def load_queue(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        raise SystemExit(f"队列文件不存在：{p}")
    q = json.loads(p.read_text(encoding="utf-8-sig"))
    tasks = q.get("tasks") or []
    if not tasks:
        raise SystemExit("队列里没有 tasks")
    for t in tasks:
        for k in ("id", "prompt", "verify"):
            if not t.get(k):
                raise SystemExit(f"任务缺字段 {k}：{t}")
    return q


def _state_path(state_dir: pathlib.Path) -> pathlib.Path:
    return state_dir / "batch-state.json"


def load_state(state_dir: pathlib.Path) -> dict:
    p = _state_path(state_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state_dir: pathlib.Path, state: dict) -> None:
    p = _state_path(state_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _run(cmd: str, cwd: str | None, timeout: int = 900) -> tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                           errors="replace", timeout=timeout, creationflags=flags)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, f"超时 {timeout}s"
    except Exception as e:                     # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _mtime(path: pathlib.Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def run_queue(cfg: dict, st, queue_path: str, *, max_tasks: int = 0,
              only: str = "", dry_run: bool = False, retries: int = 1) -> int:
    import zburn

    q = load_queue(queue_path)
    cwd = q.get("cwd") or None
    poll = int(q.get("poll_seconds") or 20)
    wait = int(q.get("timeout_seconds") or cfg.get("batch", {}).get("timeout_seconds") or 900)
    tasks = q["tasks"]
    if only:
        tasks = [t for t in tasks if only in t["id"] or only == t.get("group")]
        if not tasks:
            raise SystemExit(f"没有匹配 {only} 的任务")

    state = load_state(st.dir)
    if not dry_run and not (zburn.feed_targets(cfg) or (cfg["uia"].get("session_regex") or "").strip()):
        print(zburn.NO_SELECTION_MSG)
        return 3

    done = skipped = failed = 0
    limit = max_tasks or len(tasks)
    for t in tasks:
        if done + failed >= limit:
            break
        tid = t["id"]
        rec = state.get(tid) or {}
        if rec.get("status") == "done":
            skipped += 1
            continue
        if int(rec.get("tries") or 0) > retries:
            skipped += 1
            continue

        watch = (pathlib.Path(cwd or ".") / t["watch"]) if t.get("watch") else None
        before = _mtime(watch) if watch else 0.0

        if dry_run:
            print(f"[dry-run] {tid}  verify: {t['verify']}")
            done += 1
            if done >= limit:
                break
            continue

        prompt = t["prompt"]
        if rec.get("note"):                     # 上一轮失败原因回灌给模型
            prompt += f" Previous attempt failed: {rec['note'][:160]}"
        st.log(f"[batch] 投喂 {tid}：{prompt[:90]}…")
        code, summary = zburn.feed_once(cfg, st, prompt)
        if code != 0:
            st.log(f"[batch] {tid} 投喂失败 exit={code} {summary}")
            print(f"[x] {tid} 投喂失败：{summary}")
            state[tid] = {**rec, "status": "feed-failed", "note": str(summary)[:200],
                          "tries": int(rec.get("tries") or 0) + 1,
                          "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_state(st.dir, state)
            failed += 1
            continue

        t0 = time.time()
        verdict, note = "timeout", "等待超时"
        while time.time() - t0 < wait:
            time.sleep(poll)
            if watch and _mtime(watch) <= before:
                continue                        # 文件还没被动过，别急着验收
            vcode, vout = _run(t["verify"], cwd, timeout=min(600, wait))
            if vcode == 0:
                verdict, note = "done", "验收通过"
                break
            note = (vout or f"exit {vcode}").splitlines()[-1][:200]
        state[tid] = {"status": verdict, "note": note,
                      "tries": int(rec.get("tries") or 0) + (0 if verdict == "done" else 1),
                      "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                      "seconds": int(time.time() - t0)}
        save_state(st.dir, state)
        st.log(f"[batch] {tid} → {verdict}（{note[:120]}，{state[tid]['seconds']}s）")
        print(f"[{'OK' if verdict == 'done' else 'x '}] {tid} {verdict} {note[:100]}")
        done += verdict == "done"
        failed += verdict != "done"

    print(f"\n完成 {done}，失败 {failed}，跳过（已做过）{skipped}；状态：{_state_path(st.dir)}")
    return 0 if failed == 0 else 1
