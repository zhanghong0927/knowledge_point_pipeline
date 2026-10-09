"""回归短版权页路由、跨空白信号以及稀疏决策表的保守保留契约。"""

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from book_extractor.markdown import Unit, parse_markdown
from book_extractor.models import Regions
from book_extractor.pipeline import preprocess, region_candidates


class RegionClient:
    """只模拟区域服务响应，不创建网络连接。"""

    def __init__(self, *, failure: bool = False) -> None:
        """记录逻辑调用数，并可选择模拟一次服务故障。"""
        self.calls = 0
        self.failure = failure

    def call(
        self,
        model: type[Regions],
        messages: list[dict[str, Any]],
        *,
        context: dict[str, Any],
    ) -> Regions:
        """将请求目标判为出版信息，或抛出可恢复异常以检查默认保留。"""
        self.calls += 1
        if self.failure:
            raise RuntimeError("offline service failure")
        return model.model_validate(
            {
                "decisions": [
                    {
                        "region_id": context["region_ids"][0],
                        "action": "exclude",
                        "reason": "版权信息",
                    }
                ]
            },
            context=context,
        )


class RegionTests(unittest.TestCase):
    """验证路由只产生真实决策，短内容不漏检，正文和受保护结构不误删。"""

    def setUp(self) -> None:
        """准备独立检查点目录及短版权示例。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.copyright = "ISBN 978-7-1234-5678-9\n出版社：示例出版社\n定价：32元\n"

    def test_short_copyright_is_routed_without_padding(self) -> None:
        """不到 256 token 的多字段版权信息也进入判断，但不由规则直接删除。"""
        units = parse_markdown(self.copyright)
        self.assertEqual(region_candidates(units), units)
        client = RegionClient()
        active, rows = preprocess(units, client, self.directory)
        self.assertEqual((client.calls, len(rows), active), (1, 1, []))

    def test_signals_cross_blank_units_but_not_body_targets(self) -> None:
        """空白不隔断相邻字段；周围纯正文没有自身书目信号则不能送去删除。"""
        units = parse_markdown(
            "ISBN 978-7-1234-5678-9\n\n出版社：示例出版社\n\n"
            "出版社的定价影响需求。\n\n" + "正文解释市场价格。\n\n" * 100
        )
        selected = region_candidates(units)
        self.assertEqual(
            [unit.text.strip() for unit in selected],
            ["ISBN 978-7-1234-5678-9", "出版社：示例出版社"],
        )
        compact = [unit for unit in units if unit.text.strip()]
        self.assertEqual(region_candidates(compact), selected)
        active, _ = preprocess(units, RegionClient(), self.directory)
        self.assertTrue(any("出版社的定价影响需求" in unit.text for unit in active))

    def test_protected_structures_and_publishing_prose_stay(self) -> None:
        """代码、表格、公式和讨论出版经济的正文不能只因关键词被路由。"""
        for kind in ("code", "table", "math"):
            unit = Unit("u1", self.copyright, 1, 3, [], kind)
            self.assertEqual(region_candidates([unit]), [])
        units = parse_markdown(
            "出版社的定价影响需求。读者可参见1891年出版的《合作运动》第三版。\n"
        )
        client = RegionClient()
        active, rows = preprocess(units, client, self.directory)
        self.assertEqual((active, rows, client.calls), (units, [], 0))
        summary = json.loads((self.directory / "preprocess_summary.json").read_text())
        self.assertEqual(summary["routing_status"], "zero_candidates")
        self.assertEqual(summary["default_retained_units"], len(units))

    def test_runtime_failure_retries_on_resume_then_caches_success(self) -> None:
        """临时错误保留原文，下一执行只补失败区域，成功后再次恢复零调用。"""
        units = parse_markdown(self.copyright)
        client = RegionClient(failure=True)
        active, rows = preprocess(units, client, self.directory)
        self.assertEqual(active, units)
        self.assertEqual(rows[0]["action"], "uncertain")
        again = RegionClient()
        active, rows = preprocess(units, again, self.directory)
        summary = json.loads((self.directory / "preprocess_summary.json").read_text())
        self.assertTrue(summary["cache_reused"])
        self.assertEqual(summary["decision_status"], "resolved")
        self.assertEqual(summary["uncertain_decisions"], 0)
        self.assertEqual(summary["llm_logical_calls_this_execution"], 1)
        self.assertEqual((again.calls, active, rows[0]["action"]), (1, [], "exclude"))
        cached = RegionClient()
        preprocess(units, cached, self.directory)
        self.assertEqual(cached.calls, 0)

    def test_semantic_uncertainty_does_not_retry(self) -> None:
        """模型已完成的不确定判断不是服务失败，恢复不能反复请求直到删掉原文。"""
        units = parse_markdown(self.copyright)
        (self.directory / "regions.json").write_text(
            json.dumps(
                [
                    {
                        "region_id": units[0].id,
                        "action": "uncertain",
                        "reason": "包含正文信息",
                        "decision_origin": "model",
                    }
                ]
            ),
            encoding="utf-8",
        )
        client = RegionClient()
        active, rows = preprocess(units, client, self.directory)
        self.assertEqual(
            (active, client.calls, rows[0]["action"]), (units, 0, "uncertain")
        )

    def test_empty_cache_does_not_claim_new_candidates_were_reviewed(self) -> None:
        """已有空决策缓存仍复用，但摘要须暴露当前候选尚无判断。"""
        (self.directory / "regions.json").write_text("[]", encoding="utf-8")
        units = parse_markdown(self.copyright)
        client = RegionClient()
        active, rows = preprocess(units, client, self.directory)
        summary = json.loads((self.directory / "preprocess_summary.json").read_text())
        self.assertEqual((active, rows, client.calls), (units, [], 0))
        self.assertTrue(summary["cache_reused"])
        self.assertEqual(summary["current_candidates"], 1)
        self.assertEqual(summary["unrecorded_candidates"], 1)
        self.assertEqual(summary["decision_status"], "unresolved")


if __name__ == "__main__":
    unittest.main()
