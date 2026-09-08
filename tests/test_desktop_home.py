from __future__ import annotations

from pathlib import Path
import sys
import tkinter as tk
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pc_client"))
try:
    from desktop_home import HomeGrid
    from desktop_widgets import BG, CARD, TEXT, RoundedPanel
finally:
    sys.path.pop(0)


class HomeGridLayoutTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.root.configure(bg=BG)
        self.errors = []
        self.root.report_callback_exception = lambda *args: self.errors.append(args)
        self.addCleanup(self.root.destroy)

    def make_grid(self, breakpoint):
        grid = HomeGrid(self.root, breakpoint=breakpoint)
        # Explicit child geometry works while the top-level stays withdrawn.
        # No deiconify/focus/window positioning, so an open preview is untouched.
        grid.place(x=0, y=0, width=900, height=320)
        labels = []
        for text in ("Действия на этом ПК", "Файлы и архив", "Версия и установка"):
            panel = RoundedPanel(grid, padx=16, pady=14)
            label = tk.Label(panel, text=text, font=("Segoe UI", 11), bg=CARD, fg=TEXT)
            label.pack(anchor="w")
            labels.append(label)
            grid.add(panel)
        return grid, labels

    def resize(self, grid, width):
        grid.place_configure(width=width)
        self.root.update_idletasks()
        grid.arrange()
        self.root.update_idletasks()
        self.assertEqual(grid.winfo_width(), width)
        self.assertEqual(self.root.state(), "withdrawn")
        self.assertEqual(self.errors, [])

    def assert_wide(self, grid, width):
        self.assertTrue(grid._wide)
        for index, panel in enumerate(grid.items):
            info = panel.grid_info()
            self.assertEqual((int(info["row"]), int(info["column"])), (0, index))
            self.assertEqual(grid.columnconfigure(index, "uniform"), "home")
            self.assertEqual(int(grid.columnconfigure(index, "weight")), 1)
            self.assertGreaterEqual(panel.winfo_width(), width // 3 - 14)
            self.assertLessEqual(panel.winfo_width(), width // 3 + 1)
            self.assertGreaterEqual(panel.winfo_x(), 0)
            self.assertLessEqual(panel.winfo_x() + panel.winfo_width(), width)

    def assert_narrow(self, grid, labels, width):
        self.assertFalse(grid._wide)
        for index, panel in enumerate(grid.items):
            info = panel.grid_info()
            self.assertEqual((int(info["row"]), int(info["column"])), (index, 0))
            self.assertFalse(grid.columnconfigure(index, "uniform"))
            self.assertEqual(int(grid.columnconfigure(index, "weight")), 1 if index == 0 else 0)
            # Regression: inactive uniform columns previously stole 2/3 of this width.
            self.assertEqual(panel.winfo_x(), 0)
            self.assertEqual(panel.winfo_width(), width)
            label = labels[index]
            # Tk does not place nested packed labels until their ancestors map;
            # requested text size still gives an exact no-clipping requirement.
            self.assertLessEqual(label.winfo_reqwidth(), panel.winfo_width() - 32)
            self.assertLessEqual(label.winfo_reqheight(), panel.winfo_height() - 28)
        self.assertEqual(grid.grid_bbox(1, 0)[2], 0)
        self.assertEqual(grid.grid_bbox(2, 0)[2], 0)

    def test_metrics_and_actions_return_to_three_columns_after_900_560_900(self):
        for breakpoint in (760, 900):
            with self.subTest(breakpoint=breakpoint):
                grid, labels = self.make_grid(breakpoint)
                self.resize(grid, 900)
                self.assert_wide(grid, 900)
                self.resize(grid, 560)
                self.assert_narrow(grid, labels, 560)
                self.resize(grid, 900)
                self.assert_wide(grid, 900)
                grid.destroy()

    def test_breakpoint_is_inclusive_and_narrow_drops_all_uniform_groups(self):
        for breakpoint in (760, 900):
            with self.subTest(breakpoint=breakpoint):
                grid, labels = self.make_grid(breakpoint)
                self.resize(grid, breakpoint)
                self.assert_wide(grid, breakpoint)
                self.resize(grid, breakpoint - 1)
                self.assert_narrow(grid, labels, breakpoint - 1)
                grid.destroy()


if __name__ == "__main__":
    unittest.main()
