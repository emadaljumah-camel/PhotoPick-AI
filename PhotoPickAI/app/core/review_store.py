from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.core.models import DuplicateGroup, ReviewDecision
from app.utils.file_utils import ensure_directory
from app.utils.json_utils import read_json_file, write_json_file


class ReviewStore:
    """Persist metadata, grouping, and review decisions."""

    def __init__(self, root: Path) -> None:
        self.root = ensure_directory(root)

    def write_metadata_cache(self, name: str, records: list[dict[str, Any]]) -> Path:
        path = self.root / f"{name}.json"
        write_json_file(path, {"images": records})
        return path

    def write_groups(self, name: str, groups: list[DuplicateGroup]) -> Path:
        path = self.root / f"{name}.json"
        write_json_file(path, {"groups": [group.to_dict() for group in groups]})
        return path

    def read_groups(self, name: str) -> list[DuplicateGroup]:
        payload = read_json_file(self.root / f"{name}.json", default={})
        raw_groups = payload.get("groups", []) if isinstance(payload, dict) else []
        return [DuplicateGroup.from_dict(item) for item in raw_groups if isinstance(item, dict)]

    def write_review_json(self, name: str, decisions: list[ReviewDecision]) -> Path:
        path = self.root / f"{name}.json"
        write_json_file(path, {"decisions": [decision.to_dict() for decision in decisions]})
        return path

    def read_review_json(self, name: str) -> list[ReviewDecision]:
        payload = read_json_file(self.root / f"{name}.json", default={})
        raw_decisions = payload.get("decisions", []) if isinstance(payload, dict) else []
        decisions: list[ReviewDecision] = []
        for item in raw_decisions:
            if not isinstance(item, dict):
                continue
            decisions.append(
                ReviewDecision(
                    group_id=item.get("group_id", ""),
                    winner=item.get("winner"),
                    alternates=list(item.get("alternates", [])),
                    rejected=list(item.get("rejected", [])),
                    reviewed=bool(item.get("reviewed", False)),
                    group_key=item.get("group_key", ""),
                    local_ai_selected=list(item.get("local_ai_selected", [])),
                    local_ai_reason=item.get("local_ai_reason", ""),
                    vision_llm_selected=list(item.get("vision_llm_selected", [])),
                    vision_llm_reason=item.get("vision_llm_reason", ""),
                    vision_llm_subgroups=list(item.get("vision_llm_subgroups", [])),
                    status=item.get("status", "Pending"),
                )
            )
        return decisions

    def write_review_csv(self, name: str, decisions: list[ReviewDecision]) -> Path:
        path = self.root / f"{name}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "group_id",
                    "group_key",
                    "winner",
                    "alternates",
                    "rejected",
                    "reviewed",
                    "status",
                    "local_ai_selected",
                    "local_ai_reason",
                    "vision_llm_selected",
                    "vision_llm_reason",
                    "vision_llm_subgroups",
                ],
            )
            writer.writeheader()
            for decision in decisions:
                writer.writerow(
                    {
                        "group_id": decision.group_id,
                        "group_key": decision.group_key,
                        "winner": decision.winner or "",
                        "alternates": ";".join(decision.alternates),
                        "rejected": ";".join(decision.rejected),
                        "reviewed": decision.reviewed,
                        "status": decision.status,
                        "local_ai_selected": ";".join(decision.local_ai_selected),
                        "local_ai_reason": decision.local_ai_reason,
                        "vision_llm_selected": ";".join(decision.vision_llm_selected),
                        "vision_llm_reason": decision.vision_llm_reason,
                        "vision_llm_subgroups": json.dumps(decision.vision_llm_subgroups, ensure_ascii=True),
                    }
                )
        return path

    def restore_group_review_state(
        self,
        groups: list[DuplicateGroup],
        decisions: list[ReviewDecision],
    ) -> list[DuplicateGroup]:
        """Apply previously saved review state onto newly built groups."""

        decisions_by_key = {decision.group_key: decision for decision in decisions if decision.group_key}
        decisions_by_id = {decision.group_id: decision for decision in decisions if decision.group_id}

        for group in groups:
            decision = decisions_by_key.get(group.group_key) or decisions_by_id.get(group.group_id)
            if decision is None:
                continue
            selected = [item for item in [decision.winner, *decision.alternates] if item]
            filenames = {image.filename for image in group.images}
            group.user_selected = [item for item in selected if item in filenames]
            group.local_ai_selected = [item for item in decision.local_ai_selected if item in filenames]
            group.local_ai_reason = decision.local_ai_reason
            group.vision_llm_selected = [item for item in decision.vision_llm_selected if item in filenames]
            group.vision_llm_reason = decision.vision_llm_reason
            group.vision_llm_subgroups = decision.vision_llm_subgroups
            group.status = decision.status or ("Reviewed" if decision.reviewed else "Pending")
        return groups
