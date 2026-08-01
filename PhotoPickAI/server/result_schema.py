from __future__ import annotations

from server.models import GroupDecision, JobResult


def validate_result(result: JobResult, selected_photo_ids: set[str]) -> JobResult:
    errors = list(result.errors)
    for group in result.groups:
        group_ids = set(group.photos)
        reps = set(group.representatives)
        rejected = set(group.rejected)
        unknown = (group_ids | reps | rejected) - selected_photo_ids
        if unknown:
            errors.append(f"{group.group_id} contains unknown photo ids: {sorted(unknown)}")
        if reps & rejected:
            errors.append(f"{group.group_id} has photos both representative and rejected: {sorted(reps & rejected)}")
        if not reps:
            errors.append(f"{group.group_id} has no representative")
    return JobResult(
        job_id=result.job_id,
        status="completed" if not errors else "failed",
        groups=result.groups,
        ungrouped=result.ungrouped,
        skipped=result.skipped,
        errors=errors,
    )
