"""Tests for pipeline/generator data injection"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from pipeline.analyzer import ActionInfo, ElementHint
from pipeline.generator import PlaywrightGenerator, DataRef


def make_action(
    action_type: str, page_context: str = "",
    label: str = "", placeholder: str = "", text: str = "",
    input_value: str = "", url: str = "",
) -> ActionInfo:
    """Helper to build ActionInfo for tests"""
    return ActionInfo(
        frame_idx=0, timestamp=0.0, frame_path="",
        action_type=action_type,
        element_hint=ElementHint(
            text=text, placeholder=placeholder, label=label, role="textbox",
        ),
        input_value=input_value,
        page_context=page_context,
        url=url,
        description="",
        confidence=0.9,
    )


class TestBuildDataEntries:
    """Tests for _build_data_entries"""

    def test_fill_actions_extracted(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("fill", page_context="登录页",
                        placeholder="请输入用户名", input_value="admin"),
            make_action("fill", page_context="登录页",
                        placeholder="请输入密码", input_value="secret"),
        ]
        data, refs = gen._build_data_entries(actions)
        assert len(data) == 1
        assert "登录页" in data
        assert data["登录页"]["请输入用户名"] == "admin"
        assert data["登录页"]["请输入密码"] == "secret"

    def test_click_actions_skipped(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("click", page_context="首页", text="登录按钮"),
        ]
        data, refs = gen._build_data_entries(actions)
        assert len(data) == 0

    def test_select_included(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("select", page_context="表单",
                        label="课程类型", input_value="必修"),
        ]
        data, refs = gen._build_data_entries(actions)
        assert data["表单"]["课程类型"] == "必修"

    def test_duplicate_groups_get_suffix(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("fill", page_context="课程管理",
                        text="课程名称", input_value="语文"),
            make_action("fill", page_context="课程管理",
                        text="课程名称", input_value="数学"),
        ]
        data, refs = gen._build_data_entries(actions)
        assert "课程管理" in data
        assert "课程管理_2" not in data
        assert data["课程管理"]["课程名称"] == "语文"
        assert data["课程管理"]["课程名称_2"] == "数学"

    def test_field_priority_label_over_placeholder(self):
        gen = PlaywrightGenerator()
        action = make_action("fill", page_context="表单",
                             label="用户名", placeholder="请输入", input_value="test")
        data, _ = gen._build_data_entries([action])
        assert "用户名" in data["表单"]

    def test_fallback_group_and_field(self):
        gen = PlaywrightGenerator()
        action = make_action("fill", page_context="", input_value="bare value")
        data, refs = gen._build_data_entries([action])
        assert "默认步骤" in data
        assert "字段" in data["默认步骤"]

    def test_same_group_field_dedup(self):
        gen = PlaywrightGenerator()
        actions = [
            make_action("fill", page_context="表单",
                        text="字段", input_value="a"),
            make_action("fill", page_context="表单",
                        text="字段", input_value="b"),
        ]
        data, _ = gen._build_data_entries(actions)
        assert "字段" in data["表单"]
        assert "字段_2" in data["表单"]


class TestRenderActionCode:
    """Tests for _render_action_code with data refs"""

    def test_fill_uses_d_call(self):
        gen = PlaywrightGenerator()
        action = make_action("fill", placeholder="请输入密码", input_value="old")
        ref = DataRef(group="登录页", field="请输入密码", value="newpass")
        code = gen._render_action_code(action, ref)
        assert "d('登录页', '请输入密码')" in code
        assert "old" not in code

    def test_fill_fallback_without_ref(self):
        gen = PlaywrightGenerator()
        action = make_action("fill", placeholder="搜索", input_value="hello")
        code = gen._render_action_code(action, None)
        assert ".fill('hello')" in code

    def test_click_unchanged(self):
        gen = PlaywrightGenerator()
        action = make_action("click", text="提交按钮")
        code = gen._render_action_code(action, None)
        assert ".click()" in code
        assert "d(" not in code


class TestRenderDataJson:
    """Tests for _render_data_json"""

    def test_valid_json_output(self):
        gen = PlaywrightGenerator()
        data = {"登录页": {"密码": "secret"}}
        output = gen._render_data_json(data)
        parsed = json.loads(output)
        assert parsed == data

    def test_chinese_keys_preserved(self):
        gen = PlaywrightGenerator()
        data = {"用户登录": {"请输入密码": "abc123"}}
        output = gen._render_data_json(data)
        assert "用户登录" in output
        assert "请输入密码" in output


class TestGenerateIntegration:
    """Integration tests — generate with data output"""

    def test_generate_produces_data_json(self):
        gen = PlaywrightGenerator(suite_name="测试")
        actions = [
            make_action("fill", page_context="登录", placeholder="用户名",
                        input_value="admin"),
            make_action("fill", page_context="登录", placeholder="密码",
                        input_value="pass"),
            make_action("click", page_context="登录", text="登录"),
            make_action("navigate", page_context="首页", url="http://example.com"),
            make_action("fill", page_context="表单", label="课程名",
                        input_value="数学"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = str(Path(tmp) / "test.spec.ts")
            result = gen.generate(actions, output_path=spec_path)

            assert Path(spec_path).exists()
            assert result.data_json_path
            assert Path(result.data_json_path).exists()

            # Check data.json content
            data_content = Path(result.data_json_path).read_text("utf-8")
            data = json.loads(data_content)
            assert "登录" in data
            assert data["登录"]["用户名"] == "admin"
            assert data["登录"]["密码"] == "pass"
            assert "表单" in data
            assert data["表单"]["课程名"] == "数学"

            # Check spec.ts uses d() calls
            assert "d('登录', '用户名')" in result.script
            assert "d('登录', '密码')" in result.script
            assert "d('表单', '课程名')" in result.script

            # Check no hardcoded fill values
            assert ".fill('admin')" not in result.script
            assert ".fill('pass')" not in result.script
            assert ".fill('数学')" not in result.script

    def test_no_data_when_no_fill_actions(self):
        gen = PlaywrightGenerator(suite_name="导航测试")
        actions = [
            make_action("navigate", url="http://example.com"),
            make_action("click", text="链接"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            spec_path = str(Path(tmp) / "nav.spec.ts")
            result = gen.generate(actions, output_path=spec_path)
            assert Path(result.data_json_path).exists()
            data = json.loads(Path(result.data_json_path).read_text("utf-8"))
            assert data == {}
