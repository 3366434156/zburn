# -*- coding: utf-8 -*-
"""夜间总控：把一份队列切成 N 片，各喂一个对话，谁也不许停。

它自己不投喂、不验收 —— 只做三件事：
1. **分片**：round-robin 切成 N 份，每份配一个专用对话（feed.targets），互不干扰；
2. **看住**：每 60 秒检查一次，子进程死了就重拉（跳过已做完的），全部做完才退出；
3. **保命**：应用没了拉起、无障碍树空了带开关重启、网关掉了探活拉起；看到 STOP 文件就整体收工。

```powershell
python zburn.py autopilot --queue …\\queue_fx.json `
    --target "<对话id1>" --target "<对话id2>" `
    --state-dir .zburn/night
```
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent


def split_queue(queue_path: str, n: int, out_dir: pathlib.Path) -> list[tuple[str, list[dict]]]:
    q = json.loads(pathlib.Path(queue_path).read_text(encoding="utf-8-sig"))
    tasks = q.get("tasks") or []
    out_dir.mkdir(parents=True, exist_ok=True)
    shards: list[tuple[str, list[dict]]] = []
    for i in range(n):
        part = tasks[i::n]
        p = out_dir / f"queue_shard{i}.json"
        p.write_text(json.dumps({**{k: v for k, v in q.items() if k != "tasks"}, "tasks": part},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        shards.append((str(p), part))
    return shards


def _pending(queue_file: str, state_file: pathlib.Path) -> int:
    try:
        tasks = json.loads(pathlib.Path(queue_file).read_text(encoding="utf-8-sig")).get("tasks") or []
    except Exception:
        return 0
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    return sum(1 for t in tasks if (state.get(t["id"]) or {}).get("status") != "done")


def run(cfg: dict, st, queue_path: str, targets: list[str], *, shards: int = 0,
        max_per_shard: int = 0, config_path: str | None = None,
        poll: int = 60, max_hours: float = 12.0) -> int:
    import zburn

    if not targets:
        print("必须用 --target 指定至少一个专用对话（贴面板「会话监控」里的 id）")
        return 3
    n = shards or len(targets)
    n = min(n, len(targets))
    base = st.dir
    parts = split_queue(queue_path, n, base / "queues")
    log_path = base / "autopilot.log"

    def log(msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        st.log(f"[autopilot] {msg}")

    procs: list[subprocess.Popen | None] = [None] * n
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    def spawn(i: int) -> subprocess.Popen:
        qf, _ = parts[i]
        sdir = base / f"shard{i}"
        sdir.mkdir(parents=True, exist_ok=True)
        # 每个分片一份自己的配置：监控名单只放自己那个对话。
        # 对话 id 里带引号（"…"），走命令行会被吃掉，落成 JSON 最稳。
        shard_cfg = json.loads(json.dumps(cfg))
        shard_cfg.setdefault("feed", {})["targets"] = [targets[i % len(targets)]]
        shard_cfg.setdefault("paths", {})["state_dir"] = str(sdir)
        cfg_file = sdir / "config.json"
        cfg_file.write_text(json.dumps(shard_cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        args = [sys.executable, str(HERE / "zburn.py"), "--config", str(cfg_file),
                "batch", "--queue", qf]
        if max_per_shard:
            args += ["--max", str(max_per_shard)]
        out = open(sdir / "batch.out", "a", encoding="utf-8")
        startup = 0
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
        p = subprocess.Popen(args, stdout=out, stderr=out, creationflags=flags,
                             startupinfo=startup or None,
                             env={**os.environ, "ZBURN_SKIP_SCHED_LOCK": "1"})
        log(f"分片 {i} 起 pid={p.pid}（对话 …{targets[i % len(targets)][-18:]}，队列 {pathlib.Path(qf).name}）")
        return p

    log(f"夜间总控启动：{n} 片，队列 {queue_path}")
    for i in range(n):
        procs[i] = spawn(i)

    t0 = time.time()
    uia_dead_streak = 0
    while True:
        time.sleep(poll)
        if st.stop_file.exists():
            log("发现 STOP 文件 → 结束所有分片")
            for p in procs:
                if p and p.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                                   capture_output=True, creationflags=flags)
            return 0
        if (time.time() - t0) / 3600 > max_hours:
            log(f"到达 {max_hours} 小时上限 → 收工（明天接着跑就行）")
            return 0

        # 网关 / 应用 / 无障碍树
        try:
            zburn.ensure_gateway(cfg, st)
        except Exception as e:                    # noqa: BLE001
            log(f"网关检查异常：{e}")
        if not zburn.app_running(cfg):
            log("应用不在了 → 拉起")
            zburn.start_app(cfg)
            time.sleep(20)
        else:
            ok, why = zburn.uia_ok(cfg)
            if not ok:
                uia_dead_streak += 1
                log(f"无障碍树不可用（{why}）第 {uia_dead_streak} 次")
                if uia_dead_streak >= 3:          # 连续三次才动，避免误判打断正在生成的对话
                    log("连续 3 次不可用 → 带 a11y 开关重启应用")
                    subprocess.run(["taskkill", "/IM", f"{cfg['app']['process_name']}.exe", "/F"],
                                   capture_output=True, creationflags=flags)
                    time.sleep(5)
                    zburn.start_app(cfg)
                    time.sleep(25)
                    uia_dead_streak = 0
            else:
                uia_dead_streak = 0

        alive = 0
        for i in range(n):
            p = procs[i]
            if p and p.poll() is None:
                alive += 1
                continue
            left = _pending(parts[i][0], base / f"shard{i}" / "batch-state.json")
            if left:
                log(f"分片 {i} 已退出但还剩 {left} 条 → 重拉")
                procs[i] = spawn(i)
            else:
                if not procs[i]:                  # 已完成的分片标记一次
                    procs[i] = None
                    log(f"分片 {i} 全部完成")
        if alive == 0 and all(_pending(parts[i][0], base / f"shard{i}" / "batch-state.json") == 0
                              for i in range(n)):
            log("所有分片都已完成 → 总控退出")
            return 0
