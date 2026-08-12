"""Manual end-to-end smoke test for presentation images, charts, and branding."""

import json
import sys
import tempfile
from pathlib import Path


PATCH_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(r"F:\agent_unnameko")
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PATCH_ROOT))

from desktop_agent_core import DesktopWorkflowExecutor  # noqa: E402


def main() -> None:
    output_root = PATCH_ROOT / ".smoke_presentation"
    output_root.mkdir(parents=True, exist_ok=True)
    executor = DesktopWorkflowExecutor(str(output_root))
    task_id = "presentation-brand-smoke"
    assets = executor.execute(
        "presentation.image_search",
        {
            "queries": [{
                "slide_index": 1,
                "query": "summer forest landscape sunlight",
                "alt": "夏日阳光下的森林",
            }],
        },
        {"task_id": task_id},
    )
    task = {
        "task_id": task_id,
        "title": "未名子的夏日工作小结",
        "steps": [{"sequence": 1, "kind": "presentation.image_search", "output": assets}],
    }
    result = executor.execute(
        "presentation.prepare",
        {
            "deck_title": "未名子的夏日工作小结",
            "subtitle": "一份配图、原生图表与品牌模板联动的验收稿",
            "purpose": "验证图片搜索、图表生成和品牌模板能力",
            "audience": "主人",
            "author": "未名子",
            "brand_template": "unnameko_green",
            "layout_strategy": "auto_grid",
            "asset_step_sequence": 1,
            "slides": [
                {
                    "title": "夏日灵感",
                    "bullets": ["柔和绿色来自树海与风", "图片带来源与许可信息", "视觉素材只进入私有暂存区"],
                    "image_query": "summer forest landscape sunlight",
                },
                {
                    "title": "任务完成量",
                    "bullets": ["以下是验收用的明确测试数据", "图表在 PPT 中保持可编辑"],
                    "chart": {
                        "type": "bar",
                        "title": "季度任务完成量（测试数据）",
                        "categories": ["Q1", "Q2", "Q3", "Q4"],
                        "series": [{"name": "完成量", "values": [12, 18, 25, 31]}],
                        "number_format": "0",
                    },
                },
                {
                    "title": "时间分配",
                    "bullets": ["使用另一种原生图表验证图例和数据标签"],
                    "chart": {
                        "type": "doughnut",
                        "title": "活动时间占比（测试数据）",
                        "categories": ["陪伴", "学习", "整理"],
                        "series": [{"name": "占比", "values": [50, 30, 20]}],
                        "number_format": "0%",
                    },
                },
            ],
            "include_closing": True,
        },
        task,
    )
    print(json.dumps({"assets": assets, "presentation": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
