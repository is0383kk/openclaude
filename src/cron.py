"""OpenClaude Cron スケジューラ - apscheduler による定期ジョブ管理。

CronJob データクラスとスケジューラを提供する。
ジョブ定義は jobs.json に永続化され、デーモン再起動後も自動復元される。
"""

import asyncio
import json
import logging
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

_logger = logging.getLogger(__name__)

try:
    from .config import CRON_DIR, CRON_JOBS_FILE, CRON_RUNS_DIR
except ImportError:
    _pkg_root = str(Path(__file__).parent.parent)
    import sys

    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    from src.config import CRON_DIR, CRON_JOBS_FILE, CRON_RUNS_DIR


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------


@dataclass
class CronJob:
    """Cron ジョブの定義を表すデータクラス。"""

    id: str
    name: str
    schedule: str
    session_id: str
    message: str
    enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_run_at: str | None = None
    last_run_status: str | None = None  # "success" | "error" | None


# ---------------------------------------------------------------------------
# スケジューラ
# ---------------------------------------------------------------------------


class CronScheduler:
    """apscheduler を使って Cron ジョブを管理するクラス。"""

    def __init__(self, execute_fn: Callable[[str, str, str], Awaitable[None]]) -> None:
        """スケジューラを初期化する。

        Args:
            execute_fn: ジョブ実行時に呼び出す非同期関数。
                        シグネチャ: execute_fn(job_id, session_id, message)
        """
        self._execute_fn = execute_fn
        self._jobs: dict[str, CronJob] = {}
        self._scheduler = AsyncIOScheduler()

    async def start(self) -> None:
        """永続化ファイルからジョブを読み込み、スケジューラを起動する。"""
        CRON_DIR.mkdir(parents=True, exist_ok=True)
        CRON_RUNS_DIR.mkdir(parents=True, exist_ok=True)

        for job in self._load_jobs():
            self._jobs[job.id] = job
            if job.enabled:
                self._register_job(job)

        self._scheduler.start()
        _logger.info("CronScheduler started with %d job(s)", len(self._jobs))

    async def stop(self) -> None:
        """スケジューラを停止する。"""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        _logger.info("CronScheduler stopped")

    def add_job(self, name: str | None, schedule: str, session_id: str, message: str) -> CronJob:
        """ジョブを追加してスケジューラに登録し、永続化する。

        Args:
            name: ジョブの表示名。None の場合は "job-<id>" を使用する。
            schedule: 5フィールド cron 式（例: "0 9 * * *"）。
            session_id: 送信先セッション alias。
            message: 送信メッセージ本文。

        Returns:
            追加された CronJob。

        Raises:
            ValueError: cron 式が不正な場合。
        """
        # バリデーション兼トリガー生成（不正な cron 式は ValueError を送出する）
        trigger = CronTrigger.from_crontab(schedule)

        job_id = secrets.token_hex(4)
        job = CronJob(
            id=job_id,
            name=name if name is not None else f"job-{job_id}",
            schedule=schedule,
            session_id=session_id,
            message=message,
        )
        self._jobs[job_id] = job
        self._register_job(job, trigger)
        self._save_jobs()

        _logger.info("cron_add: id=%s, name=%s, schedule=%s, session=%s", job_id, job.name, schedule, session_id)
        return job

    def list_jobs(self) -> list[CronJob]:
        """ジョブ一覧を返す。"""
        return list(self._jobs.values())

    def delete_job(self, job_id: str) -> None:
        """ジョブをスケジューラから削除し、永続化ファイルからも削除する。

        Args:
            job_id: 削除するジョブの ID。

        Raises:
            ValueError: job_id が存在しない場合。
        """
        if job_id not in self._jobs:
            raise ValueError(f"Job not found: {job_id}")

        if self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id)

        del self._jobs[job_id]
        self._save_jobs()
        _logger.info("cron_delete: id=%s", job_id)

    async def run_job_now(self, job_id: str) -> None:
        """ジョブを非同期で即時実行する。

        Args:
            job_id: 実行するジョブの ID。

        Raises:
            ValueError: job_id が存在しない場合。
        """
        if job_id not in self._jobs:
            raise ValueError(f"Job not found: {job_id}")

        asyncio.create_task(self._execute_job(job_id))
        _logger.info("cron_run: id=%s (manual trigger)", job_id)

    # ------------------------------------------------------------------
    # 内部メソッド
    # ------------------------------------------------------------------

    def _register_job(self, job: CronJob, trigger: CronTrigger | None = None) -> None:
        """ジョブを apscheduler に登録する。

        Args:
            job: 登録するジョブ。
            trigger: 使用する CronTrigger。None の場合は job.schedule から生成する（復元時）。
        """
        if trigger is None:
            trigger = CronTrigger.from_crontab(job.schedule)
        self._scheduler.add_job(self._execute_job, trigger, id=job.id, args=[job.id], replace_existing=True)

    async def _execute_job(self, job_id: str) -> None:
        """スケジューラから呼び出されるジョブ実行関数。

        execute_fn を呼び出し、last_run_at / last_run_status を更新する。
        実行履歴を runs/<job_id>.jsonl に追記する。
        """
        job = self._jobs.get(job_id)
        if job is None:
            _logger.warning("cron_execute: job not found: %s", job_id)
            return

        started_at = datetime.now(UTC).isoformat()
        _logger.info("cron_execute: start, id=%s, session=%s", job_id, job.session_id)

        status: str
        error_msg: str | None = None
        try:
            await self._execute_fn(job_id, job.session_id, job.message)
            status = "success"
        except Exception as e:
            status = "error"
            error_msg = str(e)
            _logger.error("cron_execute: error, id=%s, error=%s", job_id, e)

        finished_at = datetime.now(UTC).isoformat()

        # last_run_at / last_run_status を更新
        if job_id in self._jobs:
            self._jobs[job_id].last_run_at = finished_at
            self._jobs[job_id].last_run_status = status
            self._save_jobs()

        # 実行履歴を JSONL に追記
        record: dict[str, Any] = {
            "job_id": job_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
        }
        if error_msg is not None:
            record["error"] = error_msg
        self._append_run_log(job_id, record)

        _logger.info("cron_execute: done, id=%s, status=%s", job_id, status)

    def _load_jobs(self) -> list[CronJob]:
        """jobs.json からジョブ一覧を読み込む。"""
        try:
            data = json.loads(CRON_JOBS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            jobs = []
            for item in data:
                try:
                    jobs.append(
                        CronJob(
                            id=item["id"],
                            name=item["name"],
                            schedule=item["schedule"],
                            session_id=item["session_id"],
                            message=item["message"],
                            enabled=item.get("enabled", True),
                            created_at=item.get("created_at", ""),
                            last_run_at=item.get("last_run_at"),
                            last_run_status=item.get("last_run_status"),
                        )
                    )
                except (KeyError, TypeError) as e:
                    _logger.warning("cron_load: skipping malformed job entry: %s", e)
            return jobs
        except FileNotFoundError:
            _logger.debug("cron_load: jobs.json not found, starting with empty jobs")
        except Exception as e:
            _logger.warning("cron_load: failed to load jobs: %s", e)
        return []

    def _save_jobs(self) -> None:
        """self._jobs を jobs.json にアトミックに書き込む。"""
        try:
            CRON_DIR.mkdir(parents=True, exist_ok=True)
            data = [asdict(job) for job in self._jobs.values()]
            fd, tmp = tempfile.mkstemp(dir=str(CRON_DIR), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, str(CRON_JOBS_FILE))
        except Exception as e:
            _logger.warning("cron_save: failed to save jobs: %s", e)

    def _append_run_log(self, job_id: str, record: dict[str, Any]) -> None:
        """runs/<job_id>.jsonl に実行レコードを追記する。

        Args:
            job_id: ジョブ ID。
            record: 追記する実行レコード。
        """
        try:
            log_path = CRON_RUNS_DIR / f"{job_id}.jsonl"
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            _logger.warning("cron_run_log: failed to append run log for %s: %s", job_id, e)
