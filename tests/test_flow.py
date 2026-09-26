import os
import sys
import tempfile
import unittest
from datetime import date
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
        self.db.create_draft()
        first = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        second = self.db.schedule_slot("2026-09-28", "10:00", self.p2, "华东")
        self.assertEqual("planned", self.db.get_slot(first)["status"])
        replaced = self.db.replace_slot(first, self.p2)
        self.assertEqual("replaced", replaced["status"])
        self.assertEqual(self.p2, replaced["program_id"])
        self.db.publish_draft()
        self.db.record_playout(first, "09:00", 5, self.p1, "临时切回旧内容")
        self.db.record_playout(second, "10:00", 5, self.p2)
        exceptions = self.db.reconcile_date("2026-09-28")
        kinds = {(row["slot_id"], row["kind"]) for row in exceptions}
        self.assertIn((first, "wrong_program"), kinds)

    def test_rejects_overlap_and_unauthorized_region(self):
        self.db.create_draft()
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

    def _publish_first_version(self):
        self.db.create_draft()
        slot_id = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        self.db.publish_draft()
        return slot_id

    def test_draft_is_internal_until_published(self):
        self.db.create_draft(note="九月调整")
        slot_id = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        snap = self.db.snapshot()
        self.assertIsNone(snap["current_version"])
        self.assertEqual([], snap["slots"])
        self.assertEqual([slot_id], [s["id"] for s in snap["draft_slots"]])
        version = self.db.publish_draft()
        self.assertEqual(1, version["version_no"])
        self.assertEqual("新增1 · 调整0 · 取消0", version["summary"])
        snap = self.db.snapshot()
        self.assertEqual(1, snap["current_version"]["version_no"])
        self.assertEqual([slot_id], [s["id"] for s in snap["slots"]])
        self.assertIsNone(snap["draft"])

    def test_published_version_immutable_and_single_draft(self):
        slot_id = self._publish_first_version()
        with self.assertRaisesRegex(DomainError, "草案"):
            self.db.replace_slot(slot_id, self.p2)
        with self.assertRaisesRegex(DomainError, "草案"):
            self.db.cancel_slot(slot_id)
        with self.assertRaisesRegex(DomainError, "草案"):
            self.db.schedule_slot("2026-09-28", "10:00", self.p1, "华东")
        self.db.create_draft()
        with self.assertRaisesRegex(DomainError, "草案"):
            self.db.create_draft()
        copy_id = self.db.snapshot()["draft_slots"][0]["id"]
        self.assertNotEqual(slot_id, copy_id)
        replaced = self.db.replace_slot(copy_id, self.p2)
        self.assertEqual("replaced", replaced["status"])
        v2 = self.db.publish_draft()
        self.assertEqual(2, v2["version_no"])
        self.assertEqual("新增0 · 调整1 · 取消0", v2["summary"])
        # 已发布的 v1 排期保持不动
        self.assertEqual(self.p1, self.db.get_slot(slot_id)["program_id"])

    def test_empty_draft_cannot_publish_and_can_be_discarded(self):
        self.db.create_draft()
        with self.assertRaisesRegex(DomainError, "无法发布"):
            self.db.publish_draft()
        self.db.discard_draft()
        self.assertIsNone(self.db.snapshot()["draft"])

    def test_publish_revalidates_full_plan(self):
        self.db.create_draft()
        self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        weekday = date(2026, 9, 28).weekday()
        self.db.add_blocked_window("华东", weekday, "08:30", "09:30", "早间检修")
        with self.assertRaisesRegex(DomainError, "禁播"):
            self.db.publish_draft()
        # 发布失败草案仍然保留，可以修正后再发
        self.assertIsNotNone(self.db.snapshot()["draft"])

    def test_playout_requires_published_version(self):
        self.db.create_draft()
        slot_id = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        with self.assertRaisesRegex(DomainError, "发布"):
            self.db.record_playout(slot_id, "09:00", 30)

    def test_rollback_keeps_versions_and_playout(self):
        slot_id = self._publish_first_version()
        self.db.record_playout(slot_id, "09:00", 30, self.p2, "临时替换")
        # v2：替换节目后发布
        self.db.create_draft()
        copy_id = self.db.snapshot()["draft_slots"][0]["id"]
        self.db.replace_slot(copy_id, self.p2)
        self.db.publish_draft()
        # 对账仍按登记时的 v1 排期核对实播
        exceptions = self.db.reconcile_date("2026-09-28")
        kinds = {(row["slot_id"], row["kind"]) for row in exceptions}
        self.assertIn((slot_id, "wrong_program"), kinds)
        # 当前版本排期的来源已有实播记录，不再重复报漏播
        self.assertNotIn("missed", {row["kind"] for row in exceptions})
        # 回滚：从 v1 重新建草案并发布为 v3
        versions = {v["version_no"]: v for v in self.db.snapshot()["recent_versions"]}
        self.db.create_draft(source_version_id=versions[1]["id"], note="回滚到 v1")
        draft_slots = self.db.snapshot()["draft_slots"]
        self.assertEqual(self.p1, draft_slots[0]["program_id"])
        v3 = self.db.publish_draft()
        self.assertEqual(3, v3["version_no"])
        self.assertEqual(versions[1]["id"], v3["source_version_id"])
        # 历史版本、当时的排期和实播记录全部保留
        snap = self.db.snapshot()
        self.assertEqual([3, 2, 1], [v["version_no"] for v in snap["recent_versions"]])
        self.assertEqual(self.p2, self.db.get_slot(copy_id)["program_id"])
        logs = self.db.conn.execute("SELECT COUNT(*) FROM playout_logs WHERE slot_id=?", (slot_id,)).fetchone()[0]
        self.assertEqual(1, logs)
        # 回滚后对账结果不变，历史依据仍可追溯
        exceptions = self.db.reconcile_date("2026-09-28")
        kinds = {(row["slot_id"], row["kind"]) for row in exceptions}
        self.assertIn((slot_id, "wrong_program"), kinds)


if __name__ == "__main__":
    unittest.main()
