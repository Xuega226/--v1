"""主人身份查询工具 — check_owner。

让 Bot 能通过用户 ID 确认谁是主人，避免群友问到时胡扯。
"""

from .base import Tool


class CheckOwnerTool(Tool):
    """检查用户 ID 是否为主人（创造者）。"""

    name = "check_owner"
    description = (
        "检查给定的 QQ 用户 ID 是否是主人（创造者）。"
        "当有人问「你的主人是谁」「谁是你的主人」「创造者是谁」等问题时，"
        "用此工具传入消息中的用户 ID 来确认身份。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "消息中标注的用户 QQ ID",
            }
        },
        "required": ["user_id"],
    }

    def __init__(self, owner_id: str = ""):
        self.owner_id = str(owner_id)

    def execute(self, user_id: str) -> str:
        is_owner = str(user_id) == self.owner_id
        if is_owner:
            return f"确认：用户(ID:{user_id}) 正是我的主人喵！"
        else:
            return f"用户(ID:{user_id}) 不是主人，只是普通群友喵。"
