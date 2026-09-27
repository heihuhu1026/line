"""导入正确性核对的离线冒烟（`verify.import_symbol_problems` / `import_check_spec`）。

**为什么要有这一个**：真机连续几轮都栽在**连 import 都过不去**的硬错上，而它们
在 dev 阶段完全没被拦住（那时只有 pyright 与字面检查），一路活到 verify：

    · run 20260927-123032：`main.py` 里 `from main import main`，而 main.py 没定义 main
    · run 20260927-120929：`from tkinter import Label, after`（幻觉导入）
    · run 20260927-134222：缺陷单里出现 `importlib.py` —— 与标准库同名，在沙箱里**遮蔽**标准库

锁定的语义：
  ① `from m import f` 而本批产出的 m.py 里没有 f ⇒ 判出（自导入同样要核）
  ② 产出文件与标准库同名 ⇒ 判出（`__init__` / `__main__` 豁免）
  ③ 定义在函数体里的名字也算"有定义"（**故意放宽**：宁可漏判，不可把对的实现打回）
  ④ 语法坏掉 / 相对导入 / 外部依赖（`from tkinter import event`）⇒ 这里**不判**
     （外部符号交给「真跑一次 import」的导入探针）
  ⑤ 导入探针的命令规格：模块名正确、排除 `__init__`、无可导入模块时返回 None
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import verify as V  # noqa: E402

PASS = FAIL = 0


def check(cond: bool, name: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  <- {extra}" if extra else ""))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="smoke-imports-") as tmp:
        work = Path(tmp)

        def put(name: str, text: str) -> None:
            (work / name).write_text(text, encoding="utf-8")

        print("== ① 本地符号缺失 / 自导入未定义 ==")
        put("main.py", "from main import main\n\n\ndef run():\n    main()\n")
        put("ui.py", "from helper import paint\n")
        put("helper.py", "def draw():\n    pass\n")
        probs = V.import_symbol_problems(work, ["main.py", "ui.py", "helper.py"])
        joined = " | ".join(probs)
        check(any("from main import main" in p for p in probs),
              "自导入 + 未定义 ⇒ 判出（真机 123032 的形态）", joined)
        check(any("helper" in p and "paint" in p for p in probs),
              "兄弟模块里没有这个符号 ⇒ 判出", joined)

        print("== ② 有定义就不许误报（含 class / 赋值 / import 别名 / 函数体内定义） ==")
        put("model.py", (
            "import os as _os\n"
            "VERSION = 1\n"
            "class Game:\n    pass\n"
            "def build():\n    pass\n"
        ))
        put("inner.py", "def outer():\n    def hidden():\n        pass\n    return hidden\n")
        put("user.py", "from model import Game, build, VERSION, _os\nfrom inner import hidden\n")
        probs = V.import_symbol_problems(work, ["model.py", "inner.py", "user.py"])
        check(probs == [], "类 / 函数 / 赋值 / import 别名 / 函数体内定义都算有定义 ⇒ 不误报", str(probs))

        print("== ③ 与标准库同名 ⇒ 判出（会遮蔽标准库） ==")
        put("importlib.py", "def helper():\n    pass\n")
        probs = V.import_symbol_problems(work, ["importlib.py", "user.py"])
        check(any("遮蔽" in p and "importlib" in p for p in probs),
              "`importlib.py` ⇒ 判出（真机 134222 的缺陷单里真的出现过）", " | ".join(probs))
        put("__init__.py", "")
        put("__main__.py", "")
        probs2 = V.import_symbol_problems(work, ["__init__.py", "__main__.py"])
        check(probs2 == [], "`__init__` / `__main__` 豁免（它们是包入口约定）", str(probs2))

        print("== ④ 该跳过的要跳过（不误伤） ==")
        put("ext.py", "from tkinter import event\nfrom . import sibling\nimport os\n")
        probs3 = V.import_symbol_problems(work, ["ext.py"])
        check(probs3 == [],
              "外部依赖 / 相对导入 ⇒ 这里不判（交给导入探针真跑一次）", str(probs3))
        put("broken.py", "def f(:\n")
        check(isinstance(V.import_symbol_problems(work, ["broken.py"]), list),
              "语法坏掉的文件 ⇒ 不崩、不重复报（由别的档去报）")

        print("== ⑤ 导入探针命令规格 ==")
        spec = V.import_check_spec(work, ["model.py", "user.py", "__init__.py"])
        check(spec is not None and spec.get("source") == "import", "给出 source=import 的命令规格", str(spec))
        check(spec is not None and "model" in spec["command"] and "user" in spec["command"],
              "命令里带上产出的模块名", str(spec and spec.get("command")))
        init_spec = V.import_check_spec(work, ["__init__.py"])
        check(init_spec is None, "只有包入口 ⇒ 无模块可 import，返回 None（不发空命令）",
              str(init_spec))
        no_py = Path(tmp) / "empty"
        no_py.mkdir()
        check(V.import_check_spec(no_py, []) is None, "无产出文件 ⇒ 返回 None")

    print(f"\n通过 {PASS}，失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
