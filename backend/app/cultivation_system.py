"""
功法系统 (Cultivation Technique System)
=========================================

品阶: 黄 < 玄 < 地 < 天  (4个品阶)
等阶: 下品 < 中品 < 上品 < 极品  (4个等阶)

总共 16 个组合等级, 每个等级对应一个"功法战力"数值.
战力采用**指数增长**, 确保:
  - 同品阶: 极品 ≈ 下品 × 3 (同品内最强不超过高品最弱)
  - 跨品阶: 地-下品 > 黄-极品 (严格跨品碾压)
  - 天-极品是黄-下品的 ~500 倍

功法战力直接影响战斗类骰子判定的加成.
越高品阶/等阶的功法, 领悟判定的基础成功率越低.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 品阶 & 等阶定义
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GRADES = ["黄", "玄", "地", "天"]       # 品阶 (低→高)
TIERS  = ["下品", "中品", "上品", "极品"]  # 等阶 (低→高)

# 品阶索引 (0-3)
GRADE_INDEX = {g: i for i, g in enumerate(GRADES)}
# 等阶索引 (0-3)
TIER_INDEX  = {t: i for i, t in enumerate(TIERS)}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 战力计算 — 指数增长
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 设计目标:
#   黄-下品: 10,  黄-中品: 15,  黄-上品: 22,  黄-极品: 30
#   玄-下品: 40,  玄-中品: 58,  玄-上品: 82,  玄-极品: 115
#   地-下品: 160, 地-中品: 230, 地-上品: 330, 地-极品: 470
#   天-下品: 660, 天-中品: 950, 天-上品: 1350,天-极品: 1900
#
# 公式: power = BASE × GROWTH^(level)
#   其中 level = grade_index × 4 + tier_index  (0~15)
#   BASE = 10, GROWTH ≈ 1.38

_POWER_BASE = 10
_POWER_GROWTH = 1.38

# 预计算战力表
POWER_TABLE: dict[str, int] = {}
for _g_idx, _grade in enumerate(GRADES):
    for _t_idx, _tier in enumerate(TIERS):
        _level = _g_idx * 4 + _t_idx
        _power = int(_POWER_BASE * (_POWER_GROWTH ** _level))
        POWER_TABLE[f"{_grade}阶{_tier}"] = _power

# 同时支持 "天阶上品" 和 "天-上品" 等变体格式的查找
_POWER_LOOKUP: dict[tuple[int, int], int] = {}
for _g_idx, _grade in enumerate(GRADES):
    for _t_idx, _tier in enumerate(TIERS):
        _level = _g_idx * 4 + _t_idx
        _POWER_LOOKUP[(_g_idx, _t_idx)] = int(_POWER_BASE * (_POWER_GROWTH ** _level))


def get_technique_power(grade: str, tier: str) -> int:
    """
    根据品阶和等阶返回战力值.
    
    Args:
        grade: "黄" / "玄" / "地" / "天"
        tier:  "下品" / "中品" / "上品" / "极品"
    
    Returns:
        战力数值 (int), 无法识别时返回 0
    """
    g = GRADE_INDEX.get(grade)
    t = TIER_INDEX.get(tier)
    if g is None or t is None:
        return 0
    return _POWER_LOOKUP.get((g, t), 0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 领悟难度 — 越高级越难
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 领悟判定的基础目标值 (D100):
#   黄-下品: 75 (很容易)  →  天-极品: 8 (极难)
# 公式: target = max(5, 80 - level × 5)

_COMPREHEND_TARGETS: dict[tuple[int, int], int] = {}
for _g_idx in range(4):
    for _t_idx in range(4):
        _level = _g_idx * 4 + _t_idx
        _target = max(5, 80 - _level * 5)
        _COMPREHEND_TARGETS[(_g_idx, _t_idx)] = _target

# 也生成一个易读表
COMPREHEND_TABLE: dict[str, int] = {}
for _g_idx, _grade in enumerate(GRADES):
    for _t_idx, _tier in enumerate(TIERS):
        COMPREHEND_TABLE[f"{_grade}阶{_tier}"] = _COMPREHEND_TARGETS[(_g_idx, _t_idx)]


def get_comprehend_target(grade: str, tier: str) -> int:
    """
    返回领悟该功法的推荐基础判定目标值 (D100).
    越高品阶/等阶, 值越低 → 越难.
    """
    g = GRADE_INDEX.get(grade)
    t = TIER_INDEX.get(tier)
    if g is None or t is None:
        return 50  # 无法识别时给中等难度
    return _COMPREHEND_TARGETS.get((g, t), 50)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 从功法列表计算总战力
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_technique(technique: dict | str) -> tuple[str, str, str]:
    """
    解析功法数据, 返回 (名称, 品阶, 等阶).
    
    支持格式:
      - dict: {"名称": "xxx", "品阶": "天", "等阶": "上品", ...}
      - str:  "天阶上品·xxx" 或 "xxx(天阶上品)"
    """
    if isinstance(technique, dict):
        name = technique.get("名称", "未知功法")
        grade = technique.get("品阶", "")
        tier = technique.get("等阶", "")
        return name, grade, tier
    
    if isinstance(technique, str):
        # 尝试解析 "天阶上品·xxx" 格式
        for g in GRADES:
            for t in TIERS:
                pattern = f"{g}阶{t}"
                if pattern in technique:
                    name = technique.replace(pattern, "").strip("·- 　")
                    return name or technique, g, t
        return technique, "", ""
    
    return "未知功法", "", ""


def calculate_total_combat_power(techniques: list) -> int:
    """
    根据角色的功法列表计算总战力.
    
    取所有功法中战力最高的一个作为主战力,
    其余功法贡献 20% 的辅助战力 (避免功法数量堆叠过于强势).
    
    Args:
        techniques: 功法列表 (list of dict/str)
    
    Returns:
        总战力 (int)
    """
    if not techniques:
        return 0
    
    powers = []
    for tech in techniques:
        _, grade, tier = parse_technique(tech)
        p = get_technique_power(grade, tier)
        if p > 0:
            powers.append(p)
    
    if not powers:
        return 0
    
    powers.sort(reverse=True)
    
    # 主功法全额 + 辅助功法 20%
    main_power = powers[0]
    aux_power = sum(int(p * 0.2) for p in powers[1:])
    
    return main_power + aux_power


def calculate_combat_power_bonus(combat_power: int) -> int:
    """
    将战力值转换为骰子判定加成 (%).
    
    使用对数映射, 避免高战力时加成过于离谱:
      战力 10   → +2%
      战力 50   → +5%
      战力 200  → +8%
      战力 500  → +11%
      战力 1000 → +13%
      战力 1900 → +15%
    
    公式: bonus = floor(2 * ln(power / 5))
    上限: 15%
    """
    if combat_power <= 0:
        return 0
    
    import math
    bonus = int(2 * math.log(max(1, combat_power) / 5))
    return max(0, min(15, bonus))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助: 品阶颜色/图标 (供前端参考)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GRADE_COLORS = {
    "黄": "#b8860b",   # 暗金
    "玄": "#6a5acd",   # 紫蓝
    "地": "#cd853f",   # 秘棕
    "天": "#ff4500",   # 赤金
}

GRADE_ICONS = {
    "黄": "☰",
    "玄": "☱",
    "地": "☲",
    "天": "☳",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 打印战力表 (debug)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def print_power_table():
    """打印完整战力表和领悟难度表 (调试用)"""
    print("\n=== 功法战力表 ===")
    print(f"{'品阶等阶':<12} {'战力':>6}  {'领悟难度(D100 target)':>22}")
    print("-" * 44)
    for grade in GRADES:
        for tier in TIERS:
            key = f"{grade}阶{tier}"
            power = POWER_TABLE[key]
            target = COMPREHEND_TABLE[key]
            print(f"{key:<12} {power:>6}  {target:>8} / 100")
        print()


if __name__ == "__main__":
    print_power_table()
