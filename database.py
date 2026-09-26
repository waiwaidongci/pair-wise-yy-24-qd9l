from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime


class DomainError(ValueError):
    """A business-rule violation that should be shown to the API caller."""


PROGRAM_KINDS = {"music", "ad", "talk", "live"}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

# A slot belongs to the live plan until a published version supersedes it.
# Superseded rows are never deleted: playout logs keep their original context
# and reconciliation can always trace what the plan was at the time.
LIVE_SLOT = "status != 'cancelled' AND superseded_by_version_id IS NULL"


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _overlap(a_start: str, a_duration: int, b_start: str, b_duration: int) -> bool:
    start_a, start_b = _minutes(a_start), _minutes(b_start)
    return start_a < start_b + b_duration and start_b < start_a + a_duration


class RadioDB:
    """SQLite-backed radio scheduling service.

    The service keeps planning and actual playout separate. Schedule edits are
    staged in internal drafts; a draft is validated and published as an
    immutable version, and only then changes the live plan. Published versions
    and playout logs are never rewritten, so rolling back to a historical
    version never hides what was planned or what actually aired.
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
              note TEXT NOT NULL DEFAULT '',
              source_version_id INTEGER REFERENCES schedule_versions(id),
              draft_id INTEGER,
              created_at TEXT NOT NULL
            );
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
              version_id INTEGER REFERENCES schedule_versions(id),
              superseded_by_version_id INTEGER REFERENCES schedule_versions(id),
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region);
            CREATE TABLE IF NOT EXISTS version_slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version_id INTEGER NOT NULL REFERENCES schedule_versions(id) ON DELETE CASCADE,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_version_slots_version ON version_slots(version_id);
            CREATE TABLE IF NOT EXISTS schedule_drafts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','published','abandoned')),
              base_version_id INTEGER REFERENCES schedule_versions(id),
              published_version_id INTEGER REFERENCES schedule_versions(id),
              created_at TEXT NOT NULL,
              published_at TEXT
            );
            CREATE TABLE IF NOT EXISTS draft_slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              draft_id INTEGER NOT NULL REFERENCES schedule_drafts(id) ON DELETE CASCADE,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_draft_slots_draft ON draft_slots(draft_id);
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
        # Older databases predate versioning: add the columns in place.
        slot_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(slots)")}
        if "version_id" not in slot_columns:
            self.conn.execute("ALTER TABLE slots ADD COLUMN version_id INTEGER REFERENCES schedule_versions(id)")
        if "superseded_by_version_id" not in slot_columns:
            self.conn.execute(
                "ALTER TABLE slots ADD COLUMN superseded_by_version_id INTEGER REFERENCES schedule_versions(id)"
            )
        self.conn.commit()

    def seed_demo(self) -> None:
        existing = self.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        if existing:
            return
        music = self.add_program("晨间轻音乐", "music", 30, "2026-01-01", "2026-12-31", "青柠饮品", 45, ["华东"])
        news = self.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        ad = self.add_program("青柠饮品广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠饮品", 60, ["华东"])
        self.add_sponsor_policy("青柠饮品", 90)
        self.add_blocked_window("华东", 0, "08:00", "08:30", "周一设备检修")
        self.schedule_slot("2026-09-28", "09:00", music, "华东")
        self.schedule_slot("2026-09-28", "10:00", news, "华东")
        self.schedule_slot("2026-09-28", "11:00", ad, "华东")
        with self.transaction():
            version_id = self._new_version_row("初始版本", None, None)
            self._snapshot_live_into(version_id)
            self.conn.execute("UPDATE slots SET version_id=? WHERE version_id IS NULL", (version_id,))

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

    def _validate_slot(self, air_date: str, start_time: str, duration: int, program_id: int,
                       region: str, ignore_slot_id: int | None = None) -> None:
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
        sql = "SELECT * FROM slots WHERE air_date=? AND region=? AND status!='cancelled' AND superseded_by_version_id IS NULL"
        params: list[object] = [air_date, region]
        if ignore_slot_id is not None:
            sql += " AND id!=?"
            params.append(ignore_slot_id)
        for existing in self.conn.execute(sql, params).fetchall():
            if _overlap(start_time, duration, existing["start_time"], existing["duration_minutes"]):
                raise DomainError(f"与排期 #{existing['id']} 时间重叠")
        if program["cooldown_minutes"]:
            previous = self.conn.execute(
                "SELECT * FROM slots WHERE air_date=? AND region=? AND program_id=? AND status!='cancelled' "
                "AND superseded_by_version_id IS NULL AND id!=? AND start_time < ? ORDER BY start_time DESC LIMIT 1",
                (air_date, region, program_id, ignore_slot_id or -1, start_time),
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
                    "WHERE s.air_date=? AND s.region=? AND s.status!='cancelled' AND s.superseded_by_version_id IS NULL "
                    "AND p.sponsor=? AND s.id!=?",
                    (air_date, region, program["sponsor"], ignore_slot_id or -1),
                ).fetchall()
                for other in all_sponsored:
                    if _overlap(start_time, duration, other["start_time"], other["duration_minutes"]):
                        raise DomainError(f"与赞助商 {program['sponsor']} 的其他节目冲突")
                    distance = abs(_minutes(start_time) - (_minutes(other["start_time"]) + other["duration_minutes"]))
                    if distance < gap:
                        raise DomainError(f"与赞助商 {program['sponsor']} 的节目间隔不足 {gap} 分钟")

    def schedule_slot(self, air_date: str, start_time: str, program_id: int, region: str) -> int:
        program = self.conn.execute("SELECT duration_minutes FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在")
        with self.transaction():
            self._validate_slot(air_date, start_time, int(program["duration_minutes"]), program_id, region)
            cur = self.conn.execute(
                "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,created_at) VALUES(?,?,?,?,?,?)",
                (air_date, start_time, int(program["duration_minutes"]), program_id, region, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def replace_slot(self, slot_id: int, new_program_id: int) -> dict:
        """Replace a planned item and revalidate the resulting plan atomically."""
        with self.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=? AND status='planned'", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("只能替换尚未播出且状态为 planned 的排期")
            program = self.conn.execute("SELECT * FROM programs WHERE id=?", (new_program_id,)).fetchone()
            if not program:
                raise DomainError("替换节目不存在")
            self._validate_slot(slot["air_date"], slot["start_time"], int(program["duration_minutes"]), new_program_id, slot["region"], slot_id)
            self.conn.execute(
                "UPDATE slots SET program_id=?, duration_minutes=?, replaced_from=?, status='replaced' WHERE id=?",
                (new_program_id, int(program["duration_minutes"]), slot["program_id"], slot_id),
            )
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
    # Drafts and published versions
    # ------------------------------------------------------------------

    def create_draft(self, title: str, base_version_id: int | None = None, copy_current: bool = False) -> int:
        """Open an internal draft, optionally seeded from a historical version
        (the rollback path) or from the current live plan."""
        title = title.strip()
        if not title:
            raise DomainError("草案名称不能为空")
        with self.transaction():
            if base_version_id is not None:
                if not self.conn.execute("SELECT 1 FROM schedule_versions WHERE id=?", (base_version_id,)).fetchone():
                    raise DomainError("历史版本不存在")
            now = datetime.now().isoformat()
            cur = self.conn.execute(
                "INSERT INTO schedule_drafts(title,base_version_id,created_at) VALUES(?,?,?)",
                (title, base_version_id, now),
            )
            draft_id = int(cur.lastrowid)
            if base_version_id is not None:
                self.conn.execute(
                    "INSERT INTO draft_slots(draft_id,air_date,start_time,duration_minutes,program_id,region,created_at) "
                    "SELECT ?,air_date,start_time,duration_minutes,program_id,region,? FROM version_slots WHERE version_id=?",
                    (draft_id, now, base_version_id),
                )
            elif copy_current:
                self.conn.execute(
                    "INSERT INTO draft_slots(draft_id,air_date,start_time,duration_minutes,program_id,region,created_at) "
                    f"SELECT ?,air_date,start_time,duration_minutes,program_id,region,? FROM slots WHERE {LIVE_SLOT}",
                    (draft_id, now),
                )
        return draft_id

    def _require_open_draft(self, draft_id: int) -> sqlite3.Row:
        draft = self.conn.execute("SELECT * FROM schedule_drafts WHERE id=?", (draft_id,)).fetchone()
        if not draft:
            raise DomainError("草案不存在")
        if draft["status"] != "open":
            raise DomainError("草案已发布或已废弃，不能再修改")
        return draft

    def get_draft(self, draft_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM schedule_drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            raise DomainError("草案不存在")
        draft = dict(row)
        draft["slots"] = [dict(r) for r in self.conn.execute(
            "SELECT ds.*, p.title, p.kind FROM draft_slots ds JOIN programs p ON p.id=ds.program_id "
            "WHERE ds.draft_id=? ORDER BY ds.air_date, ds.start_time", (draft_id,)
        ).fetchall()]
        return draft

    def list_drafts(self) -> list[dict]:
        rows = self.conn.execute("SELECT id FROM schedule_drafts WHERE status='open' ORDER BY id DESC").fetchall()
        return [self.get_draft(row["id"]) for row in rows]

    def add_draft_slot(self, draft_id: int, air_date: str, start_time: str, program_id: int, region: str) -> int:
        """Stage one slot in a draft. Full business validation happens at
        validate/publish time; here we only catch malformed input and
        overlaps inside the draft itself."""
        region = region.strip()
        if not region:
            raise DomainError("地区不能为空")
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        try:
            _minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        program = self.conn.execute("SELECT * FROM programs WHERE id=? AND active=1", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在或未启用")
        duration = int(program["duration_minutes"])
        with self.transaction():
            self._require_open_draft(draft_id)
            for existing in self.conn.execute(
                "SELECT * FROM draft_slots WHERE draft_id=? AND air_date=? AND region=?", (draft_id, air_date, region)
            ).fetchall():
                if _overlap(start_time, duration, existing["start_time"], existing["duration_minutes"]):
                    raise DomainError(f"与草案内 {existing['air_date']} {existing['start_time']} 的安排时间重叠")
            cur = self.conn.execute(
                "INSERT INTO draft_slots(draft_id,air_date,start_time,duration_minutes,program_id,region,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (draft_id, air_date, start_time, duration, program_id, region, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def remove_draft_slot(self, draft_id: int, draft_slot_id: int) -> None:
        with self.transaction():
            self._require_open_draft(draft_id)
            cur = self.conn.execute("DELETE FROM draft_slots WHERE id=? AND draft_id=?", (draft_slot_id, draft_id))
            if cur.rowcount == 0:
                raise DomainError("草案排期不存在")

    def abandon_draft(self, draft_id: int) -> None:
        with self.transaction():
            self._require_open_draft(draft_id)
            self.conn.execute("UPDATE schedule_drafts SET status='abandoned' WHERE id=?", (draft_id,))

    def _draft_scopes(self, draft_id: int) -> set[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT DISTINCT air_date, region FROM draft_slots WHERE draft_id=?", (draft_id,)
        ).fetchall()
        return {(row["air_date"], row["region"]) for row in rows}

    def _apply_scope(self, draft_id: int, air_date: str, region: str, version_id: int) -> None:
        """Make the live plan for one (air_date, region) match the draft.

        Slots the draft no longer contains are superseded in place (never
        deleted, so playout logs keep their target); new draft slots are
        validated against the resulting plan and inserted.
        """
        draft_rows = self.conn.execute(
            "SELECT * FROM draft_slots WHERE draft_id=? AND air_date=? AND region=? ORDER BY start_time",
            (draft_id, air_date, region),
        ).fetchall()
        live_rows = self.conn.execute(
            f"SELECT * FROM slots WHERE air_date=? AND region=? AND {LIVE_SLOT}", (air_date, region)
        ).fetchall()
        draft_keys = {(row["start_time"], row["program_id"]) for row in draft_rows}
        live_keys = {(row["start_time"], row["program_id"]) for row in live_rows}
        for row in live_rows:
            if (row["start_time"], row["program_id"]) not in draft_keys:
                self.conn.execute("UPDATE slots SET superseded_by_version_id=? WHERE id=?", (version_id, row["id"]))
        for row in draft_rows:
            if (row["start_time"], row["program_id"]) not in live_keys:
                self._validate_slot(row["air_date"], row["start_time"], row["duration_minutes"], row["program_id"], row["region"])
                self.conn.execute(
                    "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,version_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (row["air_date"], row["start_time"], row["duration_minutes"], row["program_id"], row["region"],
                     version_id, datetime.now().isoformat()),
                )

    def _new_version_row(self, note: str, source_version_id: int | None, draft_id: int | None) -> int:
        version_no = self.conn.execute("SELECT COALESCE(MAX(version_no), 0) + 1 FROM schedule_versions").fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO schedule_versions(version_no,note,source_version_id,draft_id,created_at) VALUES(?,?,?,?,?)",
            (version_no, note, source_version_id, draft_id, datetime.now().isoformat()),
        )
        return int(cur.lastrowid)

    def _snapshot_live_into(self, version_id: int) -> None:
        self.conn.execute(
            "INSERT INTO version_slots(version_id,air_date,start_time,duration_minutes,program_id,region) "
            f"SELECT ?,air_date,start_time,duration_minutes,program_id,region FROM slots WHERE {LIVE_SLOT}",
            (version_id,),
        )

    def validate_draft(self, draft_id: int) -> list[str]:
        """Dry-run publishing and return the list of problems (empty = clean)."""
        self._require_open_draft(draft_id)
        scopes = self._draft_scopes(draft_id)
        if not scopes:
            return ["草案没有任何排期"]
        errors: list[str] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            version_id = self._new_version_row("（发布前校验）", None, draft_id)
            for air_date, region in sorted(scopes):
                self.conn.execute("SAVEPOINT draft_scope")
                try:
                    self._apply_scope(draft_id, air_date, region, version_id)
                except DomainError as exc:
                    errors.append(f"{air_date} {region}: {exc}")
                finally:
                    self.conn.execute("ROLLBACK TO SAVEPOINT draft_scope")
                    self.conn.execute("RELEASE SAVEPOINT draft_scope")
        finally:
            self.conn.rollback()
        return errors

    def publish_draft(self, draft_id: int, note: str = "") -> dict:
        """Validate the draft and publish it as a new immutable version.

        The whole change is atomic: on any validation error the live plan is
        left untouched and no version is created.
        """
        with self.transaction():
            draft = self.conn.execute("SELECT * FROM schedule_drafts WHERE id=?", (draft_id,)).fetchone()
            if not draft:
                raise DomainError("草案不存在")
            if draft["status"] != "open":
                raise DomainError("草案已发布或已废弃，不能重复发布")
            scopes = self._draft_scopes(draft_id)
            if not scopes:
                raise DomainError("草案没有任何排期，无法发布")
            version_id = self._new_version_row(note.strip(), draft["base_version_id"], draft_id)
            for air_date, region in sorted(scopes):
                self._apply_scope(draft_id, air_date, region, version_id)
            self._snapshot_live_into(version_id)
            self.conn.execute(
                "UPDATE schedule_drafts SET status='published', published_version_id=?, published_at=? WHERE id=?",
                (version_id, datetime.now().isoformat(), draft_id),
            )
        return self.get_version(version_id)

    def _version_keys(self, version_id: int) -> Counter:
        rows = self.conn.execute(
            "SELECT air_date, start_time, program_id, region FROM version_slots WHERE version_id=?", (version_id,)
        ).fetchall()
        return Counter((row["air_date"], row["start_time"], row["program_id"], row["region"]) for row in rows)

    def list_versions(self, limit: int = 5) -> list[dict]:
        """Recent versions, newest first, each with a change summary against
        its predecessor so the page can show what actually changed."""
        rows = [dict(row) for row in self.conn.execute(
            "SELECT v.*, d.title AS draft_title FROM schedule_versions v "
            "LEFT JOIN schedule_drafts d ON d.id=v.draft_id ORDER BY v.version_no DESC LIMIT ?",
            (limit + 1,),
        ).fetchall()]
        keys = [self._version_keys(row["id"]) for row in rows]
        versions = []
        for index, row in enumerate(rows[:limit]):
            current = keys[index]
            previous = keys[index + 1] if index + 1 < len(rows) else Counter()
            row["slot_count"] = sum(current.values())
            row["added"] = sum((current - previous).values())
            row["removed"] = sum((previous - current).values())
            versions.append(row)
        return versions

    def get_version(self, version_id: int) -> dict:
        row = self.conn.execute(
            "SELECT v.*, d.title AS draft_title FROM schedule_versions v "
            "LEFT JOIN schedule_drafts d ON d.id=v.draft_id WHERE v.id=?", (version_id,)
        ).fetchone()
        if not row:
            raise DomainError("版本不存在")
        version = dict(row)
        version["slots"] = [dict(r) for r in self.conn.execute(
            "SELECT vs.*, p.title, p.kind FROM version_slots vs JOIN programs p ON p.id=vs.program_id "
            "WHERE vs.version_id=? ORDER BY vs.air_date, vs.start_time", (version_id,)
        ).fetchall()]
        return version

    # ------------------------------------------------------------------
    # Playout and reconciliation
    # ------------------------------------------------------------------

    def record_playout(self, slot_id: int, actual_start: str, actual_duration_minutes: int,
                       actual_program_id: int | None = None, note: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM slots WHERE id=?", (slot_id,)).fetchone():
            raise DomainError("排期不存在")
        if actual_duration_minutes < 0:
            raise DomainError("实际时长不能为负数")
        _minutes(actual_start)
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,created_at) VALUES(?,?,?,?,?,?)",
                (slot_id, actual_start, actual_duration_minutes, actual_program_id, note, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def reconcile_date(self, air_date: str) -> list[dict]:
        """Compare the latest playout per slot with the plan and persist exceptions.

        Superseded slots are included whenever a playout log references them,
        so rolling back to an older version never hides what actually aired.
        """
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        with self.transaction():
            self.conn.execute("DELETE FROM reconciliation_exceptions WHERE air_date=?", (air_date,))
            version_nos = {row["id"]: row["version_no"] for row in self.conn.execute("SELECT id, version_no FROM schedule_versions")}
            slots = self.conn.execute(
                "SELECT s.*, p.title, p.sponsor, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.air_date=? AND ((s.status!='cancelled' AND s.superseded_by_version_id IS NULL) "
                "OR EXISTS (SELECT 1 FROM playout_logs pl WHERE pl.slot_id=s.id)) ORDER BY s.start_time", (air_date,)
            ).fetchall()
            exceptions: list[tuple[int, str, str]] = []
            for slot in slots:
                suffix = ""
                if slot["superseded_by_version_id"]:
                    source = version_nos.get(slot["version_id"])
                    target = version_nos.get(slot["superseded_by_version_id"])
                    suffix = f"（排期源自版本v{source if source else '旧数据'}，已被版本v{target}取代）"
                log = self.conn.execute(
                    "SELECT * FROM playout_logs WHERE slot_id=? ORDER BY id DESC LIMIT 1", (slot["id"],)
                ).fetchone()
                if not log:
                    exceptions.append((slot["id"], "missed", "没有实播记录" + suffix))
                    continue
                actual_program_id = log["actual_program_id"] or slot["program_id"]
                if actual_program_id != slot["program_id"]:
                    exceptions.append((slot["id"], "wrong_program", f"计划节目 #{slot['program_id']}，实播节目 #{actual_program_id}" + suffix))
                delta = log["actual_duration_minutes"] - slot["duration_minutes"]
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot["id"], kind, f"与计划相差 {delta:+d} 分钟" + suffix))
                actual = self.conn.execute(
                    "SELECT p.* FROM programs p WHERE p.id=?", (actual_program_id,)
                ).fetchone()
                if actual:
                    region_ok = self.conn.execute(
                        "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (actual_program_id, slot["region"])
                    ).fetchone()
                    if not region_ok or not (actual["start_date"] <= air_date <= actual["end_date"]):
                        exceptions.append((slot["id"], "out_of_license", "实播节目超出地区或日期授权" + suffix))
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
        slots = [dict(row) for row in self.conn.execute(
            "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
            "WHERE s.status!='cancelled' AND s.superseded_by_version_id IS NULL ORDER BY s.air_date, s.start_time"
        ).fetchall()]
        versions = self.list_versions()
        return {
            "programs": programs,
            "slots": slots,
            "exceptions": [dict(row) for row in self.conn.execute(
                "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
            ).fetchall()],
            "current_version": versions[0] if versions else None,
            "versions": versions,
            "drafts": self.list_drafts(),
        }
