"""TRPG/跑团工具集 — 角色卡管理、属性检定、战斗辅助、随机表、团务管理。

数据按群隔离存储：
  角色卡:     <workspace>/trpg/<群号>/characters/<角色名>.json
  团务状态:   <workspace>/trpg/<群号>/game_session.json

支持 CoC 7th 和 D&D 5e 两种规则体系。
所有需要群上下文的工具都必须传入 group_id 参数。
（LLM 会从消息前缀【群聊XXXXXXXXX】中提取群号）
"""

import json
import os
import random
import datetime
from tools.base import Tool


# ── 角色卡 JSON 模板 ─────────────────────────────────────────

def _default_coc_sheet(name: str, player: str) -> dict:
    """生成默认 CoC 7th 空白角色卡。"""
    return {
        "name": name,
        "player": player,
        "system": "coc7",
        "age": 25,
        "occupation": "调查员",
        "attributes": {
            "STR": 50, "CON": 50, "SIZ": 50, "DEX": 50,
            "APP": 50, "INT": 50, "POW": 50, "EDU": 50,
        },
        "derived": {
            "HP": 10, "max_hp": 10,
            "SAN": 50, "max_san": 99,
            "LUCK": 50,
            "MP": 10, "max_mp": 10,
        },
        "skills": {},
        "inventory": [],
        "notes": "",
        "created_at": datetime.datetime.now().isoformat(),
    }


def _default_dnd_sheet(name: str, player: str) -> dict:
    """生成默认 D&D 5e 空白角色卡。"""
    return {
        "name": name,
        "player": player,
        "system": "dnd5e",
        "race": "人类",
        "class": "战士",
        "level": 1,
        "attributes": {
            "STR": 10, "DEX": 10, "CON": 10,
            "INT": 10, "WIS": 10, "CHA": 10,
        },
        "derived": {
            "HP": 10, "max_hp": 10,
            "AC": 10,
            "initiative_bonus": 0,
        },
        "skills": {},
        "inventory": [],
        "notes": "",
        "created_at": datetime.datetime.now().isoformat(),
    }


def _default_game_session(group_id: str) -> dict:
    """默认团务会话状态。"""
    return {
        "group_id": group_id,
        "active": False,
        "gm_name": "",
        "system": "coc7",
        "phase": "idle",  # idle / intro / exploration / combat / wrapup
        "scene": "",
        "turn_order": [],       # [{name, initiative, hp, is_npc}]
        "current_turn_index": 0,
        "round_number": 0,
        "npcs": [],             # [{name, hp, max_hp, ac, notes}]
        "notes": "",
        "updated_at": datetime.datetime.now().isoformat(),
    }


# ── 快速检测：某群是否有进行中的跑团 ─────────────────────────

def is_trpg_active(workspace: str, group_id: str) -> bool:
    """检查指定群是否有进行中的跑团游戏。

    供 qq_bot.py 在路由消息前调用：如果返回 True，
    全群消息统一路由到共享会话 `trpg_{group_id}`。
    """
    path = os.path.join(workspace, "trpg", str(group_id), "game_session.json")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("active", False)
    except Exception:
        return False


# ── 基类 ─────────────────────────────────────────────────────

class _TRPGBaseTool(Tool):
    """TRPG 工具基类：按群号隔离角色卡和团务数据。"""

    def __init__(self, workspace: str = "."):
        self.workspace = os.path.abspath(workspace)

    # ── 目录结构 ───────────────────────────────────────────

    def _group_dir(self, group_id: str) -> str:
        d = os.path.join(self.workspace, "trpg", str(group_id))
        os.makedirs(d, exist_ok=True)
        return d

    def _char_dir(self, group_id: str) -> str:
        d = os.path.join(self._group_dir(group_id), "characters")
        os.makedirs(d, exist_ok=True)
        return d

    def _char_path(self, group_id: str, name: str) -> str:
        safe = name.replace("/", "_").replace("\\", "_").replace(":", "_")
        return os.path.join(self._char_dir(group_id), f"{safe}.json")

    def _game_session_path(self, group_id: str) -> str:
        return os.path.join(self._group_dir(group_id), "game_session.json")

    # ── 角色卡读写 ─────────────────────────────────────────

    def _load_char(self, group_id: str, name: str) -> dict | None:
        path = self._char_path(group_id, name)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_char(self, group_id: str, name: str, data: dict):
        path = self._char_path(group_id, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _delete_char_file(self, group_id: str, name: str):
        path = self._char_path(group_id, name)
        if os.path.isfile(path):
            os.remove(path)

    def _list_chars(self, group_id: str) -> list[str]:
        d = self._char_dir(group_id)
        names = []
        if not os.path.isdir(d):
            return names
        for fname in os.listdir(d):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(d, fname), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    names.append(data.get("name", fname[:-5]))
                except Exception:
                    names.append(fname[:-5])
        return sorted(names)

    # ── 团务会话读写 ──────────────────────────────────────

    def _load_game_session(self, group_id: str) -> dict:
        path = self._game_session_path(group_id)
        if not os.path.isfile(path):
            return _default_game_session(group_id)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_game_session(self, gs: dict):
        gs["updated_at"] = datetime.datetime.now().isoformat()
        path = self._game_session_path(gs["group_id"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(gs, f, ensure_ascii=False, indent=2)

    # ── 通用工具方法 ──────────────────────────────────────

    @staticmethod
    def _mod(value: int) -> int:
        return (value - 10) // 2

    @staticmethod
    def _mod_str(value: int) -> str:
        m = (value - 10) // 2
        return f"+{m}" if m >= 0 else str(m)


# ── 1. 创建角色卡 ────────────────────────────────────────────

class CreateCharacterTool(_TRPGBaseTool):
    """创建 TRPG 角色卡。"""

    @property
    def name(self) -> str:
        return "trpg_create_character"

    @property
    def description(self) -> str:
        return (
            "创建一张 TRPG 角色卡。创建后可用其他跑团工具进行属性检定、修改 HP/SAN 等。"
            "参数: group_id=群号(必填，从消息前缀【群聊XXXXXXXXX】中获取), "
            "name=角色名(必填), player=玩家名(可选), system='coc7'或'dnd5e'(默认coc7), "
            "attributes=自定义属性字典(可选，JSON字符串如'{\"STR\":70,\"DEX\":60}'), "
            "occupation=职业(coc7可选), race=种族(dnd5e可选), class_name=职业(dnd5e可选)"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号（必填），从消息开头的【群聊XXXXXXXXX】中提取"},
                "name": {"type": "string", "description": "角色名称（必填）"},
                "player": {"type": "string", "description": "玩家名字，默认取群友昵称"},
                "system": {"type": "string", "enum": ["coc7", "dnd5e"], "description": "规则系统，默认 coc7"},
                "attributes": {"type": "string", "description": "自定义属性值，JSON格式字符串"},
                "occupation": {"type": "string", "description": "职业（仅 coc7）"},
                "race": {"type": "string", "description": "种族（仅 dnd5e）"},
                "class_name": {"type": "string", "description": "职业（仅 dnd5e）"},
            },
            "required": ["group_id", "name"],
        }

    def execute(
        self, group_id: str, name: str, player: str = "", system: str = "coc7",
        attributes: str = "", occupation: str = "", race: str = "", class_name: str = "",
    ) -> str:
        existing = self._load_char(group_id, name)
        if existing:
            return f"⚠️ 本群已有角色「{name}」喵！换个名字吧~"

        if system == "dnd5e":
            sheet = _default_dnd_sheet(name, player)
            if race:
                sheet["race"] = race
            if class_name:
                sheet["class"] = class_name
        else:
            sheet = _default_coc_sheet(name, player)
            if occupation:
                sheet["occupation"] = occupation

        if attributes:
            try:
                custom_attrs = json.loads(attributes)
                sheet["attributes"].update(custom_attrs)
                if system == "coc7":
                    con = sheet["attributes"].get("CON", 50)
                    siz = sheet["attributes"].get("SIZ", 50)
                    pow_ = sheet["attributes"].get("POW", 50)
                    hp = (con + siz) // 10
                    sheet["derived"]["HP"] = hp
                    sheet["derived"]["max_hp"] = hp
                    sheet["derived"]["SAN"] = pow_
                    sheet["derived"]["MP"] = pow_ // 5
                    sheet["derived"]["max_mp"] = pow_ // 5
                else:
                    con = sheet["attributes"].get("CON", 10)
                    sheet["derived"]["HP"] = sheet["level"] + self._mod(con)
                    sheet["derived"]["max_hp"] = sheet["derived"]["HP"]
            except json.JSONDecodeError:
                return "⚠️ attributes 参数 JSON 格式错误喵！"

        self._save_char(group_id, name, sheet)
        out = self._format_sheet(sheet)
        return f"✨ 角色「{name}」创建成功喵！\n\n{out}"

    def _format_sheet(self, sheet: dict) -> str:
        system = sheet.get("system", "coc7")
        lines = [f"📜 {sheet['name']}"]
        if sheet.get("player"):
            lines.append(f"   玩家: {sheet['player']}")
        if system == "dnd5e":
            lines.append(f"   {sheet.get('race','?')} {sheet.get('class','?')} Lv.{sheet.get('level',1)}")
        else:
            lines.append(f"   {sheet.get('occupation','?')} | 年龄 {sheet.get('age','?')}")
        lines.append("")
        lines.append("   📊 属性:")
        for attr, val in sheet["attributes"].items():
            lines.append(f"      {attr}: {val} ({self._mod_str(val)})")
        lines.append("")
        lines.append("   ❤️ 状态:")
        for k, v in sheet.get("derived", {}).items():
            lines.append(f"      {k}: {v}")
        return "\n".join(lines)


# ── 2. 查看角色卡 ────────────────────────────────────────────

class ViewCharacterTool(_TRPGBaseTool):
    """查看已有角色卡。"""

    @property
    def name(self) -> str:
        return "trpg_view_character"

    @property
    def description(self) -> str:
        return "查看指定角色的完整角色卡。参数: group_id=群号(必填), name=角色名(必填)"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号（必填）"},
                "name": {"type": "string", "description": "要查看的角色名称"},
            },
            "required": ["group_id", "name"],
        }

    def execute(self, group_id: str, name: str) -> str:
        sheet = self._load_char(group_id, name)
        if not sheet:
            return f"⚠️ 本群找不到角色「{name}」喵… 先用 trpg_create_character 创建吧！"
        return CreateCharacterTool._format_sheet(self, sheet)


# ── 3. 列出所有角色 ──────────────────────────────────────────

class ListCharactersTool(_TRPGBaseTool):
    """列出本群所有角色。"""

    @property
    def name(self) -> str:
        return "trpg_list_characters"

    @property
    def description(self) -> str:
        return "列出当前群内所有已创建的 TRPG 角色。参数: group_id=群号(必填)"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号（必填）"},
            },
            "required": ["group_id"],
        }

    def execute(self, group_id: str) -> str:
        names = self._list_chars(group_id)
        if not names:
            return "📭 本群还没有角色喵~ 用 trpg_create_character 来创建第一个吧！"

        lines = ["📋 本群角色列表:"]
        for n in names:
            sheet = self._load_char(group_id, n)
            if sheet:
                system = sheet.get("system", "coc7")
                hp = sheet.get("derived", {}).get("HP", "?")
                max_hp = sheet.get("derived", {}).get("max_hp", "?")
                player = sheet.get("player", "")
                pinfo = f" ({player})" if player else ""
                lines.append(f"   🎭 {n}{pinfo} [{system}] HP:{hp}/{max_hp}")
            else:
                lines.append(f"   🎭 {n}")
        return "\n".join(lines)


# ── 4. 属性/技能检定 ────────────────────────────────────────

class AttributeCheckTool(_TRPGBaseTool):
    """属性/技能检定。"""

    @property
    def name(self) -> str:
        return "trpg_attribute_check"

    @property
    def description(self) -> str:
        return (
            "对指定角色进行属性或技能检定。自动适配 CoC(d100) 和 D&D(d20)。"
            "参数: group_id=群号(必填), character=角色名(必填), attribute=属性/技能名(必填), "
            "difficulty='regular'/'hard'/'extreme'(仅CoC), dc=难度等级(仅D&D), "
            "advantage='normal'/'advantage'/'disadvantage'(仅D&D), bonus=额外加值"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号（必填）"},
                "character": {"type": "string", "description": "角色名称（必填）"},
                "attribute": {"type": "string", "description": "要检定的属性或技能名"},
                "difficulty": {"type": "string", "enum": ["regular", "hard", "extreme"], "description": "CoC 难度"},
                "dc": {"type": "integer", "description": "D&D 难度等级 DC"},
                "advantage": {"type": "string", "enum": ["normal", "advantage", "disadvantage"], "description": "优劣势（D&D）"},
                "bonus": {"type": "integer", "description": "额外加值"},
            },
            "required": ["group_id", "character", "attribute"],
        }

    def execute(
        self, group_id: str, character: str, attribute: str,
        difficulty: str = "regular", dc: int = 0,
        advantage: str = "normal", bonus: int = 0,
    ) -> str:
        sheet = self._load_char(group_id, character)
        if not sheet:
            return f"⚠️ 本群找不到角色「{character}」喵…"

        system = sheet.get("system", "coc7")
        if system == "coc7":
            return self._coc_check(sheet, attribute, difficulty, bonus)
        else:
            return self._dnd_check(sheet, attribute, dc, advantage, bonus)

    def _coc_check(self, sheet: dict, attribute: str, difficulty: str, bonus: int) -> str:
        character = sheet["name"]
        attr_val = sheet["attributes"].get(attribute.upper())
        attr_source = "属性"
        if attr_val is None:
            attr_val = sheet.get("skills", {}).get(attribute)
            attr_source = "技能"
        if attr_val is None:
            return (
                f"⚠️ 角色「{character}」没有「{attribute}」这个属性/技能喵。\n"
                f"可用属性: {', '.join(sheet['attributes'].keys())}\n"
                f"可用技能: {', '.join(sheet.get('skills', {}).keys()) or '(无)'}"
            )

        target = attr_val + bonus
        if difficulty == "hard":
            target = target // 2
            diff_label = "困难(½)"
        elif difficulty == "extreme":
            target = target // 5
            diff_label = "极难(⅕)"
        else:
            diff_label = "常规"

        roll = random.randint(1, 100)
        if roll == 1:
            result = "🌟 大成功！"
        elif roll >= 96:
            result = "💀 大失败…"
        elif roll <= target:
            result = "✅ 成功！"
        else:
            result = "❌ 失败"

        return (
            f"🎲 {character} 进行{attr_source}「{attribute}」检定 ({diff_label})\n"
            f"   目标值: ≤{target} | 掷骰: d100 = {roll}\n"
            f"   → {result}"
        )

    def _dnd_check(self, sheet: dict, attribute: str, dc: int, advantage: str, bonus: int) -> str:
        character = sheet["name"]
        attr_val = sheet["attributes"].get(attribute.upper())
        if attr_val is None:
            attr_val = sheet.get("skills", {}).get(attribute, 10)
            attr_source = "技能"
        else:
            attr_source = "属性"

        mod = self._mod(attr_val) + bonus
        mod_str = f"+{mod}" if mod >= 0 else str(mod)

        if advantage == "advantage":
            d1, d2 = random.randint(1, 20), random.randint(1, 20)
            roll = max(d1, d2)
            roll_detail = f"优势掷骰: [{d1}, {d2}] → 取高 = {roll}"
        elif advantage == "disadvantage":
            d1, d2 = random.randint(1, 20), random.randint(1, 20)
            roll = min(d1, d2)
            roll_detail = f"劣势掷骰: [{d1}, {d2}] → 取低 = {roll}"
        else:
            roll = random.randint(1, 20)
            roll_detail = f"d20 = {roll}"

        total = roll + mod
        if roll == 20:
            result = "🌟 天然20！大成功！"
        elif roll == 1:
            result = "💀 天然1… 大失败"
        elif dc > 0:
            result = f"{'✅ 成功' if total >= dc else '❌ 失败'} (vs DC {dc})"
        else:
            result = f"总计 {total}"

        return (
            f"🎲 {character} 进行{attr_source}「{attribute}」检定\n"
            f"   {roll_detail} | 调整值: {mod_str}\n"
            f"   → 结果: {total} {result}"
        )


# ── 5. 更新角色状态 ──────────────────────────────────────────

class UpdateCharacterTool(_TRPGBaseTool):
    """更新角色的 HP、SAN、物品等。"""

    @property
    def name(self) -> str:
        return "trpg_update_character"

    @property
    def description(self) -> str:
        return (
            "更新角色状态：扣血/回血、SAN变化、物品增减、属性修改等。"
            "参数: group_id=群号(必填), character=角色名(必填), "
            "hp_delta=HP变化量, san_delta=SAN变化量, luck_delta=幸运变化, mp_delta=MP变化, "
            "add_item=添加物品, remove_item=移除物品, notes=备注, set_attr=设置属性(JSON)"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号（必填）"},
                "character": {"type": "string", "description": "角色名称（必填）"},
                "hp_delta": {"type": "integer", "description": "HP 变化值"},
                "san_delta": {"type": "integer", "description": "SAN 变化值"},
                "luck_delta": {"type": "integer", "description": "幸运变化值"},
                "mp_delta": {"type": "integer", "description": "MP 变化值"},
                "add_item": {"type": "string", "description": "要添加的物品名"},
                "remove_item": {"type": "string", "description": "要移除的物品名"},
                "notes": {"type": "string", "description": "备注信息"},
                "set_attr": {"type": "string", "description": "设置属性，JSON字符串"},
            },
            "required": ["group_id", "character"],
        }

    def execute(
        self, group_id: str, character: str,
        hp_delta: int = 0, san_delta: int = 0, luck_delta: int = 0, mp_delta: int = 0,
        add_item: str = "", remove_item: str = "", notes: str = "", set_attr: str = "",
    ) -> str:
        sheet = self._load_char(group_id, character)
        if not sheet:
            return f"⚠️ 本群找不到角色「{character}」喵…"

        changes = []
        derived = sheet.get("derived", {})

        if hp_delta != 0:
            old_hp = derived.get("HP", 0)
            max_hp = derived.get("max_hp", old_hp)
            new_hp = max(0, min(max_hp, old_hp + hp_delta))
            derived["HP"] = new_hp
            arrow = f"+{hp_delta}" if hp_delta > 0 else str(hp_delta)
            changes.append(f"❤️ HP: {old_hp} → {new_hp} ({arrow})")
            if new_hp <= 0:
                changes.append("   💀 角色已昏迷/濒死！")

        if san_delta != 0:
            old_san = derived.get("SAN", 0)
            max_san = derived.get("max_san", 99)
            new_san = max(0, min(max_san, old_san + san_delta))
            derived["SAN"] = new_san
            arrow = f"+{san_delta}" if san_delta > 0 else str(san_delta)
            changes.append(f"🧠 SAN: {old_san} → {new_san} ({arrow})")
            if san_delta <= -5:
                changes.append(f"   ⚡ 一次性失去 {-san_delta} 点 SAN，可能触发疯狂检定！")

        if luck_delta != 0:
            old_luck = derived.get("LUCK", 0)
            new_luck = max(0, old_luck + luck_delta)
            derived["LUCK"] = new_luck
            arrow = f"+{luck_delta}" if luck_delta > 0 else str(luck_delta)
            changes.append(f"🍀 LUCK: {old_luck} → {new_luck} ({arrow})")

        if mp_delta != 0:
            old_mp = derived.get("MP", 0)
            max_mp = derived.get("max_mp", old_mp)
            new_mp = max(0, min(max_mp, old_mp + mp_delta))
            derived["MP"] = new_mp
            arrow = f"+{mp_delta}" if mp_delta > 0 else str(mp_delta)
            changes.append(f"💠 MP: {old_mp} → {new_mp} ({arrow})")

        inventory = sheet.get("inventory", [])
        if add_item:
            inventory.append(add_item)
            changes.append(f"🎒 获得物品: {add_item}")
        if remove_item:
            if remove_item in inventory:
                inventory.remove(remove_item)
                changes.append(f"🗑️ 失去物品: {remove_item}")
            else:
                changes.append(f"⚠️ 背包中没有「{remove_item}」")

        if set_attr:
            try:
                attr_updates = json.loads(set_attr)
                sheet["attributes"].update(attr_updates)
                for k, v in attr_updates.items():
                    changes.append(f"📊 {k}: → {v}")
            except json.JSONDecodeError:
                return "⚠️ set_attr 参数 JSON 格式错误喵！"

        if notes:
            existing_notes = sheet.get("notes", "")
            sheet["notes"] = f"{existing_notes}\n{notes}".strip() if existing_notes else notes
            changes.append(f"📝 备注: {notes}")

        if not changes:
            return f"🤔 没有对「{character}」做出任何修改喵…"

        sheet["derived"] = derived
        sheet["inventory"] = inventory
        self._save_char(group_id, character, sheet)
        return f"🔄 角色「{character}」更新完成！\n" + "\n".join(f"   {c}" for c in changes)


# ── 6. 删除角色卡 ────────────────────────────────────────────

class DeleteCharacterTool(_TRPGBaseTool):
    """删除角色卡。"""

    @property
    def name(self) -> str:
        return "trpg_delete_character"

    @property
    def description(self) -> str:
        return "删除一张角色卡，不可撤销！参数: group_id=群号(必填), name=角色名(必填)"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号（必填）"},
                "name": {"type": "string", "description": "要删除的角色名称"},
            },
            "required": ["group_id", "name"],
        }

    def execute(self, group_id: str, name: str) -> str:
        sheet = self._load_char(group_id, name)
        if not sheet:
            return f"⚠️ 本群找不到角色「{name}」喵…"
        self._delete_char_file(group_id, name)
        return f"🗑️ 角色「{name}」已删除。愿ta安息喵…"


# ── 7. 先攻掷骰 + 团务状态 ──────────────────────────────────

class CombatInitiativeTool(_TRPGBaseTool):
    """多角色先攻掷骰，自动排序，并保存到团务状态中。"""

    @property
    def name(self) -> str:
        return "trpg_initiative"

    @property
    def description(self) -> str:
        return (
            "为多个角色掷先攻并排序，同时自动更新团务状态中的回合顺序。"
            "参数: group_id=群号(必填), characters=角色名列表(逗号分隔), "
            "bonuses=对应加值(逗号分隔,可选), npcs=NPC名列表(逗号分隔,可选), npc_bonuses=NPC加值(可选)"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号（必填）"},
                "characters": {"type": "string", "description": "PC 角色名，逗号分隔"},
                "bonuses": {"type": "string", "description": "PC 先攻加值，逗号分隔"},
                "npcs": {"type": "string", "description": "NPC/怪物名，逗号分隔"},
                "npc_bonuses": {"type": "string", "description": "NPC 先攻加值，逗号分隔"},
            },
            "required": ["group_id", "characters"],
        }

    def execute(
        self, group_id: str, characters: str,
        bonuses: str = "", npcs: str = "", npc_bonuses: str = "",
    ) -> str:
        pc_names = [n.strip() for n in characters.split(",") if n.strip()]
        pc_bonuses = [int(b.strip()) for b in bonuses.split(",")] if bonuses else []
        npc_names = [n.strip() for n in npcs.split(",")] if npcs else []
        npc_bonuses_list = [int(b.strip()) for b in npc_bonuses.split(",")] if npc_bonuses else []

        results = []

        # PC 先攻
        for i, name in enumerate(pc_names):
            bonus = pc_bonuses[i] if i < len(pc_bonuses) else 0
            sheet = self._load_char(group_id, name)
            if sheet and sheet.get("system") == "dnd5e":
                dex = sheet["attributes"].get("DEX", 10)
                bonus = self._mod(dex) + bonus
            roll = random.randint(1, 20)
            total = roll + bonus
            hp = sheet.get("derived", {}).get("HP", "?") if sheet else "?"
            results.append((total, roll, bonus, name, hp, False))

        # NPC 先攻
        for i, name in enumerate(npc_names):
            bonus = npc_bonuses_list[i] if i < len(npc_bonuses_list) else 0
            roll = random.randint(1, 20)
            total = roll + bonus
            results.append((total, roll, bonus, name, "?", True))

        results.sort(key=lambda x: x[0], reverse=True)

        # ── 更新团务状态 ──────────────────────────────
        gs = self._load_game_session(group_id)
        gs["phase"] = "combat"
        gs["round_number"] = 1
        gs["current_turn_index"] = 0
        gs["turn_order"] = [
            {
                "name": name, "initiative": total,
                "hp": hp, "is_npc": is_npc,
            }
            for (total, roll, bonus, name, hp, is_npc) in results
        ]
        self._save_game_session(gs)

        # ── 格式化输出 ──────────────────────────────
        lines = [f"⚔️ 先攻顺序 (第 1 回合):"]
        for rank, (total, roll, bonus, name, hp, is_npc) in enumerate(results):
            bonus_str = f"+{bonus}" if bonus >= 0 else str(bonus)
            tag = "👹" if is_npc else "🎭"
            if rank <= 2:
                emoji = ["🥇", "🥈", "🥉"][rank]
            else:
                emoji = f"{rank+1}."
            lines.append(
                f"   {emoji} {tag} {name} — 先攻 {total} (d20={roll}{bonus_str})"
                + (f" | HP:{hp}" if hp != "?" else "")
            )
        lines.append("")
        lines.append(f"💡 当前行动: {results[0][3]}（用 trpg_next_turn 推进到下一位）")
        return "\n".join(lines)


# ── 8. 回合推进 ──────────────────────────────────────────────

class NextTurnTool(_TRPGBaseTool):
    """推进回合到下一位行动者。"""

    @property
    def name(self) -> str:
        return "trpg_next_turn"

    @property
    def description(self) -> str:
        return (
            "推进到下一个行动者，如果是最后一位则开始新回合。"
            "参数: group_id=群号(必填)"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号（必填）"},
            },
            "required": ["group_id"],
        }

    def execute(self, group_id: str) -> str:
        gs = self._load_game_session(group_id)
        if not gs.get("active") or not gs.get("turn_order"):
            return "⚠️ 当前没有进行中的战斗喵！先用 trpg_initiative 开始战斗吧。"

        order = gs["turn_order"]
        idx = gs["current_turn_index"]
        round_num = gs["round_number"]

        # 推进
        idx += 1
        if idx >= len(order):
            idx = 0
            round_num += 1
            gs["round_number"] = round_num

        gs["current_turn_index"] = idx
        current = order[idx]
        self._save_game_session(gs)

        tag = "👹" if current.get("is_npc") else "🎭"
        next_idx = (idx + 1) % len(order)
        next_up = order[next_idx]
        next_tag = "👹" if next_up.get("is_npc") else "🎭"

        lines = [
            f"⏩ 第 {round_num} 回合",
            f"   当前行动: {tag} {current['name']} (先攻 {current['initiative']})",
            f"   下一位: {next_tag} {next_up['name']}",
        ]
        if current.get("hp") and current["hp"] != "?":
            lines.append(f"   HP: {current['hp']}")
        return "\n".join(lines)


# ── 9. 团务会话管理（开团/结束/状态） ───────────────────────

class GameSessionTool(_TRPGBaseTool):
    """管理多人跑团的团务会话：开团、设置GM、查看状态、结束。"""

    @property
    def name(self) -> str:
        return "trpg_game_session"

    @property
    def description(self) -> str:
        return (
            "管理跑团团务会话。action 可选: "
            "'start'=开始新团(设置GM和规则), 'status'=查看当前团务状态(回合/先攻/场景), "
            "'set_scene'=更新当前场景描述, 'add_npc'=添加NPC到战斗中, "
            "'update_npc'=更新NPC的HP或状态, 'end_combat'=结束战斗, "
            "'pause'=暂停跑团(冻结共享会话，保留所有进度), 'resume'=恢复跑团, "
            "'end'=结束跑团(清除全部数据)。"
            "参数: group_id=群号(必填), action=操作(必填), "
            "gm_name=GM名(start时用), system=规则系统(start时用), scene=场景描述, "
            "npc_name/npc_hp/npc_ac/npc_notes=NPC信息, "
            "npc_hp_delta=NPC的HP变化量"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "group_id": {"type": "string", "description": "群号（必填）"},
                "action": {
                    "type": "string",
                    "enum": ["start", "status", "set_scene", "add_npc", "update_npc", "end_combat", "pause", "resume", "end"],
                    "description": "操作类型",
                },
                "gm_name": {"type": "string", "description": "GM（主持人）的名字"},
                "system": {"type": "string", "enum": ["coc7", "dnd5e"], "description": "规则系统"},
                "scene": {"type": "string", "description": "当前场景描述"},
                "npc_name": {"type": "string", "description": "NPC 名字"},
                "npc_hp": {"type": "integer", "description": "NPC HP"},
                "npc_ac": {"type": "integer", "description": "NPC AC（仅D&D）"},
                "npc_notes": {"type": "string", "description": "NPC 备注/描述"},
                "npc_hp_delta": {"type": "integer", "description": "NPC HP 变化量"},
            },
            "required": ["group_id", "action"],
        }

    def execute(
        self, group_id: str, action: str,
        gm_name: str = "", system: str = "coc7", scene: str = "",
        npc_name: str = "", npc_hp: int = 0, npc_ac: int = 10, npc_notes: str = "",
        npc_hp_delta: int = 0,
    ) -> str:
        if action == "start":
            return self._start(group_id, gm_name, system)

        gs = self._load_game_session(group_id)

        if action == "status":
            return self._status(gs)
        elif action == "set_scene":
            if scene:
                gs["scene"] = scene
                gs["phase"] = "exploration" if gs["phase"] == "idle" else gs["phase"]
                self._save_game_session(gs)
                return f"🎬 场景已更新:\n   {scene}"
            return "⚠️ 请提供 scene 参数喵~"
        elif action == "add_npc":
            if not npc_name:
                return "⚠️ 请提供 npc_name 喵~"
            gs.setdefault("npcs", []).append({
                "name": npc_name, "hp": npc_hp, "max_hp": npc_hp,
                "ac": npc_ac, "notes": npc_notes,
            })
            self._save_game_session(gs)
            return f"👹 NPC「{npc_name}」已加入战斗！HP:{npc_hp} AC:{npc_ac}"
        elif action == "update_npc":
            npcs = gs.get("npcs", [])
            for npc in npcs:
                if npc["name"] == npc_name:
                    old_hp = npc["hp"]
                    npc["hp"] = max(0, npc["hp"] + npc_hp_delta)
                    arrow = f"+{npc_hp_delta}" if npc_hp_delta > 0 else str(npc_hp_delta)
                    msg = f"👹 {npc_name} HP: {old_hp} → {npc['hp']} ({arrow})"
                    if npc["hp"] <= 0:
                        msg += "\n   💀 该 NPC 已被击败！"
                    self._save_game_session(gs)
                    return msg
            return f"⚠️ 找不到 NPC「{npc_name}」喵~"
        elif action == "end_combat":
            gs["phase"] = "exploration"
            gs["turn_order"] = []
            gs["current_turn_index"] = 0
            gs["round_number"] = 0
            gs["npcs"] = []
            self._save_game_session(gs)
            return "🏁 战斗结束！大家辛苦了喵~ 现在是探索阶段。"
        elif action == "pause":
            gs["phase_before_pause"] = gs.get("phase", "exploration")  # 记住暂停前的阶段
            gs["active"] = False  # 冻结共享会话路由，但保留全部进度
            gs["phase"] = "paused"
            self._save_game_session(gs)
            return (
                "⏸️ 跑团已暂停！\n"
                f"   场景: {gs.get('scene', '(未设置)')}\n"
                f"   回合: 第 {gs.get('round_number', 0)} 回合\n"
                f"   所有角色卡、NPC、先攻顺序均已保留喵~\n"
                "💡 继续时用 trpg_game_session action=resume"
            )
        elif action == "resume":
            if gs.get("active"):
                return "⚠️ 跑团本来就在进行中喵~"
            if gs.get("phase") == "idle":
                return "⚠️ 当前没有暂停中的跑团喵，用 trpg_game_session action=start 开新团吧~"
            gs["active"] = True
            gs["phase"] = gs.get("phase_before_pause", gs.get("phase", "exploration"))
            self._save_game_session(gs)
            return (
                f"▶️ 跑团继续！\n"
                f"   GM: {gs.get('gm_name', '?')}\n"
                f"   场景: {gs.get('scene', '(未设置)')}\n"
                f"   回合: 第 {gs.get('round_number', 1)} 回合\n"
                f"   当前行动: {gs.get('turn_order', [{}])[gs.get('current_turn_index', 0)].get('name', '?') if gs.get('turn_order') else '(自由行动)'}"
            )
        elif action == "end":
            gs["active"] = False
            gs["phase"] = "idle"
            gs["turn_order"] = []
            gs["current_turn_index"] = 0
            gs["round_number"] = 0
            gs["npcs"] = []
            self._save_game_session(gs)
            return "📕 本次跑团结束！感谢所有调查员/冒险者，本小姐也辛苦啦喵~"
        else:
            return f"⚠️ 未知操作: {action}"

    def _start(self, group_id: str, gm_name: str, system: str) -> str:
        gs = _default_game_session(group_id)
        gs["active"] = True
        gs["gm_name"] = gm_name
        gs["system"] = system
        gs["phase"] = "intro"
        self._save_game_session(gs)

        char_list = self._list_chars(group_id)
        char_summary = "\n".join(f"   🎭 {c}" for c in char_list) if char_list else "   (暂无角色，先用 trpg_create_character 创建吧)"

        return (
            f"📖 跑团开始！\n"
            f"   GM: {gm_name}\n"
            f"   规则: {system.upper()}\n"
            f"   当前参团角色:\n{char_summary}\n"
            f"💡 接下来请 GM 用 trpg_game_session action=set_scene 设置开场场景喵~"
        )

    def _status(self, gs: dict) -> str:
        if not gs.get("active"):
            return "📭 当前没有进行中的跑团喵。用 trpg_game_session action=start 开始吧！"

        lines = [
            f"📖 团务状态",
            f"   GM: {gs.get('gm_name', '?')}",
            f"   规则: {gs.get('system', '?').upper()}",
            f"   阶段: {gs.get('phase', '?')}",
            f"   场景: {gs.get('scene') or '(未设置)'}",
        ]

        order = gs.get("turn_order", [])
        if order:
            lines.append(f"   回合: 第 {gs.get('round_number', 1)} 回合")
            lines.append(f"   先攻顺序:")
            for i, entry in enumerate(order):
                marker = " ◀ 当前" if i == gs.get("current_turn_index", 0) else ""
                tag = "👹" if entry.get("is_npc") else "🎭"
                lines.append(f"      {tag} {entry['name']} (先攻{entry['initiative']}){marker}")

        npcs = gs.get("npcs", [])
        if npcs:
            lines.append(f"   NPC:")
            for npc in npcs:
                lines.append(f"      👹 {npc['name']} HP:{npc['hp']}/{npc['max_hp']} AC:{npc['ac']}")

        return "\n".join(lines)


# ── 10. 随机跑团表 ───────────────────────────────────────────

class RandomTRPGTableTool(Tool):
    """随机跑团内容表。"""

    ENCOUNTERS = [
        "深夜小巷里传来奇怪的啃咬声…",
        "一个穿风衣的陌生人递来一张皱巴巴的名片，上面只有一个符号。",
        "图书馆角落里有一本不该存在的书，封面是温热的。",
        "街角的老式电话亭响了，周围没有人。",
        "下水道井盖被从下面顶开了，飘出腐臭的绿雾。",
        "博物馆夜班保安说有一尊雕像的位置每晚都在变。",
        "便利店的监控拍到的东西，店员死活不肯说。",
        "废弃医院三楼窗户亮了一秒又灭了，但那里早就断电了。",
        "地铁最后一班车，车厢里只有一个穿旧式校服的女孩。",
        "码头上漂来一个密封的铁箱，上面刻着不属于任何文明的文字。",
        "邻居已经三天没出门了，从门缝里渗出暗红色的黏液。",
        "古董店里的一面镜子里映出的不是你。",
        "森林深处的废弃木屋周围，所有指南针都指向它。",
        "收音机在凌晨三点自动开启，播报着明天的新闻。",
        "路边的流浪汉突然叫出了你的全名，然后转身消失在人海中。",
    ]

    LOOT_TABLES = {
        "common": [
            "一把生锈的小刀（1d4伤害）", "旧绷带×2（回复1d4 HP）", "手电筒（电池还能用2小时）",
            "一瓶可疑的绿色液体", "破旧的笔记本，里面画满了看不懂的符号",
            "一包烟和一只打火机", "50块钱现金", "一张地图碎片",
            "旅馆钥匙，上面标着「13号房」", "一盒火柴（剩7根）",
        ],
        "uncommon": [
            "银质十字架（对超自然生物+1伤害）", "防弹衣（AC+2）", "急救包（回复2d6 HP）",
            "古旧的咒文书页（可用一次防护法术）", "夜视镜", "圣水×2",
            "一把.38左轮（1d10伤害，6发子弹）", "学者笔记，记载着一个邪教的弱点",
            "墨镜 + 黑色西装套装", "智能设备，但操作系统你从未见过",
        ],
        "rare": [
            "远古印记护符（SAN回复1d10）", "附魔短剑（2d6伤害，对神话生物+1d6）",
            "传送卷轴（一次性，回到安全屋）", "黄金罗盘（总是指向最近的异常）",
            "诅咒人偶（……你不想知道它有什么用）", "完整版的《死灵之书》抄本",
        ],
    }

    NPC_ADJ = ["神秘", "暴躁", "神经质", "优雅", "邋遢", "阴郁", "亢奋", "冷漠", "多疑", "过度热情"]
    NPC_OCCUPATION = [
        "私家侦探", "图书馆管理员", "古董商", "记者", "医生", "大学教授",
        "流浪汉", "酒吧老板", "警察", "殡仪馆员工", "出租车司机", "神父",
        "药剂师", "街头艺人", "保险推销员", "退休军官",
    ]
    NPC_QUIRK = [
        "说话时总下意识地搓手", "左眼偶尔会变成金色", "对数字13极度恐惧",
        "随身带着一只黑猫", "永远戴着墨镜，即使在室内", "知道自己会在哪一天死",
        "能准确说出陌生人的名字", "每次见面都会给你不同的名片", "口袋里总有吃不完的糖",
        "手指上纹着奇怪的符号", "说梦话时会用拉丁语祈祷", "能闻到「不正常」的东西",
    ]

    SURNAMES_CN = ["李", "王", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴", "林", "白", "叶", "沈", "顾", "苏"]
    GIVEN_CN = ["明", "华", "强", "伟", "芳", "敏", "静", "丽", "勇", "军", "磊", "洋", "雪", "琳", "宇", "哲"]
    SURNAMES_JP = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤"]
    GIVEN_JP_M = ["翔太", "大輝", "健太", "拓海", "龍之介", "優斗", "蓮", "陽太"]
    GIVEN_JP_F = ["桜", "凛", "美咲", "葵", "七海", "由衣", "花子", "優美"]

    MADNESS = [
        "偏执狂：你坚信有人在跟踪你。每次有人从背后叫你，都要过 SAN 否则恐慌。",
        "幽闭恐惧：进入密闭空间时，所有检定获得一个惩罚骰。",
        "幻觉：你总能听到墙里有虫子在爬的声音。有时这是真的。",
        "强迫行为：每次进门必须敲门三次，否则浑身不舒服。",
        "失忆症：你记不住新认识的人的脸。每次见到「陌生人」都要过 INT 检定。",
        "嗜睡症：紧张时会突然睡着 1d10 分钟。战斗中也一样。",
        "洁癖：碰到不干净的东西后必须立即洗手，否则所有检定 -10%。",
        "双重人格：每天投 d2，1 是正常，2 是暴力人格（社交检定自动失败）。",
        "语言混乱：紧张时说出的话会变成听不懂的古代语，持续 1d6 分钟。",
        "食人冲动：看到新鲜伤口时，必须过 POW 检定否则想要咬一口…",
    ]

    @property
    def name(self) -> str:
        return "trpg_random_table"

    @property
    def description(self) -> str:
        return (
            "随机跑团内容生成器。type 可选: 'encounter'(随机遭遇), "
            "'loot'(随机战利品), 'npc'(随机NPC), 'name'(随机名字), "
            "'madness'(随机疯狂症状)。rarity 可选 'common'/'uncommon'/'rare'(仅loot)。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["encounter", "loot", "npc", "name", "madness"], "description": "随机表类型"},
                "rarity": {"type": "string", "enum": ["common", "uncommon", "rare"], "description": "稀有度（loot）"},
                "count": {"type": "integer", "description": "生成数量，默认 1"},
            },
            "required": ["type"],
        }

    def execute(self, type: str, rarity: str = "common", count: int = 1) -> str:
        count = max(1, min(count, 5))
        emoji_map = {"encounter": "🌙", "loot": "💎", "npc": "👤", "name": "📛", "madness": "🌀"}
        label_map = {
            "encounter": "随机遭遇", "loot": f"随机战利品 ({rarity})",
            "npc": "随机 NPC", "name": "随机名字", "madness": "随机疯狂症状",
        }

        if type == "encounter":
            items = random.sample(self.ENCOUNTERS, min(count, len(self.ENCOUNTERS)))
        elif type == "loot":
            pool = self.LOOT_TABLES.get(rarity, self.LOOT_TABLES["common"])
            items = random.choices(pool, k=count)
        elif type == "npc":
            items = [self._gen_npc() for _ in range(count)]
        elif type == "name":
            items = [self._gen_name() for _ in range(count)]
        elif type == "madness":
            items = random.sample(self.MADNESS, min(count, len(self.MADNESS)))
        else:
            return "⚠️ 不支持的类型喵！可选: encounter / loot / npc / name / madness"

        lines = [f"{emoji_map.get(type, '❓')} {label_map.get(type, type)}:"]
        for i, item in enumerate(items, 1):
            lines.append(f"   {i}. {item}")
        return "\n".join(lines)

    def _gen_npc(self) -> str:
        adj = random.choice(self.NPC_ADJ)
        occ = random.choice(self.NPC_OCCUPATION)
        quirk = random.choice(self.NPC_QUIRK)
        name = self._gen_name()
        return f"{adj}的{occ}「{name}」，{quirk}"

    def _gen_name(self) -> str:
        if random.random() < 0.7:
            surname = random.choice(self.SURNAMES_CN)
            given_len = random.choice([1, 2])
            given = "".join(random.choices(self.GIVEN_CN, k=given_len))
            return f"{surname}{given}"
        else:
            surname = random.choice(self.SURNAMES_JP)
            given = random.choice(self.GIVEN_JP_M + self.GIVEN_JP_F)
            return f"{surname} {given}"
