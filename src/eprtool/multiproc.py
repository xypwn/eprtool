"""Simple and very naive multiprocessing abstraction for the GUI apps

Specifically made for the GUI app need of running some process that modifies application state.
Doesn't support more than one queue item at the moment.
"""

from dataclasses import dataclass
import multiprocessing as mp
import multiprocessing.queues as mpq
import traceback
import typing

State = typing.TypeVar("State")

@dataclass
class Status:
    SpecialProgress = typing.Literal["infinite", "textonly"]

    msg: str
    progress: float|SpecialProgress = "textonly"

@dataclass
class Task:
    fn: typing.Callable
    args: typing.Iterable

# For use as sentinel value
class Stop:
    def __eq__(self, other):
        return isinstance(other, Stop)

# Context passed to any task function
@dataclass
class Context:
    msgq: mp.Queue[Status]

class TaskException(BaseException):
    def __init__(self, parent: BaseException):
        super().__init__(parent)
        self.formatted_tb: list[str] = traceback.format_tb(parent.__traceback__)

def _task_worker[State](ctx: Context, taskq: mp.Queue[Task|Stop], resq: mp.Queue[State|None|TaskException]):
    for task in iter(taskq.get, Stop()):
        assert isinstance(task, Task)
        ok = False
        try:
            res = task.fn(*([ctx]+list(task.args)))
            ok = True
        except BaseException as e:  # noqa: BLE001
            resq.put(TaskException(e))
        if ok:
            res = typing.cast(State|None, res)
            resq.put(res)

# Main process data of task worker state
class TaskManager[State]:
    def __init__(self):
        # Queue for sending tasks to worker
        self.taskq: mpq.Queue[Task|Stop] = mp.Queue()
        # Result from worker
        self.resq: mpq.Queue[State|None|TaskException] = mp.Queue()
        # Queue for sending status messages from worker
        self.msgq: mpq.Queue[Status] = mp.Queue()
        # Worker process
        self.proc = mp.Process(target=_task_worker, args=(
            Context(self.msgq),
            self.taskq,
            self.resq,
        ))
        # Whether worker is currently executing a task
        self.working = False
        # Current display status
        self.status = Status("")
        # Last error message
        self.error: TaskException|None = None

        self.proc.start()

    def enqueue(self, task: Task):
        self.taskq.put(task)
        self.status = Status("working")
        self.working = True

    def dequeue(self) -> State|None|TaskException:
        v = self.resq.get_nowait()
        self.working = False
        return v

    def shutdown(self):
        self.taskq.put(Stop())
        self.proc.join()
        self.taskq.close()
        self.resq.close()
        self.msgq.close()
        self.proc.close()