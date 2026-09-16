"""Job persistence, resume + rollback helpers (Motor-backed)."""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

from .models import DcteJob, DcteJobStatus


class JobManager:
    def __init__(self, jobs_col, transforms_col, events_col, reports_col) -> None:
        self.jobs = jobs_col
        self.transforms = transforms_col
        self.events = events_col
        self.reports = reports_col

    async def create(self, job: DcteJob) -> DcteJob:
        doc = job.model_dump()
        doc["_id"] = job.id
        await self.jobs.insert_one(doc)
        return job

    async def get(self, job_id: str) -> DcteJob | None:
        doc = await self.jobs.find_one({"_id": job_id})
        if not doc:
            return None
        doc.pop("_id", None)
        return DcteJob(**doc)

    async def list(self, tenant_id: str | None = None) -> list[DcteJob]:
        q: dict[str, Any] = {}
        if tenant_id:
            q["tenant_id"] = tenant_id
        out: list[DcteJob] = []
        async for doc in self.jobs.find(q).sort("created_at", -1):
            doc.pop("_id", None)
            out.append(DcteJob(**doc))
        return out

    async def update_status(
        self, job_id: str, status: DcteJobStatus | str, progress: float | None = None,
        error: str | None = None,
    ) -> None:
        upd: dict[str, Any] = {
            "status": status.value if isinstance(status, DcteJobStatus) else str(status),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if progress is not None:
            upd["progress"] = float(progress)
        if error is not None:
            upd["error"] = error
        await self.jobs.update_one({"_id": job_id}, {"$set": upd})

    async def append_event(self, evt: Any) -> None:
        doc = evt.model_dump()
        doc["_id"] = doc.pop("id")
        await self.events.insert_one(doc)

    async def append_transform(self, rec: Any) -> None:
        doc = rec.model_dump()
        doc["_id"] = doc.pop("id")
        await self.transforms.insert_one(doc)

    async def append_report(self, rep: Any) -> None:
        doc = rep.model_dump()
        doc["_id"] = doc.pop("id")
        await self.reports.insert_one(doc)

    async def events_for(self, job_id: str, limit: int = 200) -> list[dict]:
        out: list[dict] = []
        async for doc in self.events.find({"job_id": job_id}).sort("at", -1).limit(limit):
            doc.pop("_id", None)
            out.append(doc)
        return list(reversed(out))

    async def reports_for(self, job_id: str) -> list[dict]:
        out: list[dict] = []
        async for doc in self.reports.find({"job_id": job_id}):
            doc.pop("_id", None)
            out.append(doc)
        return out

    async def transforms_for(self, job_id: str) -> list[dict]:
        out: list[dict] = []
        async for doc in self.transforms.find({"job_id": job_id}):
            doc.pop("_id", None)
            out.append(doc)
        return out
