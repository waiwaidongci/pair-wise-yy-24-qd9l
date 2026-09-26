import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import DomainError, RadioDB


class RadioSchedulingFlowTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.p1 = self.db.add_program("早间新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.p2 = self.db.add_program("品牌广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_complete_replace_playout_and_reconcile_flow(self):
        first = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        second = self.db.schedule_slot("2026-09-28", "10:00", self.p2, "华东")
        self.assertEqual("planned", self.db.get_slot(first)["status"])
        replaced = self.db.replace_slot(first, self.p2)
        self.assertEqual("replaced", replaced["status"])
        self.assertEqual(self.p2, replaced["program_id"])
        self.db.record_playout(first, "09:00", 5, self.p1, "临时切回旧内容")
        self.db.record_playout(second, "10:00", 5, self.p2)
        exceptions = self.db.reconcile_date("2026-09-28")
        kinds = {(row["slot_id"], row["kind"]) for row in exceptions}
        self.assertIn((first, "wrong_program"), kinds)

    def test_rejects_overlap_and_unauthorized_region(self):
        self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.schedule_slot("2026-09-28", "09:15", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "未授权"):
            self.db.schedule_slot("2026-09-28", "11:00", self.p1, "华北")


class DraftPublishFlowTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.p1 = self.db.add_program("早间新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.p2 = self.db.add_program("品牌广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_draft_publish_creates_immutable_version(self):
        draft = self.db.create_draft("国庆特别编排")
        self.db.add_draft_slot(draft, "2026-09-28", "09:00", self.p1, "华东")
        self.assertEqual([], self.db.validate_draft(draft))
        version = self.db.publish_draft(draft, "首次发布")
        self.assertEqual(1, version["version_no"])
        live = self.db.snapshot()["slots"]
        self.assertEqual(1, len(live))
        self.assertEqual(self.p1, live[0]["program_id"])
        # 发布后草案关闭，不能再改也不能重复发布
        with self.assertRaisesRegex(DomainError, "不能再修改"):
            self.db.add_draft_slot(draft, "2026-09-28", "10:00", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "不能重复发布"):
            self.db.publish_draft(draft)
        # 再发一版：未变化的排期保留原排期 ID，实播关联不会断
        follow_up = self.db.create_draft("追加排期", copy_current=True)
        self.db.add_draft_slot(follow_up, "2026-09-28", "10:00", self.p1, "华东")
        second = self.db.publish_draft(follow_up, "追加一档")
        self.assertEqual(2, second["version_no"])
        kept = [s for s in self.db.snapshot()["slots"] if s["start_time"] == "09:00"]
        self.assertEqual(live[0]["id"], kept[0]["id"])
        # 已发布版本保持原样，不受后续发布影响
        self.assertEqual(1, len(self.db.get_version(version["id"])["slots"]))
        self.assertEqual(2, len(self.db.get_version(second["id"])["slots"]))

    def test_invalid_draft_is_rejected_without_touching_plan(self):
        draft = self.db.create_draft("越权编排")
        self.db.add_draft_slot(draft, "2026-09-28", "09:00", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.add_draft_slot(draft, "2026-09-28", "09:15", self.p1, "华东")
        self.db.add_draft_slot(draft, "2026-09-28", "11:00", self.p1, "华北")
        errors = self.db.validate_draft(draft)
        self.assertTrue(any("未授权" in e for e in errors))
        with self.assertRaisesRegex(DomainError, "未授权"):
            self.db.publish_draft(draft)
        # 校验失败不产生版本，正式计划和草案都不受影响
        self.assertEqual([], self.db.snapshot()["slots"])
        self.assertEqual([], self.db.list_versions())
        self.assertEqual("open", self.db.get_draft(draft)["status"])

    def test_draft_requires_valid_source(self):
        with self.assertRaisesRegex(DomainError, "历史版本不存在"):
            self.db.create_draft("无源草案", base_version_id=999)
        with self.assertRaisesRegex(DomainError, "草案名称不能为空"):
            self.db.create_draft("  ")

    def test_rollback_keeps_versions_and_playout_records(self):
        first = self.db.create_draft("初始编排")
        self.db.add_draft_slot(first, "2026-09-28", "09:00", self.p1, "华东")
        v1 = self.db.publish_draft(first, "初始版本")
        aired_slot = self.db.snapshot()["slots"][0]["id"]
        self.db.record_playout(aired_slot, "09:00", 5, self.p2, "临时切广告")
        # 第二版把 09:00 换成广告
        second = self.db.create_draft("改播广告", copy_current=True)
        old = self.db.get_draft(second)["slots"][0]
        self.db.remove_draft_slot(second, old["id"])
        self.db.add_draft_slot(second, "2026-09-28", "09:00", self.p2, "华东")
        v2 = self.db.publish_draft(second, "09:00 改播广告")
        # 出问题后从历史版本 v1 重建草案并发布，完成回滚
        rollback = self.db.create_draft("回滚", base_version_id=v1["id"])
        v3 = self.db.publish_draft(rollback, "回滚到 v1")
        live = self.db.snapshot()["slots"]
        self.assertEqual(1, len(live))
        self.assertEqual(self.p1, live[0]["program_id"])
        self.assertEqual(3, self.db.snapshot()["current_version"]["version_no"])
        self.assertEqual(v1["id"], v3["source_version_id"])
        # 历史版本保持原样
        self.assertEqual(self.p1, self.db.get_version(v1["id"])["slots"][0]["program_id"])
        self.assertEqual(self.p2, self.db.get_version(v2["id"])["slots"][0]["program_id"])
        # 被取代排期的实播记录仍然参与对账，并标注版本来龙去脉
        exceptions = self.db.reconcile_date("2026-09-28")
        kinds = {(row["slot_id"], row["kind"]) for row in exceptions}
        self.assertIn((aired_slot, "wrong_program"), kinds)
        self.assertIn((live[0]["id"], "missed"), kinds)
        detail = [e["detail"] for e in exceptions if e["slot_id"] == aired_slot and e["kind"] == "wrong_program"][0]
        self.assertIn("已被版本v2取代", detail)


class LegacyMigrationTest(unittest.TestCase):
    def test_legacy_slots_table_gains_version_columns(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL, start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL, program_id INTEGER NOT NULL,
              region TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
              replaced_from INTEGER, created_at TEXT NOT NULL);
            INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,created_at)
              VALUES('2026-09-28','09:00',30,1,'华东','2026-09-01T00:00:00');
            """
        )
        conn.close()
        db = RadioDB(path)
        try:
            columns = {row[1] for row in db.conn.execute("PRAGMA table_info(slots)")}
            self.assertIn("version_id", columns)
            self.assertIn("superseded_by_version_id", columns)
            row = db.conn.execute("SELECT * FROM slots").fetchone()
            self.assertEqual("2026-09-28", row["air_date"])
            self.assertIsNone(row["version_id"])
        finally:
            db.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
