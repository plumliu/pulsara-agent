"""Native Ubuntu regression checks for the desktop payload dependency collector."""

from importlib.metadata import distribution
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from prepare_resources import bundle_linux_dependencies, linux_dependencies


class LinuxDependencyTests(unittest.TestCase):
    def test_pillow_wheel_loads_and_encodes_images_after_relocation(self):
        # The locked x64 Pillow wheel's libXau exposes corruption from repeated
        # patchelf RPATH growth. Exercise real upstream ELFs, not a mock loader.
        with tempfile.TemporaryDirectory() as temporary:
            resources = Path(temporary)
            (resources / "licenses").mkdir()
            root = resources / "original pillow"
            root.mkdir()
            wheel = distribution("pillow")
            for directory in ("PIL", "pillow.libs"):
                shutil.copytree(wheel.locate_file(directory), root / directory)
            with patch("prepare_resources.RESOURCES", resources):
                bundle_linux_dependencies(root)
            relocated = resources / "relocated pillow"
            shutil.move(root, relocated)
            subprocess.run([sys.executable, "-c", """
from io import BytesIO
from pathlib import Path
from PIL import Image
assert Path(Image.__file__).resolve().is_relative_to(Path.cwd())
for format in ("PNG", "JPEG", "TIFF", "WEBP"):
    buffer = BytesIO()
    Image.new("RGB", (16, 16), "red").save(buffer, format=format)
    buffer.seek(0)
    with Image.open(buffer) as image:
        image.load()
        assert image.size == (16, 16)
"""], cwd=relocated, check=True)

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

    def test_wheel_sibling_libraries_become_independently_relocatable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "wheel payload"
            libraries = root / "package.libs"
            libraries.mkdir(parents=True)
            leaf = libraries / "libwheel_leaf.so"
            parent = libraries / "libwheel_parent.so"
            source = root / "leaf.c"
            source.write_text("double leaf(double x) { return x; }\n")
            subprocess.run([
                "cc", "-shared", "-fPIC", str(source),
                "-Wl,-soname,libwheel_leaf.so", "-o", str(leaf),
            ], check=True)
            source.write_text("extern double leaf(double);\ndouble parent(double x) { return leaf(x); }\n")
            subprocess.run([
                "cc", "-shared", "-fPIC", str(source), str(leaf),
                "-o", str(parent),
            ], check=True)
            # Like a wheel's internal .libs objects, this ELF initially relies
            # on an importing object's search path, not its own RPATH.
            with self.assertRaisesRegex(RuntimeError, "Unresolved native dependency"):
                linux_dependencies(parent)
            bundle_linux_dependencies(root)
            self.assertIn(leaf, linux_dependencies(parent))
            relocated = Path(temporary) / "moved wheel payload"
            shutil.move(root, relocated)
            self.assertIn(relocated / "package.libs" / leaf.name,
                          linux_dependencies(relocated / "package.libs" / parent.name))


if __name__ == "__main__":
    unittest.main()
