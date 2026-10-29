"""SDK imports and shared test fixtures keep one isolated data root."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class AstrBotDataRootTests(unittest.TestCase):
    def test_sdk_paths_stay_isolated_across_fixture_import_orders(self):
        """Keep SDK paths consistent and remove them after the process exits."""
        root = Path(__file__).resolve().parents[1]
        source = r"""
import asyncio
import base64
import importlib
from pathlib import Path
import sys

import tests
from astrbot.core import db_helper
from astrbot.core.agent.tool_image_cache import tool_image_cache
from astrbot.core.tools.message_tools import _file_send_allowed_roots, _is_path_within
from astrbot.core.utils.astrbot_path import get_astrbot_root

sdk_root = Path(get_astrbot_root()).resolve()
asyncio.run(db_helper.initialize())
for name in sys.argv[1:]:
    module = importlib.import_module(name)
    assert Path(get_astrbot_root()).resolve() == sdk_root, name
    cleanup = getattr(module, "tearDownModule", None)
    if cleanup is not None:
        cleanup()
    assert sdk_root.is_dir(), name
    assert Path(get_astrbot_root()).resolve() == sdk_root, name

from tests.astrbot_data_root import ASTRBOT_ROOT, assert_sdk_database_isolated

assert sdk_root == ASTRBOT_ROOT
assert_sdk_database_isolated(db_helper)
assert Path(tool_image_cache._cache_dir).is_relative_to(ASTRBOT_ROOT)
image = tool_image_cache.save_image(
    base64.b64encode(b"isolated-image").decode("ascii"), "root-check", "test"
)
image_path = Path(image.file_path).resolve()
assert image_path.read_bytes() == b"isolated-image"
assert _is_path_within(image_path, _file_send_allowed_roots(None))
print("SDK paths stayed isolated across fixture imports")
print("SDK root: " + str(sdk_root))
"""
        modules = ("tests.test_astrbot_staging_host", "tests.test_host_options_api")
        for order in (modules, modules[::-1]):
            with (
                self.subTest(order=order),
                TemporaryDirectory(prefix="companion-v2-caller-root-") as temporary,
            ):
                environment = os.environ.copy()
                environment["ASTRBOT_ROOT"] = temporary
                result = subprocess.run(
                    [sys.executable, "-X", "utf8", "-c", source, *order],
                    cwd=root,
                    env=environment,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=90,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(
                    "SDK paths stayed isolated across fixture imports", result.stdout
                )
                sdk_root = next(
                    line.removeprefix("SDK root: ")
                    for line in result.stdout.splitlines()
                    if line.startswith("SDK root: ")
                )
                self.assertFalse(Path(sdk_root).exists(), result.stderr)
