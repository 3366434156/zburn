# -*- coding: utf-8 -*-
"""多 agent / 监督交接 / ZCode 会话库的核心纯函数测试（不碰桌面应用）。"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agents  # noqa: E402
import zsession  # noqa: E402


def make_state(root: pathlib.Path):
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(
        dir=state,
        log_path=state / "zburn.log",
        status_path=state / "status.json",
    )


BASE = {
    "app": {"process_name": "ZCode", "exe": "z.exe", "start_args": [], "strip_env": []},
    "uia": {"send_button_regex": "Send", "busy_regex": "Stop", "queued_regex": "Queue",
            "session_regex": "", "window_title_regex": "", "max_depth": 40,
            "char_delay_ms": 20, "session_db": "", "session_work_dir": ""},
    "prompt": {"templates": ["Continue."], "file": "next-prompt.txt"},
    "feed": {"max_sessions": 5, "interval_seconds": 120, "targets": []},
    "paths": {"state_dir": ".zburn", "stop_file": "STOP", "bridge_dir": ""},
    "orchestrator": {"enabled": True, "supervisor": "", "worker": ""},
    "new_chat": {"enabled": True, "button_regex": "New chat", "project": "", "send_button_regex": ""},
}


class AgentConfigTests(unittest.TestCase):
    def test_legacy_single_app(self):
        specs = agents.agent_specs(dict(BASE))
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["id"], "default")
        self.assertEqual(specs[0]["role"], "worker")
        self.assertEqual(specs[0]["app"]["process_name"], "ZCode")

    def test_multi_agent_roles_and_defaults(self):
        cfg = json.loads(json.dumps(BASE))
        cfg["agents"] = [
            {"id": "zcode", "name": "ZCode", "role": "worker"},
            {"id": "dsh", "name": "dsh", "role": "supervisor", "supervises": "zcode",
             "app": {"process_name": "DeepSeek Harness"}},
        ]
        specs = agents.agent_specs(cfg)
        self.assertEqual([s["id"] for s in specs], ["zcode", "dsh"])
        self.assertEqual(specs[1]["feed"]["interval_seconds"], 600)  # supervisor 默认 10 分钟
        self.assertEqual(specs[1]["prompt"]["file"], "supervisor-prompt.txt")
        sup, work = agents.orchestrator(cfg)
        self.assertEqual(sup["id"], "dsh")
        self.assertEqual(work["id"], "zcode")

    def test_non_default_agent_does_not_inherit_worker_filters(self):
        cfg = json.loads(json.dumps(BASE))
        cfg["feed"]["targets"] = ["zcode only"]
        cfg["uia"]["session_regex"] = "zcode"
        cfg["uia"]["window_title_regex"] = "zcode"
        cfg["agents"] = [
            {"id": "zcode", "role": "worker"},
            {"id": "dsh", "role": "supervisor"},
        ]
        zcode, dsh = agents.agent_specs(cfg)
        self.assertEqual(zcode["feed"]["targets"], ["zcode only"])
        self.assertEqual(dsh["feed"]["targets"], [])
        self.assertEqual(dsh["uia"]["session_regex"], "")
        self.assertEqual(dsh["uia"]["window_title_regex"], "")

    def test_supervisor_first_does_not_steal_worker_defaults(self):
        cfg = json.loads(json.dumps(BASE))
        cfg["feed"]["targets"] = ["worker only"]
        cfg["agents"] = [
            {"id": "dsh", "role": "supervisor"},
            {"id": "zcode", "role": "worker"},
        ]
        dsh, zcode = agents.agent_specs(cfg)
        self.assertEqual(dsh["feed"]["targets"], [])
        self.assertEqual(dsh["prompt"]["file"], "supervisor-prompt.txt")
        self.assertEqual(zcode["feed"]["targets"], ["worker only"])
        self.assertEqual(zcode["prompt"]["file"], "next-prompt.txt")

    def test_new_chat_inherits_and_overrides(self):
        cfg = json.loads(json.dumps(BASE))
        cfg["new_chat"]["project"] = "top"
        cfg["agents"] = [
            {"id": "zcode", "role": "worker"},
            {"id": "dsh", "role": "supervisor", "new_chat": {"project": "own"}},
        ]
        zcode, dsh = agents.agent_specs(cfg)
        self.assertEqual(zcode["new_chat"]["project"], "top")
        self.assertEqual(dsh["new_chat"]["project"], "own")

    def test_dynamic_targets_merge(self):
        cfg = json.loads(json.dumps(BASE))
        cfg["agents"] = [{"id": "zcode", "role": "worker"}]
        with tempfile.TemporaryDirectory() as td:
            st = make_state(pathlib.Path(td))
            spec = agents.agent_specs(cfg)[0]
            self.assertEqual(agents.agent_view(cfg, spec, st)["feed"]["targets"], [])
            agents.add_dynamic_target(st, "zcode", "新对话")
            self.assertEqual(agents.agent_view(cfg, spec, st)["feed"]["targets"], ["新对话"])
            agents.clear_dynamic_targets(st, "zcode")
            self.assertEqual(agents.agent_view(cfg, spec, st)["feed"]["targets"], [])


class HandoffTests(unittest.TestCase):
    def test_pending_roundtrip_and_ascii(self):
        with tempfile.TemporaryDirectory() as td:
            st = make_state(pathlib.Path(td))
            rec = agents.set_pending(st, "zcode", "new_chat", "Do it.\nNow", project="p")
            self.assertEqual(rec["action"], "new_chat")
            self.assertEqual(rec["prompt"], "Do it. Now")
            self.assertEqual(agents.get_pending(st, "zcode")["project"], "p")
            agents.clear_pending(st, "zcode")
            self.assertIsNone(agents.get_pending(st, "zcode"))

    def test_write_read_consume(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = json.loads(json.dumps(BASE))
            cfg["paths"]["bridge_dir"] = str(root / "bridge")
            st = make_state(root)
            rec = agents.write_handoff(cfg, st, "feed", "Continue the task.", agent="zcode")
            self.assertEqual(rec["action"], "feed")
            got, src = agents.read_handoff(cfg, st, "zcode")
            self.assertEqual(got["prompt"], "Continue the task.")
            self.assertTrue(src.endswith("handoff.json"))
            consumed = agents.consume_handoff(cfg, st, "zcode")
            self.assertEqual(consumed["action"], "feed")
            self.assertFalse((root / "bridge" / "handoff.json").exists())
            self.assertTrue((root / "bridge" / "handoff-ack.txt").exists())
            archives = list((root / "state" / "handoffs").glob("*.json"))
            self.assertEqual(len(archives), 1)
            self.assertIsNone(agents.consume_handoff(cfg, st, "zcode"))

    def test_handoff_txt_fallback_and_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = json.loads(json.dumps(BASE))
            cfg["paths"]["bridge_dir"] = str(root / "bridge")
            st = make_state(root)
            bridge = root / "bridge"
            bridge.mkdir()
            (bridge / "handoff.txt").write_text("Plain text handoff.", encoding="utf-8")
            rec = agents.consume_handoff(cfg, st, "zcode")
            self.assertEqual(rec["action"], "feed")
            self.assertEqual(rec["prompt"], "Plain text handoff.")
            with self.assertRaises(ValueError):
                agents.write_handoff(cfg, st, "feed", "中文")
            with self.assertRaises(ValueError):
                agents.write_handoff(cfg, st, "feed", "中文 with ASCII")


class ZSessionTests(unittest.TestCase):
    def _db(self, path: pathlib.Path):
        con = sqlite3.connect(path)
        con.execute("create table session(id text,title text,directory text,"
                    "time_updated int,last_outcome text,time_deleted int)")
        con.execute("create table message(id text,session_id text,data text,sequence int)")
        con.execute("create table part(id text,message_id text,data text,sequence int)")
        con.execute("insert into session values(?,?,?,?,?,null)",
                    ("s1", "任务A", r"C:\work", 2000, "completed"))
        con.execute("insert into session values(?,?,?,?,?,null)",
                    ("s2", "别的任务", r"C:\other", 1000, "completed"))
        msgs = [
            ("m1", "s1", json.dumps({"role": "user"}), 1),
            ("m2", "s1", json.dumps({"role": "assistant"}), 2),
        ]
        for m in msgs:
            con.execute("insert into message values(?,?,?,?)", m)
        con.execute("insert into part values(?,?,?,?)",
                    ("p1", "m1", json.dumps({"type": "text", "text": "Do the thing"}), 1))
        con.execute("insert into part values(?,?,?,?)",
                    ("p2", "m2", json.dumps({"type": "text", "text": "Done"}), 1))
        con.execute("insert into part values(?,?,?,?)",
                    ("p3", "m2", json.dumps({"type": "tool", "tool": "Bash",
                                             "state": {"status": "completed",
                                                       "input": {"command": "pytest"}}}), 2))
        con.commit()
        con.row_factory = sqlite3.Row
        return con

    def test_find_and_transcript(self):
        with tempfile.TemporaryDirectory() as td:
            con = self._db(pathlib.Path(td) / "db.sqlite")
            s = zsession.find_session(con, targets=["任务A"], work_dir="")
            self.assertEqual(s["id"], "s1")
            self.assertEqual(zsession.find_session(con, targets=["任务A#1"], work_dir="")["id"], "s1")
            self.assertEqual(zsession.find_session(con, targets=[], work_dir=r"C:\other")["id"], "s2")
            self.assertIsNone(zsession.find_session(con, targets=["不存在的会话"],
                                                    work_dir=r"C:\other"))
            text = zsession.transcript(con, s)
            self.assertIn("Do the thing", text)
            self.assertIn("Done", text)
            self.assertIn("Bash", text)
            self.assertIn("## task", text)
            con.close()


if __name__ == "__main__":
    unittest.main()
