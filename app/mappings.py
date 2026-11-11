"""新旧 OAI 标识对照的整批登记。

登记规则：
- 旧号必须已入库且处于 active（在架）；
- 新号不得已入库（无论书目当前状态）；
- 任一标识只能在“本批”或“已登记对照”中出现一次，不能重复参与换号；
- 任一冲突都会指出对应标识，并让整批登记失败（不写入任何一行）。

登记只写 identifier_mappings，不触碰书目内容、书目状态或采集进度。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import db


@dataclass(frozen=True)
class MappingConflict:
    identifier: str
    reason: str
    message: str

    def as_dict(self) -> dict:
        return {
            "identifier": self.identifier,
            "reason": self.reason,
            "message": self.message,
        }


class MappingConflictError(Exception):
    """本批存在冲突；事务回滚，整批不登记。"""

    def __init__(self, conflicts: list[MappingConflict]) -> None:
        self.conflicts = conflicts
        super().__init__("存在冲突，整批未登记")


def register_mappings(conn, pairs: list[tuple[str, str]]) -> list[dict]:
    """在一个已开启的事务内校验并登记整批对照。

    返回本批登记成功的对照（含 registered_at）。发生冲突时抛出
    MappingConflictError；调用方负责回滚，因此不会留下半批数据。
    """
    conflicts: list[MappingConflict] = []
    seen_in_batch: dict[str, str] = {}

    existing_ids: set[str] = set()
    for mapping in db.list_identifier_mappings(conn):
        existing_ids.add(mapping["old_identifier"])
        existing_ids.add(mapping["new_identifier"])

    registered_at = db.utc_now_iso()
    accepted: list[tuple[str, str]] = []

    for old_identifier, new_identifier in pairs:
        pair_conflicts: list[MappingConflict] = []

        if old_identifier == new_identifier:
            pair_conflicts.append(MappingConflict(
                old_identifier,
                "duplicate_in_batch",
                "同一对中的新旧标识不能相同",
            ))

        # 本批内去重：一个标识无论作为旧号还是新号，都只能出现一次
        for identifier, role in (
            (old_identifier, "旧号"),
            (new_identifier, "新号"),
        ):
            if identifier in seen_in_batch:
                pair_conflicts.append(MappingConflict(
                    identifier,
                    "duplicate_in_batch",
                    f"{role}在本批中重复参与换号",
                ))
            else:
                seen_in_batch[identifier] = role

            # 已登记的对照同样不允许该标识再参与换号
            if identifier in existing_ids:
                pair_conflicts.append(MappingConflict(
                    identifier,
                    "already_registered",
                    f"{role}已出现在已登记的对照中",
                ))

        # 旧号必须在架
        old_status = db.get_bib_status(conn, old_identifier)
        if old_status is None:
            pair_conflicts.append(MappingConflict(
                old_identifier,
                "old_not_found",
                "旧号不在联合目录中",
            ))
        elif old_status != "active":
            pair_conflicts.append(MappingConflict(
                old_identifier,
                "old_not_active",
                f"旧号当前状态为 {old_status}，不在架",
            ))

        # 新号不能已入库（active 或 withdrawn 都算已入库）
        if db.get_bib_status(conn, new_identifier) is not None:
            pair_conflicts.append(MappingConflict(
                new_identifier,
                "new_already_in_catalog",
                "新号已入库，不能重复登记",
            ))

        if pair_conflicts:
            conflicts.extend(pair_conflicts)
        else:
            accepted.append((old_identifier, new_identifier))

    if conflicts:
        # 同一标识的同一原因只报一次，保持登记顺序
        deduped: list[MappingConflict] = []
        seen_keys: set[tuple[str, str]] = set()
        for conflict in conflicts:
            key = (conflict.identifier, conflict.reason)
            if key not in seen_keys:
                seen_keys.add(key)
                deduped.append(conflict)
        raise MappingConflictError(deduped)

    for old_identifier, new_identifier in accepted:
        db.insert_identifier_mapping(
            conn,
            old_identifier=old_identifier,
            new_identifier=new_identifier,
            registered_at=registered_at,
        )

    return [
        {
            "old_identifier": old_identifier,
            "new_identifier": new_identifier,
            "registered_at": registered_at,
        }
        for old_identifier, new_identifier in accepted
    ]
