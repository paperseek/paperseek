import ast
from pathlib import Path
import unittest


BACKEND = Path(__file__).resolve().parents[1]
SOURCE = BACKEND / "src"
LAYERS = {
    "paperseek_core": 0,
    "paperseek_app": 1,
    "paperseek_community": 2,
}


def imported_modules(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.lineno, node.module


class ImportBoundaryTest(unittest.TestCase):
    def test_dependency_direction_is_core_then_app_then_community(self):
        violations = []
        for package, layer in LAYERS.items():
            for path in (SOURCE / package).rglob("*.py"):
                for lineno, imported in imported_modules(path):
                    imported_root = imported.split(".", 1)[0]
                    imported_layer = LAYERS.get(imported_root)
                    if imported_layer is not None and imported_layer > layer:
                        violations.append(
                            f"{path.relative_to(BACKEND)}:{lineno} imports higher layer {imported}"
                        )
        self.assertEqual(violations, [], "\n".join(violations))

    def test_core_does_not_import_framework_or_composition_dependencies(self):
        forbidden = {"alembic", "fastapi", "paperseek_app", "paperseek_community", "sqlalchemy", "supabase"}
        violations = []
        for path in (SOURCE / "paperseek_core").rglob("*.py"):
            for lineno, imported in imported_modules(path):
                if imported.split(".", 1)[0] in forbidden:
                    violations.append(f"{path.relative_to(BACKEND)}:{lineno} imports {imported}")
        self.assertEqual(violations, [], "\n".join(violations))

if __name__ == "__main__":
    unittest.main()
