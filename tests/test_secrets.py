"""密钥私密策略回归测试。

注意：样本密钥一律用字符串拼接构造，避免测试源码本身被提交闸门拦下。
"""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cncagent.config import masked_key                      # noqa: E402
from scripts.check_secrets import (ALLOWED_ENV_FILES, SENSITIVE_NAME,  # noqa: E402
                                   _findings)


class TestMaskedKey(unittest.TestCase):
    def test_never_echo_plaintext(self):
        key = "sk-" + "A" * 24 + "Z9"
        out = masked_key(key)
        self.assertNotIn(key, out)
        self.assertNotIn("A" * 10, out)          # 不输出连续明文片段
        self.assertIn(f"长度 {len(key)}", out)

    def test_empty_and_short(self):
        self.assertEqual(masked_key(""), "(未配置)")
        self.assertNotIn("abc12345", masked_key("abc12345"))

    def test_prefix_suffix_bounded(self):
        out = masked_key("sk-" + "B" * 30)
        # 打码后明文最多只剩 3+2 个字符
        self.assertTrue(out.startswith("sk-"))
        self.assertLessEqual(out.count("B"), 2)


class TestScanner(unittest.TestCase):
    def test_detects_real_looking_key(self):
        key = "sk-" + "A" * 24
        self.assertTrue(_findings(f'api_key = "{key}"', "probe.py"))
        self.assertTrue(_findings(f'Authorization: Bearer {"B" * 30}', "probe.py"))

    def test_placeholders_are_allowed(self):
        self.assertEqual(_findings('api_key = "your_api_key_here"', "probe.py"), [])
        self.assertEqual(_findings("LLM_API_KEY=", ".env.example"), [])
        self.assertEqual(_findings("PANEL_TOKEN=os.getenv('PANEL_TOKEN')", "probe.py"), [])

    def test_sensitive_filename_gate(self):
        self.assertTrue(SENSITIVE_NAME.search(".env"))
        self.assertTrue(SENSITIVE_NAME.search("config/credentials.json"))
        self.assertIn(".env.example", ALLOWED_ENV_FILES)

    def test_worktree_is_clean(self):
        r = subprocess.run([sys.executable, "scripts/check_secrets.py"],
                           cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
