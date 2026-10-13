"""Static checks that run off-Pi (do not import hardware modules)."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "campi.py"


class ReleaseChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_source_is_valid_python(self):
        compile(self.source, str(SOURCE), "exec")

    def test_no_original_account_path(self):
        self.assertNotIn("/home/samm", self.source)

    def test_configurable_paths_and_hardware(self):
        for name in (
            "CAMPI_CONFIG_DIR", "CAMPI_INTERNAL_CAPTURE_DIR", "CAMPI_USB_CAPTURE_DIR",
            "CAMPI_LCD_DC", "CAMPI_LCD_RST", "CAMPI_TOUCH_IRQ_GPIO",
            "CAMPI_STATUS_FONT", "CAMPI_HDMI_FB",
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.source)

    def test_dv_and_hdv_formats_present(self):
        self.assertIn('capture_format = "hdv" if is_hdv else "raw"', self.source)
        self.assertIn('"-format", capture_format', self.source)


if __name__ == "__main__":
    unittest.main()
