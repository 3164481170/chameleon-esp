"""类名 -> 类别规则。

资源启发（来自游戏 pak/utoc 的资产名）：
    猎人阵营：BP_FirstPersonCharacter_cLeon_Character_Hunter[_Default|_Cube|_Fukuyoka]
    幸存者阵营：BP_FirstPersonCharacter_cLeon_Character_Survivor[_Default_Base|_Cube|_Fukuyoka]
    其他：BP_FirstPersonCharacter_Main / _LINK / ABP_MainCharacter_PaintMan / ABP_Penguin 等
    状态：BP_FirstPersonPlayerState[_Online|_LINK|_cLeon]、BP_GameState_cLeon、BP_PlayerController_cLeon

规则全部以正则写在 DEFAULTS 里，可以直接改，也可以用 --filter 里的自定义 JSON 覆盖。
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

HIDER = "hider"
HUNTER = "hunter"
DUMMY = "dummy"
OTHER = "other"
SKIP = "skip"

# 顺序敏感：先匹配的赢
DEFAULTS: List[Tuple[str, str]] = [
    # --- 噪声：静态 UI / 材质 / 世界静态对象，不画 ---
    (r"(?i)^(WBP_|UUserWidget|WidgetBlueprint|Default__|SKM_|SM_|MI_|M_|T_|StaticMesh|SkeletalMesh)", SKIP),
    (r"(?i)(HUD|Overlay|Menu|UI_|Slate|HUDWidget)", SKIP),

    # --- 阵营判定：优先 Survivor / Hunter 关键标签 ---
    (r"(?i)(Survivor|Hider|Runner)", HIDER),
    (r"(?i)(Hunter|Seeker|Killer)", HUNTER),

    # --- 其余角色/Pawn：兜底放到 other，交给用户开关 ---
    (r"(?i)(Pawn|Character|PlayerState)", OTHER),
]


class Filter:
    def __init__(self, rules: Optional[List[Tuple[str, str]]] = None):
        self.rules: List[Tuple[str, str, "re.Pattern[str]"]] = []
        for pattern, tag in (rules or DEFAULTS):
            try:
                self.rules.append((pattern, tag, re.compile(pattern)))
            except re.error:
                continue

    def classify(self, class_name: str, object_name: str = "", full_name: str = "") -> str:
        text = " | ".join(x for x in (class_name, object_name, full_name) if x)
        if not text:
            return SKIP
        for _pattern, tag, rx in self.rules:
            if rx.search(text):
                return tag
        return OTHER

    def add(self, pattern: str, tag: str) -> None:
        self.rules.append((pattern, tag, re.compile(pattern)))

    @classmethod
    def from_file(cls, path: str) -> "Filter":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return cls()
        rules: List[Tuple[str, str]] = []
        for pattern, tag in (data.get("rules") or []):
            rules.append((str(pattern), str(tag)))
        return cls(rules or DEFAULTS)


def default_rules_json() -> str:
    return json.dumps({"rules": [{"pattern": p, "tag": t} for p, t in DEFAULTS]},
                      ensure_ascii=False, indent=2)
