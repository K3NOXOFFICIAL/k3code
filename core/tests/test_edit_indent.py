"""edit: a replacement keeps the file's own indentation characters when the model wrote another kind."""

from __future__ import annotations

from k3code.tools import tool_edit


async def edit(tmp_path, name: str, text: str, old: str, new: str) -> str:
    f = tmp_path / name
    f.write_text(text)
    res = await tool_edit({"path": name, "old_string": old, "new_string": new}, cwd=tmp_path)
    assert res.get("ok"), res
    return f.read_text()


async def test_a_tab_indented_go_file_edited_with_spaces_keeps_tabs_on_every_line(tmp_path):
    text = "func main() {\n\tif ready {\n\t\tfoo()\n\t}\n}\n"
    old = "    if ready {\n        foo()\n    }\n"
    new = "    if ready {\n        foo()\n        bar()\n    }\n"
    out = await edit(tmp_path, "m.go", text, old, new)
    assert out == "func main() {\n\tif ready {\n\t\tfoo()\n\t\tbar()\n\t}\n}\n"


async def test_a_new_deeper_level_in_a_tab_file_is_written_with_tabs(tmp_path):
    text = "def f(x):\n\tif x:\n\t\treturn 1\n\treturn 2\n"
    old = "    if x:\n        return 1\n"
    new = "    if x:\n        if x > 1:\n            return 3\n        return 1\n"
    out = await edit(tmp_path, "f.py", text, old, new)
    assert out == "def f(x):\n\tif x:\n\t\tif x > 1:\n\t\t\treturn 3\n\t\treturn 1\n\treturn 2\n"
    compile(out, "f.py", "exec")


async def test_a_space_file_edited_with_tabs_keeps_spaces(tmp_path):
    text = "def f(x):\n    if x:\n        return 1\n    return 2\n"
    old = "\tif x:\n\t\treturn 1\n"
    new = "\tif x:\n\t\treturn 3\n"
    out = await edit(tmp_path, "f.py", text, old, new)
    assert out == "def f(x):\n    if x:\n        return 3\n    return 2\n"


async def test_a_two_space_file_edited_with_four_spaces_keeps_its_two(tmp_path):
    text = "function f() {\n  if (a) {\n    go();\n  }\n}\n"
    old = "    if (a) {\n        go();\n    }\n"
    new = "    if (a) {\n        go();\n        stop();\n    }\n"
    out = await edit(tmp_path, "f.js", text, old, new)
    assert out == "function f() {\n  if (a) {\n    go();\n    stop();\n  }\n}\n"


async def test_a_replacement_copied_with_reads_line_numbers_is_told_so(tmp_path):
    (tmp_path / "m.py").write_text("def f():\n    return 1\n")
    old = "     1\tdef f():\n     2\t    return 1\n"  # as `read` prints it
    res = await tool_edit({"path": "m.py", "old_string": old, "new_string": "def f():\n    return 2\n"}, cwd=tmp_path)
    assert "error" in res and "line numbers" in res["error"] and "without them" in res["error"]
    # ordinary code with a number at the start of a line is not mistaken for it
    res = await tool_edit({"path": "m.py", "old_string": "1 + 1\n", "new_string": "2\n"}, cwd=tmp_path)
    assert "line numbers" not in res["error"]


# ── old_string whose first line is at column 0 (a `def`, a `build:` target, `func main() {`) ───────────────────────


async def test_a_makefile_recipe_keeps_its_tabs_when_old_string_starts_at_column_0(tmp_path):
    text = "build:\n\tgcc -Wall -o app main.c\n\t./app\n"
    old = "build:\n    gcc -Wall -o app main.c\n    ./app\n"
    new = "build:\n    gcc -Wall -o app main.c\n    ./app\n    echo done\n"
    out = await edit(tmp_path, "Makefile", text, old, new)
    assert out == "build:\n\tgcc -Wall -o app main.c\n\t./app\n\techo done\n"


async def test_a_tab_indented_python_file_stays_consistent_when_old_string_starts_with_the_def(tmp_path):
    text = "def f(x):\n\tif x:\n\t\treturn 1\n\treturn 2\n"
    out = await edit(
        tmp_path, "f.py", text, "def f(x):\n    if x:\n        return 1\n", "def f(x):\n    if x:\n        return 3\n"
    )
    assert out == "def f(x):\n\tif x:\n\t\treturn 3\n\treturn 2\n"
    compile(out, "f.py", "exec")


async def test_a_space_file_edited_with_tabs_keeps_spaces_when_old_string_starts_at_column_0(tmp_path):
    text = "def f(x):\n    if x:\n        return 1\n    return 2\n"
    out = await edit(tmp_path, "f.py", text, "def f(x):\n\tif x:\n\t\treturn 1\n", "def f(x):\n\tif x:\n\t\treturn 3\n")
    assert out == "def f(x):\n    if x:\n        return 3\n    return 2\n"


async def test_a_two_space_file_keeps_its_two_when_old_string_starts_at_column_0(tmp_path):
    text = "function f() {\n  if (a) {\n    go();\n  }\n}\n"
    old = "function f() {\n    if (a) {\n        go();\n    }\n}\n"
    new = "function f() {\n    if (a) {\n        go();\n        stop();\n    }\n}\n"
    out = await edit(tmp_path, "f.js", text, old, new)
    assert out == "function f() {\n  if (a) {\n    go();\n    stop();\n  }\n}\n"


async def test_an_edit_whose_whitespace_already_matches_is_not_reindented(tmp_path):
    text = "def f(x):\n    if x:\n        return 1\n"
    out = await edit(
        tmp_path, "f.py", text, "def f(x):\n    if x:\n        return 1", "def f(x):\n    if x:\n        return 2"
    )
    assert out == "def f(x):\n    if x:\n        return 2\n"
