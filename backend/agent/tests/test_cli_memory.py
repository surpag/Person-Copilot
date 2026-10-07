"""cli/memory.py 的集成测试。

只测关键路径，不追求 100% 覆盖。
每个测试用 tmp DB，通过 monkeypatch 替换 sys.argv。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import memory as cli_memory


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "cli_test.db")


def run_cli(db_path: str, *argv) -> None:
    """模拟 `python -m cli.memory --db <db_path> <argv>` 执行一次。"""
    old_argv = sys.argv
    try:
        sys.argv = ["memory.py", "--db", db_path, *argv]
        cli_memory.main()
    finally:
        sys.argv = old_argv


class TestPreferences:
    def test_set_and_list(self, db_path, capsys):
        run_cli(db_path, "set-preference", "city", "南京")
        out = capsys.readouterr().out
        assert "已新建" in out

        run_cli(db_path, "list-preferences")
        out = capsys.readouterr().out
        assert "city" in out
        assert "南京" in out

    def test_update_existing(self, db_path, capsys):
        run_cli(db_path, "set-preference", "city", "南京")
        run_cli(db_path, "set-preference", "city", "上海")
        out = capsys.readouterr().out
        assert "已更新" in out

        run_cli(db_path, "list-preferences", "--json")
        data = json.loads(capsys.readouterr().out)
        assert data[0]["value"] == "上海"

    def test_delete(self, db_path, capsys):
        run_cli(db_path, "set-preference", "city", "南京")
        run_cli(db_path, "delete-preference", "city")
        out = capsys.readouterr().out
        assert "已删除" in out

    def test_delete_nonexistent_exits_1(self, db_path, capsys):
        with pytest.raises(SystemExit) as exc:
            run_cli(db_path, "delete-preference", "nope")
        assert exc.value.code == 1


class TestTodos:
    def test_add_and_list(self, db_path, capsys):
        run_cli(db_path, "add-todo", "买菜")
        out = capsys.readouterr().out
        assert "#1" in out
        assert "买菜" in out

        run_cli(db_path, "list-todos")
        out = capsys.readouterr().out
        assert "买菜" in out
        assert "⏳" in out

    def test_add_with_due(self, db_path, capsys):
        run_cli(db_path, "add-todo", "开会", "--due", "2026-09-25")
        out = capsys.readouterr().out
        assert "截止：2026-09-25" in out

    def test_complete(self, db_path, capsys):
        run_cli(db_path, "add-todo", "买菜")
        run_cli(db_path, "complete-todo", "1")
        out = capsys.readouterr().out
        assert "已完成 #1" in out

        run_cli(db_path, "list-todos", "--status", "done")
        out = capsys.readouterr().out
        assert "✅" in out

    def test_delete(self, db_path, capsys):
        run_cli(db_path, "add-todo", "买菜")
        run_cli(db_path, "delete-todo", "1")
        out = capsys.readouterr().out
        assert "已删除 #1" in out


class TestPrivacy:
    def test_export_stdout(self, db_path, capsys):
        run_cli(db_path, "set-preference", "city", "南京")
        run_cli(db_path, "add-todo", "买菜")
        capsys.readouterr()  # 丢弃前面的输出

        run_cli(db_path, "export")
        data = json.loads(capsys.readouterr().out)
        assert len(data["preferences"]) == 1
        assert len(data["todos"]) == 1

    def test_export_to_file(self, db_path, tmp_path, capsys):
        run_cli(db_path, "set-preference", "city", "南京")
        out_file = tmp_path / "backup.json"
        run_cli(db_path, "export", "--output", str(out_file))

        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert data["preferences"][0]["key"] == "city"

    def test_clear_all_requires_confirmation(self, db_path, capsys, monkeypatch):
        run_cli(db_path, "set-preference", "city", "南京")
        capsys.readouterr()

        # 模拟用户输入 "no"
        monkeypatch.setattr("builtins.input", lambda _: "no")
        run_cli(db_path, "clear", "--all")
        out = capsys.readouterr().out
        assert "已取消" in out

        # 数据还在
        run_cli(db_path, "list-preferences")
        assert "city" in capsys.readouterr().out

    def test_clear_all_with_yes(self, db_path, capsys):
        run_cli(db_path, "set-preference", "city", "南京")
        run_cli(db_path, "add-todo", "买菜")
        capsys.readouterr()

        run_cli(db_path, "clear", "--all", "--yes")
        out = capsys.readouterr().out
        assert "已清空偏好" in out
        assert "已清空待办" in out

        run_cli(db_path, "list-preferences")
        assert "（无偏好）" in capsys.readouterr().out
