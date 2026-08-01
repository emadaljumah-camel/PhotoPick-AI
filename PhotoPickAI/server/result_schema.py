from __future__ import annotations

from server.models import GroupDecision, JobResult, PhotoGroup


def group_decision_errors(source_group: PhotoGroup, decisions: list[GroupDecision]) -> list[str]:
    errors: list[str] = []
    expected_photos = source_group.photos
    expected_set = set(expected_photos)
    if not decisions:
        return [f"{source_group.group_id} has no decisions"]
    if len(expected_set) != len(expected_photos):
        errors.append(f"{source_group.group_id} contains duplicate source photo ids")

    seen_photos: set[str] = set()
    seen_decision_ids: set[str] = set()
    for decision in decisions:
        photo_ids = decision.photos
        photo_set = set(photo_ids)
        representatives = set(decision.representatives)
        rejected = set(decision.rejected)
        if decision.group_id in seen_decision_ids:
            errors.append(f"{source_group.group_id} repeats decision id {decision.group_id}")
        seen_decision_ids.add(decision.group_id)
        if not photo_ids or len(photo_set) != len(photo_ids):
            errors.append(f"{decision.group_id} has no photos or repeats a photo")
        if seen_photos & photo_set:
            errors.append(f"{source_group.group_id} assigns photos more than once: {sorted(seen_photos & photo_set)}")
        seen_photos.update(photo_set)
        if not representatives:
            errors.append(f"{decision.group_id} has no representative")
        if not representatives <= photo_set or not rejected <= photo_set:
            errors.append(f"{decision.group_id} has selections outside its photos")
        if representatives & rejected:
            errors.append(f"{decision.group_id} has photos both representative and rejected")
        if representatives | rejected != photo_set:
            errors.append(f"{decision.group_id} does not classify every photo")

    if seen_photos != expected_set:
        missing = expected_set - seen_photos
        unknown = seen_photos - expected_set
        errors.append(f"{source_group.group_id} decision coverage mismatch; missing={sorted(missing)} unknown={sorted(unknown)}")
    return errors


def validate_result(result: JobResult, selected_photo_ids: set[str]) -> JobResult:
    errors = list(result.errors)
    seen_group_ids: set[str] = set()
    seen_photo_ids: set[str] = set()
    for group in result.groups:
        group_ids = set(group.photos)
        reps = set(group.representatives)
        rejected = set(group.rejected)
        if group.group_id in seen_group_ids:
            errors.append(f"Duplicate result group id: {group.group_id}")
        seen_group_ids.add(group.group_id)
        if len(group_ids) != len(group.photos):
            errors.append(f"{group.group_id} repeats photo ids")
        duplicates = seen_photo_ids & group_ids
        if duplicates:
            errors.append(f"Photos appear in multiple result decisions: {sorted(duplicates)}")
        seen_photo_ids.update(group_ids)
        unknown = (group_ids | reps | rejected) - selected_photo_ids
        if unknown:
            errors.append(f"{group.group_id} contains unknown photo ids: {sorted(unknown)}")
        if reps & rejected:
            errors.append(f"{group.group_id} has photos both representative and rejected: {sorted(reps & rejected)}")
        if not reps <= group_ids or not rejected <= group_ids:
            errors.append(f"{group.group_id} has selections outside its photos")
        if reps | rejected != group_ids:
            errors.append(f"{group.group_id} does not classify every photo")
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
