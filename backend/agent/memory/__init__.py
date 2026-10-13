"""
长期记忆模块。

用法：
    from memory import init_tables, PreferencesStore, TodosStore

    db = await connect_db("data/agent.db")
    await init_tables(db)
    prefs = PreferencesStore(db)
    todos = TodosStore(db)
"""

from .preferences import PreferencesStore
from .schema import connect, init_tables
from .todos import TodosStore
from .notes import NotesStore
from .tasks import TasksStore
from .task_runs import TaskRunsStore
__all__ = ["connect", "init_tables", "PreferencesStore", "TodosStore", "NotesStore", "TasksStore","TaskRunsStore", ]
