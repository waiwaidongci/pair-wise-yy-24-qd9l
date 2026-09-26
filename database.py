from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path


class DomainError(ValueError):
    """A business-rule violation that should be shown to the API caller."""


PROGRAM_KINDS = {"music", "ad", "talk", "live"}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _overlap(a_start: str, a_duration: int, b_start: str, b_duration: int) -> bool:
    start_a, start_b = _minutes(a_start), _minutes(b_start)
    return start_a < start_b + b_duration and start_b < start_a + a_duration


class RadioDB:
    """SQLite-backed radio scheduling service.

    Schedule changes never take effect directly. Edits happen in a single
    draft version that is only visible internally; once the whole draft
    validates it is published as a new immutable version. Published versions
    and the playout logs registered against them are never rewritten, so
    reconciliation can always trace the plan that was in effect when
    something actually aired. A new draft can be seeded from any published
    version to roll back.
    """

    def __init__(self, path: str = "radio.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS programs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              kind TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              sponsor TEXT,
              cooldown_minutes INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_minutes >= 0),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              UNIQUE(title, start_date, end_date)
            );
            CREATE TABLE IF NOT EXISTS program_regions (
              program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
              region TEXT NOT NULL,
              PRIMARY KEY(program_id, region)
            );
            CREATE TABLE IF NOT EXISTS blocked_windows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              region TEXT NOT NULL,
              weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
              start_time TEXT NOT NULL,
              end_time TEXT NOT NULL,
              reason TEXT NOT NULL,
              CHECK(start_time < end_time)
            );
            CREATE TABLE IF NOT EXISTS sponsor_policies (
              sponsor TEXT PRIMARY KEY,
              min_gap_minutes INTEGER NOT NULL CHECK(min_gap_minutes >= 0)
            );
            CREATE TABLE IF NOT EXISTS schedule_versions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version_no INTEGER NOT NULL UNIQUE,
              status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft','published')),
              source_version_id INTEGER REFERENCES schedule_versions(id),
              note TEXT NOT NULL DEFAULT '',
              summary TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              published_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_single_draft
              ON schedule_versions(status) WHERE status='draft';
            CREATE TABLE IF NOT EXISTS slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned'
                CHECK(status IN ('planned','replaced','cancelled')),
              replaced_from INTEGER REFERENCES programs(id),
              version_id INTEGER NOT NULL REFERENCES schedule_versions(id),
              origin_slot_id INTEGER REFERENCES slots(id),
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region);
            CREATE TABLE IF NOT EXISTS playout_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              actual_start TEXT NOT NULL,
              actual_duration_minutes INTEGER NOT NULL CHECK(actual_duration_minutes >= 0),
              actual_program_id INTEGER REFERENCES programs(id),
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(air_date, slot_id, kind)
            );
            """
        )
        self._migrate_slots_to_versions()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_slots_version ON slots(version_id)")
        self.conn.commit()

    def _migrate_slots_to_versions(self) -> None:
        """Attach pre-versioning slots to a published baseline version."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(slots)")}
        if "version_id" in columns:
            return
        self.conn.execute("ALTER TABLE slots ADD COLUMN version_id INTEGER REFERENCES schedule_versions(id)")
        self.conn.execute("ALTER TABLE slots ADD COLUMN origin_slot_id INTEGER REFERENCES slots(id)")
        orphans = self.conn.execute("SELECT COUNT(*) FROM slots WHERE version_id IS NULL").fetchone()[0]
        if orphans:
            now = datetime.now().isoformat()
            cur = self.conn.execute(
                "INSERT INTO schedule_versions(version_no,status,note,summary,created_at,published_at) "
                "VALUES(1,'published','历史数据迁移','既有排期整体迁入',?,?)",
                (now, now),
            )
            self.conn.execute("UPDATE slots SET version_id=? WHERE version_id IS NULL", (int(cur.lastrowid),))

    # ------------------------------------------------------------------
    # Versions and drafts
    # ------------------------------------------------------------------
    def open_draft(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM schedule_versions WHERE status='draft' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def current_version(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM schedule_versions WHERE status='published' ORDER BY version_no DESC LIMIT 1"
        ).fetchone()

    def _version_info(self, version_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM schedule_versions WHERE id=?", (version_id,)).fetchone()
        return dict(row) if row else None

    def get_version(self, version_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM schedule_versions WHERE id=?", (version_id,)).fetchone()
        if not row:
            raise DomainError("版本不存在")
        slots = [dict(r) for r in self.conn.execute(
            "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
            "WHERE s.version_id=? ORDER BY s.air_date, s.start_time", (version_id,)
        ).fetchall()]
        return {"version": dict(row), "slots": slots}

    def create_draft(self, source_version_id: int | None = None, note: str = "") -> int:
        """Open a draft seeded from a published version (default: the current one).

        Passing a historical source_version_id is the rollback path: the old
        version itself stays untouched, its slots are copied into the draft.
        """
        with self.transaction():
            if self.open_draft():
                raise DomainError("已存在未发布的草案，请先发布或废弃后再新建")
            if source_version_id is None:
                source = self.current_version()
            else:
                source = self.conn.execute(
                    "SELECT * FROM schedule_versions WHERE id=? AND status='published'", (source_version_id,)
                ).fetchone()
                if not source:
                    raise DomainError("来源版本不存在或尚未发布")
            version_no = self.conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM schedule_versions"
            ).fetchone()[0]
            now = datetime.now().isoformat()
            cur = self.conn.execute(
                "INSERT INTO schedule_versions(version_no,status,source_version_id,note,created_at) VALUES(?,'draft',?,?,?)",
                (version_no, source["id"] if source else None, (note or "").strip(), now),
            )
            draft_id = int(cur.lastrowid)
            if source:
                self.conn.execute(
                    "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,status,replaced_from,"
                    "version_id,origin_slot_id,created_at) "
                    "SELECT air_date,start_time,duration_minutes,program_id,region,'planned',NULL,?,id,? "
                    "FROM slots WHERE version_id=? AND status!='cancelled'",
                    (draft_id, now, source["id"]),
                )
        return draft_id

    def discard_draft(self) -> None:
        with self.transaction():
            draft = self.open_draft()
            if not draft:
                raise DomainError("当前没有可废弃的草案")
            self.conn.execute("DELETE FROM slots WHERE version_id=?", (draft["id"],))
            self.conn.execute("DELETE FROM schedule_versions WHERE id=?", (draft["id"],))

    def _summarize_version(self, version_id: int) -> str:
        added = self.conn.execute(
            "SELECT COUNT(*) FROM slots WHERE version_id=? AND origin_slot_id IS NULL AND status!='cancelled'",
            (version_id,),
        ).fetchone()[0]
        cancelled = self.conn.execute(
            "SELECT COUNT(*) FROM slots WHERE version_id=? AND status='cancelled' AND origin_slot_id IS NOT NULL",
            (version_id,),
        ).fetchone()[0]
        changed = self.conn.execute(
            "SELECT COUNT(*) FROM slots d JOIN slots o ON o.id=d.origin_slot_id "
            "WHERE d.version_id=? AND d.status!='cancelled' AND (d.program_id!=o.program_id OR d.air_date!=o.air_date "
            "OR d.start_time!=o.start_time OR d.duration_minutes!=o.duration_minutes OR d.region!=o.region)",
            (version_id,),
        ).fetchone()[0]
        return f"新增{added} · 调整{changed} · 取消{cancelled}"

    def publish_draft(self) -> dict:
        """Validate the whole draft plan and freeze it as the current version."""
        with self.transaction():
            draft = self.open_draft()
            if not draft:
                raise DomainError("当前没有可发布的草案")
            slots = self.conn.execute(
                "SELECT * FROM slots WHERE version_id=? AND status!='cancelled' ORDER BY air_date, start_time",
                (draft["id"],),
            ).fetchall()
            if not slots:
                raise DomainError("草案内没有有效排期，无法发布")
            for slot in slots:
                self._validate_slot(
                    slot["air_date"], slot["start_time"], slot["duration_minutes"],
                    slot["program_id"], slot["region"], draft["id"], slot["id"],
                )
            summary = self._summarize_version(draft["id"])
            now = datetime.now().isoformat()
            self.conn.execute(
                "UPDATE schedule_versions SET status='published', published_at=?, summary=? WHERE id=?",
                (now, summary, draft["id"]),
            )
        return self._version_info(draft["id"])

    # ------------------------------------------------------------------
    # Programs and policies
    # ------------------------------------------------------------------
    def seed_demo(self) -> None:
        existing = self.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        if existing:
            return
        music = self.add_program("晨间轻音乐", "music", 30, "2026-01-01", "2026-12-31", "青柠饮品", 45, ["华东"])
        news = self.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        ad = self.add_program("青柠饮品广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠饮品", 60, ["华东"])
        self.add_sponsor_policy("青柠饮品", 90)
        self.add_blocked_window("华东", 0, "08:00", "08:30", "周一设备检修")
        self.create_draft(note="初始排期")
        self.schedule_slot("2026-09-28", "09:00", music, "华东")
        self.schedule_slot("2026-09-28", "10:00", news, "华东")
        self.schedule_slot("2026-09-28", "11:00", ad, "华东")
        self.publish_draft()

    def add_program(self, title: str, kind: str, duration_minutes: int, start_date: str, end_date: str,
                    sponsor: str | None = None, cooldown_minutes: int = 0,
                    regions: list[str] | None = None) -> int:
        if not title.strip():
            raise DomainError("节目名称不能为空")
        if kind not in PROGRAM_KINDS:
            raise DomainError(f"不支持的节目类型: {kind}")
        if duration_minutes <= 0:
            raise DomainError("节目时长必须大于0")
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        if end < start:
            raise DomainError("授权结束日期不能早于开始日期")
        if cooldown_minutes < 0:
            raise DomainError("冷却时间不能为负数")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO programs(title,kind,duration_minutes,start_date,end_date,sponsor,cooldown_minutes) VALUES(?,?,?,?,?,?,?)",
                (title.strip(), kind, duration_minutes, start_date, end_date, (sponsor or "").strip() or None, cooldown_minutes),
            )
            program_id = int(cur.lastrowid)
            for region in regions or []:
                self.conn.execute("INSERT INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))
        return program_id

    def authorize_region(self, program_id: int, region: str) -> None:
        if not region.strip():
            raise DomainError("地区不能为空")
        with self.transaction():
            if not self.conn.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone():
                raise DomainError("节目不存在")
            self.conn.execute("INSERT OR IGNORE INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))

    def add_sponsor_policy(self, sponsor: str, min_gap_minutes: int) -> None:
        if not sponsor.strip() or min_gap_minutes < 0:
            raise DomainError("赞助商和最小间隔必须有效")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO sponsor_policies(sponsor,min_gap_minutes) VALUES(?,?) "
                "ON CONFLICT(sponsor) DO UPDATE SET min_gap_minutes=excluded.min_gap_minutes",
                (sponsor.strip(), min_gap_minutes),
            )

    def add_blocked_window(self, region: str, weekday: int, start_time: str, end_time: str, reason: str) -> int:
        if weekday not in range(7) or _minutes(start_time) >= _minutes(end_time):
            raise DomainError("禁播时段参数无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                (region.strip(), weekday, start_time, end_time, reason.strip() or "禁播"),
            )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # Slot editing (draft only)
    # ------------------------------------------------------------------
    def _validate_slot(self, air_date: str, start_time: str, duration: int, program_id: int,
                       region: str, version_id: int, ignore_slot_id: int | None = None) -> None:
        try:
            day = datetime.strptime(air_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        try:
            _minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        if duration <= 0:
            raise DomainError("排期时长必须大于0")
        program = self.conn.execute("SELECT * FROM programs WHERE id=? AND active=1", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在或未启用")
        if program["duration_minutes"] != duration:
            raise DomainError(f"排期时长必须等于节目时长 {program['duration_minutes']} 分钟")
        if not (program["start_date"] <= air_date <= program["end_date"]):
            raise DomainError("播出日期超出授权窗口")
        if not self.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (program_id, region)
        ).fetchone():
            raise DomainError(f"节目未授权在{region}播出")
        end_minutes = _minutes(start_time) + duration
        blocked = self.conn.execute(
            "SELECT * FROM blocked_windows WHERE region=? AND weekday=?",
            (region, day.weekday()),
        ).fetchall()
        for window in blocked:
            if _minutes(window["start_time"]) < end_minutes and _minutes(start_time) < _minutes(window["end_time"]):
                raise DomainError(f"与禁播时段冲突: {window['reason']}")
        sql = "SELECT * FROM slots WHERE air_date=? AND region=? AND status!='cancelled' AND version_id=?"
        params: list[object] = [air_date, region, version_id]
        if ignore_slot_id is not None:
            sql += " AND id!=?"
            params.append(ignore_slot_id)
        for existing in self.conn.execute(sql, params).fetchall():
            if _overlap(start_time, duration, existing["start_time"], existing["duration_minutes"]):
                raise DomainError(f"与排期 #{existing['id']} 时间重叠")
        if program["cooldown_minutes"]:
            previous = self.conn.execute(
                "SELECT * FROM slots WHERE air_date=? AND region=? AND program_id=? AND status!='cancelled' "
                "AND version_id=? AND id!=? AND start_time < ? ORDER BY start_time DESC LIMIT 1",
                (air_date, region, program_id, version_id, ignore_slot_id or -1, start_time),
            ).fetchone()
            if previous:
                gap = _minutes(start_time) - (_minutes(previous["start_time"]) + previous["duration_minutes"])
                if gap < program["cooldown_minutes"]:
                    raise DomainError(f"与上一期节目间隔不足冷却时间 {program['cooldown_minutes']} 分钟")
        if program["sponsor"]:
            policy = self.conn.execute("SELECT min_gap_minutes FROM sponsor_policies WHERE sponsor=?", (program["sponsor"],)).fetchone()
            if policy:
                gap = policy["min_gap_minutes"]
                all_sponsored = self.conn.execute(
                    "SELECT s.*, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
                    "WHERE s.air_date=? AND s.region=? AND s.status!='cancelled' AND s.version_id=? AND p.sponsor=? AND s.id!=?",
                    (air_date, region, version_id, program["sponsor"], ignore_slot_id or -1),
                ).fetchall()
                for other in all_sponsored:
                    if _overlap(start_time, duration, other["start_time"], other["duration_minutes"]):
                        raise DomainError(f"与赞助商 {program['sponsor']} 的其他节目冲突")
                    distance = abs(_minutes(start_time) - (_minutes(other["start_time"]) + other["duration_minutes"]))
                    if distance < gap:
                        raise DomainError(f"与赞助商 {program['sponsor']} 的节目间隔不足 {gap} 分钟")

    def _require_draft_slot(self, slot_id: int) -> sqlite3.Row:
        slot = self.conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not slot:
            raise DomainError("排期不存在")
        draft = self.open_draft()
        if not draft or slot["version_id"] != draft["id"]:
            raise DomainError("已发布版本的排期不可修改，请在草案中调整")
        return slot

    def schedule_slot(self, air_date: str, start_time: str, program_id: int, region: str) -> int:
        program = self.conn.execute("SELECT duration_minutes FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在")
        with self.transaction():
            draft = self.open_draft()
            if not draft:
                raise DomainError("请先创建编排草案，节目调整只在草案中生效")
            self._validate_slot(air_date, start_time, int(program["duration_minutes"]), program_id, region, draft["id"])
            cur = self.conn.execute(
                "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,version_id,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (air_date, start_time, int(program["duration_minutes"]), program_id, region,
                 draft["id"], datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def replace_slot(self, slot_id: int, new_program_id: int) -> dict:
        """Replace a planned item in the draft and revalidate the plan atomically."""
        with self.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=? AND status='planned'", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("只能替换尚未播出且状态为 planned 的排期")
            draft = self.open_draft()
            if not draft or slot["version_id"] != draft["id"]:
                raise DomainError("已发布版本的排期不可修改，请在草案中调整")
            program = self.conn.execute("SELECT * FROM programs WHERE id=?", (new_program_id,)).fetchone()
            if not program:
                raise DomainError("替换节目不存在")
            self._validate_slot(slot["air_date"], slot["start_time"], int(program["duration_minutes"]),
                                new_program_id, slot["region"], draft["id"], slot_id)
            self.conn.execute(
                "UPDATE slots SET program_id=?, duration_minutes=?, replaced_from=?, status='replaced' WHERE id=?",
                (new_program_id, int(program["duration_minutes"]), slot["program_id"], slot_id),
            )
        return self.get_slot(slot_id)

    def cancel_slot(self, slot_id: int) -> dict:
        with self.transaction():
            slot = self._require_draft_slot(slot_id)
            if slot["status"] == "cancelled":
                raise DomainError("排期已取消")
            self.conn.execute("UPDATE slots SET status='cancelled' WHERE id=?", (slot_id,))
        return self.get_slot(slot_id)

    def get_slot(self, slot_id: int) -> dict:
        row = self.conn.execute(
            "SELECT s.*, p.title, p.kind, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if not row:
            raise DomainError("排期不存在")
        return dict(row)

    # ------------------------------------------------------------------
    # Playout and reconciliation
    # ------------------------------------------------------------------
    def record_playout(self, slot_id: int, actual_start: str, actual_duration_minutes: int,
                       actual_program_id: int | None = None, note: str = "") -> int:
        slot = self.conn.execute(
            "SELECT s.id, v.status AS version_status FROM slots s "
            "JOIN schedule_versions v ON v.id=s.version_id WHERE s.id=?", (slot_id,)
        ).fetchone()
        if not slot:
            raise DomainError("排期不存在")
        if slot["version_status"] != "published":
            raise DomainError("草案中的排期不能登记实播，请先发布版本")
        if actual_duration_minutes < 0:
            raise DomainError("实际时长不能为负数")
        _minutes(actual_start)
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,created_at) VALUES(?,?,?,?,?,?)",
                (slot_id, actual_start, actual_duration_minutes, actual_program_id, note, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def _origin_aired(self, slot: sqlite3.Row) -> bool:
        """True if any slot along the origin chain already has a playout log."""
        origin_id = slot["origin_slot_id"]
        while origin_id is not None:
            if self.conn.execute("SELECT 1 FROM playout_logs WHERE slot_id=? LIMIT 1", (origin_id,)).fetchone():
                return True
            row = self.conn.execute("SELECT origin_slot_id FROM slots WHERE id=?", (origin_id,)).fetchone()
            origin_id = row["origin_slot_id"] if row else None
        return False

    def reconcile_date(self, air_date: str) -> list[dict]:
        """Compare registered playout with the plan version it was recorded against.

        A playout log is always reconciled against the slot it was registered
        on, so rolling back to a historical version never erases the basis
        for what actually aired. Slots in the current published version with
        no playout (directly or via their origin chain) are reported missed.
        """
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        with self.transaction():
            self.conn.execute("DELETE FROM reconciliation_exceptions WHERE air_date=?", (air_date,))
            logs = self.conn.execute(
                "SELECT l.*, s.program_id AS planned_program_id, s.duration_minutes AS planned_duration, s.region "
                "FROM playout_logs l JOIN slots s ON s.id=l.slot_id WHERE s.air_date=? ORDER BY l.slot_id, l.id",
                (air_date,),
            ).fetchall()
            latest: dict[int, sqlite3.Row] = {}
            for log in logs:
                latest[log["slot_id"]] = log
            exceptions: list[tuple[int, str, str]] = []
            for slot_id, log in latest.items():
                planned_program_id = log["planned_program_id"]
                actual_program_id = log["actual_program_id"] or planned_program_id
                if actual_program_id != planned_program_id:
                    exceptions.append((slot_id, "wrong_program", f"计划节目 #{planned_program_id}，实播节目 #{actual_program_id}"))
                delta = log["actual_duration_minutes"] - log["planned_duration"]
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot_id, kind, f"与计划相差 {delta:+d} 分钟"))
                actual = self.conn.execute("SELECT * FROM programs WHERE id=?", (actual_program_id,)).fetchone()
                if actual:
                    region_ok = self.conn.execute(
                        "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (actual_program_id, log["region"])
                    ).fetchone()
                    if not region_ok or not (actual["start_date"] <= air_date <= actual["end_date"]):
                        exceptions.append((slot_id, "out_of_license", "实播节目超出地区或日期授权"))
            current = self.current_version()
            if current:
                planned = self.conn.execute(
                    "SELECT * FROM slots WHERE version_id=? AND air_date=? AND status!='cancelled'",
                    (current["id"], air_date),
                ).fetchall()
                for slot in planned:
                    if slot["id"] in latest or self._origin_aired(slot):
                        continue
                    exceptions.append((slot["id"], "missed", "没有实播记录"))
            for slot_id, kind, detail in exceptions:
                self.conn.execute(
                    "INSERT INTO reconciliation_exceptions(air_date,slot_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                    (air_date, slot_id, kind, detail, datetime.now().isoformat()),
                )
        return self.get_exceptions(air_date)

    def get_exceptions(self, air_date: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE air_date=? ORDER BY slot_id, kind", (air_date,)
        ).fetchall()]

    def snapshot(self) -> dict:
        programs = [dict(row) for row in self.conn.execute("SELECT * FROM programs ORDER BY id").fetchall()]
        current = self.current_version()
        draft = self.open_draft()
        slots: list[dict] = []
        if current:
            slots = [dict(row) for row in self.conn.execute(
                "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.version_id=? AND s.status!='cancelled' ORDER BY s.air_date, s.start_time", (current["id"],)
            ).fetchall()]
        draft_slots: list[dict] = []
        if draft:
            draft_slots = [dict(row) for row in self.conn.execute(
                "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.version_id=? ORDER BY s.air_date, s.start_time", (draft["id"],)
            ).fetchall()]
        recent_versions = [dict(row) for row in self.conn.execute(
            "SELECT * FROM schedule_versions WHERE status='published' ORDER BY version_no DESC LIMIT 5"
        ).fetchall()]
        return {
            "programs": programs,
            "current_version": dict(current) if current else None,
            "slots": slots,
            "draft": dict(draft) if draft else None,
            "draft_slots": draft_slots,
            "recent_versions": recent_versions,
            "exceptions": [dict(row) for row in self.conn.execute(
                "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
            ).fetchall()],
        }
