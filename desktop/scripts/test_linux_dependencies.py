"""Native Ubuntu regression checks for the desktop payload dependency collector."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from prepare_resources import linux_dependencies


class LinuxDependencyTests(unittest.TestCase):
    def test_origin_needed_keeps_transitive_dependencies_after_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "original payload"
            (root / "lib").mkdir(parents=True)
            (root / "nested").mkdir()
            leaf = root / "nested" / "libprobe_leaf.so"
            parent = root / "lib" / "libprobe_parent.so"
            leaf_source = root / "leaf.c"
            parent_source = root / "parent.c"
            leaf_source.write_text("#include <math.h>\ndouble leaf(double x) { return cos(x); }\n")
            parent_source.write_text("extern double leaf(double);\ndouble parent(double x) { return leaf(x); }\n")
            subprocess.run([
                "cc", "-shared", "-fPIC", str(leaf_source), "-lm",
                "-Wl,-soname,libprobe_leaf.so", "-o", str(leaf),
            ], check=True)
            subprocess.run([
                "cc", "-shared", "-fPIC", str(parent_source), str(leaf),
                "-o", str(parent),
            ], check=True)
            subprocess.run([
                "patchelf", "--replace-needed", "libprobe_leaf.so",
                "$ORIGIN/../nested/libprobe_leaf.so", str(parent),
            ], check=True)

            dependencies = linux_dependencies(parent)
            self.assertIn(leaf, dependencies)
            self.assertTrue(any(path.name == "libm.so.6" for path in dependencies))

            relocated = Path(temporary) / "relocated payload"
            shutil.move(root, relocated)
            parent = relocated / "lib" / parent.name
            leaf = relocated / "nested" / leaf.name
            dependencies = linux_dependencies(parent)
            self.assertIn(leaf, dependencies)
            self.assertTrue(any(path.name == "libm.so.6" for path in dependencies))
            self.assertFalse(any(path.is_relative_to(root) for path in dependencies))

            leaf.unlink()
            with self.assertRaisesRegex(RuntimeError, "Unresolved native dependency"):
                linux_dependencies(parent)


if __name__ == "__main__":
    unittest.main()
